# -*- coding: utf-8 -*-
# ==========================================================================
# api/_audit.py — 관리 기능 접근·감사 로그 (Access & Audit Log). 의존성 0.
# --------------------------------------------------------------------------
# 목적(상용 필수 §접근·감사 로그): "누가 · 언제 · 어떤 관리 기능에 · 어떤 자격으로
#       접근했고 · 허용/거부됐는지"를 사후 추적 가능한 형태로 남긴다.
#       분쟁·침해사고 조사에서 요구되는 최소 기록이며, 일반 요청 로그(_log.py)와
#       달리 **관리 기능 접근만** 골라 별도 스트림(kind=audit)으로 남긴다.
#
# 설계 원칙
#  - **PII·비밀값 미기록**: 원문 IP·API 키·웹훅 토큰·쿼리값·본문은 절대 담지 않는다.
#      IP 는 솔트 적용 해시(앞 12자)만 남겨 "같은 호출자인지"만 판별 가능하게 한다.
#      API 키는 값 대신 지문(sha256 앞 8자)만 남긴다.
#  - **거부도 남긴다**: 실패한 접근 시도(403/401/429)가 오히려 감사 가치가 높다.
#  - **append-only**: 기록 후 수정·삭제 API 를 두지 않는다. 버퍼는 오래된 것부터
#      밀려날 뿐(evicted 카운터로 유실을 드러냄) 임의 삭제가 불가능하다.
#  - **감사 실패가 서비스 장애가 되지 않는다**: 모든 예외 흡수(가용성 우선).
#  - **읽기 경로는 아직 열지 않는다**: recent() 는 프로세스 내부용. HTTP 조회
#      엔드포인트는 관리자 인증 체계 확정 후 배선한다 **[승인 필요]**.
#
# 한계(정직하게 드러냄)
#  - 서버리스 인스턴스 메모리 버퍼라 재기동 시 휘발한다. 영속 보관은 stdout
#    JSON 라인을 플랫폼 로그 보존소가 수집하는 것에 의존한다. 변조 방지(WORM)
#    저장소 연동은 **[승인 필요]**.
#
# 사용 예
#     import _audit
#     _audit.record(self.headers, "ops.stats.read", "allow", status=200,
#                   request_id=rq.request_id, period=period)
# ==========================================================================
import os
import sys
import time
import json
import hashlib
import threading

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

try:
    from _log import emit as _emit, safe_path as _safe_path
except Exception:  # 로깅 모듈이 없어도 감사 기록은 동작해야 한다
    def _emit(rec):
        try:
            sys.stdout.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    def _safe_path(p):
        p = str(p or "")
        for sep in ("?", "#"):
            i = p.find(sep)
            if i >= 0:
                p = p[:i]
        return p[:200]

SERVICE = "callbot-portal"
KIND = "audit"
MAX_FIELD = 200
DEFAULT_BUFFER = 200
BUFFER_HARD_MAX = 2000          # 메모리 방어 상한

RESULTS = ("allow", "deny", "error")

# 관리 기능으로 간주하는 경로 → 감사 액션명.
# 여기에 없는 경로는 일반 트래픽으로 보고 감사 스트림에 남기지 않는다
# (감사 로그가 일반 로그에 묻히면 조사 가치가 떨어진다).
ADMIN_ACTIONS = {
    "/api/ops_stats": "ops.stats.read",       # 운영 대시보드 지표 조회
    "/api/health": "ops.health.deep",         # deep 점검(네트워크 도달성)만 기록
    "/api/voice": "call.webhook",             # 통화 웹훅(외부 시스템 연동 지점)
}

_LOCK = threading.Lock()
_BUF = []
_COUNT = {"total": 0, "allow": 0, "deny": 0, "error": 0, "evicted": 0}
_SALT = None


# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------
def enabled():
    """CALLBOT_AUDIT=off 로 끌 수 있다(로컬·테스트용). 기본 on."""
    return (os.environ.get("CALLBOT_AUDIT") or "").strip().lower() not in ("off", "none", "0", "false")


def buffer_size():
    try:
        n = int((os.environ.get("CALLBOT_AUDIT_BUFFER") or "").strip() or DEFAULT_BUFFER)
    except Exception:
        n = DEFAULT_BUFFER
    return max(1, min(n, BUFFER_HARD_MAX))


def _salt():
    """IP 해시용 솔트. 환경변수 미설정 시 프로세스 임의값.

    임의 솔트면 인스턴스 간 해시가 달라 상관분석이 제한되지만, 원문 IP 를 남기지
    않는다는 원칙이 우선이다. 장기 상관분석이 필요하면 CALLBOT_AUDIT_SALT 를
    고정값으로 등록한다 [승인 필요: 사람이 등록].
    """
    global _SALT
    env = (os.environ.get("CALLBOT_AUDIT_SALT") or "").strip()
    if env:
        return env
    if _SALT is None:
        _SALT = hashlib.sha256(os.urandom(16)).hexdigest()
    return _SALT


def _h(value, n=12):
    try:
        return hashlib.sha256((_salt() + "|" + str(value)).encode("utf-8")).hexdigest()[:n]
    except Exception:
        return "-"


def _env_name():
    return (os.environ.get("VERCEL_ENV") or os.environ.get("CALLBOT_ENV") or "development").strip()


def _release():
    return (os.environ.get("VERCEL_GIT_COMMIT_SHA") or os.environ.get("CALLBOT_BUILD") or "dev").strip()


# --------------------------------------------------------------------------
# 호출자 식별 — 값이 아니라 "자격의 종류와 지문"만 남긴다
# --------------------------------------------------------------------------
def _get(headers, name):
    """헤더 1개 읽기. 실패는 삼키지 않는다 — 호출부가 '식별 실패'와 '값 없음'을
    구분할 수 있어야 감사 기록이 사실과 어긋나지 않는다(anonymous 로 단정 금지)."""
    return (headers.get(name) or "").strip()


def actor(headers):
    """{type, id} — 어떤 자격으로 통과했는지.

    api_key  : X-API-Key 가 설정값과 일치 (id=키 지문 8자, 값 미노출)
    webhook  : 웹훅 토큰 헤더 제시 (일치 여부는 _guard 판정 결과가 말해준다)
    origin   : 브라우저 동일출처/허용 오리진 (id=오리진 호스트, 비밀 아님)
    anonymous: 위 어느 것도 아님
    """
    try:
        key = (os.environ.get("CALLBOT_API_KEY") or "").strip()
        sent = _get(headers, "x-api-key")
        if key and sent and sent == key:
            return {"type": "api_key", "id": _h(sent, 8)}
        if sent:
            return {"type": "api_key_invalid", "id": _h(sent, 8)}
        if _get(headers, "x-webhook-token"):
            return {"type": "webhook", "id": "-"}
        o = _get(headers, "origin")
        if o:
            return {"type": "origin", "id": o[:100]}
        if (_get(headers, "sec-fetch-site").lower() == "same-origin"):
            return {"type": "origin", "id": "same-origin"}
        return {"type": "anonymous", "id": "-"}
    except Exception:
        return {"type": "unknown", "id": "-"}


def client_hash(headers):
    """호출자 IP 의 솔트 해시. 원문 IP 는 어디에도 남기지 않는다."""
    try:
        xf = _get(headers, "x-forwarded-for")
        ip = xf.split(",")[0].strip() if xf else (_get(headers, "x-real-ip") or "unknown")
        return _h(ip)
    except Exception:
        return "-"


def ua_family(headers):
    """User-Agent 를 대분류로만 축약(원문 미기록 — 핑거프린팅 방지)."""
    try:
        ua = _get(headers, "user-agent").lower()
    except Exception:
        return "unknown"
    if not ua:
        return "none"
    for k, v in (("bot", "bot"), ("curl", "cli"), ("wget", "cli"), ("python", "cli"),
                 ("postman", "cli"), ("edg/", "browser"), ("chrome", "browser"),
                 ("safari", "browser"), ("firefox", "browser")):
        if k in ua:
            return v
    return "other"


def action_for(path):
    """경로 → 감사 액션명. 관리 기능이 아니면 None."""
    return ADMIN_ACTIONS.get(_safe_path(path))


# --------------------------------------------------------------------------
# 기록
# --------------------------------------------------------------------------
def _sanitize(v):
    """보조 필드 정리 — 문자열은 길이 제한, 그 외 스칼라만 통과."""
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
        return v
    if isinstance(v, str):
        return v[:MAX_FIELD]
    return str(v)[:MAX_FIELD]


def record(headers, action, result="allow", status=200, request_id=None,
           target=None, method=None, path=None, **extra):
    """감사 이벤트 1건 기록. 예외를 던지지 않는다.

    반환: 기록된 dict(테스트·호출부 확인용). 비활성 시 None.
    """
    try:
        if not enabled():
            return None
        res = result if result in RESULTS else "error"
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": KIND,
            "service": SERVICE,
            "env": _env_name(),
            "release": _release(),
            "action": str(action or "-")[:64],
            "result": res,
            "status": int(status or 0),
            "actor": actor(headers),
            "client": client_hash(headers),
            "ua": ua_family(headers),
        }
        if request_id:
            rec["request_id"] = str(request_id)[:64]
        if target:
            rec["target"] = _sanitize(target)
        if method:
            rec["method"] = str(method).upper()[:8]
        if path:
            rec["path"] = _safe_path(path)
        if extra:
            rec["extra"] = {str(k)[:40]: _sanitize(v) for k, v in extra.items() if v is not None}

        _emit(rec)                       # 영속 보관은 플랫폼 로그 수집에 의존
        _store(rec)                      # 프로세스 내 최근 이력(운영 점검용)
        return rec
    except Exception:
        return None


def _store(rec):
    try:
        cap = buffer_size()
        with _LOCK:
            _BUF.append(rec)
            _COUNT["total"] += 1
            _COUNT[rec.get("result", "error")] = _COUNT.get(rec.get("result", "error"), 0) + 1
            while len(_BUF) > cap:
                _BUF.pop(0)
                _COUNT["evicted"] += 1
    except Exception:
        pass


def record_request(headers, path, method, result, status, request_id=None, **extra):
    """관리 경로일 때만 기록하는 편의 진입점. 일반 경로면 None."""
    act = action_for(path)
    if not act:
        return None
    return record(headers, act, result=result, status=status, request_id=request_id,
                  method=method, path=path, **extra)


# --------------------------------------------------------------------------
# 조회 — 프로세스 내부용(HTTP 노출은 관리자 인증 확정 후) [승인 필요]
# --------------------------------------------------------------------------
def recent(limit=50):
    try:
        n = max(1, min(int(limit or 50), BUFFER_HARD_MAX))
        with _LOCK:
            return [dict(r) for r in _BUF[-n:]]
    except Exception:
        return []


def counters():
    with _LOCK:
        return dict(_COUNT)


def snapshot():
    """운영 점검용 요약 — 개별 이벤트·식별자는 담지 않는다(개수만)."""
    try:
        c = counters()
        with _LOCK:
            buffered = len(_BUF)
            last = _BUF[-1]["ts"] if _BUF else None
        return {
            "enabled": enabled(),
            "buffered": buffered,
            "capacity": buffer_size(),
            "counts": c,
            "last_ts": last,
            "salt_fixed": bool((os.environ.get("CALLBOT_AUDIT_SALT") or "").strip()),
            "actions": sorted(set(ADMIN_ACTIONS.values())),
            "sink": "stdout(json) + in-process ring buffer",
            "note": "in-process buffer is volatile; WORM/persistent audit store not wired",
        }
    except Exception as e:
        return {"enabled": False, "note": "audit unavailable: %s" % type(e).__name__}


def reset():
    """테스트 전용 — 버퍼·카운터 초기화."""
    with _LOCK:
        del _BUF[:]
        for k in _COUNT:
            _COUNT[k] = 0


if __name__ == "__main__":
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    os.environ["CALLBOT_LOG"] = "off"     # 셀프테스트 중 stdout 소음 제거
    reset()
    h = H({"x-forwarded-for": "203.0.113.9, 10.0.0.1", "origin": "https://callbot-portal.vercel.app",
           "user-agent": "Mozilla/5.0 Chrome/120"})
    r = record(h, "ops.stats.read", "allow", status=200, request_id="rq1", period="today")
    assert r["action"] == "ops.stats.read" and r["result"] == "allow"
    assert r["actor"]["type"] == "origin" and r["ua"] == "browser"
    assert "203.0.113.9" not in json.dumps(r, ensure_ascii=False)      # 원문 IP 미기록
    assert r["extra"]["period"] == "today"
    d = record(H({}), "ops.stats.read", "deny", status=403)
    assert d["actor"]["type"] == "anonymous" and d["result"] == "deny"
    assert counters()["allow"] == 1 and counters()["deny"] == 1 and counters()["total"] == 2
    assert action_for("/api/ops_stats?period=week") == "ops.stats.read"
    assert action_for("/api/chat") is None
    assert record_request(h, "/api/chat", "POST", "allow", 200) is None
    assert record_request(h, "/api/ops_stats", "GET", "allow", 200)["method"] == "GET"
    # 키 지문만 남고 원문 키는 남지 않는다
    os.environ["CALLBOT_API_KEY"] = "supersecret"
    k = record(H({"x-api-key": "supersecret"}), "ops.stats.read", "allow", status=200)
    assert k["actor"]["type"] == "api_key" and "supersecret" not in json.dumps(k)
    bad = record(H({"x-api-key": "wrong"}), "ops.stats.read", "deny", status=403)
    assert bad["actor"]["type"] == "api_key_invalid"
    os.environ.pop("CALLBOT_API_KEY", None)
    # 버퍼 상한 · evicted
    os.environ["CALLBOT_AUDIT_BUFFER"] = "3"
    reset()
    for i in range(5):
        record(h, "ops.stats.read", "allow", status=200, request_id="r%d" % i)
    assert len(recent(100)) == 3 and counters()["evicted"] == 2
    os.environ.pop("CALLBOT_AUDIT_BUFFER", None)
    s = snapshot()
    assert s["enabled"] and s["capacity"] == DEFAULT_BUFFER and "ops.stats.read" in s["actions"]
    json.dumps(s, ensure_ascii=False)
    # 비활성화
    os.environ["CALLBOT_AUDIT"] = "off"
    assert record(h, "x", "allow") is None and snapshot()["enabled"] is False
    os.environ.pop("CALLBOT_AUDIT", None)
    reset()
    print("audit selftest OK")
