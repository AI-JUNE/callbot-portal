"""CPaaS 콜 연계 웹훅 — 실제 전화망 <-> 기존 콜봇 두뇌 연결 어댑터.

회선/발신/녹음/재생 = CPaaS 위임(종량제), STT/LLM/시나리오/TTS = 자체 재사용.
- 무료 테스트: sim(텍스트 주입) · dry-run 캠페인 -> 통신비 0
- 실제 인바운드: Twilio(TwiML) 지원(자체 STT/TTS). 한국 070은 ClawOps 등 국내 CPaaS.
- 과금은 CPAAS_LIVE=1 + 실제 통화 발생 시에만.
"""
from __future__ import annotations
import os, sys, json, time
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

try:
    from engine import run_turn
except Exception:
    run_turn = None
try:
    from stt import transcribe
except Exception:
    transcribe = None
try:
    from assist import run_assist
except Exception:
    run_assist = None

CPAAS = os.environ.get("CPAAS_PROVIDER", "sim")
GREETING = os.environ.get("CALLBOT_GREETING", "안녕하세요, 콜봇 상담센터입니다. 무엇을 도와드릴까요?")
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


def _log_call(entry):
    entry["t"] = _now()
    RECENT.insert(0, entry)
    del RECENT[60:]


def _xesc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _twiml_gather(text):
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say language="ko-KR">%s</Say>'
            '<Gather input="speech" language="ko-KR" speechTimeout="auto" action="/api/voice" method="POST">'
            '<Say language="ko-KR">말씀해 주세요.</Say></Gather>'
            '<Redirect method="POST">/api/voice</Redirect></Response>') % _xesc(text)


def _twiml_hangup(text):
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say language="ko-KR">%s</Say><Hangup/></Response>') % _xesc(text)


def _twiml_dial(text, target):
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say language="ko-KR">%s</Say><Dial>%s</Dial></Response>') % (_xesc(text), _xesc(target))


def handle_twilio(p):
    cid = p.get("CallSid", "tw-call")
    frm = p.get("From", "")
    status = p.get("CallStatus", "")
    speech = (p.get("SpeechResult", "") or "").strip()
    scenario = os.environ.get("CALLBOT_DEFAULT_SCENARIO", "refund")
    if status in ("completed", "canceled", "busy", "no-answer", "failed"):
        _Session.drop(cid)
        _log_call({"from": frm, "ev": "통화종료", "text": ""})
        return _twiml_hangup("이용해 주셔서 감사합니다.")
    sess = _Session.get(cid)
    if not sess:
        sess = {"messages": [], "scenario": scenario, "phone": frm}
        _Session.put(cid, sess)
        _log_call({"from": frm, "ev": "통화연결", "text": ""})
    if not speech:
        return _twiml_gather(GREETING)
    _log_call({"from": frm, "ev": "고객발화", "text": speech})
    if run_turn:
        sess["messages"].append({"role": "user", "content": speech})
        r = run_turn(sess["messages"], phone=frm, scenario=sess["scenario"])
        sess["messages"] = r["messages"]
        _Session.put(cid, sess)
        _log_call({"from": frm, "ev": "봇응답", "text": r["reply"]})
        if r.get("transferred"):
            return _twiml_dial("상담사에게 연결해 드리겠습니다.", AGENT_SIP)
        return _twiml_gather(r["reply"])
    return _twiml_hangup("현재 점검 중입니다. 잠시 후 다시 시도해 주세요.")


def _parse_event(body):
    if CPAAS == "clawops":
        et = body.get("event")
        typ = {"call.answered": "answered", "call.recording": "speech",
               "call.completed": "completed"}.get(et, et)
        return {"type": typ, "call_id": body.get("callId") or body.get("call_id"),
                "from": body.get("from"), "to": body.get("to"),
                "scenario": (body.get("metadata") or {}).get("scenario", "refund"),
                "audio_b64": body.get("audio"), "mime": body.get("mime", "audio/wav"),
                "recording_url": body.get("recordingUrl")}
    return {"type": body.get("type", "answered"), "call_id": body.get("call_id", "demo-call"),
            "from": body.get("from", "01000000000"), "to": body.get("to", ""),
            "scenario": body.get("scenario", "refund"),
            "audio_b64": body.get("audio_b64"), "mime": body.get("mime", "audio/webm"),
            "recording_url": body.get("recording_url"), "text": body.get("text")}


def _act_say_then_listen(text):
    return {"actions": [{"action": "say", "text": text, "tts": "self"},
                        {"action": "record", "endpoint": "/api/voice", "vad": True, "maxSeconds": 15}]}


def _act_transfer():
    return {"actions": [{"action": "say", "text": "상담사에게 연결해 드리겠습니다. 잠시만 기다려 주세요."},
                        {"action": "dial", "sip": AGENT_SIP}]}


def handle_event(ev):
    cid = ev["call_id"]
    if ev["type"] == "answered":
        _Session.put(cid, {"messages": [], "scenario": ev["scenario"], "phone": ev["from"], "started": time.time()})
        return _act_say_then_listen(GREETING)
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
            if r.get("transferred"):
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
        _persist_call_result(cid, sess, result)
        _Session.drop(cid)
        return {"ok": True, "summary": result}
    return {"ok": True, "ignored": ev["type"]}


def _transcribe_url(url):
    try:
        import urllib.request, base64
        with urllib.request.urlopen(url, timeout=20) as resp:
            audio = resp.read()
        if transcribe:
            return transcribe(base64.b64encode(audio).decode(), "audio/wav")
    except Exception:
        pass
    return ""


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


class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_xml(self, xml, code=200):
        b = xml.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("op", [""])[0] == "log":
            self._send({"ok": True, "live": LIVE, "recent": RECENT, "webhook": "/api/voice", "provider": CPAAS})
            return
        self._send({"ok": True, "endpoint": "voice-webhook", "provider": CPAAS,
                    "live": LIVE, "engine": bool(run_turn), "stt": bool(transcribe),
                    "note": "CPaaS webhook adapter"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n else b""
            ctype = (self.headers.get("Content-Type", "") or "").lower()
            if "x-www-form-urlencoded" in ctype or CPAAS == "twilio":
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "ignore")).items()}
                self._send_xml(handle_twilio(form))
                return
            body = json.loads(raw or b"{}")
            if body.get("op") == "campaign":
                self._send(trigger_campaign(body.get("numbers", []), body.get("scenario", "care"), body.get("meta")))
                return
            ev = _parse_event(body)
            if ev.get("type") == "answered":
                _log_call({"from": ev.get("from", ""), "ev": "통화연결", "text": ""})
            elif ev.get("type") == "speech" and ev.get("text"):
                _log_call({"from": ev.get("from", ""), "ev": "고객발화", "text": ev.get("text")})
            self._send(handle_event(ev))
        except Exception as e:
            self._send({"error": str(e)}, 500)
