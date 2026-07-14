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
#   CALLBOT_RATE_LIMIT   IP당 분당 허용 횟수(기본 40)
# ==========================================================================
import os, time, json
from urllib.parse import urlparse, parse_qs


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

WINDOW = 60.0
_HITS = {}


def _limit():
    try:
        return int((os.environ.get("CALLBOT_RATE_LIMIT") or "40").strip())
    except Exception:
        return 40


def _client_ip(headers):
    xf = (headers.get("x-forwarded-for") or "")
    if xf:
        return xf.split(",")[0].strip()
    return (headers.get("x-real-ip") or "unknown")


def rate_ok(headers):
    ip = _client_ip(headers)
    now = time.time()
    q = [t for t in _HITS.get(ip, []) if now - t < WINDOW]
    if len(q) >= _limit():
        _HITS[ip] = q
        return False
    q.append(now)
    _HITS[ip] = q
    return True


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
    if not rate_ok(headers):
        return (False, 429, "rate limit exceeded")

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


def deny(h, code, msg):
    body = json.dumps({"ok": False, "error": msg, "code": code}, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Access-Control-Allow-Origin", allow_origin_header(h.headers))
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
