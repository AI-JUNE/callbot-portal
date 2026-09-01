# -*- coding: utf-8 -*-
# ==========================================================================
# api/monitoring.py — 오류 모니터링 (Sentry 호환) 경량 리포터. 의존성 0.
# --------------------------------------------------------------------------
# 이식 출처: Dev\3. Chatbot\src\lib\monitoring.ts (MONITORING_GUIDE.md 절차)
#
# 설계 원칙 (MONITORING_GUIDE.md)
#  - SENTRY_DSN 미설정이면 완전한 no-op. 로컬·미설정 환경에서 아무 동작 없음.
#  - DSN 하드코딩 금지 — 환경변수로만 주입(Vercel Environment Variables).
#  - 전송 전 PII 마스킹(주민등록번호·카드·휴대전화·이메일·계좌).
#  - 전송 실패가 서비스에 영향을 주지 않는다(모든 예외 흡수, 재던지기 금지).
#  - 공식 SDK 도입 시 capture_error()만 교체하면 된다.
#
# 사용 예 (API 핸들러 except 블록)
#     import monitoring
#     try:
#         ...
#     except Exception as e:
#         eid = monitoring.capture_error(e, route="/api/chat", method="POST")
#         self._send(500, {"error": str(e), "event_id": eid})
# ==========================================================================
import os
import re
import json
import time
import uuid
import threading
import traceback
from urllib.parse import urlparse

CLIENT = "gowon-lite-py/1.0"
TIMEOUT = 3.0          # 전송 대기 상한(초)
MAX_MSG = 800          # 메시지 길이 상한
MAX_CTX = 300          # 컨텍스트 값 길이 상한


def _dsn():
    return (os.environ.get("SENTRY_DSN") or "").strip()


def _env():
    return (os.environ.get("VERCEL_ENV")
            or os.environ.get("CALLBOT_ENV")
            or "development").strip()


def _release():
    return (os.environ.get("VERCEL_GIT_COMMIT_SHA")
            or os.environ.get("CALLBOT_BUILD")
            or "dev").strip()


def parse_dsn(dsn):
    """DSN -> (envelope_url, public_key). 형식이 아니면 None.

    https://<key>@<host>/<project_id>  ->  https://<host>/api/<project_id>/envelope/
    """
    try:
        u = urlparse(dsn)
        if u.scheme not in ("http", "https"):
            return None
        project = (u.path or "").lstrip("/").strip("/")
        if not u.username or not project or not u.hostname:
            return None
        host = u.hostname + (":%d" % u.port if u.port else "")
        return ("%s://%s/api/%s/envelope/" % (u.scheme, host, project), u.username)
    except Exception:
        return None


# --- PII 마스킹 -----------------------------------------------------------
# 순서 주의: 카드(16자리) -> 주민번호 -> 휴대전화 -> 이메일 -> 계좌
_RULES = (
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "****-****-****-****"),   # 카드
    (re.compile(r"\b(\d{6})[-\s]?[1-4]\d{6}\b"), r"\1-*******"),          # 주민등록번호
    (re.compile(r"\b01[0-9][-\s]?\d{3,4}[-\s]?\d{4}\b"), "01*-****-****"),  # 휴대전화
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "***@***"),              # 이메일
    (re.compile(r"\b\d{2,3}-\d{2,6}-\d{2,6}\b"), "***-****-****"),         # 계좌
)


def scrub(value):
    """문자열에서 개인정보로 보이는 패턴을 마스킹한다. 항상 str 반환."""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unprintable>"
    for rx, rep in _RULES:
        s = rx.sub(rep, s)
    return s


def enabled():
    """DSN이 유효하게 설정돼 있는지. 매 호출 시 환경변수를 재평가한다."""
    d = _dsn()
    return bool(d) and parse_dsn(d) is not None


def status():
    """/health 노출용 요약 — DSN 값은 절대 포함하지 않는다."""
    return {
        "enabled": enabled(),
        "dsn_present": bool(_dsn()),
        "environment": _env(),
        "release": _release(),
    }


def _frames(exc):
    """스택트레이스 프레임 — 파일·라인·함수만. 소스/변수는 담지 않는다."""
    out = []
    try:
        for fr in traceback.extract_tb(exc.__traceback__)[-20:]:
            out.append({
                "filename": os.path.basename(fr.filename or ""),
                "lineno": fr.lineno,
                "function": fr.name,
            })
    except Exception:
        pass
    return out


def _envelope(exc, ctx, event_id):
    e_type = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
    e_msg = scrub(getattr(exc, "args", None) and str(exc) or str(exc))[:MAX_MSG]
    extra = {}
    for k, v in (ctx or {}).items():
        if v is None:
            continue
        extra[str(k)[:64]] = scrub(v)[:MAX_CTX] if isinstance(v, str) else v
    header = json.dumps({
        "event_id": event_id,
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, ensure_ascii=False)
    body = json.dumps({
        "event_id": event_id,
        "timestamp": time.time(),
        "platform": "python",
        "level": "error",
        "environment": _env(),
        "release": _release(),
        "logger": "callbot-portal",
        "exception": {"values": [{
            "type": e_type,
            "value": e_msg,
            "stacktrace": {"frames": _frames(exc)},
        }]},
        "extra": extra,
    }, ensure_ascii=False)
    item = json.dumps({"type": "event"}, ensure_ascii=False)
    return ("%s\n%s\n%s\n" % (header, item, body)).encode("utf-8")


def _post(url, key, payload):
    try:
        import urllib.request
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-sentry-envelope")
        req.add_header("X-Sentry-Auth",
                       "Sentry sentry_version=7, sentry_key=%s, sentry_client=%s" % (key, CLIENT))
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            pass
    except Exception:
        # 모니터링 실패가 서비스에 영향을 주지 않는다
        pass


def capture_error(exc, **ctx):
    """오류 1건 전송. 절대 예외를 던지지 않는다.

    반환값: event_id(전송 시도) 또는 None(no-op).
    ctx 예: route="/api/chat", method="POST", request_id="..."
    """
    try:
        target = parse_dsn(_dsn())
        if not target:
            return None
        event_id = uuid.uuid4().hex
        payload = _envelope(exc, ctx, event_id)
        url, key = target
        # 응답을 기다리며 요청 처리를 막지 않는다
        t = threading.Thread(target=_post, args=(url, key, payload), daemon=True)
        t.start()
        t.join(TIMEOUT)
        return event_id
    except Exception:
        return None


def guard(route, method=None, **ctx):
    """with 블록용 컨텍스트 매니저. 오류를 리포트하고 그대로 전파한다.

        with monitoring.guard("/api/chat", "POST"):
            ...
    """
    class _G(object):
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, et, ev, tb):
            if ev is not None:
                capture_error(ev, route=route, method=method, **ctx)
            return False  # 재전파
    return _G()
