# -*- coding: utf-8 -*-
# ==========================================================================
# api/_errors.py — 표준 에러 응답 + 입력검증 (의존성 0)
# --------------------------------------------------------------------------
# 목적: /api/* 가 핸들러마다 제각각인 형태로 오류를 반환하던 문제를 없앤다.
#       (기존: {"error": str(e)} / {"ok":false,"error":...} / 본문 없는 400)
#       클라이언트가 하나의 규약만 알면 되고, 내부 예외 문구가 새어나가지 않는다.
#
# 표준 봉투 (모든 4xx·5xx 공통)
#     {
#       "ok": false,
#       "error": "요청 형식이 올바르지 않습니다.",   # 사람이 읽는 안전한 문구(문자열)
#       "code": "INVALID_REQUEST",                    # 안정적인 기계용 코드
#       "status": 400,
#       "request_id": "…",                            # 로그·모니터링 상호추적 키
#       "details": [{"field":"task","reason":"…"}],   # 입력검증 실패시에만
#       "event_id": "…"                               # 모니터링 전송 성공시에만
#     }
#
# 설계 원칙
#  - **내부 문구 미노출**: str(exc) 는 기본적으로 응답에 넣지 않는다. 스택·경로·
#    업스트림 메시지에는 키·토큰·PII 가 섞일 수 있다. 진단이 필요하면
#    CALLBOT_DEBUG_ERRORS=1 일 때만 scrub 를 거쳐 "debug" 키로 덧붙인다.
#  - **하위호환**: "error" 는 계속 문자열이다. 콘솔(public/admin.html)이
#    '오류: ' + d.error 형태로 그대로 쓰고 있으므로 타입을 바꾸지 않는다.
#  - **의존성 0**: 표준 라이브러리만. _guard·_log·monitoring 은 지연 import 로
#    묶어 순환 참조와 부재 상황을 모두 견딘다.
#  - **검증은 거부만**: 값을 몰래 고쳐 통과시키지 않는다. 잘라내기(truncate)가
#    필요한 자리는 호출측이 명시적으로 max_len 을 준다.
# ==========================================================================
import os
import sys
import json

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

# 기본 본문 상한 — 과금·메모리 방어. 오디오 업로드처럼 큰 입력은 호출측이 올린다.
MAX_BODY = 1024 * 1024          # 1 MiB
MAX_BODY_AUDIO = 8 * 1024 * 1024  # 8 MiB (STT base64)

# 상태코드 -> 안정 코드 / 사용자 문구
CODE_BY_STATUS = {
    400: "INVALID_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}

MESSAGE_BY_CODE = {
    "INVALID_REQUEST": "요청 형식이 올바르지 않습니다.",
    "UNAUTHORIZED": "인증이 필요합니다.",
    "FORBIDDEN": "허용되지 않은 요청입니다.",
    "NOT_FOUND": "요청한 리소스를 찾을 수 없습니다.",
    "METHOD_NOT_ALLOWED": "허용되지 않은 메서드입니다.",
    "REQUEST_TIMEOUT": "요청 처리 시간이 초과됐습니다.",
    "PAYLOAD_TOO_LARGE": "요청 본문이 너무 큽니다.",
    "UNSUPPORTED_MEDIA_TYPE": "지원하지 않는 형식입니다.",
    "UNPROCESSABLE": "요청을 처리할 수 없습니다.",
    "RATE_LIMITED": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.",
    "INTERNAL_ERROR": "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
    "UPSTREAM_ERROR": "외부 서비스 연동에 실패했습니다. 잠시 후 다시 시도해 주세요.",
    "SERVICE_UNAVAILABLE": "서비스를 일시적으로 사용할 수 없습니다.",
    "UPSTREAM_TIMEOUT": "외부 서비스 응답이 지연되고 있습니다.",
}


def _debug_on():
    return (os.environ.get("CALLBOT_DEBUG_ERRORS") or "").strip() in ("1", "true", "yes")


def _scrub(v):
    try:
        from monitoring import scrub
        return scrub(v)
    except Exception:
        return v if isinstance(v, str) else str(v)


def _allow_origin(headers):
    try:
        import _guard
        return _guard.allow_origin_header(headers)
    except Exception:
        return "null"


# --------------------------------------------------------------------------
# 검증 예외
# --------------------------------------------------------------------------
class ValidationError(Exception):
    """입력검증 실패. 400 + details 로 직렬화된다. 내부 정보를 담지 않는다."""

    status = 400

    def __init__(self, details=None, message=None, code="INVALID_REQUEST", status=400):
        self.details = list(details or [])
        self.code = code
        self.status = status
        self.message = message or MESSAGE_BY_CODE.get(code, MESSAGE_BY_CODE["INVALID_REQUEST"])
        super(ValidationError, self).__init__(self.message)

    @classmethod
    def field(cls, name, reason, **kw):
        return cls(details=[{"field": str(name), "reason": str(reason)}], **kw)


# --------------------------------------------------------------------------
# 봉투 생성 / 전송
# --------------------------------------------------------------------------
def payload(status=500, code=None, message=None, request_id=None,
            details=None, event_id=None, debug=None):
    """표준 에러 봉투(dict)."""
    try:
        status = int(status)
    except Exception:
        status = 500
    code = (code or CODE_BY_STATUS.get(status) or "INTERNAL_ERROR")
    out = {
        "ok": False,
        "error": message or MESSAGE_BY_CODE.get(code) or MESSAGE_BY_CODE["INTERNAL_ERROR"],
        "code": code,
        "status": status,
    }
    if request_id:
        out["request_id"] = str(request_id)[:64]
    if details:
        out["details"] = list(details)[:20]
    if event_id:
        out["event_id"] = str(event_id)[:64]
    if debug and _debug_on():
        out["debug"] = _scrub(str(debug))[:300]
    return out


def send(h, status=500, code=None, message=None, rq=None,
         details=None, event_id=None, debug=None, request_id=None):
    """표준 에러 응답을 직접 기록한다. 응답 실패는 서비스에 전파하지 않는다."""
    rid = request_id or getattr(rq, "request_id", None)
    obj = payload(status=status, code=code, message=message, request_id=rid,
                  details=details, event_id=event_id, debug=debug)
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(obj["status"])
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        if rid:
            try:
                h.send_header("X-Request-Id", str(rid)[:64])
            except Exception:
                pass
        h.send_header("Access-Control-Allow-Origin", _allow_origin(h.headers))
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    except Exception:
        pass
    return obj


def classify(exc):
    """예외 -> (status, code). 내부 버그와 업스트림 장애를 구분한다."""
    if isinstance(exc, ValidationError):
        return (exc.status, exc.code)
    name = type(exc).__name__
    mod = getattr(type(exc), "__module__", "") or ""
    if name in ("HTTPError",):
        return (502, "UPSTREAM_ERROR")
    if name in ("URLError", "ConnectionError", "ConnectionResetError",
                "ConnectionRefusedError", "SSLError", "RemoteDisconnected"):
        return (502, "UPSTREAM_ERROR")
    if name in ("timeout", "TimeoutError", "socket.timeout") or "timeout" in name.lower():
        return (504, "UPSTREAM_TIMEOUT")
    if mod.startswith("socket") or mod.startswith("urllib") or mod.startswith("ssl"):
        return (502, "UPSTREAM_ERROR")
    return (500, "INTERNAL_ERROR")


def handle(h, exc, route="", method="", rq=None):
    """핸들러 except 절 한 줄 처리: 모니터링 전송 + 구조화 로그 + 표준 응답.

    반환: 전송한 봉투(dict). 어떤 단계가 실패해도 응답은 반드시 나간다.
    """
    status, code = classify(exc)
    rid = getattr(rq, "request_id", None)
    eid = None
    if status >= 500:  # 4xx(사용자 입력 오류)는 모니터링 노이즈이므로 보내지 않는다
        try:
            import monitoring
            eid = monitoring.capture_error(exc, route=route, method=method, request_id=rid)
        except Exception:
            eid = None
    try:
        if rq is not None:
            rq.fail(exc, status, event_id=eid)
    except Exception:
        pass
    details = getattr(exc, "details", None) if isinstance(exc, ValidationError) else None
    message = getattr(exc, "message", None) if isinstance(exc, ValidationError) else None
    return send(h, status=status, code=code, message=message, rq=rq,
                details=details, event_id=eid, debug=exc)


# --------------------------------------------------------------------------
# 입력검증 헬퍼
# --------------------------------------------------------------------------
def read_json(h, max_bytes=MAX_BODY, required=True):
    """요청 본문을 JSON 객체로 읽는다. 실패시 ValidationError.

    - Content-Length 부재/비정상 -> 400
    - 상한 초과 -> 413 (본문을 읽지 않고 즉시 거부)
    - JSON 파싱 실패 / 최상위가 객체가 아님 -> 400
    """
    raw = h.headers.get("content-length") or h.headers.get("Content-Length") or "0"
    try:
        n = int(str(raw).strip() or "0")
    except Exception:
        raise ValidationError.field("content-length", "정수가 아닙니다")
    if n < 0:
        raise ValidationError.field("content-length", "음수입니다")
    if n > int(max_bytes):
        raise ValidationError(
            details=[{"field": "body", "reason": "최대 %d바이트" % int(max_bytes)}],
            code="PAYLOAD_TOO_LARGE", status=413)
    if n == 0:
        if required:
            raise ValidationError.field("body", "본문이 비어 있습니다")
        return {}
    try:
        data = h.rfile.read(n)
    except Exception:
        raise ValidationError.field("body", "본문을 읽지 못했습니다")
    try:
        obj = json.loads(data or b"{}")
    except Exception:
        raise ValidationError.field("body", "JSON 형식이 아닙니다")
    if not isinstance(obj, dict):
        raise ValidationError.field("body", "JSON 객체여야 합니다")
    return obj


def as_str(body, field, default=None, required=False, max_len=1000,
           min_len=0, strip=True, allow_empty=True):
    v = body.get(field, None)
    if v is None:
        if required:
            raise ValidationError.field(field, "필수 항목입니다")
        return default
    if not isinstance(v, str):
        raise ValidationError.field(field, "문자열이어야 합니다")
    if strip:
        v = v.strip()
    if not v and not allow_empty:
        raise ValidationError.field(field, "비어 있을 수 없습니다")
    if len(v) < int(min_len):
        raise ValidationError.field(field, "최소 %d자" % int(min_len))
    if len(v) > int(max_len):
        raise ValidationError.field(field, "최대 %d자" % int(max_len))
    return v


def as_choice(body, field, choices, default=None, required=False):
    v = body.get(field, None)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValidationError.field(field, "필수 항목입니다")
        return default
    if not isinstance(v, str):
        raise ValidationError.field(field, "문자열이어야 합니다")
    v = v.strip()
    if v not in choices:
        raise ValidationError.field(field, "허용값: %s" % ", ".join(sorted(choices)))
    return v


def as_int(body, field, default=None, required=False, minimum=None, maximum=None):
    v = body.get(field, None)
    if v is None:
        if required:
            raise ValidationError.field(field, "필수 항목입니다")
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v).strip())
        except Exception:
            raise ValidationError.field(field, "정수여야 합니다")
    if minimum is not None and v < minimum:
        raise ValidationError.field(field, "최소 %s" % minimum)
    if maximum is not None and v > maximum:
        raise ValidationError.field(field, "최대 %s" % maximum)
    return v


def as_list(body, field, default=None, required=False, max_items=100, item_type=None):
    v = body.get(field, None)
    if v is None:
        if required:
            raise ValidationError.field(field, "필수 항목입니다")
        return [] if default is None else default
    if not isinstance(v, list):
        raise ValidationError.field(field, "배열이어야 합니다")
    if len(v) > int(max_items):
        raise ValidationError.field(field, "최대 %d개" % int(max_items))
    if item_type is not None:
        for i, it in enumerate(v):
            if not isinstance(it, item_type):
                raise ValidationError.field("%s[%d]" % (field, i), "형식이 올바르지 않습니다")
    return v


def query_choice(qs, key, choices, default=None, required=False):
    """parse_qs 결과에서 화이트리스트 값을 뽑는다."""
    vals = qs.get(key) or []
    v = (vals[0] if vals else "") or ""
    v = v.strip().lower()
    if not v:
        if required:
            raise ValidationError.field(key, "필수 항목입니다")
        return default
    if v not in choices:
        raise ValidationError.field(key, "허용값: %s" % ", ".join(sorted(choices)))
    return v


def query_str(qs, key, default="", max_len=1000, required=False, allow_empty=True):
    vals = qs.get(key) or []
    v = (vals[0] if vals else "") or ""
    v = v.strip()
    if not v:
        if required or not allow_empty:
            raise ValidationError.field(key, "필수 항목입니다")
        return default
    if len(v) > int(max_len):
        raise ValidationError.field(key, "최대 %d자" % int(max_len))
    return v
