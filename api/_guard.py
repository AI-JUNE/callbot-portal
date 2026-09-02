# -*- coding: utf-8 -*-
# ==========================================================================
# API 접근 가드 (Access Guard)
# --------------------------------------------------------------------------
# 목적: /api/* 가 무인증·CORS(*) 로 전면 공개돼 있어, 외부에서 아무나 호출해
#       GOOGLE_API_KEY(Gemini) 로 과금을 유발할 수 있던 문제를 막는다.
#
# 기본 동작(환경변수 미설정): 동일 오리진(포털)에서 온 브라우저 요청만 허용.
#   → 포털은 그대로 동작, 외부 curl/타 사이트 호출은 403.
#
# 선택 강화:
#   CALLBOT_API_KEY      설정 시 헤더 X-API-Key 로도 통과 가능
#   CALLBOT_STRICT=1     오리진만으로는 불가, API 키 필수(완전 잠금)
#   CPAAS_WEBHOOK_TOKEN  실전화 웹훅용. voice 웹훅은 ?t=<토큰> 또는 X-Webhook-Token
#   CALLBOT_ALLOWED_ORIGINS  콤마구분 허용 오리진(기본: 운영 도메인 + localhost)
#   CALLBOT_RATE_LIMIT   IP당 분당 허용 횟수(기본 40) — 등급별 한도는 _ratelimit.py
# ==========================================================================
import os, sys, time, json
from urllib.parse import urlparse, parse_qs

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)


def _env_list(name, default):
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    return [x.strip().rstrip("/") for x in v.split(",") if x.strip()]


ALLOWED = _env_list("CALLBOT_ALLOWED_ORIGINS", [
    "https://callbot-portal.vercel.app",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
])

# --------------------------------------------------------------------------
# 요청 제한 — 구현은 api/_ratelimit.py (경로 등급별 IP 한도 + 전역 과금 상한).
# 아래는 하위호환 껍데기다. 기존 호출부(rate_ok)가 그대로 동작한다.
# --------------------------------------------------------------------------
WINDOW = 60.0

try:
    import _ratelimit
except Exception:  # 부재해도 서비스는 뜬다(가용성 우선)
    _ratelimit = None


def _limit(path=""):
    if _ratelimit is None:
        try:
            return int((os.environ.get("CALLBOT_RATE_LIMIT") or "40").strip())
        except Exception:
            return 40
    return _ratelimit.limits(_ratelimit.route_class(path))[0]


def _client_ip(headers):
    if _ratelimit is not None:
        return _ratelimit.client_ip(headers)
    xf = (headers.get("x-forwarded-for") or "")
    if xf:
        return xf.split(",")[0].strip()
    return (headers.get("x-real-ip") or "unknown")


def rate_ok(headers, path=""):
    """하위호환 진입점. 판정 상세는 _ratelimit.current() 로 꺼낸다."""
    if _ratelimit is None:
        return True
    try:
        return _ratelimit.check(headers, path).allowed
    except Exception:
        return True  # 제한 로직 장애가 서비스 중단이 되지 않게 한다


def rate_headers():
    """직전 판정의 RateLimit 응답 헤더 [(name, value), ...]."""
    if _ratelimit is None:
        return []
    try:
        d = _ratelimit.current()
        return d.headers() if d is not None else []
    except Exception:
        return []


def _origin_ok(headers):
    # 브라우저 동일출처 신호(위조 난이도는 Origin과 동급, 동일출처 GET에서 Origin 부재 보완)
    sfs = (headers.get("sec-fetch-site") or "").strip().lower()
    if sfs == "same-origin":
        return True
    o = (headers.get("origin") or "").strip().rstrip("/")
    if o:
        return o in ALLOWED
    r = (headers.get("referer") or "").strip()
    if r:
        try:
            p = urlparse(r)
            return ("%s://%s" % (p.scheme, p.netloc)).rstrip("/") in ALLOWED
        except Exception:
            return False
    return False  # 오리진/리퍼러 없음 = 브라우저 아님


def allow_origin_header(headers):
    o = (headers.get("origin") or "").strip().rstrip("/")
    if o in ALLOWED:
        return o
    return ALLOWED[0] if ALLOWED else "null"


def check(headers, path="", allow_webhook=False):
    """(ok, code, msg)"""
    if not rate_ok(headers, path):
        scope = ""
        try:
            d = _ratelimit.current()
            scope = getattr(d, "scope", "") or ""
        except Exception:
            pass
        # scope="global" 은 개별 호출자 잘못이 아니라 서비스 전체 상한이다.
        return (False, 429, "rate limit exceeded (%s)" % (scope or "ip"))

    key = (os.environ.get("CALLBOT_API_KEY") or "").strip()
    if key and (headers.get("x-api-key") or "").strip() == key:
        return (True, 200, "")

    strict = (os.environ.get("CALLBOT_STRICT") or "").strip() == "1"
    if (not strict) and _origin_ok(headers):
        return (True, 200, "")

    if allow_webhook:
        tok = (os.environ.get("CPAAS_WEBHOOK_TOKEN") or "").strip()
        if tok:
            sent = (headers.get("x-webhook-token") or "").strip()
            if not sent:
                try:
                    sent = (parse_qs(urlparse(path).query).get("t", [""])[0] or "").strip()
                except Exception:
                    sent = ""
            if sent == tok:
                return (True, 200, "")
        return (False, 401, "webhook auth required (set CPAAS_WEBHOOK_TOKEN and pass ?t=)")

    if strict:
        return (False, 401, "unauthorized: API key required")
    return (False, 403, "forbidden: cross-origin")


def deny(h, code, msg, rq=None):
    """거부 응답 — 표준 에러 봉투(api/_errors.py)로 통일.

    msg 는 개발자용 원인 문구(설정 힌트 포함)라 기본적으로 노출하지 않는다.
    CALLBOT_DEBUG_ERRORS=1 일 때만 "debug" 키로 덧붙는다.
    _errors 가 없는 환경에서도 동작하도록 폴백을 남긴다.
    """
    try:
        import _errors
        return _errors.send(h, status=code, rq=rq, debug=msg,
                            extra_headers=(rate_headers() if code == 429 else None))
    except Exception:
        pass
    body = json.dumps({"ok": False, "error": msg, "code": code}, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", allow_origin_header(h.headers))
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
