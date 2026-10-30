"""CPaaS 콜 연계 웹훅 — 실제 전화망 <-> 기존 콜봇 두뇌 연결 어댑터.

회선/발신/녹음/재생 = CPaaS 위임(종량제), STT/LLM/시나리오/TTS = 자체 재사용.
- 무료 테스트: sim(텍스트 주입) · dry-run 캠페인 -> 통신비 0
- 실제 인바운드: Twilio(TwiML) 지원(자체 STT/TTS). 한국 070은 ClawOps 등 국내 CPaaS.
- 과금은 CPAAS_LIVE=1 + 실제 통화 발생 시에만.
"""
from __future__ import annotations
import os, sys, json, time, base64
import urllib.request
from urllib.parse import parse_qs, urlparse, quote
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

# SSRF 가드는 보안 통제라 폴백을 두지 않는다 — 모듈을 못 불러오면 검증 없이
# 외부 URL 로 나가는 것보다 import 시점에 드러나는 편이 안전하다(fail-closed).
import _urlguard

def _scrub_unavailable(v):
    """마스킹 모듈을 못 불러왔을 때의 폴백 — 발화를 **통과시키지 않는다**.

    폴백이 원문을 그대로 돌려주면 모듈 하나가 빠진 순간 조회 경로(`?op=log`)로
    발화가 그대로 흘러간다(fail-open). 가려서 못 읽는 쪽이 낫다.
    """
    return "(마스킹 모듈 없음)" if v else ""


try:
    # 발화 마스킹용. 없어도 통화는 동작해야 하므로 폴백을 둔다(fail-closed).
    from _monitoring import scrub as _scrub
except Exception:                       # pragma: no cover - 모듈 부재 폴백
    _scrub = _scrub_unavailable

try:
    from _engine import run_turn, trim_history
except Exception:
    run_turn = None
    trim_history = None
try:
    from _stt import transcribe
except Exception:
    transcribe = None
try:
    from assist import run_assist
except Exception:
    run_assist = None
try:
    # 실측 통화 지표 수집기. 없어도 통화는 정상 처리된다(집계만 비어 있을 뿐).
    # 호출은 전부 예외를 전파하지 않는 래퍼이고, 삼킨 실패는 collector.errors 로 드러난다.
    import _call_metrics as call_metrics
except Exception:                       # pragma: no cover
    call_metrics = None


def _metric(fn_name, *a, **kw):
    """지표 수집이 통화를 끊지 않게 감싼다(수집 실패 < 통화 성공)."""
    if call_metrics is None:
        return None
    try:
        return getattr(call_metrics, fn_name)(*a, **kw)
    except Exception:                   # pragma: no cover
        return None

CPAAS = os.environ.get("CPAAS_PROVIDER", "sim")
try:
    import disclosure as _disclosure      # AI 고지 문구(테넌트별) — 없으면 아래 폴백
except Exception:
    _disclosure = None

# 하위 호환: CALLBOT_GREETING 환경변수는 계속 존중한다(운영자 명시 설정).
# 단 AI 고지 요건 검사 결과는 /api/disclosure 가 env_override 로 드러낸다(조용히 덮지 않는다).
GREETING = os.environ.get("CALLBOT_GREETING", "")


def greeting(tenant_id=None):
    """통화 첫 발화 = AI 고지 문구.

    우선순위: 테넌트 설정(/api/disclosure) > CALLBOT_GREETING 환경변수 > 기본 고지 문구.
    어떤 경우에도 빈 문자열을 돌려주지 않는다(고지가 빠지는 쪽으로 실패하지 않는다).
    """
    if _disclosure is not None:
        try:
            eff = _disclosure.effective(tenant_id)
            if eff.get("source") == "tenant":
                return eff["text"]
        except Exception:
            pass
    env = (os.environ.get("CALLBOT_GREETING") or "").strip()
    if env:
        return env
    if _disclosure is not None:
        try:
            return _disclosure.greeting(None)
        except Exception:
            pass
    return "안녕하세요, AI 상담원입니다. 이 통화는 인공지능이 응대합니다. 무엇을 도와드릴까요?"
AGENT_SIP = os.environ.get("CALLBOT_AGENT_SIP", "sip:agent@pbx.local")


def live():
    """실발신 게이트를 **호출 시점에** 읽는다(기본 OFF).

    규칙은 손대지 않았다 — 다른 라우트와 같은 `"1"` 정확 일치다
    (`wellbeing.live_mode()`·`caller_id.cpaas_live()`·`_sip_adapter.is_live()`).
    바뀐 것은 **읽는 시점**뿐이다.

    예전에는 모듈 전역 상수였다. 서버리스 인스턴스는 웜으로 수 시간을 살기
    때문에 그 사이 환경변수를 바꿔도 이미 뜬 인스턴스에는 반영되지 않는다 —
    켜는 쪽은 불편할 뿐이지만 **끄는 쪽은 비상정지가 듣지 않는다**
    (`CPAAS_LIVE` 를 0 으로 내려도 그 인스턴스의 캠페인은 계속
    `mode="live"` 로 답하고 `GET /api/voice` 도 `live:true` 라고 답한다).
    게다가 안부·발신번호 라우트는 호출 시점에 읽으므로 **같은 인스턴스
    안에서 라우트마다 게이트가 엇갈린다**(안부는 501 로 막는데 여기는
    `live:false`). `_speech_providers.is_live()` 가 같은 이유로 이미 고쳐
    두고 docstring 에 이유까지 적어 둔 자리인데, 여기만 비켜 가 있었다.

    앞뒤 공백도 버린 뒤에 비교한다 — `CPAAS_LIVE=" 1 "` 하나로 네 라우트가
    둘씩 갈렸다(`_sip_adapter.is_live()` 의 같은 주석 참조).
    """
    return (os.environ.get("CPAAS_LIVE") or "0").strip() == "1"


# 하위호환: 모듈 로드 시점 스냅샷. **판정에는 `live()` 를 쓴다**
# (`_sip_adapter.CPAAS_LIVE` 와 같은 규약).
LIVE = live()


# --------------------------------------------------------------------------
# 통화 세션 — 인스턴스 메모리. **상한과 만료가 있다.**
#
# 세션 1건에는 그 통화의 대화 원문(`messages`: 고객 발화 전사 + 봇 응답)과
# 발신번호(`phone`, 툴 조회에 쓰므로 원문)가 들어 있다. 삭제는 `completed`
# 이벤트 한 곳뿐이었는데 그 이벤트는 **보장되지 않는다** — 연결이 끊긴 통화,
# CPaaS 가 재시도에 실패한 종료 이벤트, `_parse_event` 가 모르는 이벤트 종류로
# 끝난 통화는 세션이 그대로 남는다. 웜 인스턴스는 수 시간을 살기 때문에,
# 상한이 없으면 (1) 통화를 처리할수록 메모리가 자라 결국 **진행 중인 통화**가
# 죽고(26차가 전환 티켓에서 고친 것과 같은 결함), (2) 통화가 끝난 뒤에도
# 대화 원문이 기한 없이 남는다(개인정보 보관 기간 = 인스턴스 수명).
#
# 같은 통화를 **세는** 쪽(`_call_metrics`)은 이미 `MAX_OPEN`·`RETENTION_SEC` 를
# 갖고 있었다 — 눈금을 같이 쓴다. 상한 초과 시에는 **가장 오래 접촉이 없던**
# 세션부터 버린다(방금 말한 고객의 맥락을 먼저 버리지 않는다). 세션이 사라져도
# 통화는 죽지 않는다 — 호출부가 프레시 세션으로 폴백한다(대화 맥락만 잃는다).
# 버린 건수는 조용히 사라지지 않고 `GET /api/voice.sessions` 와 로그에 남는다.
# --------------------------------------------------------------------------
SESSION_MAX = 500          # 동시 보관 세션 상한(`_call_metrics.MAX_OPEN` 과 같은 눈금)
SESSION_TTL = 1800.0       # 마지막 접촉 후 만료(초). 통화는 이보다 길지 않다.
# 만료·축출 집계 — 만든 수치가 아니라 이 인스턴스가 실제로 버린 건수.
# `trimmed`(이력을 자른 통화 수)·`trimmed_msgs`(버린 메시지 수)는 32차 추가분.
SESSION_STATS = {"expired": 0, "evicted": 0, "trimmed": 0, "trimmed_msgs": 0,
                 "last_reason": None}


def _session_limit(name, default, cast):
    """상한·만료는 호출 시점에 읽는다(배포 후 조정이 반영된다).

    잘못된 값으로 상한이 **사라지는** 쪽으로 실패하지 않는다 — 해석 불가·0 이하는
    기본값으로 되돌린다(`_sip_adapter.is_live()` 와 같은 '호출 시점 판독' 규약).
    """
    try:
        v = cast((os.environ.get(name) or "").strip())
        return v if v > 0 else default
    except Exception:
        return default


def _session_note(kind, reason):
    """세션을 버린 사실을 드러낸다. 집계 실패가 통화를 끊지 않는다.

    통화ID·발화 원문은 남기지 않는다 — 우리가 만든 고정 사유 문구만 남긴다.
    """
    try:
        SESSION_STATS[kind] = int(SESSION_STATS.get(kind) or 0) + 1
        SESSION_STATS["last_reason"] = reason
        _log.emit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "level": "warn", "service": _log.SERVICE, "route": "/api/voice",
                   "event": "session_drop", "result": kind, "reason": reason})
    except Exception:                    # pragma: no cover - 집계 실패가 통화를 끊지 않는다
        pass


class _Session:
    _mem = {}

    @staticmethod
    def _seen(data):
        try:
            return float(data.get("seen") or 0.0)
        except Exception:                # pragma: no cover - 외부가 넣은 값 방어
            return 0.0

    @classmethod
    def sweep(cls, now=None):
        """만료분 정리 + 하드 상한. 버린 건수는 집계·로그로 드러낸다."""
        now = time.time() if now is None else now
        ttl = _session_limit("CALLBOT_SESSION_TTL", SESSION_TTL, float)
        for cid in [c for c, d in list(cls._mem.items()) if now - cls._seen(d) > ttl]:
            cls._mem.pop(cid, None)
            _session_note("expired", "마지막 접촉 후 %d초 초과" % int(ttl))
        cap = _session_limit("CALLBOT_SESSION_MAX", SESSION_MAX, int)
        if len(cls._mem) > cap:
            # 가장 오래 접촉이 없던 것부터 — 방금 말한 고객의 맥락이 먼저 사라지지 않는다.
            old = sorted(cls._mem.items(), key=lambda kv: cls._seen(kv[1]))
            for cid, _d in old[:len(cls._mem) - cap]:
                cls._mem.pop(cid, None)
                _session_note("evicted", "동시 세션 상한 %d건 초과" % cap)

    @classmethod
    def get(cls, cid):
        data = cls._mem.get(cid)
        if data is None:
            return None
        ttl = _session_limit("CALLBOT_SESSION_TTL", SESSION_TTL, float)
        if time.time() - cls._seen(data) > ttl:
            # 만료된 세션은 '없는 것'으로 본다 — 몇 시간 전 대화를 이어받지 않는다.
            cls._mem.pop(cid, None)
            _session_note("expired", "마지막 접촉 후 %d초 초과" % int(ttl))
            return None
        data["seen"] = time.time()
        return data

    @classmethod
    def put(cls, cid, data):
        try:
            data["seen"] = time.time()
        except Exception:                # pragma: no cover - dict 아닌 값 방어
            pass
        cls._mem[cid] = data
        cls.sweep()

    @classmethod
    def drop(cls, cid):
        cls._mem.pop(cid, None)

    @classmethod
    def stats(cls):
        """노출용 사실만 — 통화ID·발화는 담지 않는다."""
        d = dict(SESSION_STATS)
        d["live"] = len(cls._mem)
        d["max"] = _session_limit("CALLBOT_SESSION_MAX", SESSION_MAX, int)
        d["ttl_sec"] = int(_session_limit("CALLBOT_SESSION_TTL", SESSION_TTL, float))
        return d


# --------------------------------------------------------------------------
# 세션이 **저장하는** 대화 이력 (32차)
#
# 30차는 세션 **건수**에 상한을 뒀고, 31차는 **모델에 보내는 창**에 상한을 뒀다.
# 그런데 세션이 들고 있는 이력 자체에는 상한이 없었다 — 31차 보고서가 한계 ②로
# 적어 둔 자리다. 그래서 한 통화 안에서 (1) 고객이 말한 전사 **원문**이 통화가
# 끝날 때까지(또는 `SESSION_TTL` 30분까지) 전부 쌓이고 (2) 긴 통화 하나가 쓰는
# 메모리에 한도가 없다. 영속 저장소가 [승인 필요]인 동안 통화 데이터의 보관
# 기간은 코드가 적는 것이고, 여기에는 적혀 있지 않았다.
#
# 31차가 손대지 않은 이유는 fail-open 이었다: 가드의 기억(`_engine._mem`)은 전체
# 이력에서 계산되므로 이력을 **버리면** 견적·한도가 함께 사라진다. 그래서 버리지
# 않고 **접어서 넘긴다**(`_engine.trim_history` → `mem_prior`). 접힌 쪽에는 발화가
# 없고 금액·판정 6칸만 남는다.
#
# 자른 사실은 조용히 지나가지 않는다 — 통화당 1줄(건수만)과
# `GET /api/voice.sessions` 의 `trimmed`·`trimmed_msgs` 로 드러난다.
# --------------------------------------------------------------------------
def _session_trim_note(dropped, kept, first):
    """이력을 잘랐다는 사실을 센다. 집계 실패가 통화를 끊지 않는다.

    로그는 **통화당 1줄**이다(60턴 통화에서 20줄을 남기면 실제 트래픽이 묻힌다 —
    18차가 `/api/health` 에서 내린 것과 같은 판단). 건수만 남기고 발화는 담지 않는다.
    """
    try:
        SESSION_STATS["trimmed_msgs"] = int(SESSION_STATS.get("trimmed_msgs") or 0) + dropped
        if first:
            SESSION_STATS["trimmed"] = int(SESSION_STATS.get("trimmed") or 0) + 1
            _log.emit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "level": "info", "service": _log.SERVICE, "route": "/api/voice",
                       "event": "history_trim", "dropped": dropped, "kept": kept})
    except Exception:                    # pragma: no cover - 집계 실패가 통화를 끊지 않는다
        pass


def _store_history(sess, messages):
    """턴이 끝난 이력을 세션에 **상한과 함께** 저장한다.

    `trim_history` 가 없으면(엔진 모듈 부재 폴백) 자르지 않고 그대로 저장한다 —
    상한이 없는 쪽으로 넘어지지만, 그 경로에서는 `run_turn` 도 없어서 통화가
    진행되지 않는다(이력이 자랄 일 자체가 없다).
    """
    if trim_history is None:             # pragma: no cover - 엔진 부재 폴백
        sess["messages"] = messages
        return 0
    kept, folded, dropped = trim_history(messages, sess.get("mem"))
    sess["messages"] = kept
    sess["mem"] = folded
    if dropped:
        # 통화 전체 메시지 수는 계속 센다 — 잘랐다고 `turn_count` 가 작아지면
        # 통화 종료 로그가 거짓이 된다(상한은 보관량의 상한이지 사실의 상한이 아니다).
        sess["dropped_total"] = int(sess.get("dropped_total") or 0) + dropped
        _session_trim_note(dropped, len(kept), sess["dropped_total"] == dropped)
    return dropped


def _session_ref(cid):
    """전환 티켓에 남길 통화 참조 — 통화ID **지문**(원문 아님).

    지문을 만들 수 없더라도 `sim-` 이라고 적지 않는다 — 실통화 전환을 시연으로
    읽게 만드는 쪽이 모르는 것보다 나쁘다(`_engine._esc_ref` 주석 참조).
    """
    d = _call_digest(cid)
    return "call-%s" % d if d else "call-unknown"


RECENT = []
MAX_RECENT = 60            # 인입 콜 로그 보관 건수(조회 경로 `?op=log`)
MAX_LOG_TEXT = 300         # 로그 1건의 발화 길이 상한(마스킹 후 측정)


def _now():
    return time.strftime("%H:%M:%S")


def _mask_phone(v):
    """통화 로그에 원문 번호를 남기지 않는다(앞 3자리 + 뒤 4자리만).

    /api/voice?op=log 는 콘솔이 4초마다 폴링하는 공개 경로다. 화면은 번호를
    쓰지 않으므로(admin.html cpaasRenderLog) 마스킹해도 기능 손실이 없다.
    """
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    if not s:
        return ""
    if len(s) < 8:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def _mask_text(v):
    """통화 로그의 발화 — 개인정보 패턴을 마스킹하고 길이를 자른다.

    `?op=log` 는 콘솔이 4초마다 폴링하는 조회 경로이고, 앞단 `_guard` 는 인증이
    아니다(`Origin` 헤더 한 줄로 통과한다 — `check_recording_url` 주석 참조).
    발신번호는 `_mask_phone` 으로 막아 두었는데 **같은 레코드의 발화 원문**은
    그대로 나갔다 — 고객이 통화 중 말하는 주민번호·카드번호·계좌번호가 그
    경로로 노출된다. 안부 결과 요약이 이미 `monitoring.scrub()` 를 통과하는
    것과 같은 규칙을 쓴다(한쪽만 지켜지지 않도록).

    화면 기능(대화 흐름 확인)은 유지된다 — 지우는 것이 아니라 가린다.
    """
    s = _scrub(v) if v else ""
    return s[:MAX_LOG_TEXT] if len(s) > MAX_LOG_TEXT else s


def _log_call(entry):
    entry["t"] = _now()
    if "from" in entry:
        entry["from"] = _mask_phone(entry.get("from"))
    if "text" in entry:
        entry["text"] = _mask_text(entry.get("text"))
    RECENT.insert(0, entry)
    del RECENT[MAX_RECENT:]


def _xesc(s):
    """XML 텍스트 노드 이스케이프."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xattr(s):
    """XML 속성값 이스케이프 — 따옴표까지 막아야 속성 주입이 불가능하다.

    CallId·발신번호는 외부(CPaaS)에서 들어오는 값이라 텍스트 이스케이프만으로는
    `" onX="` 형태로 속성을 덧붙일 수 있었다.
    """
    return _xesc(s).replace('"', "&quot;").replace("'", "&apos;")


def _vml(inner):
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + inner + '</Response>'


def _action_url(cid):
    base = os.environ.get("CALLBOT_BASE_URL", "https://callbot-portal.vercel.app").rstrip("/")
    tok = (os.environ.get("CPAAS_WEBHOOK_TOKEN", "") or "").strip()
    qs = []
    if tok:
        qs.append("t=" + quote(tok, safe=""))
    if cid:
        # 퍼센트 인코딩 후에는 XML 특수문자가 남지 않는다(속성 주입 차단).
        qs.append("cid=" + quote(str(cid), safe=""))
    return base + "/api/voice" + ("?" + "&amp;".join(qs) if qs else "")


def _say_then_record(text, cid):
    # ClawOps VoiceML: <Gather input="speech"> 미지원 -> <Record> 로 발화 수집 후 action 콜백
    return _vml(
        '<Say language="ko-KR">%s</Say>'
        '<Record maxLength="12" finishOnKey="#" playBeep="true" action="%s"/>'
        '<Hangup/>' % (_xesc(text), _action_url(cid)))


def _say_then_hangup(text):
    return _vml('<Say language="ko-KR">%s</Say><Hangup/>' % _xesc(text))


def _say_then_dial(text, number, caller_id, cid):
    # ClawOps <Dial> 은 <Number> noun 만 지원(SIP 미지원)
    return _vml(
        '<Say language="ko-KR">%s</Say>'
        '<Dial timeout="30" callerId="%s" action="%s"><Number>%s</Number></Dial>'
        '<Say language="ko-KR">상담원 연결이 어렵습니다. 잠시 후 다시 이용해 주세요.</Say><Hangup/>'
        % (_xesc(text), _xattr(caller_id), _action_url(cid), _xesc(number)))


def handle_twilio(p):
    # ClawOps VoiceML(TwiML 호환) 인바운드 핸들러.
    # 요청 파라미터: CallId, AccountId, From, To, CallStatus, Direction
    # 콜백 파라미터: RecordingUrl, RecordingDuration, Digits, DialCallStatus
    cid = p.get("CallId") or p.get("CallSid") or "call"
    frm = p.get("From", "")
    to = p.get("To", "")
    status = (p.get("CallStatus", "") or "").lower()
    rec_url = (p.get("RecordingUrl", "") or "").strip()
    rec_dur = str(p.get("RecordingDuration", "") or "").strip()
    dial_status = (p.get("DialCallStatus", "") or "").lower()
    scenario = os.environ.get("CALLBOT_DEFAULT_SCENARIO", "refund")

    # 통화 종료류
    if status in ("completed", "canceled", "busy", "no-answer", "failed"):
        # 실측: 연결 자체가 안 된 종료(busy/no-answer/failed)는 통화 실패로 구분한다.
        # completed/canceled 는 사유를 단정하지 않고 수집기의 관측 규칙에 맡긴다.
        _metric("finish", cid,
                outcome="failed" if status in ("busy", "no-answer", "failed") else None)
        _Session.drop(cid)
        _log_call({"from": frm, "ev": "통화종료", "text": status})
        return _say_then_hangup("이용해 주셔서 감사합니다.")

    # Dial 결과 콜백
    if dial_status:
        if dial_status == "completed":
            _Session.drop(cid)
            return _say_then_hangup("상담이 종료되었습니다. 감사합니다.")
        return _say_then_record("상담원 연결이 어렵습니다. 용건을 말씀해 주시면 도와드리겠습니다.", cid)

    # 세션 (서버리스: best-effort, 없으면 프레시 시작)
    sess = _Session.get(cid)
    if not sess:
        sess = {"messages": [], "scenario": scenario, "phone": frm}
        _Session.put(cid, sess)
        _metric("start", cid, scenario=scenario)
        _log_call({"from": frm, "ev": "통화연결", "text": ""})

    # 녹음 콜백이 아니면(=첫 진입) 인사 후 녹음 요청
    if not rec_url:
        return _say_then_record(greeting(None), cid)

    # 같은 녹음이 두 번 배달되면(웹훅 재시도) 직전 응답을 그대로 돌려준다.
    # 재처리하면 STT·LLM 이 두 번 과금되고, 환불 같은 도구가 두 번 실행된다.
    if rec_url and sess.get("last_rec") == rec_url and sess.get("last_vml"):
        return sess["last_vml"]

    # 녹음 있음 -> STT
    if rec_dur in ("", "0", "0.0"):
        return _say_then_record("죄송합니다, 잘 못 들었어요. 다시 말씀해 주시겠어요?", cid)
    user_text = _transcribe_url(rec_url)
    if not user_text:
        return _say_then_record("죄송합니다, 잘 못 들었어요. 다시 말씀해 주시겠어요?", cid)
    _log_call({"from": frm, "ev": "고객발화", "text": user_text})

    if run_turn:
        sess["messages"].append({"role": "user", "content": user_text})
        r = run_turn(sess["messages"], phone=frm, scenario=sess["scenario"],
                     mem_prior=sess.get("mem"), session_ref=_session_ref(cid))
        _store_history(sess, r["messages"])
        _log_call({"from": frm, "ev": "봇응답", "text": r.get("reply", "")})
        _metric("mark_turn", cid)
        if r.get("transferred"):
            _metric("mark_outcome", cid, "transferred")
            agent = (os.environ.get("CALLBOT_AGENT_PHONE", "") or "").strip()
            if agent:
                out = _say_then_dial("상담사에게 연결해 드리겠습니다. 잠시만 기다려 주세요.", agent, to, cid)
            else:
                out = _say_then_hangup("죄송합니다, 지금은 상담사 연결이 어렵습니다. 잠시 후 다시 이용해 주세요.")
        else:
            out = _say_then_record(r["reply"], cid)
        sess["last_rec"] = rec_url
        sess["last_vml"] = out
        _Session.put(cid, sess)
        return out
    return _say_then_hangup("현재 점검 중입니다. 잠시 후 다시 시도해 주세요.")


def _parse_event(body):
    if CPAAS == "clawops":
        et = body.get("event")
        typ = {"call.answered": "answered", "call.recording": "speech",
               "call.completed": "completed"}.get(et, et)
        meta = body.get("metadata")
        if not isinstance(meta, dict):   # 문자열·배열이 와도 500 이 되지 않게
            meta = {}
        return {"type": typ, "call_id": body.get("callId") or body.get("call_id"),
                "from": body.get("from"), "to": body.get("to"),
                "scenario": meta.get("scenario", "refund"),
                "tenant": meta.get("tenant_id") if isinstance(meta.get("tenant_id"), str) else None,
                "audio_b64": body.get("audio"), "mime": body.get("mime", "audio/wav"),
                "recording_url": body.get("recordingUrl")}
    return {"type": body.get("type", "answered"), "call_id": body.get("call_id", "demo-call"),
            "from": body.get("from", "01000000000"), "to": body.get("to", ""),
            "scenario": body.get("scenario", "refund"),
            "tenant": body.get("tenant_id") if isinstance(body.get("tenant_id"), str) else None,
            "audio_b64": body.get("audio_b64"), "mime": body.get("mime", "audio/webm"),
            "recording_url": body.get("recording_url"), "text": body.get("text")}


def _act_say_then_listen(text):
    return {"actions": [{"action": "say", "text": text, "tts": "self"},
                        {"action": "record", "endpoint": "/api/voice", "vad": True, "maxSeconds": 15}]}


def _act_transfer():
    return {"actions": [{"action": "say", "text": "상담사에게 연결해 드리겠습니다. 잠시만 기다려 주세요."},
                        {"action": "dial", "sip": AGENT_SIP}]}


def handle_event(ev):
    cid = ev.get("call_id") or "call"
    if ev["type"] == "answered":
        sess = _Session.get(cid)
        if sess is None:
            _Session.put(cid, {"messages": [], "scenario": ev["scenario"],
                               "phone": ev["from"], "started": time.time()})
            _metric("start", cid, scenario=ev.get("scenario"), tenant=ev.get("tenant"))
        # 이미 세션이 있으면 answered 재배달이다 — 대화 이력을 지우지 않는다.
        return _act_say_then_listen(greeting(ev.get("tenant")))
    if ev["type"] == "speech":
        sess = _Session.get(cid) or {"messages": [], "scenario": ev["scenario"], "phone": ev["from"]}
        user_text = ev.get("text") or ""
        if not user_text and ev.get("audio_b64") and transcribe:
            user_text = transcribe(ev["audio_b64"], ev.get("mime", "audio/webm"))
        elif not user_text and ev.get("recording_url"):
            user_text = _transcribe_url(ev["recording_url"])
        if not user_text:
            return _act_say_then_listen("죄송합니다, 잘 못 들었어요. 다시 말씀해 주시겠어요?")
        sess["messages"].append({"role": "user", "content": user_text})
        if run_turn:
            r = run_turn(sess["messages"], phone=sess.get("phone", ""),
                         scenario=sess.get("scenario", "refund"),
                         mem_prior=sess.get("mem"), session_ref=_session_ref(cid))
            _store_history(sess, r["messages"])
            _Session.put(cid, sess)
            _metric("mark_turn", cid)
            if r.get("transferred"):
                _metric("mark_outcome", cid, "transferred")
                return _act_transfer()
            return _act_say_then_listen(r["reply"])
        return _act_say_then_listen("현재 응대 엔진 점검 중입니다. 상담사에게 연결해 드릴게요.")
    if ev["type"] == "completed":
        sess = _Session.get(cid)
        result = None
        if sess and run_assist:
            convo = "\n".join("%s: %s" % (m["role"], m["content"]) for m in sess.get("messages", []))
            try:
                result = run_assist("summary", convo)
            except Exception:
                result = None
        _metric("finish", cid)
        _persist_call_result(cid, sess, result)
        _Session.drop(cid)
        out = {"ok": True, "summary": result}
        dropped = int((sess or {}).get("dropped_total") or 0)
        if dropped:
            # 요약이 보는 것은 **남아 있는 창**뿐이다(잘린 앞부분은 들어가지 않는다).
            # 말하지 않으면 소비자는 통화 전체의 요약으로 읽는다 — 긴 통화에서
            # 앞부분 약속이 요약에서 빠진 것을 모른 채 쓰게 된다.
            out["summary_partial"] = True
            out["summary_dropped_messages"] = dropped
        return out
    return {"ok": True, "ignored": ev["type"]}


# --------------------------------------------------------------------------
# 녹음 다운로드 — 외부가 준 URL 로 서버가 직접 나가는 경로(SSRF 표면)
# --------------------------------------------------------------------------
RECORDING_TIMEOUT = 15.0                 # 초 — 서버리스 30초 예산 안에 STT 까지 끝내야 한다
RECORDING_MAX_BYTES = 6 * 1024 * 1024    # 원문 6MiB -> base64 8MiB(_errors.MAX_BODY_AUDIO)
# 다운로드 결과 집계 — 실패를 조용히 삼키지 않기 위한 최소 사실만 담는다(URL 없음).
RECORDING_STATS = {"fetched": 0, "rejected": 0, "failed": 0, "last_reason": None}


def _recording_note(kind, reason):
    """녹음 다운로드 거부·실패를 드러낸다.

    `_transcribe_url` 은 요청 객체를 들고 있지 않아 요청 로그에 얹을 수 없다 —
    대신 구조화 로그 1줄과 카운터(`GET /api/voice`)로 남긴다. URL 은 기록하지
    않는다(서명 쿼리·식별자가 실린다). 사유는 우리가 만든 고정 문구이거나
    예외 '타입명' 뿐이다(예외 문구에는 URL 이 섞인다).
    """
    try:
        RECORDING_STATS[kind] = int(RECORDING_STATS.get(kind) or 0) + 1
        RECORDING_STATS["last_reason"] = reason
        _log.emit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "level": "warn", "service": _log.SERVICE, "route": "/api/voice",
                   "event": "recording_fetch", "result": kind, "reason": reason})
    except Exception:                    # pragma: no cover - 집계 실패가 통화를 끊지 않는다
        pass


def _recording_hosts():
    return _urlguard.env_hosts("CPAAS_RECORDING_HOSTS")


def check_recording_url(url):
    """(ok, reason). 녹음 URL 로 나가도 되는지 판정한다.

    녹음 URL 은 웹훅 **본문**(`RecordingUrl`·`recordingUrl`)으로 들어오는 외부
    값이다. 검증 없이 내려받으면 서버가 대신 `file:///etc/passwd`·사설망·클라우드
    메타데이터(169.254.169.254)를 읽어 STT(LLM)로 흘려보낸다. 웹훅 앞단 가드는
    Origin 헤더만으로도 통과하므로(`_guard._origin_ok`) 인증으로 치지 않는다.
    규칙은 안부 콜백과 같은 모듈(`_urlguard`)을 쓴다.
    """
    return _urlguard.check(url, label="recording_url",
                           allow_insecure=_urlguard.env_flag("CPAAS_ALLOW_INSECURE_RECORDING"),
                           allowlist=_recording_hosts())


def _fetch_recording(url, timeout=None, max_bytes=None):
    """녹음 파일 바이트. 상한 초과·리다이렉트 이탈은 예외로 거부한다.

    `urlopen` 은 리다이렉트를 따라가므로 검증을 통과한 주소가 302 로 사설망을
    가리키면 가드가 무력화된다 — 최종 URL(`geturl()`)을 **다시** 검증해 본문을
    버린다. 중간 요청 자체를 막는 것은 아웃바운드 프록시·호스트 화이트리스트
    (`CPAAS_RECORDING_HOSTS`) 몫이다.

    상한·타임아웃은 기본값으로 굳히지 않고 호출 시점에 읽는다(운영 중 조정 반영).
    """
    timeout = RECORDING_TIMEOUT if timeout is None else timeout
    max_bytes = RECORDING_MAX_BYTES if max_bytes is None else max_bytes
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "callbot-voice/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        try:
            final = resp.geturl() or ""
        except Exception:                # geturl 을 못 읽어도 다운로드는 계속한다
            final = ""
        if final and final != url:
            ok, reason = check_recording_url(final)
            if not ok:
                raise PermissionError("리다이렉트 이탈: " + reason)
        data = resp.read(int(max_bytes) + 1)
    if len(data) > max_bytes:
        raise ValueError("녹음이 상한(%d바이트)을 넘습니다" % max_bytes)
    return data


def _transcribe_url(url):
    """녹음 URL -> 전사 텍스트. 실패하면 "" (호출부는 재청취를 안내한다)."""
    ok, reason = check_recording_url(url)
    if not ok:
        _recording_note("rejected", reason)
        return ""
    if not transcribe:
        _recording_note("failed", "STT 모듈 미적재")
        return ""
    try:
        audio = _fetch_recording(url)
    except Exception as e:
        _recording_note("failed", type(e).__name__)
        return ""
    if not audio:
        _recording_note("failed", "빈 녹음")
        return ""
    try:
        text = transcribe(base64.b64encode(audio).decode(), "audio/wav")
    except Exception as e:
        _recording_note("failed", type(e).__name__)
        return ""
    RECORDING_STATS["fetched"] = int(RECORDING_STATS.get("fetched") or 0) + 1
    return text or ""


def _call_digest(cid):
    """통화ID 지문. 원문은 로그에 남기지 않는다(`_call_metrics.digest` 와 같은 값).

    통화ID 는 CPaaS 가 주는 외부 식별자라 형식이 보장되지 않는다 — 번호 같은
    값이 섞여 들어와도 로그에 원문이 남지 않도록 지표 수집기와 **같은 지문**을
    쓴다(한 인스턴스 안에서 로그 ↔ 지표를 맞출 수 있다).
    """
    if call_metrics is None:
        return None
    try:
        return call_metrics.digest(cid)
    except Exception:
        return None


def _persist_call_result(cid, sess, result):
    """통화 종료 사실을 구조화 로그 1줄로 남긴다. **요약 본문은 싣지 않는다.**

    예전에는 `print("[voice] call done", cid, json.dumps(result))` 였다. 요약은
    대화 전체를 모델이 자유 문장으로 옮긴 것이고(`assist.parse_json` 이 파싱에
    실패하면 `{"raw": <모델 원문>}` 을 돌려준다), 그 안에는 고객이 말한
    성명·주소·주문번호가 섞인다. 17·18차가 접근로그와 요청로그에서 막아 둔
    발화 원문이 이 한 줄로 플랫폼 로그(stdout)에 그대로 남고 있었다 — `print`
    는 scrub·`_log` 등록부·요청ID 어느 것도 거치지 않는다.

    요약 본문은 응답(`{"ok":true,"summary":...}`)으로만 돌려준다 — 그 소비자는
    통화 주체다. 통화 이력 영속 보관은 **[승인 필요]**(저장소 미승인).
    """
    try:
        msgs = (sess or {}).get("messages") or []
        # 32차: 이력을 잘라 저장하므로 `len(msgs)` 는 **보관량**이다. 통화가 몇 턴
        # 이었는지는 잘라 버린 수를 더해야 사실이 된다(상한이 사실을 줄이지 않는다).
        dropped = int((sess or {}).get("dropped_total") or 0)
        _log.emit({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "level": "info", "service": _log.SERVICE, "route": "/api/voice",
                   "event": "call_done", "call": _call_digest(cid),
                   "result": "summarized" if isinstance(result, dict) and result else "no_summary",
                   "turn_count": len(msgs) + dropped, "kept": len(msgs)})
    except Exception:                    # pragma: no cover - 기록 실패가 응답을 막지 않는다
        pass


CAMPAIGN_MAX = 500          # 한 요청의 대상 수 상한(응답 크기·메모리 방어)
# 번호 자릿수 — `_sip_adapter._clean_number` 와 **같은 규칙**이다(실발신이 열리면
# 이 계획이 그대로 그 함수로 들어간다). 회귀가 두 규칙의 일치를 강제한다.
CAMPAIGN_DIGITS = (4, 20)
# 대본 이름 — voice 는 시나리오 화이트리스트를 갖고 있지 않다(이벤트 경로도
# 같다). 임의 객체가 metadata 로 새지 않게 **꼴과 길이만** 본다.
CAMPAIGN_SCENARIO_MAX = 40


def campaign_targets(numbers):
    """캠페인 대상 번호 검증 → 숫자만 남긴 목록. 어긋나면 ValueError.

    42차 결함: 라우트가 본문의 `numbers` 를 그대로 `for n in numbers` 에 넣었다.
    문자열이 오면 파이썬은 **글자마다 한 통**을 만든다 — 「01012345678」 한 건이
    `to:"0"`·`to:"1"`… 11건으로 늘어난다. 돌려주는 계획은 그럴듯해 보이므로
    사람이 눈치채지 못하고, 사전(`dict`)이면 키가 번호 자리에 들어앉는다.
    개수 상한도 없어서 한 요청이 수만 건의 계획을 만들 수 있었다.
    **실발신이 열린 뒤에는 이 계획이 그대로 발신 요청이 된다** — 그래서
    계획 단계에서 막는다(`_sip_adapter.dial` 이 번호 1건에 하는 검증의 목록판).
    """
    if numbers is None:
        raise ValueError("numbers 가 필요합니다(대상 번호 배열)")
    if isinstance(numbers, (str, bytes)) or not isinstance(numbers, (list, tuple)):
        raise ValueError("numbers 는 배열이어야 합니다")
    if len(numbers) > CAMPAIGN_MAX:
        raise ValueError("numbers 는 한 번에 %d건까지 가능합니다" % CAMPAIGN_MAX)
    lo, hi = CAMPAIGN_DIGITS
    out = []
    for i, n in enumerate(numbers):
        digits = "".join(ch for ch in n if ch.isdigit()) if isinstance(n, str) else ""
        if not digits or not (lo <= len(digits) <= hi):
            raise ValueError("numbers[%d] 가 전화번호 꼴이 아닙니다(숫자 %d~%d자리)"
                             % (i, lo, hi))
        out.append(digits)
    return out


def trigger_campaign(numbers, scenario="care", meta=None):
    """아웃바운드 캠페인 **계획**을 만든다. 발신은 하지 않는다.

    `queued` 는 「실제로 발신 큐에 넣은 건수」다. 실회선 결선은 아직 없으므로
    (3R·**[승인 필요]**) 게이트가 켜져 있어도 **0** 이다 — 그 전에는 게이트가
    켜지면 `queued: N` 이라고 답했는데, 아무것도 걸지 않았으므로 **하지 않은
    일을 보고하는 숫자**였다(회귀 하나가 「실제로 걸지 않는다」는 제목을 달고
    그 숫자를 정답으로 박아 두고 있었다). 계획 건수는 `would_call` 이다.
    """
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("meta 는 객체여야 합니다")
    if not isinstance(scenario, str) or not scenario.strip():
        raise ValueError("scenario 는 문자열이어야 합니다")
    if len(scenario) > CAMPAIGN_SCENARIO_MAX:
        raise ValueError("scenario 는 %d자 이하여야 합니다" % CAMPAIGN_SCENARIO_MAX)
    targets = campaign_targets(numbers)
    meta = dict(meta or {})
    calls = [{"to": n, "from": os.environ.get("CALLBOT_CALLER_ID", "070-0000-0000"),
              "webhook": "/api/voice", "metadata": {"scenario": scenario, **meta},
              "consent_required": True} for n in targets]
    if not live():
        return {"mode": "dry-run(무과금)", "queued": 0, "would_call": len(calls),
                "calls": calls}
    return {"mode": "live", "queued": 0, "would_call": len(calls), "calls": calls,
            "note": "실회선 발신 결선이 없어 계획만 만들었습니다 — 실발신은 [승인 필요]"}


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _errors
import _log
try:
    import _audit          # 관리 기능 접근 감사 (부재해도 웹훅은 동작한다)
except Exception:          # pragma: no cover
    _audit = None

MAX_WEBHOOK_BODY = 256 * 1024


def _audit_ev(headers, path, method, result, status, **extra):
    """웹훅 접근 감사 — 실패해도 통화 처리를 막지 않는다(가용성 우선)."""
    if _audit is None:
        return None
    try:
        return _audit.record_request(headers, path, method, result, status, **extra)
    except Exception:
        return None


def _audit_result(status):
    """상태코드 → 감사 결과 라벨. 판정은 `_audit.result_for` 한 곳에만 둔다."""
    if _audit is None:
        return "error"
    try:
        return _audit.result_for(status)
    except Exception:                      # pragma: no cover - 판정 장애 격리
        return "error"


def _status_of(env, default=500):
    """오류 봉투에서 실제 응답 상태를 읽는다(감사·집계용)."""
    try:
        return int((env or {}).get("status") or default)
    except Exception:                      # pragma: no cover - 봉투 모양 방어
        return default


def _outcome(h, method, status, **extra):
    """관리 감사 기록 — **결과가 확정된 뒤** 실제 상태로 남긴다.

    5차가 `ops_stats` 에서 고친 결함이 이 라우트에도 남아 있었다: 가드를
    통과한 직후 `allow/200` 을 적었으므로 입력오류(400)·과대본문(413)·
    내부오류(500)가 감사 스트림에서 전부 성공으로 보였다. 이 장부
    (`call.webhook`)는 **외부 시스템 연동 지점**의 근거다 — 캠페인 발신
    시도(`op=campaign`)가 어떻게 끝났는지가 특히 중요하다.

    핸들러 메서드가 아니라 모듈 함수다 — 회귀가 쓰는 대역 핸들러는 응답
    헬퍼만 빌려 쓰므로, 판정·기록을 메서드로 두면 대역에서 돌지 않는다.
    """
    _audit_ev(h.headers, h.path, method, _audit_result(status), status, **extra)


def _mark_op(h, op):
    """감사용 동작 라벨을 핸들러에 적어 둔다(예외 경로에서도 읽히게)."""
    try:
        h._audit_op = op
    except Exception:                      # pragma: no cover - 속성 설정이 막힌 대역
        pass


def _marked_op(h):
    return getattr(h, "_audit_op", None)


def _serve_get(h, q):
    """조회 동작 — 응답을 보내고 그 **상태코드**를 돌려준다(감사 기록용).

    상태는 `_send` 의 반환값에 기대지 않고 이 자리에서 분명히 적는다 —
    회귀의 대역 핸들러가 응답 헬퍼를 갈아끼울 수 있기 때문이다.
    """
    if q.get("op", [""])[0] == "log":
        h._send({"ok": True, "live": live(), "recent": RECENT,
                 "webhook": "/api/voice", "provider": CPAAS})
        return 200
    h._send({
        "ok": True, "endpoint": "voice-webhook", "provider": CPAAS,
        "live": live(), "engine": bool(run_turn), "stt": bool(transcribe),
        # 실측 집계(만든 수치가 아니라 이 인스턴스가 실제로 처리한 건수).
        # 거부·실패가 0 이 아니면 녹음 주소가 가드에 걸리고 있다는 뜻이다.
        "recording": dict(RECORDING_STATS),
        # 세션 보관 현황. evicted 가 0 이 아니면 동시 통화가 상한에
        # 닿았다는 뜻이고(상한을 올릴 판단 근거), expired 는 종료
        # 이벤트가 오지 않은 통화가 있다는 뜻이다.
        "sessions": _Session.stats(),
        "note": "CPaaS webhook adapter"})
    return 200


def _serve_post(h, rq):
    """웹훅·캠페인 처리 — (상태코드, 감사용 op) 를 돌려준다."""
    # 입력검증: 본문 상한(256KiB). CPaaS 이벤트·폼은 작다 — 과대 본문은 413.
    try:
        n = int((h.headers.get("Content-Length") or "0").strip() or "0")
    except Exception:
        raise _errors.ValidationError.field("content-length", "정수가 아닙니다")
    if n < 0:
        raise _errors.ValidationError.field("content-length", "음수입니다")
    if n > MAX_WEBHOOK_BODY:
        raise _errors.ValidationError(
            details=[{"field": "body", "reason": "최대 %d바이트" % MAX_WEBHOOK_BODY}],
            code="PAYLOAD_TOO_LARGE", status=413)
    raw = h.rfile.read(n) if n else b""
    ctype = (h.headers.get("Content-Type", "") or "").lower()
    # JSON 본문(콘솔의 캠페인 dry-run 등)은 CPaaS 가 twilio 여도 아래 JSON 경로로 보낸다
    if "x-www-form-urlencoded" in ctype or (CPAAS == "twilio" and "json" not in ctype):
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}
        h._send_xml(handle_twilio(form))
        return 200, None
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        raise _errors.ValidationError.field("body", "JSON 형식이 아닙니다")
    if not isinstance(body, dict):
        raise _errors.ValidationError.field("body", "JSON 객체여야 합니다")
    if body.get("op") == "campaign":
        rq.set(op="campaign")
        # 실패해도 감사에 `op` 가 남아야 한다(발신 시도는 추적 가치가 높다) —
        # 예외로 빠져나가면 반환값이 없으므로 핸들러에 적어 둔다.
        _mark_op(h, "campaign")
        try:
            plan = trigger_campaign(body.get("numbers"),
                                    body.get("scenario", "care"),
                                    body.get("meta"))
        except ValueError as e:
            # 입력 오류를 500 으로 보고하면 사용자 오타가 서비스 장애 알림으로
            # 울린다(6차가 chat 에서 고친 것과 같은 규약).
            raise _errors.ValidationError.field("campaign", str(e))
        h._send(plan)
        return 200, "campaign"
    ev = _parse_event(body)
    # 이벤트 종류만 남긴다 — 발화 원문·번호는 담지 않는다(PII).
    rq.set(ev=_label(ev.get("type"), _EVENTS))
    if ev.get("type") == "answered":
        _log_call({"from": ev.get("from", ""), "ev": "통화연결", "text": ""})
    elif ev.get("type") == "speech" and ev.get("text"):
        _log_call({"from": ev.get("from", ""), "ev": "고객발화", "text": ev.get("text")})
    h._send(handle_event(ev))
    return 200, None


# 로그 보조필드는 **아는 값만** 남긴다. `op`·`type` 은 외부(CPaaS·콘솔)가 보내는
# 값이라 그대로 담으면 임의 문자열이 로그에 섞이고 집계 카디널리티가 터진다.
_OPS = ("log", "campaign")
_EVENTS = ("answered", "speech", "completed")


def _label(value, known):
    v = value.strip().lower() if isinstance(value, str) else ""
    if not v:
        return None
    return v if v in known else "other"


def _begin(h, method):
    """요청 1건 = 구조화 로그 1줄.

    `h._rq` 로 걸어 두면 `_guard.deny`·`_errors.handle` 이 request_id 를 승계해
    거부(401/403/429)와 오류까지 한 줄씩 남는다(조용한 실패 금지).
    경로는 `_log.safe_path` 가 쿼리를 잘라내므로 웹훅 인증 `?t=<CPAAS_WEBHOOK_TOKEN>`
    은 로그에 남지 않는다.
    """
    rq = _log.begin(h.headers, "/api/voice", method, h.path)
    try:
        h._rq = rq
    except Exception:          # pragma: no cover - 속성 설정이 막힌 대역
        pass
    return rq


def _close(rq, code, **extra):
    """구조화 로그를 닫는다. 로깅 실패가 통화 처리를 죽이지 않는다."""
    try:
        if rq is not None:
            rq.finish(code, **extra)
    except Exception:
        pass


class handler(BaseHTTPRequestHandler):
    # 기본 접근로그는 요청라인을 그대로 찍는다 — CPaaS 웹훅은 인증을
    # `?t=<CPAAS_WEBHOOK_TOKEN>` 으로 받으므로 그 토큰이 로그에 남는다.
    log_message = _log.suppress_access_log

    def _send(self, obj, code=200):
        """응답을 보내고 **그 상태코드를 돌려준다**(감사 기록이 응답과 같은 말을
        하려면 호출부가 '무엇으로 끝났는지'를 알아야 한다 · 42차)."""
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        rq = getattr(self, "_rq", None)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        _log.attach(self, rq)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        # X-Request-Id 는 CORS 안전목록에 없다 — 노출하지 않으면 허용된 다른
        # 오리진에서 사용자 신고와 로그를 맞출 수 없다(오류 응답과 같은 규약).
        self.send_header("Access-Control-Expose-Headers", _errors.EXPOSE_HEADERS)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
        _close(rq, code)
        return code

    def _send_xml(self, xml, code=200):
        b = xml.encode("utf-8")
        rq = getattr(self, "_rq", None)
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        _log.attach(self, rq)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
        _close(rq, code, kind="voiceml")
        return code

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Webhook-Token")
        self.end_headers()

    def do_GET(self):
        rq = _begin(self, "GET")
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=True)
        if not _ok:
            _audit_ev(self.headers, self.path, "GET", "deny", _c)
            return _guard.deny(self, _c, _m, rq)
        op = None
        try:
            q = parse_qs(urlparse(self.path).query)
            op = _label(q.get("op", [""])[0], _OPS)
            rq.set(op=op)
            status = _serve_get(self, q)
        except Exception as e:
            # 조회 경로도 표준 봉투로 끝낸다 — 예외가 핸들러를 뚫고 나가면
            # 응답도 감사 기록도 남지 않는다(조용한 실패 금지).
            status = _status_of(_errors.handle(self, e, route="/api/voice",
                                               method="GET", rq=rq))
        _outcome(self, "GET", status, op=op)

    def do_POST(self):
        rq = _begin(self, "POST")
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=True)
        if not _ok:
            _audit_ev(self.headers, self.path, "POST", "deny", _c)
            return _guard.deny(self, _c, _m, rq)
        _mark_op(self, None)
        op = None
        try:
            status, op = _serve_post(self, rq)
        except Exception as e:
            # 표준 에러 봉투 — 웹훅 응답도 같은 규약을 따른다(내부 문구 미노출)
            status = _status_of(_errors.handle(self, e, route="/api/voice",
                                               method="POST", rq=rq))
            op = _marked_op(self)
        # 감사는 결과가 확정된 뒤에 — 400·413·500 을 성공으로 적지 않는다.
        _outcome(self, "POST", status, op=op, live=live())
