# -*- coding: utf-8 -*-
"""보이스 스튜디오(09-29) — IVR·안내 멘트를 표준 음성 또는 **동의 받은 복제 목소리**로 만든다.

진입점은 `speech.py`(mode=studio). 밑줄 파일이라 Vercel 함수 수(Hobby 12개)를 늘리지 않는다.
URL: /api/voice-studio?op=status (GET) · /api/voice-studio?op=synth (POST)  — vercel.json rewrite

두 가지 엔진
  - standard : 기존 콜봇 TTS(edge-tts 신경망 음성). 늘 쓸 수 있다.
  - clone    : 음성합성 SaaS 워커(Qwen3-TTS, voice.gowon.co.kr 백엔드). 환경변수가 있을 때만 켜진다.
               VOICE_ENGINE_URL · VOICE_ENGINE_SECRET(HMAC) · VOICE_STUDIO_VOICES(JSON 목록)
               목록에는 **동의 확인을 마친** 목소리만 넣는다(consent="ok" 가 아니면 거절한다).

목소리 목록의 출처 (10-07, Voice-SaaS 백로그 P2 「AICC 포털 보이스 스튜디오 연동」)
  - VOICE_STUDIO_VOICE_SOURCE=engine 이면 워커의 `GET /v1/voices`(서명 필수, 본문 없는 GET 이라
    `ts + "\n" + ""` 에 서명)를 불러 **동의 확인된 목록을 워커가 준 대로** 쓴다 — 사람이 적어 넣는
    환경변수 목록은 동의 기록과 어긋날 수 있다. 호출 실패(타임아웃·401·500·깨진 JSON)는 빈 결과로
    삼고 VOICE_STUDIO_VOICES 를 **대체 수단**으로 쓴다. 매 status 마다 GPU 서버를 두드리지 않도록
    짧게 캐시한다(VOICES_CACHE_TTL). 동의는 통과했지만 아직 쓸 수 없는 목소리(`unavailable`)는
    `clone_pending` 으로 내보내 화면이 「준비 중」으로 그린다(골랐다가 409·501 을 보지 않게).
  - 기본값(미설정)은 종전과 같다(환경변수 목록만) — build now, activate on approval.

원칙
  - 형식 변환(8kHz PCM·μ-law·A-law)과 AI 안내 음성 이어 붙이기, 파일 묶음(ZIP)은 브라우저가 한다 — 서버는 한 줄씩 소리만 돌려준다.
  - 실호출 비용: 복제 엔진은 GPU 를 쓴다. 한 번에 한 줄(최대 MAX_CHARS 글자)만 받는다.
  - 내부 예외 문구·비밀값은 응답에 싣지 않는다.
  - **오류 응답은 저장소 표준 봉투**(`_errors`)를 쓴다 — `code`·`request_id`·`details[].field`
    가 붙어야 콘솔이 어느 칸이 왜 틀렸는지 알려주고, 429 는 `Retry-After` 를 실을 수 있다.
    합성 실패를 한 문장으로 삼키지 않고 분류(500/502/504)·모니터링을 거친다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

import _errors

MAX_CHARS = 300
MAX_BODY = 8192          # 한 줄 합성 요청이라 본문은 작다(과금·메모리 방어)
# 복제 엔진(GPU) 대기 예산. 서버리스 응답 한도(30초)보다 넉넉히 짧아야
# 플랫폼이 먼저 요청을 끊지 않고 우리가 표준 봉투로 답할 수 있다.
CLONE_TIMEOUT = 25
# 목소리 목록 조회 예산·캐시. 목록은 상태 조회(화면 진입)마다 필요하므로 GPU 워커를 오래 기다리지
# 않고(3초), 받은 결과는 60초, 실패는 15초만 기억한다(죽은 워커를 매번 3초씩 기다리지 않게).
VOICES_TIMEOUT = 3
VOICES_CACHE_TTL = 60
VOICES_FAIL_TTL = 15
VOICE_SOURCES = ("env", "engine")
STANDARD_VOICES = [
    {"id": "ko-KR-SunHiNeural", "name": "선희 (여성 · 안내)", "kind": "standard"},
    {"id": "ko-KR-InJoonNeural", "name": "인준 (남성 · 안내)", "kind": "standard"},
    {"id": "ko-KR-HyunsuMultilingualNeural", "name": "현수 (남성 · 다국어)", "kind": "standard"},
]
_STD_IDS = {v["id"] for v in STANDARD_VOICES}
FORMATS = ("wav24k", "pcm8k", "ulaw8k", "alaw8k")
NOTICE_TEXT = "안내 말씀 드립니다. 이 음성은 인공지능으로 생성되었습니다."


def clone_voices(env=None):
    """VOICE_STUDIO_VOICES 에서 동의 확인을 마친 목소리만 — 형식이 틀리면 빈 목록."""
    env = os.environ if env is None else env
    try:
        raw = json.loads(env.get("VOICE_STUDIO_VOICES") or "[]")
    except Exception:
        return []
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, dict) and v.get("id") and v.get("consent") == "ok":
            out.append({"id": str(v["id"])[:64], "name": str(v.get("name") or v["id"])[:40], "kind": "clone",
                        "note": str(v.get("note") or "")[:80]})
    return out


def engine_ready(env=None):
    env = os.environ if env is None else env
    return bool((env.get("VOICE_ENGINE_URL") or "").startswith("https://") and (env.get("VOICE_ENGINE_SECRET") or "").strip())


def voice_source(env=None):
    """목소리 목록 출처 스위치 — `engine` 정확 일치만 켠다(그 밖은 전부 `env`, 기본 OFF)."""
    env = os.environ if env is None else env
    v = (env.get("VOICE_STUDIO_VOICE_SOURCE") or "").strip().lower()
    return v if v in VOICE_SOURCES else "env"


def _norm_voice(v, default_kind="clone"):
    """워커 응답 한 줄 → 포털 목소리 칸. consent="ok" 가 아니면 None(포털에서 한 번 더 거른다)."""
    if not isinstance(v, dict) or not v.get("id") or v.get("consent") != "ok":
        return None
    kind = str(v.get("kind") or default_kind)
    if kind not in ("clone", "builtin"):
        kind = default_kind
    out = {"id": str(v["id"])[:64], "name": str(v.get("name") or v["id"])[:40], "kind": kind,
           "note": str(v.get("note") or "")[:80]}
    if v.get("reason"):
        out["reason"] = str(v["reason"])[:120]
    return out


# 프로세스 안 캐시: url → {"until": 만료 시각, "cat": 목록 또는 None(실패)}
_VOICES_CACHE = {}


def _voices_cache_clear():
    _VOICES_CACHE.clear()


def fetch_catalog(env=None, now=None):
    """워커 `GET /v1/voices` → {"voices": [...], "unavailable": [...]} 또는 실패 시 None.

    서명은 synth 와 같은 규약(`sign`)이고 본문이 없으므로 빈 바이트에 서명한다.
    어떤 예외(네트워크·타임아웃·401/500·JSON 아님·`ok` 거짓)도 밖으로 내지 않는다 — 목록 조회가
    실패해도 화면은 표준 음성으로 계속 일해야 한다. 결과는 짧게 캐시한다.
    """
    env = os.environ if env is None else env
    if not engine_ready(env):
        return None
    url = env["VOICE_ENGINE_URL"].rstrip("/") + "/v1/voices"
    t = time.time() if now is None else float(now)
    hit = _VOICES_CACHE.get(url)
    if hit is not None and hit["until"] > t:
        return hit["cat"]
    cat = None
    try:
        ts = int(t)
        r = urllib.request.Request(url, method="GET", headers={
            "X-Timestamp": str(ts), "X-Signature": sign(env["VOICE_ENGINE_SECRET"].strip(), ts, b"")})
        with urllib.request.urlopen(r, timeout=VOICES_TIMEOUT) as res:
            j = json.loads(res.read().decode("utf-8"))
        if isinstance(j, dict) and j.get("ok") is True and isinstance(j.get("voices"), list):
            voices = [x for x in (_norm_voice(v) for v in j["voices"]) if x]
            pend = j.get("unavailable") if isinstance(j.get("unavailable"), list) else []
            unavailable = [x for x in (_norm_voice(v) for v in pend) if x]
            cat = {"voices": voices, "unavailable": unavailable}
    except Exception:
        cat = None
    _VOICES_CACHE[url] = {"until": t + (VOICES_CACHE_TTL if cat is not None else VOICES_FAIL_TTL), "cat": cat}
    return cat


def fetch_voices(env=None, now=None):
    """지금 워커가 실제로 합성해 주는(동의 확인된) 목소리 목록 — 어떤 실패에도 빈 목록."""
    cat = fetch_catalog(env, now)
    return list(cat["voices"]) if cat else []


def status(env=None):
    env = os.environ if env is None else env
    engine = engine_ready(env)
    clones, pending, source = clone_voices(env), [], "env"
    if engine and voice_source(env) == "engine":
        cat = fetch_catalog(env)
        if cat is not None:
            clones, pending, source = cat["voices"], cat["unavailable"], "engine"
        else:
            source = "env_fallback"    # 워커 응답이 없어 환경변수 목록으로 대신한다(화면에 드러낸다)
    ready = engine and bool(clones)
    if ready:
        note = ""
    elif engine and pending:
        note = "복제 목소리가 준비 중입니다 — 동의 확인은 끝났지만 아직 합성할 수 없습니다. 표준 음성으로 제작할 수 있습니다."
    else:
        note = "복제 목소리 엔진 연결 전 — 표준 음성으로 제작할 수 있습니다. 목소리 복제는 본인 동의 녹음을 마친 목소리만 등록됩니다."
    return {
        "ok": True,
        "standard": STANDARD_VOICES,
        "clone": clones if ready else [],
        "clone_pending": pending if engine else [],
        "clone_ready": ready,
        "clone_source": source,
        "clone_note": note,
        "max_chars": MAX_CHARS,
        "formats": list(FORMATS),
        "notice_text": NOTICE_TEXT,
    }


_NAME_RE = re.compile(r"[^0-9A-Za-z가-힣_\-]+")


def parse_script(text):
    """`파일이름 | 문장` 형식(음성복제도구와 같은 형식) → [{name, text}]. # 주석·빈 줄은 건너뛴다.

    이름이 없으면 순번(001…)을 붙이고, 파일 이름에 쓸 수 없는 글자는 밑줄로 바꾼다. 같은 이름은 뒤에 -2, -3.
    """
    rows, seen = [], {}
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "|" in s:
            name, body = s.split("|", 1)
        else:
            name, body = "", s
        body = body.split("   #", 1)[0].strip()
        if not body:
            continue
        name = _NAME_RE.sub("_", name.strip()).strip("_")[:40] or "%03d" % (len(rows) + 1)
        n = seen.get(name, 0) + 1
        seen[name] = n
        rows.append({"name": name if n == 1 else "%s-%d" % (name, n), "text": body})
    return rows


def validate(req):
    """합성 요청 검사 → (True, 정리된 요청) 또는 (False, {"field","reason"}).

    실패 쪽이 **어느 칸이 왜 틀렸는지**를 들고 나간다(화면 인라인 검증용).
    목소리 미선택은 복제 거절(409)이 아니라 입력 오류다 — 예전에는 빈 목소리가
    복제로 분류돼 "동의 확인이 끝난 목소리만" 이라는 엉뚱한 안내를 받았다.
    """
    if not isinstance(req, dict):
        return False, {"field": "body", "reason": "요청은 JSON 객체여야 합니다."}
    text = str(req.get("text") or "").strip()
    if not text:
        return False, {"field": "text", "reason": "읽을 문장을 넣어 주세요."}
    if len(text) > MAX_CHARS:
        return False, {"field": "text",
                       "reason": "한 줄은 %d자까지입니다. 문장을 나눠 주세요." % MAX_CHARS}
    voice = str(req.get("voice") or "")
    if not voice:
        return False, {"field": "voice", "reason": "목소리를 골라 주세요."}
    kind = "standard" if voice in _STD_IDS else "clone"
    try:
        rate = float(req.get("rate", 1.0))
    except Exception:
        rate = 1.0
    # min(상한, 값) 순서가 load-bearing 이다 — NaN 은 비교가 모두 False 라
    # 상한이 앞에 와야 NaN 이 그대로 통과하지 않는다(회귀로 고정).
    rate = max(0.8, min(1.25, rate))
    return True, {"text": text, "voice": voice, "kind": kind, "rate": rate}


def sign(secret, ts, body):
    """음성합성 SaaS 워커와 같은 서명: hex(HMAC-SHA256(secret, ts + "\\n" + body))."""
    return hmac.new(secret.encode(), str(ts).encode() + b"\n" + body, hashlib.sha256).hexdigest()


def synth_standard(text, voice, rate):
    import asyncio
    import edge_tts
    pct = int(round((rate - 1.0) * 100))
    rate_s = ("+%d%%" % pct) if pct >= 0 else ("%d%%" % pct)

    async def run():
        buf = bytearray()
        async for ch in edge_tts.Communicate(text, voice, rate=rate_s).stream():
            if ch.get("type") == "audio":
                buf.extend(ch["data"])
        return bytes(buf)
    data = asyncio.run(run())
    if not data:
        raise RuntimeError("empty audio")
    return data, "audio/mpeg"


def synth_clone(text, voice, rate, env=None):
    env = os.environ if env is None else env
    body = json.dumps({"voice_id": voice, "text": text, "speed": rate, "format": "wav24k",
                       "job_id": "studio-%d" % int(time.time() * 1000)}).encode()
    ts = int(time.time())
    r = urllib.request.Request(env["VOICE_ENGINE_URL"].rstrip("/") + "/v1/synthesize", data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Timestamp": str(ts), "X-Signature": sign(env["VOICE_ENGINE_SECRET"].strip(), ts, body)})
    with urllib.request.urlopen(r, timeout=CLONE_TIMEOUT) as res:
        j = json.loads(res.read().decode())
    b64 = j.get("audio_b64") if isinstance(j, dict) else None
    if not b64:
        # 워커가 다른 형태로 답한 것은 우리 버그가 아니라 업스트림 장애다(502로 분류).
        raise urllib.error.URLError("clone worker returned no audio")
    return base64.b64decode(b64), "audio/wav"


def _allow_origin(h):
    """허용 오리진 되비침. 가드를 못 읽으면 닫는다(장애 시 열어주지 않는다)."""
    try:
        import _guard
        return _guard.allow_origin_header(h.headers)
    except Exception:
        return "null"


def _send(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    # 오류 응답(_errors.send)은 이 헤더를 붙인다 — 성공만 빠지면 허용된 다른
    # 오리진에서 브라우저가 성공 응답만 못 읽는 엇갈림이 생긴다.
    h.send_header("Access-Control-Allow-Origin", _allow_origin(h))
    h.send_header("Access-Control-Expose-Headers", _errors.EXPOSE_HEADERS)
    rq = getattr(h, "_rq", None)
    if rq is not None:
        h.send_header("X-Request-Id", rq.request_id)
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)
    try:                          # 요청 1건 = 구조화 로그 1줄(성공 경로)
        if rq is not None:
            rq.finish(code)
    except Exception:
        pass


def op_of(path):
    try:
        return (parse_qs(urlparse(path or "").query).get("op", [""])[0] or "").strip().lower()
    except Exception:
        return ""


ROUTE = "/api/voice-studio"


def handle_get(h):
    if op_of(h.path) in ("", "status"):
        return _send(h, 200, status())
    return _errors.send(h, status=404)


def handle_post(h):
    if op_of(h.path) != "synth":
        return _errors.send(h, status=404)
    try:
        n = int(h.headers.get("content-length") or 0)
    except Exception:
        # 깨진 Content-Length 는 클라이언트 입력 오류다 — 예전에는 여기서
        # ValueError 가 올라가 500(+모니터링 알림)으로 보고됐다.
        n = -1
    if n <= 0:
        return _errors.send(h, status=400, message="요청 본문이 없습니다.",
                            details=[{"field": "body", "reason": "JSON 본문이 필요합니다."}])
    if n > MAX_BODY:
        # 크기 문제를 "형식을 확인해 주세요"로 알리면 고칠 수가 없다.
        return _errors.send(h, status=413,
                            details=[{"field": "body",
                                      "reason": "본문은 %d바이트까지입니다." % MAX_BODY}])
    try:
        req = json.loads(h.rfile.read(n).decode("utf-8"))
    except Exception:
        return _errors.send(h, status=400, message="본문이 올바른 JSON 이 아닙니다.",
                            details=[{"field": "body", "reason": "JSON 파싱 실패"}])
    ok, v = validate(req)
    if not ok:
        return _errors.send(h, status=400, message=v["reason"], details=[v])
    if v["kind"] == "clone":
        allowed = {x["id"] for x in status()["clone"]}
        if v["voice"] not in allowed:
            return _errors.send(
                h, status=409, code="CLONE_NOT_AVAILABLE",
                message="복제 목소리를 쓸 수 없습니다 — 엔진 연결과 본인 동의 확인이 끝난 목소리만 쓸 수 있습니다.",
                details=[{"field": "voice", "reason": "동의 확인된 복제 목소리가 아닙니다."}])
    t0 = time.time()
    try:
        data, mime = (synth_clone if v["kind"] == "clone" else synth_standard)(v["text"], v["voice"], v["rate"])
        if not data:
            raise RuntimeError("empty audio")
    except Exception as e:
        # 삼키지 않는다: 분류(ImportError 500 / 네트워크 502 / 타임아웃 504) +
        # 5xx 는 모니터링 전송 + event_id. 사용자에게 원인 문구는 노출하지 않는다.
        return _errors.handle(h, e, route=ROUTE, method="POST")
    return _send(h, 200, {"ok": True, "kind": v["kind"], "voice": v["voice"], "mime": mime,
                          "audio_b64": base64.b64encode(data).decode(), "elapsed": round(time.time() - t0, 2)})
