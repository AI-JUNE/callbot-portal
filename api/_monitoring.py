# -*- coding: utf-8 -*-
# ==========================================================================
# api/monitoring.py — 오류 모니터링 (Sentry 호환) 경량 리포터. 의존성 0.
# --------------------------------------------------------------------------
# 이식 출처: Dev\3. Chatbot\src\lib\monitoring.ts (MONITORING_GUIDE.md 절차)
#
# 설계 원칙 (MONITORING_GUIDE.md)
#  - SENTRY_DSN 미설정이면 완전한 no-op. 로컬·미설정 환경에서 아무 동작 없음.
#  - DSN 하드코딩 금지 — 환경변수로만 주입(Vercel Environment Variables).
#  - **나가는 주소는 검증한다**(10-09, 23차 제안 「설정 유래 아웃바운드 점검」).
#    DSN 은 환경변수지만 오타·잘못 복사한 값이면 오류 봉투(라우트·request_id·파일명·
#    마스킹된 예외 문구)와 **DSN 공개키**가 그 주소로 평문·내부로 나간다. 판정은
#    `_urlguard` 한 곳(안부 웹훅·녹음·주문 백엔드와 공용)이고, 거부되면 전송하지
#    않는다. 탈출구는 `SENTRY_ALLOW_INSECURE=1`(사내 Sentry·로컬 개발)·`SENTRY_HOSTS`.
#    `/health.monitoring` 이 거부 사실과 사유를 드러낸다(「미설정」으로 뭉개지 않는다).
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
import sys
import json
import time
import uuid
import threading
import traceback
from urllib.parse import urlparse

# 평면 import(Vercel 서버리스는 api/ 안에서 모듈을 찾는다)를 이 모듈이 스스로 보장한다.
_d = os.path.dirname(os.path.abspath(__file__))
if _d not in sys.path:                        # pragma: no cover - importer 가 이미 넣어 둔다
    sys.path.insert(0, _d)

# 아웃바운드 주소 검증은 보안 통제라 폴백을 두지 않는다 — 검증 없이 나가는 것보다
# import 시점에 드러나는 편이 안전하다(`_order_backend`·`_vstudio` 와 같은 판단).
import _urlguard  # noqa: E402

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


def check_target(url):
    """(ok, reason). envelope 주소로 나가도 되는가.

    `SENTRY_ALLOW_INSECURE=1` 은 평문 http·사내망 Sentry 용,
    `SENTRY_HOSTS` 는 호스트 화이트리스트(가장 엄격한 운영 설정).
    """
    return _urlguard.check(url, label="SENTRY_DSN",
                           allow_insecure=_urlguard.env_flag("SENTRY_ALLOW_INSECURE"),
                           allowlist=_urlguard.env_hosts("SENTRY_HOSTS"))


def target():
    """(url, key) 또는 None — 형식·주소 검증을 **모두** 통과한 DSN 만.

    두 번째 반환값이 필요 없는 호출부는 `enabled()`·`blocked_reason()` 를 쓴다.
    """
    return _resolve()[0]


def blocked_reason():
    """DSN 이 있는데 전송하지 않는 이유(없으면 "").

    「미설정」과 「거부됨」을 구분하기 위해 있다 — 헬스가 둘을 같은 말로 보고하면
    운영자는 DSN 을 등록해 놓고 수집이 안 되는 이유를 알 수 없다.
    """
    return _resolve()[1]


def _resolve():
    """((url, key) 또는 None, 거부 사유). 매 호출 시 환경변수를 재평가한다."""
    d = _dsn()
    if not d:
        return None, ""
    parsed = parse_dsn(d)
    if parsed is None:
        return None, "DSN 형식이 올바르지 않습니다"
    ok, why = check_target(parsed[0])
    if not ok:
        # 사유에는 호스트 판정 결과만 담긴다 — 가드는 URL·키를 되비추지 않는다.
        return None, why or "허용되지 않은 주소"
    return parsed, ""


def enabled():
    """DSN이 유효하게 설정돼 있는지. 매 호출 시 환경변수를 재평가한다."""
    return _resolve()[0] is not None


def status():
    """/health 노출용 요약 — DSN 값은 절대 포함하지 않는다."""
    return {
        "enabled": enabled(),
        "dsn_present": bool(_dsn()),
        "blocked_reason": blocked_reason(),
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
        # 한계(숨기지 않고 적는다): `urlopen` 은 302 를 따라가고 헤더를 그대로 다시
        # 싣는다 — 등록된 주소가 리다이렉트하면 `X-Sentry-Auth`(공개키)와 오류 봉투가
        # 다른 호스트로도 간다. 응답 본문을 쓰지 않으니 사후 재검증은 아무것도 되돌리지
        # 못하므로(`wellbeing`·`_order_backend` 와 달리 여기선 의미가 없다) 두지 않았다.
        # 엄격히 잠그려면 `SENTRY_HOSTS` 화이트리스트 또는 아웃바운드 프록시를 쓴다.
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
        tgt = target()
        if not tgt:
            # 형식이 틀렸거나 아웃바운드 가드가 거부한 주소 — **보내지 않는다**.
            # 거부된 주소로는 봉투를 만들지도 않으므로 blind 전송도 성립하지 않는다.
            return None
        event_id = uuid.uuid4().hex
        payload = _envelope(exc, ctx, event_id)
        url, key = tgt
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
