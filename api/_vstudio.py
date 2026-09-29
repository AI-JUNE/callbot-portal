# -*- coding: utf-8 -*-
"""보이스 스튜디오(09-29) — IVR·안내 멘트를 표준 음성 또는 **동의 받은 복제 목소리**로 만든다.

진입점은 `speech.py`(mode=studio). 밑줄 파일이라 Vercel 함수 수(Hobby 12개)를 늘리지 않는다.
URL: /api/voice-studio?op=status (GET) · /api/voice-studio?op=synth (POST)  — vercel.json rewrite

두 가지 엔진
  - standard : 기존 콜봇 TTS(edge-tts 신경망 음성). 늘 쓸 수 있다.
  - clone    : 음성합성 SaaS 워커(Qwen3-TTS, voice.gowon.co.kr 백엔드). 환경변수가 있을 때만 켜진다.
               VOICE_ENGINE_URL · VOICE_ENGINE_SECRET(HMAC) · VOICE_STUDIO_VOICES(JSON 목록)
               목록에는 **동의 확인을 마친** 목소리만 넣는다(consent="ok" 가 아니면 거절한다).

원칙
  - 형식 변환(8kHz PCM·μ-law·A-law)과 AI 안내 음성 이어 붙이기, 파일 묶음(ZIP)은 브라우저가 한다 — 서버는 한 줄씩 소리만 돌려준다.
  - 실호출 비용: 복제 엔진은 GPU 를 쓴다. 한 번에 한 줄(최대 MAX_CHARS 글자)만 받는다.
  - 내부 예외 문구·비밀값은 응답에 싣지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

MAX_CHARS = 300
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


def status(env=None):
    clones = clone_voices(env)
    ready = engine_ready(env) and bool(clones)
    return {
        "ok": True,
        "standard": STANDARD_VOICES,
        "clone": clones if ready else [],
        "clone_ready": ready,
        "clone_note": "" if ready else "복제 목소리 엔진 연결 전 — 표준 음성으로 제작할 수 있습니다. 목소리 복제는 본인 동의 녹음을 마친 목소리만 등록됩니다.",
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
    """합성 요청 검사 → (ok, 오류 문장 | 정리된 요청)."""
    if not isinstance(req, dict):
        return False, "요청 형식을 확인해 주세요."
    text = str(req.get("text") or "").strip()
    if not text:
        return False, "읽을 문장을 넣어 주세요."
    if len(text) > MAX_CHARS:
        return False, "한 줄은 %d자까지입니다. 문장을 나눠 주세요." % MAX_CHARS
    voice = str(req.get("voice") or "")
    kind = "standard" if voice in _STD_IDS else "clone"
    try:
        rate = float(req.get("rate", 1.0))
    except Exception:
        rate = 1.0
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
    with urllib.request.urlopen(r, timeout=55) as res:
        j = json.loads(res.read().decode())
    return base64.b64decode(j["audio_b64"]), "audio/wav"


def _send(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


def op_of(path):
    try:
        return (parse_qs(urlparse(path or "").query).get("op", [""])[0] or "").strip().lower()
    except Exception:
        return ""


def handle_get(h):
    if op_of(h.path) in ("", "status"):
        return _send(h, 200, status())
    return _send(h, 404, {"ok": False, "error": "알 수 없는 요청입니다."})


def handle_post(h):
    if op_of(h.path) != "synth":
        return _send(h, 404, {"ok": False, "error": "알 수 없는 요청입니다."})
    n = int(h.headers.get("content-length") or 0)
    if n <= 0 or n > 8192:
        return _send(h, 400, {"ok": False, "error": "요청 형식을 확인해 주세요."})
    try:
        req = json.loads(h.rfile.read(n).decode("utf-8"))
    except Exception:
        return _send(h, 400, {"ok": False, "error": "요청 형식을 확인해 주세요."})
    ok, v = validate(req)
    if not ok:
        return _send(h, 400, {"ok": False, "error": v})
    if v["kind"] == "clone":
        allowed = {x["id"] for x in status()["clone"]}
        if v["voice"] not in allowed:
            return _send(h, 409, {"ok": False, "error": "복제 목소리를 쓸 수 없습니다 — 엔진 연결과 본인 동의 확인이 끝난 목소리만 쓸 수 있습니다."})
    t0 = time.time()
    try:
        data, mime = (synth_clone if v["kind"] == "clone" else synth_standard)(v["text"], v["voice"], v["rate"])
    except Exception:
        return _send(h, 502, {"ok": False, "error": "음성을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요."})
    return _send(h, 200, {"ok": True, "kind": v["kind"], "voice": v["voice"], "mime": mime,
                          "audio_b64": base64.b64encode(data).decode(), "elapsed": round(time.time() - t0, 2)})
