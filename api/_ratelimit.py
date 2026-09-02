# -*- coding: utf-8 -*-
# ==========================================================================
# api/_ratelimit.py — 공개 API 요청 제한 (의존성 0)
# --------------------------------------------------------------------------
# 왜 필요한가
#   /api/* 는 무인증으로 공개돼 있고(오리진 검사만 함), 그 뒤에는 **호출당 과금**
#   되는 자원(LLM·STT·TTS)이 있다. 오리진 헤더는 브라우저 밖에서 위조할 수 있고
#   X-Forwarded-For 도 마찬가지다. 즉 "IP당 N회"만으로는 과금 상한이 서지 않는다.
#   그래서 두 겹으로 막는다.
#
#       1) 키(IP)별 한도  — 정상 사용자 1명의 오남용·버그 루프를 막는다
#       2) 전역 한도      — IP 를 바꿔가며 오는 분산 호출의 **총량**을 막는다
#                           (과금 상한선. 이 층이 없으면 1)은 우회된다)
#
# 비용이 다른 경로에 같은 한도를 주지 않는다. 경로를 등급으로 나눠 각각 한도를 둔다.
#
#   등급        경로                       기본 IP/분   기본 전역/분   근거
#   llm         /api/chat, /api/assist       20           240          LLM 토큰 과금
#   speech      /api/stt, /api/tts           12           120          음성 API 과금 + 대용량 본문
#   webhook     /api/voice                   120          1200         CPaaS 콜백 버스트 허용
#   default     그 외(/api/ops_stats 등)     40           600          조회성
#
# 환경변수 (모두 선택. 미설정이면 위 기본값)
#   CALLBOT_RATE_LIMIT            default 등급 IP/분 (기존 변수명 유지 — 하위호환)
#   CALLBOT_RATE_LIMIT_LLM        llm 등급 IP/분
#   CALLBOT_RATE_LIMIT_SPEECH     speech 등급 IP/분
#   CALLBOT_RATE_LIMIT_WEBHOOK    webhook 등급 IP/분
#   CALLBOT_RATE_LIMIT_GLOBAL_<등급>  등급별 전역/분 (예: ..._GLOBAL_LLM)
#   CALLBOT_RATE_LIMIT_GLOBAL_FACTOR  전역 기본값 = IP한도 x factor (기본 값은 표 참조)
#   CALLBOT_RATE_LIMIT_OFF=1      전체 비활성 (로컬 개발·테스트 전용)
#   CALLBOT_RATE_LIMIT_TRUST_XFF=0  X-Forwarded-For 를 신뢰하지 않음(모두 한 버킷)
#
# 한계 — 반드시 알고 쓸 것
#   서버리스(Vercel) 는 인스턴스가 여러 개 뜨고 수시로 재활용된다. 이 카운터는
#   **인스턴스 로컬 메모리**라서, 인스턴스가 N개면 실효 한도는 최대 N배가 된다.
#   따라서 이 층은 "정확한 쿼터"가 아니라 **폭주 차단·과금 상한의 1차 방어선**이다.
#   정확한 전역 쿼터가 필요해지면 아래 STORE 인터페이스에 Redis/Upstash 구현체를
#   끼운다(코드는 준비, 활성화는 승인 대상).
#
# 메모리 — 키 사전은 무한히 자라지 않는다. 만료분 정리 + 하드 상한(MAX_KEYS)
#   초과시 오래된 키부터 버린다. 카운터 유실은 요청 거부가 아니라 허용 쪽으로만
#   기운다(가용성 우선).
# ==========================================================================
import os
import sys
import time
import threading

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

WINDOW = 60.0          # 슬라이딩 윈도 길이(초)
MAX_KEYS = 5000        # 추적 키 상한 (메모리 방어)

# 등급 -> (IP당/분, 전역/분)
DEFAULTS = {
    "llm":     (20, 240),
    "speech":  (12, 120),
    "webhook": (120, 1200),
    "default": (40, 600),
}

# 경로 조각 -> 등급
ROUTE_CLASS = {
    "chat": "llm",
    "assist": "llm",
    "stt": "speech",
    "tts": "speech",
    "voice": "webhook",
}

_LOCK = threading.Lock()
_HITS = {}             # key -> [timestamp, ...]
_LAST = threading.local()


# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------
def _int_env(name, default):
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        n = int(v)
    except Exception:
        return default
    return n if n >= 0 else default


def disabled():
    return (os.environ.get("CALLBOT_RATE_LIMIT_OFF") or "").strip() in ("1", "true", "yes")


def _trust_xff():
    v = (os.environ.get("CALLBOT_RATE_LIMIT_TRUST_XFF") or "").strip().lower()
    return v not in ("0", "false", "no")


def route_class(path=""):
    """요청 경로에서 등급을 고른다. 알 수 없으면 default."""
    p = str(path or "").split("?", 1)[0].strip().lower().rstrip("/")
    seg = p.rsplit("/", 1)[-1]
    if seg.endswith(".py"):
        seg = seg[:-3]
    return ROUTE_CLASS.get(seg, "default")


def limits(cls):
    """(IP당/분, 전역/분). 환경변수 우선."""
    ip_def, gl_def = DEFAULTS.get(cls, DEFAULTS["default"])
    if cls == "default":
        ip = _int_env("CALLBOT_RATE_LIMIT", ip_def)
    else:
        ip = _int_env("CALLBOT_RATE_LIMIT_" + cls.upper(), ip_def)
    factor = _int_env("CALLBOT_RATE_LIMIT_GLOBAL_FACTOR", 0)
    gl_default = (ip * factor) if factor > 0 else gl_def
    gl = _int_env("CALLBOT_RATE_LIMIT_GLOBAL_" + cls.upper(), gl_default)
    return (ip, gl)


def client_ip(headers):
    """호출자 식별 키. XFF 는 위조 가능하므로 전역 한도가 함께 필요하다."""
    try:
        if _trust_xff():
            xf = (headers.get("x-forwarded-for") or "")
            if xf:
                first = xf.split(",")[0].strip()
                if first:
                    return first[:64]
            rip = (headers.get("x-real-ip") or "").strip()
            if rip:
                return rip[:64]
    except Exception:
        pass
    return "unknown"


# --------------------------------------------------------------------------
# 카운터 (슬라이딩 윈도)
# --------------------------------------------------------------------------
def _prune(now):
    """만료 항목 제거 + 하드 상한. _LOCK 보유 상태에서 호출."""
    dead = [k for k, q in _HITS.items() if not q or (now - q[-1]) >= WINDOW]
    for k in dead:
        _HITS.pop(k, None)
    if len(_HITS) > MAX_KEYS:
        # 마지막 접촉이 오래된 키부터 버린다 (가용성 우선: 카운터 유실은 허용 쪽)
        for k, _ts in sorted(_HITS.items(), key=lambda kv: kv[1][-1])[:len(_HITS) - MAX_KEYS]:
            _HITS.pop(k, None)


def _take(key, limit, now):
    """(허용?, 남은 횟수, 재시도까지 초). limit<=0 이면 무제한."""
    if limit <= 0:
        return (True, -1, 0)
    q = [t for t in _HITS.get(key, ()) if now - t < WINDOW]
    if len(q) >= limit:
        _HITS[key] = q
        retry = max(1, int(WINDOW - (now - q[0])) + 1)
        return (False, 0, retry)
    q.append(now)
    _HITS[key] = q
    return (True, max(0, limit - len(q)), 0)


class Decision(object):
    """요청 제한 판정 결과. 응답 헤더 재료를 담는다."""

    __slots__ = ("allowed", "cls", "limit", "remaining", "retry_after", "scope")

    def __init__(self, allowed=True, cls="default", limit=0, remaining=-1,
                 retry_after=0, scope=""):
        self.allowed = bool(allowed)
        self.cls = cls
        self.limit = int(limit)
        self.remaining = int(remaining)
        self.retry_after = int(retry_after)
        self.scope = scope  # "ip" | "global" | ""

    def headers(self):
        """RateLimit 관련 응답 헤더 목록 [(name, value), ...]."""
        out = []
        if self.limit > 0:
            out.append(("X-RateLimit-Limit", str(self.limit)))
            if self.remaining >= 0:
                out.append(("X-RateLimit-Remaining", str(self.remaining)))
        if not self.allowed and self.retry_after > 0:
            out.append(("Retry-After", str(self.retry_after)))
            out.append(("X-RateLimit-Reset", str(self.retry_after)))
        return out


def check(headers, path="", key=None):
    """요청 1건을 계상하고 판정한다. 부작용: 카운터 증가.

    허용이면 Decision.allowed=True. 거부 사유는 scope 로 구분한다
    ("ip" = 호출자 과다, "global" = 서비스 전체 과부하/과금 상한).
    """
    cls = route_class(path)
    if disabled():
        d = Decision(True, cls, 0, -1, 0, "")
        _remember(d)
        return d
    ip_limit, gl_limit = limits(cls)
    k = key or client_ip(headers)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        # 전역 먼저 본다: 전역이 막혔는데 IP 카운터를 올리면
        # 정상 사용자가 이중으로 벌점을 받는다.
        g_ok, _g_rem, g_retry = _take("@global:" + cls, gl_limit, now)
        if not g_ok:
            return _remember(Decision(False, cls, gl_limit, 0, g_retry, "global"))
        ok, rem, retry = _take(cls + ":" + k, ip_limit, now)
        if not ok:
            return _remember(Decision(False, cls, ip_limit, 0, retry, "ip"))
        return _remember(Decision(True, cls, ip_limit, rem, 0, ""))


def _remember(d):
    try:
        _LAST.value = d
    except Exception:
        pass
    return d


def current():
    """이 요청(스레드)의 마지막 판정. 없으면 None."""
    return getattr(_LAST, "value", None)


def reset():
    """테스트·진단용 카운터 초기화."""
    with _LOCK:
        _HITS.clear()
    try:
        _LAST.value = None
    except Exception:
        pass


def snapshot():
    """운영 점검용 요약 (PII 없음: 키는 개수만)."""
    now = time.monotonic()
    with _LOCK:
        live = {k: len([t for t in q if now - t < WINDOW]) for k, q in _HITS.items()}
    return {
        "window_sec": int(WINDOW),
        "enabled": (not disabled()),
        "tracked_keys": len(live),
        "global": {k[len("@global:"):]: v for k, v in live.items() if k.startswith("@global:")},
        "classes": {c: {"ip_per_min": limits(c)[0], "global_per_min": limits(c)[1]}
                    for c in DEFAULTS},
        "note": "instance-local counters; not a cluster-wide quota",
    }
