# -*- coding: utf-8 -*-
# ==========================================================================
# /api/health — 무인증 헬스체크 엔드포인트
# --------------------------------------------------------------------------
# 목적: 업타임 모니터/로드밸런서/배포 파이프라인이 서비스 생존 여부와 **의존성
#       상태·배포 버전**을 확인할 수 있도록 표준 헬스체크를 제공한다.
#       외부 모니터는 Origin/Referer 헤더 없이 호출하므로 _guard.check(오리진
#       검사)를 적용하지 않는다.
#
# 원칙(상용 필수 §/health 확장)
#   1) 민감정보 미노출 — 키·토큰·DSN·호스트 자격증명은 값이 아니라 "설정 여부"만.
#      커밋 해시/브랜치/배포 환경은 공개 저장소 식별자이므로 노출 허용.
#   2) 과금·부작용 없음 — LLM·CPaaS·STT/TTS 실호출을 하지 않는다. 기본은 순수
#      설정 점검(shallow)이며, 네트워크 도달성 점검(deep)은 HEALTH_DEEP=1 +
#      ?deep=1 두 조건이 모두 충족될 때만, 자격증명 없이 TCP 연결만 시도한다.
#   3) 절대 실패하지 않는다 — 개별 점검 예외는 해당 항목 status=error 로 격리.
#
# 응답 요약
#   status      healthy | degraded | unhealthy   (required 의존성 기준으로 산출)
#   version     build·commit·branch·env·region (민감정보 제외)
#   dependencies[] {name, kind, required, status, detail, checked, latency_ms}
# ==========================================================================
import os, sys, json, time, socket, platform
from http.server import BaseHTTPRequestHandler

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)
try:
    import _errors  # 표준 에러 문구 공유 (부재해도 /health 는 동작해야 한다)
except Exception:   # pragma: no cover
    class _errors:  # type: ignore
        MESSAGE_BY_CODE = {"INTERNAL_ERROR": "일시적인 오류가 발생했습니다."}

BUILD = os.environ.get("CALLBOT_BUILD", "dev")

# 의존성 status 값 (문자열 상수로 고정 — 모니터 알림 규칙이 이 값에 의존)
OK = "ok"                     # 정상 동작 가능
NOT_CONFIGURED = "not_configured"   # 미설정. required면 degraded 유발
MISCONFIGURED = "misconfigured"     # 설정이 서로 모순(예: http 백엔드인데 base URL 없음)
SIMULATED = "simulated"       # 실연동 대신 sim/데모로 동작 중(의도된 상태)
ERROR = "error"               # 점검 자체가 실패


def _bool_env(*names):
    for n in names:
        if (os.environ.get(n) or "").strip():
            return True
    return False


def _env_str(name, default=""):
    return (os.environ.get(name) or default).strip()


# --------------------------------------------------------------------------
# 버전 정보 — 배포 추적용. Vercel 시스템 환경변수를 우선 사용한다.
# 커밋 해시·브랜치·환경·리전은 비밀이 아니다(메시지 본문은 노출하지 않음).
# --------------------------------------------------------------------------
def _version():
    sha = _env_str("VERCEL_GIT_COMMIT_SHA") or _env_str("CALLBOT_COMMIT")
    return {
        "build": BUILD,
        "commit": sha or None,
        "commit_short": (sha[:7] if sha else None),
        "branch": _env_str("VERCEL_GIT_COMMIT_REF") or None,
        "env": _env_str("VERCEL_ENV") or "local",
        "region": _env_str("VERCEL_REGION") or None,
        "python": platform.python_version(),
    }


# --------------------------------------------------------------------------
# deep 점검 — 자격증명 없이 TCP 도달성만 확인. 기본 OFF.
# --------------------------------------------------------------------------
def _deep_allowed(query):
    return _env_str("HEALTH_DEEP") == "1" and ("deep=1" in (query or ""))


def _tcp_reachable(host, port=443, timeout=1.5):
    """호스트:포트 TCP 연결만 시도. 요청 본문·헤더·키를 전혀 보내지 않는다."""
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True, int((time.time() - t0) * 1000), None
    except Exception as e:
        return False, int((time.time() - t0) * 1000), type(e).__name__


def _host_of(url):
    """URL에서 호스트명만 추출(사용자·비밀번호·경로·쿼리 제거)."""
    try:
        u = (url or "").strip()
        if "://" in u:
            u = u.split("://", 1)[1]
        u = u.split("/", 1)[0]
        if "@" in u:            # user:pass@host 형태의 자격증명 제거
            u = u.rsplit("@", 1)[1]
        return u.split(":", 1)[0] or None
    except Exception:
        return None


def _dep(name, kind, required, status, detail, checked=False, latency_ms=None):
    d = {
        "name": name,
        "kind": kind,
        "required": bool(required),
        "status": status,
        "detail": detail,
        "checked": bool(checked),
    }
    if latency_ms is not None:
        d["latency_ms"] = latency_ms
    return d


# --- 개별 의존성 점검 ------------------------------------------------------
def _dep_llm(deep):
    """LLM(Gemini) — 키 설정 여부만. 실호출은 과금이므로 어떤 모드에서도 안 한다."""
    present = _bool_env("GOOGLE_API_KEY", "GEMINI_API_KEY")
    if not present:
        return _dep("llm", "external_api", True, NOT_CONFIGURED,
                    "GOOGLE_API_KEY/GEMINI_API_KEY 미설정 — 대화 응답 불가")
    if deep:
        okc, ms, err = _tcp_reachable("generativelanguage.googleapis.com")
        return _dep("llm", "external_api", True, OK if okc else ERROR,
                    "키 설정됨, TCP 도달성 확인" if okc else ("TCP 도달 실패: %s" % err),
                    checked=True, latency_ms=ms)
    return _dep("llm", "external_api", True, OK, "키 설정됨(실호출 미수행)")


def _dep_order(deep):
    """주문/환불 백엔드 — demo면 정상(시뮬), http면 base URL·쓰기 게이트 점검."""
    mode = _env_str("ORDER_BACKEND", "demo").lower() or "demo"
    if mode != "http":
        return _dep("order_backend", "backend", True, SIMULATED,
                    "ORDER_BACKEND=%s (데모 주문 데이터)" % mode)
    base = _env_str("ORDER_API_BASE")
    if not base:
        return _dep("order_backend", "backend", True, MISCONFIGURED,
                    "ORDER_BACKEND=http 이나 ORDER_API_BASE 미설정")
    write = _env_str("ORDER_API_ALLOW_WRITE") == "1"
    detail = "http 연동(쓰기 %s)" % ("허용" if write else "dry-run")
    if deep:
        host = _host_of(base)
        if host:
            okc, ms, err = _tcp_reachable(host)
            return _dep("order_backend", "backend", True, OK if okc else ERROR,
                        detail + (", TCP 도달성 확인" if okc else ", TCP 도달 실패: %s" % err),
                        checked=True, latency_ms=ms)
    return _dep("order_backend", "backend", True, OK, detail)


def _dep_speech(speech):
    """STT/TTS — SPEECH_LIVE=0이면 sim 강제. sim은 장애가 아니라 의도된 상태."""
    try:
        if not speech.get("speech_live"):
            return _dep("speech", "external_api", False, SIMULATED,
                        "SPEECH_LIVE=0 — STT/TTS sim 고정(실호출 차단)")
        stt = speech.get("stt", {}).get("active") or speech.get("stt", {}).get("requested")
        tts = speech.get("tts", {}).get("active") or speech.get("tts", {}).get("requested")
        return _dep("speech", "external_api", False, OK, "stt=%s, tts=%s" % (stt, tts))
    except Exception as e:
        return _dep("speech", "external_api", False, ERROR, "점검 실패: %s" % type(e).__name__)


def _dep_cpaas():
    """통신 회선(CPaaS) — 실발신 게이트는 승인 전까지 OFF가 정상."""
    live = _env_str("CPAAS_LIVE") == "1"
    provider = _env_str("CPAAS_PROVIDER", "sim") or "sim"
    if not live:
        return _dep("cpaas", "external_api", False, SIMULATED,
                    "CPAAS_LIVE=0 — 실발신 차단(provider=%s)" % provider)
    return _dep("cpaas", "external_api", False, OK,
                "provider=%s, 웹훅 토큰 %s" % (provider, "설정됨" if _bool_env("CPAAS_WEBHOOK_TOKEN") else "미설정"))


def _dep_monitoring(mon):
    if mon.get("enabled"):
        return _dep("monitoring", "observability", False, OK,
                    "Sentry 전송 활성(env=%s)" % mon.get("environment"))
    return _dep("monitoring", "observability", False, NOT_CONFIGURED,
                "SENTRY_DSN 미설정 — 오류 수집 비활성(no-op)")


def _dep_storage():
    """저장소 — 현재 녹취/감사 저장은 프로세스 메모리(서버리스에서 휘발).
    상용 전환 시 외부 스토리지 배선이 필요하다는 사실을 헬스로 드러낸다."""
    return _dep("storage", "storage", False, SIMULATED,
                "in-process(휘발성) — 영속 스토리지 미배선 [승인 필요]")


def _dependencies(speech, mon, deep):
    out = []
    for fn in (lambda: _dep_llm(deep), lambda: _dep_order(deep),
               lambda: _dep_speech(speech), _dep_cpaas,
               lambda: _dep_monitoring(mon), _dep_storage):
        try:
            out.append(fn())
        except Exception as e:  # 개별 점검 실패가 헬스 전체를 죽이지 않도록 격리
            out.append(_dep("unknown", "unknown", False, ERROR, "점검 예외: %s" % type(e).__name__))
    return out


def _overall(deps):
    """required 의존성만 가용성 판정에 반영. sim/데모는 의도된 상태이므로 정상."""
    bad_required = [d for d in deps if d["required"] and d["status"] in (NOT_CONFIGURED, MISCONFIGURED, ERROR)]
    if bad_required:
        return "unhealthy" if any(d["status"] == ERROR for d in bad_required) else "degraded"
    if any(d["status"] == ERROR for d in deps):
        return "degraded"
    return "healthy"


def _speech():
    """STT/TTS 프로바이더 상태 요약 — 실호출·키 값 노출 없음 (P0-1 후속).

    - requested: env(CALLBOT_STT_PROVIDER/CALLBOT_TTS_PROVIDER) 기준 요청 값.
      미설정 시 기존 라이브 경로 기본값(stt=gemini, tts=edge).
    - delegated: speech_providers 팩토리로 위임되는지 여부.
    - active/forced_sim: 위임 시 실제 선택 결과. SPEECH_LIVE=0이면 비-sim 요청도
      sim 강제(실호출 원천 차단) → forced_sim=True 로 표시.
    """
    stt_want = (os.environ.get("CALLBOT_STT_PROVIDER") or "").strip().lower()
    tts_want = (os.environ.get("CALLBOT_TTS_PROVIDER") or "").strip().lower()
    info = {
        "speech_live": (os.environ.get("SPEECH_LIVE") or "0").strip() == "1",
        "stt": {"requested": stt_want or "gemini", "delegated": bool(stt_want and stt_want != "gemini")},
        "tts": {"requested": tts_want or "edge", "delegated": bool(tts_want and tts_want != "edge")},
    }
    if info["stt"]["delegated"] or info["tts"]["delegated"]:
        try:
            d = os.path.dirname(__file__)
            if d not in sys.path:
                sys.path.insert(0, d)
            import speech_providers as sp
            if info["stt"]["delegated"]:
                p, m = sp._pick(sp._STT, "CALLBOT_STT_PROVIDER")
                info["stt"]["active"] = p.name
                info["stt"]["forced_sim"] = bool(m.get("forced_sim"))
            if info["tts"]["delegated"]:
                p, m = sp._pick(sp._TTS, "CALLBOT_TTS_PROVIDER")
                info["tts"]["active"] = p.name
                info["tts"]["forced_sim"] = bool(m.get("forced_sim"))
        except Exception as e:  # 헬스는 절대 실패하지 않도록 best-effort
            info["note"] = "speech_providers unavailable: %s" % e
    return info


def _monitoring():
    """오류 모니터링 상태 — DSN 값은 노출하지 않고 설정 여부만."""
    try:
        d = os.path.dirname(__file__)
        if d not in sys.path:
            sys.path.insert(0, d)
        import monitoring
        return monitoring.status()
    except Exception as e:  # 헬스는 절대 실패하지 않는다
        return {"enabled": False, "note": "monitoring unavailable: %s" % e}


def _payload(query=""):
    deep = _deep_allowed(query)
    speech = _speech()
    mon = _monitoring()
    deps = _dependencies(speech, mon, deep)
    status = _overall(deps)
    return {
        # ok 는 "프로세스 생존" 신호 — LB/업타임 모니터 호환을 위해 항상 True.
        # 의존성 저하는 status/dependencies 로 판단한다.
        "ok": True,
        "service": "callbot-portal",
        "status": status,
        "ts": int(time.time()),
        "build": BUILD,          # 하위호환(기존 소비자)
        "version": _version(),
        "runtime": {
            "python": platform.python_version(),
        },
        "checks": {"mode": "deep" if deep else "shallow", "deep_available": _env_str("HEALTH_DEEP") == "1"},
        "dependencies": deps,
        # 설정 여부만 노출 (값 미노출)
        "config": {
            "google_key_present": _bool_env("GOOGLE_API_KEY", "GEMINI_API_KEY"),
            "api_key_gate": _bool_env("CALLBOT_API_KEY"),
            "strict_mode": (os.environ.get("CALLBOT_STRICT") or "").strip() == "1",
            "cpaas_webhook": _bool_env("CPAAS_WEBHOOK_TOKEN"),
            "order_backend": (os.environ.get("ORDER_BACKEND") or "demo").strip(),  # order_backend.get_backend() 기본값(demo)과 일치
        },
        "speech": speech,
        "monitoring": mon,
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            q = ""
            try:
                q = (self.path or "").split("?", 1)[1]
            except Exception:
                q = ""
            self._send(200, _payload(q))
        except Exception:
            # /health 는 status 키가 헬스 등급이므로 표준 봉투의 status(HTTP 코드)와
            # 충돌한다. 등급 의미를 지키되 code 를 붙이고 내부 문구는 노출하지 않는다.
            self._send(500, {"ok": False, "status": "error",
                             "code": "INTERNAL_ERROR",
                             "error": _errors.MESSAGE_BY_CODE["INTERNAL_ERROR"]})

    # HEAD 요청(일부 모니터)도 200으로 응답
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
