# -*- coding: utf-8 -*-
"""안부 콜봇 — 이음 2R 연동 엔드포인트 (`/api/wellbeing`).

목적: 담당자 현황판의 「안부 전화」 버튼 하나로 안부 시나리오를 돌리고,
통화가 끝나면 결과를 이음이 지정한 `callback_url` 로 웹훅 POST 한다.

**실전화 없이 동작한다.** 기본 경로는 전화망을 거치지 않는 시뮬레이션이며
(CPaaS 미호출·통신비 0원), 실회선 발신은 `CPAAS_LIVE=1` + 사람 승인이 있어야
한다(3R 과제). 승인 전에는 live 요청이 501 로 거부된다.

설계 원칙
 - **판정은 규칙 기반**: mood_score·risk_level 은 LLM 문장이 아니라 답변 키워드
   규칙(`score_answers`)으로 산출한다. 같은 답변이면 항상 같은 점수가 나오고
   근거(`dimensions`)를 함께 돌려주므로 담당자가 검증할 수 있다.
 - **개인정보 미포함**: `transcript_summary` 는 원문 발화가 아니라 4개 항목의
   판정 라벨로 조립하고, 마지막에 `monitoring.scrub()` 을 한 번 더 통과시킨다.
   성명·연락처는 페이로드 어디에도 담기지 않는다(`senior_id` 는 이음이 준 익명 키).
 - **오류를 삼키지 않는다**: 웹훅 전송 실패는 응답 `delivery.delivered=false` 와
   사유로 그대로 드러낸다(200 으로 위장하지 않는다).
 - **SSRF 방어**: `callback_url` 은 https(개발 시 http 허용) + 사설/루프백/메타데이터
   주소 차단. `WELLBEING_CALLBACK_HOSTS` 로 화이트리스트를 걸 수 있다.

환경변수
  CALLBACK_SECRET            웹훅 HMAC 서명 키. 미설정 시 서명 생략(로컬)
  WELLBEING_CALLBACK_HOSTS   콜백 허용 호스트(콤마). 미설정 시 공개 https 전부 허용
  WELLBEING_ALLOW_INSECURE   1이면 http·로컬호스트 콜백 허용(로컬 개발 전용)
  CPAAS_LIVE                 1이 아니면 항상 시뮬레이션(기본). 실발신은 [승인 필요]

셀프테스트:  python3 api/wellbeing.py
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

try:
    from monitoring import scrub as _scrub
except Exception:  # 모니터링 모듈이 없어도 동작해야 한다
    def _scrub(v):
        return v if isinstance(v, str) else str(v)

SCHEMA = "wellbeing.result.v1"
EVENT = "wellbeing.result"
SIGNATURE_HEADER = "X-Callbot-Signature"
TIMESTAMP_HEADER = "X-Callbot-Timestamp"
WEBHOOK_TIMEOUT = 8.0          # 초 — 전체 처리는 30초 예산 안에 든다
SIGNATURE_TOLERANCE = 300      # 초 — 재전송(replay) 허용 시차

RISK_LOW, RISK_MID, RISK_HIGH, RISK_UNKNOWN = "low", "mid", "high", "unknown"


def live_mode():
    """실회선 발신 여부. 기본 False(시뮬레이션) — 켜는 것은 [승인 필요]."""
    return (os.environ.get("CPAAS_LIVE") or "").strip() == "1"


# --------------------------------------------------------------------------
# 1) 안부 시나리오 — 기분·식사·수면·통증 4문항
# --------------------------------------------------------------------------
QUESTIONS = [
    {"key": "mood",  "text": "요즘 기분은 어떠세요?"},
    {"key": "meal",  "text": "식사는 잘 챙겨 드시고 계세요?"},
    {"key": "sleep", "text": "밤에 잠은 잘 주무세요?"},
    {"key": "pain",  "text": "어디 아프신 데는 없으세요?"},
]
QUESTION_KEYS = [q["key"] for q in QUESTIONS]

# 시연용 응답 프로필(전화망 미경유). 실제 통화 전사가 아니라 고정 대본이다.
PROFILES = {
    "ok":      {"mood": "그냥 좋아요, 오늘 산책도 했어요",
                "meal": "세 끼 잘 챙겨 먹었어요",
                "sleep": "푹 잘 잤어요",
                "pain": "아픈 데 없어요"},
    "watch":   {"mood": "그럭저럭이요, 좀 심심해요",
                "meal": "입맛이 없어서 한 끼만 먹었어요",
                "sleep": "자주 깨요",
                "pain": "무릎이 좀 쑤셔요"},
    "risk":    {"mood": "요즘 너무 외롭고 우울해요",
                "meal": "며칠째 제대로 못 먹었어요",
                "sleep": "밤새 한숨도 못 잤어요",
                "pain": "어지러워서 잘 못 움직이겠어요"},
    "no_answer": {},
}

# 항목별 판정 규칙: (점수, [키워드]) — 위에서부터 먼저 맞는 규칙을 쓴다.
_RULES = {
    "mood": [
        (1, ["죽고 싶", "살기 싫", "다 끝내"]),
        (2, ["우울", "외롭", "외로", "힘들", "눈물", "answer_no_will"]),
        (3, ["그럭저럭", "심심", "그저 그", "별로"]),
        (5, ["아주 좋", "행복", "기분 좋"]),
        (4, ["좋아", "괜찮", "편안"]),
    ],
    "meal": [
        (1, ["며칠째", "굶", "못 먹", "안 먹"]),
        (2, ["입맛이 없", "한 끼", "거르"]),
        (5, ["세 끼", "잘 챙겨"]),
        (4, ["잘 먹", "챙겨 먹", "먹었어"]),
    ],
    "sleep": [
        (1, ["한숨도 못", "밤새", "며칠째 못 자"]),
        (2, ["못 자", "잠이 안", "새벽에 깨", "자주 깨"]),
        (5, ["푹 잘", "푹 자"]),
        (4, ["잘 자", "잘 잤"]),
    ],
    "pain": [
        (1, ["못 움직", "쓰러", "숨이 차", "피가", "가슴이 답답"]),
        (2, ["어지럽", "어지러", "많이 아파", "계속 아파", "열이"]),
        (3, ["쑤시", "결려", "뻐근", "좀 아파", "허리", "무릎"]),
        (5, ["아픈 데 없", "아무렇지"]),
        (4, ["괜찮", "견딜 만"]),
    ],
}
# 점수와 무관하게 즉시 위험으로 올리는 신호
_CRITICAL = {
    "self_harm": ["죽고 싶", "살기 싫", "다 끝내"],
    "immobile": ["못 움직", "쓰러", "숨이 차"],
    "starving": ["며칠째", "굶", "못 먹", "안 먹"],
    "dizzy": ["어지럽", "어지러"],
}
_LABEL = {
    "mood": {1: "기분 매우 저조", 2: "기분 저조", 3: "기분 보통", 4: "기분 양호", 5: "기분 좋음"},
    "meal": {1: "식사 거의 못 함", 2: "식사 부족", 3: "식사 보통", 4: "식사 양호", 5: "식사 규칙적"},
    "sleep": {1: "수면 거의 못 함", 2: "수면 부족", 3: "수면 보통", 4: "수면 양호", 5: "수면 충분"},
    "pain": {1: "거동 곤란", 2: "통증 있음", 3: "경미한 통증", 4: "통증 거의 없음", 5: "통증 없음"},
}


def score_one(key, text):
    """항목 1개 채점 — 규칙 미적중 시 3(보통). 항상 1~5."""
    t = (text or "").strip()
    if not t:
        return 3
    for score, words in _RULES.get(key, []):
        for w in words:
            if w in t:
                return score
    return 3


def score_answers(answers):
    """4문항 답변 -> {dimensions, mood_score, risk_level, flags}.

    mood_score 는 4개 항목 평균의 반올림(1~5). risk_level 은 평균만으로 정하지
    않는다 — 한 항목이라도 1점이거나 위험 신호가 있으면 high 로 올린다.
    (평균에 묻혀 '거동 곤란'이 low 로 보고되는 일을 막는다)
    """
    answers = answers if isinstance(answers, dict) else {}
    dims, flags = {}, []
    for k in QUESTION_KEYS:
        s = score_one(k, answers.get(k))
        dims[k] = {"score": s, "label": _LABEL[k][s]}
    joined = " ".join(str(answers.get(k) or "") for k in QUESTION_KEYS)
    for flag, words in _CRITICAL.items():
        if any(w in joined for w in words):
            flags.append(flag)
    scores = [dims[k]["score"] for k in QUESTION_KEYS]
    avg = sum(scores) / float(len(scores))
    mood_score = int(round(avg))
    mood_score = 1 if mood_score < 1 else (5 if mood_score > 5 else mood_score)
    if flags or min(scores) <= 1 or mood_score <= 2:
        risk = RISK_HIGH
    elif mood_score <= 3 or min(scores) <= 2:
        risk = RISK_MID
    else:
        risk = RISK_LOW
    return {"dimensions": dims, "mood_score": mood_score, "risk_level": risk, "flags": flags}


def summarize(dims, flags, answered=True):
    """통화 요약 — 원문 발화가 아니라 판정 라벨로 조립한다(성명·연락처 미포함)."""
    if not answered:
        return "통화 미응답 — 안부 확인 못 함"
    parts = [dims[k]["label"] for k in QUESTION_KEYS if k in dims]
    text = ", ".join(parts)
    if flags:
        text += " / 주의신호: " + ", ".join(flags)
    return _scrub(text)[:300]


# --------------------------------------------------------------------------
# 2) 웹훅 페이로드 · HMAC 서명
# --------------------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_ref():
    """통화 참조키 — 개인정보가 섞이지 않는 불투명 식별자."""
    return "wb_" + uuid.uuid4().hex[:12]


def build_payload(senior_id, answered=True, answers=None, raw_ref=None, mode="simulation"):
    """이음 연동 규격 페이로드. 미응답이면 mood_score=null·risk_level=unknown."""
    ref = raw_ref or new_ref()
    if not answered:
        return {
            "schema": SCHEMA, "scenario": "wellbeing", "mode": mode,
            "senior_id": senior_id, "answered": False,
            "mood_score": None, "risk_level": RISK_UNKNOWN,
            "transcript_summary": summarize({}, [], answered=False),
            "raw_ref": ref, "dimensions": {}, "flags": ["no_answer"], "ts": _now_iso(),
        }
    sc = score_answers(answers)
    return {
        "schema": SCHEMA, "scenario": "wellbeing", "mode": mode,
        "senior_id": senior_id, "answered": True,
        "mood_score": sc["mood_score"], "risk_level": sc["risk_level"],
        "transcript_summary": summarize(sc["dimensions"], sc["flags"]),
        "raw_ref": ref, "dimensions": sc["dimensions"], "flags": sc["flags"],
        "ts": _now_iso(),
    }


def _secret():
    return (os.environ.get("CALLBACK_SECRET") or "").strip()


def sign(body, secret, ts=None):
    """HMAC-SHA256 서명. 서명 대상은 `<ts>.<body>` — 본문 재사용(replay)을 막는다.

    반환: (timestamp, "sha256=<hex>"). secret 이 없으면 (ts, "") — 서명 생략.
    """
    ts = str(ts if ts is not None else int(time.time()))
    if not secret:
        return ts, ""
    if isinstance(body, str):
        body = body.encode("utf-8")
    msg = ts.encode("ascii") + b"." + body
    mac = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return ts, "sha256=" + mac


def verify_signature(body, secret, ts, signature, tolerance=SIGNATURE_TOLERANCE, now=None):
    """수신측(이음) 검증용 — 서명 규약을 문서가 아니라 코드로 남긴다."""
    if not secret:
        return False
    try:
        ts_i = int(str(ts).strip())
    except Exception:
        return False
    now = int(now if now is not None else time.time())
    if tolerance is not None and abs(now - ts_i) > int(tolerance):
        return False
    _, expected = sign(body, secret, ts_i)
    return hmac.compare_digest(expected, str(signature or ""))


# --------------------------------------------------------------------------
# 3) 콜백 URL 검증 (SSRF 방어)
# --------------------------------------------------------------------------
_BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost",
    "metadata.google.internal", "metadata", "instance-data",
}


def _allow_insecure():
    return (os.environ.get("WELLBEING_ALLOW_INSECURE") or "").strip() == "1"


def _host_allowlist():
    v = (os.environ.get("WELLBEING_CALLBACK_HOSTS") or "").strip()
    return [x.strip().lower() for x in v.split(",") if x.strip()] if v else []


def check_callback_url(url):
    """(ok, reason). 사설망·루프백·메타데이터 주소로의 웹훅을 막는다.

    한계: DNS 를 조회하지 않으므로 공개 도메인이 사설 IP 로 해석되는
    rebinding 은 막지 못한다. 그 방어는 아웃바운드 프록시 몫이며,
    엄격히 잠그려면 `WELLBEING_CALLBACK_HOSTS` 화이트리스트를 쓴다.
    """
    u = (url or "").strip()
    if not u:
        return False, "callback_url 이 비어 있습니다"
    if len(u) > 2048:
        return False, "callback_url 이 너무 깁니다"
    try:
        p = urlparse(u)
    except Exception:
        return False, "callback_url 형식이 올바르지 않습니다"
    if p.scheme not in ("http", "https"):
        return False, "http(s) 주소만 허용합니다"
    if p.scheme == "http" and not _allow_insecure():
        return False, "https 주소만 허용합니다"
    host = (p.hostname or "").lower()
    if not host:
        return False, "호스트가 없습니다"
    allow = _host_allowlist()
    if allow:
        if host not in allow:
            return False, "허용되지 않은 콜백 호스트입니다"
        return True, ""
    if host in _BLOCKED_HOSTS or host.endswith(".internal") or host.endswith(".local"):
        if not _allow_insecure():
            return False, "내부 주소로는 보낼 수 없습니다"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                           or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        if not _allow_insecure():
            return False, "사설·루프백 주소로는 보낼 수 없습니다"
    return True, ""


# --------------------------------------------------------------------------
# 4) 웹훅 발송
# --------------------------------------------------------------------------
def deliver(url, payload, timeout=WEBHOOK_TIMEOUT, secret=None):
    """결과 웹훅 POST. 실패를 삼키지 않고 사유를 그대로 반환한다."""
    secret = _secret() if secret is None else secret
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts, sig = sign(body, secret)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "callbot-wellbeing/1",
        "X-Callbot-Event": EVENT,
        TIMESTAMP_HEADER: ts,
    }
    if sig:
        headers[SIGNATURE_HEADER] = sig
    out = {"delivered": False, "signed": bool(sig), "status": None,
           "error": None, "attempted_at": _now_iso()}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            out["delivered"] = 200 <= out["status"] < 300
            if not out["delivered"]:
                out["error"] = "콜백이 %d 로 응답했습니다" % out["status"]
    except urllib.error.HTTPError as e:
        out["status"] = int(getattr(e, "code", 0) or 0)
        out["error"] = "콜백이 %s 로 응답했습니다" % (out["status"] or "오류")
    except Exception as e:
        # 예외 문구에 URL·자격정보가 섞일 수 있어 타입명만 남긴다
        out["error"] = "콜백 전송 실패(%s)" % type(e).__name__
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


# --------------------------------------------------------------------------
# 5) 시뮬레이션 실행 (전화망 미경유)
# --------------------------------------------------------------------------
RECENT = []          # 최근 실행 메타(개인정보 없음) — 콘솔 확인용
_MAX_RECENT = 50


def _remember(entry):
    RECENT.insert(0, entry)
    del RECENT[_MAX_RECENT:]


def _clean_senior_id(v):
    s = str(v or "").strip()
    if not s:
        raise ValueError("senior_id 가 필요합니다")
    if len(s) > 64:
        raise ValueError("senior_id 는 64자 이하여야 합니다")
    if not all(c.isalnum() or c in "-_:." for c in s):
        raise ValueError("senior_id 는 영숫자와 -_:. 만 허용합니다")
    return s


def run_wellbeing(senior_id, callback_url=None, profile="ok", answers=None,
                  mode="simulation", deliver_fn=None):
    """안부 통화 1건을 시뮬레이션하고 결과 웹훅을 보낸다.

    - `answers` 를 주면 그 답변으로 채점(연동 테스트용), 없으면 `profile` 대본 사용.
    - `callback_url` 이 없으면 페이로드만 만들어 돌려준다(dry-run).
    - 실회선(live)은 이 함수에서 다루지 않는다 — 핸들러가 501 로 막는다.
    """
    sid = _clean_senior_id(senior_id)
    if answers is None:
        if profile not in PROFILES:
            raise ValueError("알 수 없는 profile: %s (%s)" % (profile, "/".join(PROFILES)))
        answers = PROFILES[profile]
    if not isinstance(answers, dict):
        raise ValueError("answers 는 객체여야 합니다")
    answers = {k: str(answers.get(k) or "")[:500] for k in QUESTION_KEYS}
    answered = any(answers.get(k) for k in QUESTION_KEYS)
    ref = new_ref()
    payload = build_payload(sid, answered=answered, answers=answers, raw_ref=ref, mode=mode)
    result = {"ok": True, "mode": mode, "billing": "0원(전화망 미경유)",
              "questions": QUESTIONS, "payload": payload, "delivery": None}
    if callback_url:
        ok, reason = check_callback_url(callback_url)
        if not ok:
            raise ValueError(reason)
        fn = deliver_fn or deliver
        result["delivery"] = fn(callback_url, payload)
        result["ok"] = bool(result["delivery"].get("delivered"))
    else:
        result["delivery"] = {"delivered": False, "skipped": True,
                              "reason": "callback_url 미지정 — 페이로드만 생성(dry-run)"}
    _remember({"ts": payload["ts"], "raw_ref": ref, "senior_id": sid,
               "answered": answered, "risk_level": payload["risk_level"],
               "delivered": bool((result["delivery"] or {}).get("delivered"))})
    return result


# --------------------------------------------------------------------------
# 6) HTTP 핸들러
# --------------------------------------------------------------------------
import _guard      # noqa: E402
import _errors     # noqa: E402

try:
    import _audit
except Exception:  # pragma: no cover
    _audit = None

MAX_BODY = 64 * 1024


def _audit_ev(headers, path, method, result, status, **extra):
    if _audit is None:
        return None
    try:
        return _audit.record_request(headers, path, method, result, status, **extra)
    except Exception:
        return None


def _op_from_path(path):
    """`/api/wellbeing/call` 과 `/api/wellbeing?op=call` 을 모두 받는다.

    Vercel 은 `api/wellbeing.py` 를 `/api/wellbeing` 하나로만 노출하므로
    하위경로는 vercel.json 의 rewrite 로 `?op=` 로 넘어온다. 로컬에서는
    하위경로가 그대로 들어오므로 두 경로 모두 해석한다.
    """
    try:
        u = urlparse(path)
    except Exception:
        return ""
    seg = [x for x in (u.path or "").strip("/").split("/") if x]
    if len(seg) >= 3:
        return seg[2].lower()
    try:
        return (parse_qs(u.query).get("op", [""])[0] or "").strip().lower()
    except Exception:
        return ""


class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Webhook-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        ok, code, msg = _guard.check(self.headers, self.path, allow_webhook=True)
        if not ok:
            _audit_ev(self.headers, self.path, "GET", "deny", code)
            return _guard.deny(self, code, msg)
        _audit_ev(self.headers, self.path, "GET", "allow", 200)
        q = parse_qs(urlparse(self.path).query)
        if q.get("op", [""])[0] == "recent":
            return self._send({"ok": True, "recent": RECENT})
        self._send({
            "ok": True, "endpoint": "wellbeing", "scenario": "안부",
            "live": live_mode(),
            "mode": "live" if live_mode() else "simulation",
            "note": "실전화 미경유 시뮬레이션. 실회선 발신은 [승인 필요]",
            "questions": QUESTIONS,
            "profiles": sorted(PROFILES),
            "signature": {"header": SIGNATURE_HEADER, "timestamp_header": TIMESTAMP_HEADER,
                          "algorithm": "HMAC-SHA256(`<ts>.<body>`)",
                          "enabled": bool(_secret())},
            "result_schema": SCHEMA,
            "sample": build_payload("SR-0001", answered=True, answers=PROFILES["watch"],
                                    raw_ref="wb_sample"),
        })

    def do_POST(self):
        ok, code, msg = _guard.check(self.headers, self.path, allow_webhook=True)
        if not ok:
            _audit_ev(self.headers, self.path, "POST", "deny", code)
            return _guard.deny(self, code, msg)
        _audit_ev(self.headers, self.path, "POST", "allow", 200, live=live_mode())
        try:
            body = _errors.read_json(self, max_bytes=MAX_BODY, required=True)
            op = _op_from_path(self.path) or str(body.get("op") or "call").lower()
            if op not in ("call", ""):
                raise _errors.ValidationError.field("op", "지원하지 않는 동작입니다(call)")
            if live_mode() or str(body.get("mode") or "").lower() == "live":
                # 실발신은 코드가 아니라 사람 승인으로만 열린다.
                return _errors.send(self, status=501, code="NOT_IMPLEMENTED",
                                    message="실회선 발신은 승인 후 활성화됩니다([승인 필요]). "
                                            "현재는 전화망 미경유 시뮬레이션만 제공합니다.")
            senior_id = _errors.as_str(body, "senior_id", required=True, max_len=64,
                                       allow_empty=False)
            callback_url = _errors.as_str(body, "callback_url", default="", max_len=2048)
            profile = _errors.as_choice(body, "profile", sorted(PROFILES), default="ok")
            answers = body.get("answers")
            if answers is not None and not isinstance(answers, dict):
                raise _errors.ValidationError.field("answers", "객체여야 합니다")
            try:
                out = run_wellbeing(senior_id, callback_url or None, profile=profile,
                                    answers=answers)
            except ValueError as e:
                raise _errors.ValidationError.field("request", str(e))
            # 웹훅 전송 실패는 502 로 드러낸다 — 성공으로 위장하지 않는다.
            d = out.get("delivery") or {}
            if callback_url and not d.get("delivered"):
                return self._send({**out, "ok": False,
                                   "error": d.get("error") or "콜백 전송 실패",
                                   "code": "CALLBACK_DELIVERY_FAILED"}, code=502)
            self._send(out)
        except Exception as e:
            _errors.handle(self, e, route="/api/wellbeing", method="POST")


# --------------------------------------------------------------------------
# 셀프테스트 (네트워크 미사용)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    a = score_answers(PROFILES["ok"])
    assert a["risk_level"] == RISK_LOW, a
    assert a["mood_score"] >= 4, a
    b = score_answers(PROFILES["risk"])
    assert b["risk_level"] == RISK_HIGH, b
    assert "starving" in b["flags"], b
    c = score_answers(PROFILES["watch"])
    assert c["risk_level"] in (RISK_MID, RISK_HIGH), c

    p = build_payload("SR-1", answered=False, raw_ref="wb_x")
    assert p["mood_score"] is None and p["risk_level"] == RISK_UNKNOWN

    ts, sg = sign(b'{"a":1}', "secret", 1000)
    assert verify_signature(b'{"a":1}', "secret", ts, sg, now=1000)
    assert not verify_signature(b'{"a":2}', "secret", ts, sg, now=1000)
    assert not verify_signature(b'{"a":1}', "secret", ts, sg, now=99999)
    assert sign(b"x", "")[1] == ""

    assert not check_callback_url("http://127.0.0.1/cb")[0]
    assert not check_callback_url("https://192.168.0.5/cb")[0]
    assert not check_callback_url("ftp://x/cb")[0]
    assert check_callback_url("https://eum.example.org/hook")[0]

    calls = []
    r = run_wellbeing("SR-9", "https://eum.example.org/hook", profile="risk",
                      deliver_fn=lambda u, pl: (calls.append((u, pl)),
                                                {"delivered": True, "status": 200})[1])
    assert r["ok"] and calls and calls[0][1]["risk_level"] == RISK_HIGH
    assert "010" not in json.dumps(r, ensure_ascii=False)
    print("SELF-TEST OK:", json.dumps(r["payload"], ensure_ascii=False)[:160])
