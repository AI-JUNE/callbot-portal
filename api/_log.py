# -*- coding: utf-8 -*-
# ==========================================================================
# api/_log.py — 구조화 로깅 (JSON 1줄/요청). 의존성 0.
# --------------------------------------------------------------------------
# 목적: 요청 ID·소요시간·에러코드를 기계가 읽을 수 있는 형태로 남겨,
#       Vercel 로그에서 특정 요청을 추적하고 지연·오류를 집계할 수 있게 한다.
#
# 설계 원칙
#  - **PII 미기록**: 요청 본문·쿼리값·헤더값을 로그에 담지 않는다. 경로는
#    쿼리스트링을 잘라내고, 남는 문자열은 monitoring.scrub() 로 한 번 더 거른다.
#  - **요청 ID 전파**: 인바운드 x-request-id(또는 Vercel x-vercel-id)를 이어받고,
#    없으면 새로 만든다. 응답 헤더 X-Request-Id 로 되돌려준다.
#  - **에러코드 안정성**: 예외 타입명을 상위 스네이크 코드로 정규화해
#    (예: ValueError -> VALUE_ERROR) 메시지 문구가 바뀌어도 집계가 깨지지 않는다.
#  - **로깅 실패가 서비스에 영향 없음**: 모든 예외 흡수.
#  - 모니터링(api/monitoring.py)과 같은 request_id 를 쓰므로 로그 <-> 이벤트 상호 추적 가능.
#
# 사용 예 (API 핸들러)
#     import _log
#     def do_POST(self):
#         with _log.request(self, "/api/chat", "POST") as rq:
#             ...                      # 정상 처리
#             rq.done(200)             # 상태코드 확정
#
#   또는 수동:
#     rq = _log.begin(self.headers, "/api/chat", "POST")
#     ... ; rq.finish(200)  /  rq.fail(e)
# ==========================================================================
import os
import re
import sys
import json
import time
import uuid

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

try:
    from monitoring import scrub as _scrub
except Exception:  # 모니터링 모듈이 없어도 로깅은 동작해야 한다
    def _scrub(v):
        return v if isinstance(v, str) else str(v)

_RX_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_RX_WORD = re.compile(r"([a-z0-9])([A-Z])")

SERVICE = "callbot-portal"
MAX_FIELD = 200
# 로그 레벨: CALLBOT_LOG=off 면 완전 침묵(로컬·테스트용)
_OFF = ("off", "none", "0", "false")


def enabled():
    return (os.environ.get("CALLBOT_LOG") or "").strip().lower() not in _OFF


def _env():
    return (os.environ.get("VERCEL_ENV")
            or os.environ.get("CALLBOT_ENV")
            or "development").strip()


def _release():
    return (os.environ.get("VERCEL_GIT_COMMIT_SHA")
            or os.environ.get("CALLBOT_BUILD")
            or "dev").strip()


def new_request_id(headers=None):
    """인바운드 요청 ID를 이어받거나 새로 생성. 항상 안전한 짧은 문자열."""
    try:
        if headers is not None:
            for h in ("x-request-id", "x-vercel-id", "x-amzn-trace-id"):
                v = (headers.get(h) or "").strip()
                if v:
                    # 값 그대로 신뢰하지 않는다 — 길이 제한 + 안전문자만
                    safe = "".join(c for c in v if c.isalnum() or c in "-_:.")[:64]
                    if safe:
                        return safe
    except Exception:
        pass
    return uuid.uuid4().hex[:16]


def safe_path(path):
    """쿼리스트링 제거 — 쿼리에 개인정보가 실려도 로그에 남지 않게 한다."""
    try:
        p = str(path or "")
        for sep in ("?", "#"):
            i = p.find(sep)
            if i >= 0:
                p = p[:i]
        return _scrub(p)[:MAX_FIELD]
    except Exception:
        return "-"


def error_code(exc):
    """예외 -> 안정적인 에러코드 문자열. 메시지는 포함하지 않는다."""
    try:
        name = type(exc).__name__ if isinstance(exc, BaseException) else str(exc)
        # CamelCase -> SNAKE_CASE. 연속 대문자(약어) 경계도 처리: OSError -> OS_ERROR
        s1 = _RX_ACRONYM.sub(r"\1_\2", name)
        s2 = _RX_WORD.sub(r"\1_\2", s1)
        return (s2.upper() or "ERROR")[:64]
    except Exception:
        return "ERROR"


def emit(record):
    """JSON 1줄 출력. 실패해도 절대 예외를 던지지 않는다."""
    try:
        if not enabled():
            return
        sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class Request(object):
    """요청 1건의 수명주기. finish()/fail() 는 최초 1회만 기록된다."""

    def __init__(self, route, method, request_id, path=None):
        self.route = route
        self.method = (method or "-").upper()
        self.request_id = request_id
        self.path = safe_path(path if path is not None else route)
        self.t0 = time.time()
        self.done_flag = False
        self.extra = {}

    def duration_ms(self):
        return int((time.time() - self.t0) * 1000)

    def set(self, **kv):
        """PII가 아닌 보조 필드만 담는다(건수·플래그·모드 등). 문자열은 마스킹."""
        try:
            for k, v in kv.items():
                if v is None:
                    continue
                self.extra[str(k)[:40]] = _scrub(v)[:MAX_FIELD] if isinstance(v, str) else v
        except Exception:
            pass
        return self

    def _record(self, level, status, code=None):
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "service": SERVICE,
            "env": _env(),
            "release": _release(),
            "request_id": self.request_id,
            "route": self.route,
            "method": self.method,
            "path": self.path,
            "status": status,
            "duration_ms": self.duration_ms(),
        }
        if code:
            rec["error_code"] = code
        if self.extra:
            rec["extra"] = self.extra
        return rec

    def finish(self, status=200, **kv):
        if self.done_flag:
            return self
        self.done_flag = True
        self.set(**kv)
        level = "warn" if 400 <= int(status) < 500 else ("error" if int(status) >= 500 else "info")
        emit(self._record(level, int(status)))
        return self

    def fail(self, exc, status=500, **kv):
        """오류 종료 — 예외 '메시지'는 기록하지 않는다(PII 유입 차단). 코드만 남긴다."""
        if self.done_flag:
            return self
        self.done_flag = True
        self.set(**kv)
        emit(self._record("error", int(status), error_code(exc)))
        return self

    # with 블록 지원
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None:
            self.fail(ev)
        elif not self.done_flag:
            self.finish(200)
        return False  # 재전파


def begin(headers=None, route="-", method="-", path=None):
    """요청 로깅 시작. 예외를 던지지 않는다."""
    try:
        rid = new_request_id(headers)
    except Exception:
        rid = uuid.uuid4().hex[:16]
    return Request(route, method, rid, path)


def suppress_access_log(self, fmt, *args):
    """BaseHTTPRequestHandler.log_message 대체.

    기본 구현은 요청라인을 그대로 stderr 에 찍는데, 여기에 **쿼리스트링이 포함**돼
    `?phone=010-...` 같은 개인정보가 로그로 새어 나간다. 구조화 로그(emit)가
    같은 정보를 PII 없이 남기므로 기본 접근로그는 침묵시킨다.

    핸들러 클래스에 `log_message = _log.suppress_access_log` 로 배선한다.
    """
    return


def attach(handler, rq):
    """응답 헤더 X-Request-Id 부착 — send_header 가능한 시점에 호출."""
    try:
        handler.send_header("X-Request-Id", rq.request_id)
    except Exception:
        pass
    return rq
