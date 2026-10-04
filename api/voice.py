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

try:
    from _engine import run_turn
except Exception:
    run_turn = None
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
LIVE = os.environ.get("CPAAS_LIVE", "0") == "1"


class _Session:
    _mem = {}

    @classmethod
    def get(cls, cid):
        return cls._mem.get(cid)

    @classmethod
    def put(cls, cid, data):
        cls._mem[cid] = data

    @classmethod
    def drop(cls, cid):
        cls._mem.pop(cid, None)


RECENT = []


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


def _log_call(entry):
    entry["t"] = _now()
    if "from" in entry:
        entry["from"] = _mask_phone(entry.get("from"))
    RECENT.insert(0, entry)
    del RECENT[60:]


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
        r = run_turn(sess["messages"], phone=frm, scenario=sess["scenario"])
        sess["messages"] = r["messages"]
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
            r = run_turn(sess["messages"], phone=sess.get("phone", ""), scenario=sess.get("scenario", "refund"))
            sess["messages"] = r["messages"]
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
        return {"ok": True, "summary": result}
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


def _persist_call_result(cid, sess, result):
    try:
        print("[voice] call done", cid, json.dumps(result, ensure_ascii=False))
    except Exception:
        pass


def trigger_campaign(numbers, scenario="care", meta=None):
    meta = meta or {}
    calls = [{"to": n, "from": os.environ.get("CALLBOT_CALLER_ID", "070-0000-0000"),
              "webhook": "/api/voice", "metadata": {"scenario": scenario, **meta},
              "consent_required": True} for n in numbers]
    if not LIVE:
        return {"mode": "dry-run(무과금)", "queued": 0, "would_call": len(calls), "calls": calls}
    return {"mode": "live", "queued": len(calls), "calls": calls}


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
        _audit_ev(self.headers, self.path, "GET", "allow", 200)
        q = parse_qs(urlparse(self.path).query)
        rq.set(op=_label(q.get("op", [""])[0], _OPS))
        if q.get("op", [""])[0] == "log":
            self._send({"ok": True, "live": LIVE, "recent": RECENT, "webhook": "/api/voice", "provider": CPAAS})
            return
        self._send({"ok": True, "endpoint": "voice-webhook", "provider": CPAAS,
                    "live": LIVE, "engine": bool(run_turn), "stt": bool(transcribe),
                    # 실측 집계(만든 수치가 아니라 이 인스턴스가 실제로 처리한 건수).
                    # 거부·실패가 0 이 아니면 녹음 주소가 가드에 걸리고 있다는 뜻이다.
                    "recording": dict(RECORDING_STATS),
                    "note": "CPaaS webhook adapter"})

    def do_POST(self):
        rq = _begin(self, "POST")
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=True)
        if not _ok:
            _audit_ev(self.headers, self.path, "POST", "deny", _c)
            return _guard.deny(self, _c, _m, rq)
        _audit_ev(self.headers, self.path, "POST", "allow", 200, live=LIVE)
        try:
            # 입력검증: 본문 상한(256KiB). CPaaS 이벤트·폼은 작다 — 과대 본문은 413.
            try:
                n = int((self.headers.get("Content-Length") or "0").strip() or "0")
            except Exception:
                raise _errors.ValidationError.field("content-length", "정수가 아닙니다")
            if n < 0:
                raise _errors.ValidationError.field("content-length", "음수입니다")
            if n > MAX_WEBHOOK_BODY:
                raise _errors.ValidationError(
                    details=[{"field": "body", "reason": "최대 %d바이트" % MAX_WEBHOOK_BODY}],
                    code="PAYLOAD_TOO_LARGE", status=413)
            raw = self.rfile.read(n) if n else b""
            ctype = (self.headers.get("Content-Type", "") or "").lower()
            if "x-www-form-urlencoded" in ctype or CPAAS == "twilio":
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}
                self._send_xml(handle_twilio(form))
                return
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                raise _errors.ValidationError.field("body", "JSON 형식이 아닙니다")
            if not isinstance(body, dict):
                raise _errors.ValidationError.field("body", "JSON 객체여야 합니다")
            if body.get("op") == "campaign":
                rq.set(op="campaign")
                self._send(trigger_campaign(body.get("numbers", []), body.get("scenario", "care"), body.get("meta")))
                return
            ev = _parse_event(body)
            # 이벤트 종류만 남긴다 — 발화 원문·번호는 담지 않는다(PII).
            rq.set(ev=_label(ev.get("type"), _EVENTS))
            if ev.get("type") == "answered":
                _log_call({"from": ev.get("from", ""), "ev": "통화연결", "text": ""})
            elif ev.get("type") == "speech" and ev.get("text"):
                _log_call({"from": ev.get("from", ""), "ev": "고객발화", "text": ev.get("text")})
            self._send(handle_event(ev))
        except Exception as e:
            # 표준 에러 봉투 — 웹훅 응답도 같은 규약을 따른다(내부 문구 미노출)
            _errors.handle(self, e, route="/api/voice", method="POST", rq=rq)
