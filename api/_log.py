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
#  - **코드 두 칸을 구분한다**(2026-10-10): 한 요청에는 성질이 다른 '코드'가 둘 있다.
#      error_code : 예외 타입명 유래. **원인**별 집계용(5xx 분류는 전부
#                   INTERNAL_ERROR 라 타입명이 없으면 무엇이 터졌는지 알 수 없다).
#      code       : 응답 봉투(_errors)가 클라이언트에 내려준 값과 **같은 문자열**.
#                   사용자가 신고하는 것은 이쪽이다.
#    예전에는 로그에 error_code 만 있었고 그 값이 봉투의 code 와 달랐다
#    (봉투 INVALID_REQUEST ↔ 로그 VALUE_ERROR/VALIDATION_ERROR). 신고받은 코드로
#    로그를 찾으면 아무것도 안 나오고, **거부(401/403/429)·404·413 은 _errors.send
#    로 끝나므로 로그에 코드가 한 칸도 없었다** — 거부 사유별 집계가 불가능했다.
#    이제 두 칸을 함께 남긴다(같은 값이면 code 는 생략하지 않는다 — 집계 쿼리가
#    필드 유무로 갈리면 안 된다).
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
    from _monitoring import scrub as _scrub
except Exception:  # 모니터링 모듈이 없어도 로깅은 동작해야 한다
    def _scrub(v):
        return v if isinstance(v, str) else str(v)

_RX_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_RX_WORD = re.compile(r"([a-z0-9])([A-Z])")

SERVICE = "callbot-portal"
MAX_FIELD = 200
MAX_KEY = 24          # 보조 필드 이름 상한(규약: snake_case · 24자)

# --------------------------------------------------------------------------
# 보조 필드(extra) 등록부 — `rq.set(k=v)` / `rq.finish(code, k=v)` 로 붙는 칸.
#
# 2026-10-11(26차): 칸 이름이 라우트마다 제각각이었다(`lines`·`turns`·`msg_count`
# 가 모두 '건수'). 횡단 집계("어떤 라우트가 어떤 입력에서 걸리는가")를 하려면
# 같은 뜻에 같은 이름이어야 하고, 무엇보다 **임의 문자열 칸이 늘어나는 것**이
# 위험하다 — 집계 카디널리티를 터뜨리고 PII 유입 경로가 된다(18차가 `op`·`ev`
# 값을 화이트리스트로 접은 것과 같은 이유). 그래서 규약을 적고 게이트로 센다.
#
# 규약
#  1) snake_case ASCII, 24자(MAX_KEY) 이내.
#  2) 분류(차원) 칸에는 **라우트가 화이트리스트로 접은 라벨**만 담는다.
#     외부가 보낸 문자열을 그대로 싣지 않는다(모르는 값은 `other` 로 접는다).
#  3) 불리언은 사실 서술형(`denied`·`delivered`·`recorded`…). `is_` 접두 금지.
#  4) 수량은 `_count`, 문자 길이는 `_len`(원문은 싣지 않는다).
#  5) 새 칸은 여기에 사유와 함께 적는다 — 릴리스 게이트(`scripts/verify.py`
#     log_fields)가 등록부 밖의 칸을 **실패시킨다**.
# --------------------------------------------------------------------------
FIELDS = {
    # 차원(라벨) — 라우트가 화이트리스트로 접은 값만
    "op": "요청한 동작(라우트별 화이트리스트 라벨, 미지는 other)",
    "ev": "통화 이벤트 종류(voice · 화이트리스트 라벨)",
    "kind": "응답 형식(voice: voiceml)",
    "mode": "점검 깊이(health: shallow|deep)",
    "period": "집계 기간(ops_stats: today|week|month)",
    "scenario": "대화 시나리오(chat·sim_call · 화이트리스트)",
    "task": "assist 작업 종류(화이트리스트)",
    "risk": "안부 판정 위험도(wellbeing: low|mid|high|unknown)",
    "health": "헬스 등급(healthy|degraded|unhealthy)",
    # 사실(불리언)
    "denied": "접근 가드가 거부했다(_guard.deny 가 단다)",
    "delivered": "안부 결과 웹훅이 검증된 호스트에 실제로 들어갔다",
    "demo": "응답 수치가 데모 기준선이다(실측 아님)",
    "dry_run": "저장 없이 검사만 했다",
    "transferred": "상담사 전환으로 끝났다",
    "tenant_set": "테넌트가 지정됐다(식별자 값은 싣지 않는다)",
    # 수량·길이
    "msg_count": "대화 이력 건수",
    "line_count": "정산 명세 줄 수",
    "turn_count": "sim 통화 턴 수",
    "text_len": "입력 문자 길이(원문은 싣지 않는다)",
    # 추적
    "event_id": "모니터링 이벤트 ID(로그 <-> Sentry 상호 추적)",
}
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


def _value(v):
    """보조 필드 값 1개를 안전한 스칼라로 — 타입 보존(bool/int/float) + 마스킹·상한."""
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
        return v
    if not isinstance(v, str):
        try:
            v = str(v)
        except Exception:
            return "<unprintable>"
    return _scrub(v)[:MAX_FIELD]


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


def level_for(status):
    """상태코드 -> 로그 레벨. 2xx/3xx=info · 4xx=warn · 5xx=error.

    4xx 는 '요청한 쪽의 잘못'이라 서비스 장애가 아니다. 알림 규칙이 level 로
    걸리므로 이 구분이 흐려지면 노이즈에 묻혀 5xx 를 놓친다.
    """
    try:
        s = int(status)
    except Exception:
        return "error"
    if s >= 500:
        return "error"
    if s >= 400:
        return "warn"
    return "info"


def emit(record):
    """JSON 1줄 출력. 실패해도 절대 예외를 던지지 않는다.

    직렬화가 실패하면 **보조 필드를 떼고 한 번 더** 시도한다 — 요청 1건=1줄은
    장애 조사의 바닥이고(릴리스 게이트 request_log 가 지키는 불변식),
    extra 한 칸 때문에 상태코드·추적키까지 잃을 이유가 없다.
    """
    try:
        if not enabled():
            return
    except Exception:
        return
    try:
        sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
        return
    except Exception:
        pass
    try:
        slim = {k: v for k, v in record.items() if k != "extra"}
        slim["extra_error"] = True       # 보조 필드를 떼고 남겼다는 표시
        sys.stdout.write(json.dumps(slim, ensure_ascii=False, sort_keys=True) + "\n")
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
        """PII가 아닌 보조 필드만 담는다(건수·플래그·라벨 등). 문자열은 마스킹.

        스칼라만 남긴다 — bool/int/float 은 타입을 지키고 그 밖(dict·list·객체)은
        문자열로 접어 길이를 자른다(`_audit._fields` 와 같은 규약). 예전에는 비스칼라가
        그대로 들어가 **직렬화 실패 시 그 요청의 로그 한 줄이 통째로 사라졌다** —
        보조 필드 하나 때문에 요청 기록을 잃는 것은 교환비가 맞지 않는다.
        """
        try:
            for k, v in kv.items():
                if v is None:
                    continue
                self.extra[str(k)[:MAX_KEY]] = _value(v)
        except Exception:
            pass
        return self

    def _record(self, level, status, error_code=None, api_code=None):
        """로그 레코드 1건.

        error_code : 예외 타입명 유래(원인 집계) · api_code : 응답 봉투의 `code`
        (사용자가 신고하는 값). 둘은 같은 요청에서 서로 다른 값일 수 있으므로
        한 칸에 겹쳐 쓰지 않는다 — 겹쳐 쓰면 한쪽 집계가 반드시 틀어진다.
        """
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
        if error_code:
            rec["error_code"] = error_code
        if api_code:
            rec["code"] = str(api_code)[:64]
        if self.extra:
            rec["extra"] = self.extra
        return rec

    def finish(self, status=200, code=None, **kv):
        """정상 종료. `code` 는 응답 봉투의 `code`(있을 때만 — 2xx 는 없다).

        거부·404·413 처럼 예외 없이 표준 봉투로 끝나는 요청은 이 경로로 닫히므로,
        `code` 를 받지 않으면 그 요청의 로그에는 코드가 한 칸도 남지 않는다.
        """
        if self.done_flag:
            return self
        self.done_flag = True
        self.set(**kv)
        emit(self._record(level_for(status), int(status), None, code))
        return self

    def fail(self, exc, status=500, code=None, **kv):
        """오류 종료 — 예외 '메시지'는 기록하지 않는다(PII 유입 차단). 코드만 남긴다.

        레벨은 finish() 와 같은 규칙으로 상태코드에서 뽑는다. 예전에는 무조건
        `error` 였는데, `_errors.handle` 이 입력검증 실패(400·413)까지 이 경로로
        보내기 때문에 **사용자 오타가 서비스 장애와 같은 레벨**로 쌓였다 —
        level=error 로 거는 알림이 그만큼 울리면 진짜 5xx 가 묻힌다.
        error_code 는 상태와 무관하게 남긴다(4xx 도 어느 검증에서 걸렸는지 집계).
        `code` 를 함께 받으면 응답 봉투가 내려준 코드도 나란히 남는다 — 5xx 분류는
        모두 INTERNAL_ERROR 라서 예외 타입명을 지우면 원인을 잃고, 반대로 봉투
        코드가 없으면 사용자가 신고한 값으로 이 줄을 찾을 수 없다.
        """
        if self.done_flag:
            return self
        self.done_flag = True
        self.set(**kv)
        emit(self._record(level_for(status), int(status), error_code(exc), code))
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
