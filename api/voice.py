"""CPaaS 콜 연계 웹훅 (스켈레톤) — 실제 전화망 <-> 기존 콜봇 두뇌 연결 어댑터.

설계 원칙 (가성비):
  - 회선/발신/녹음/재생 = CPaaS(ClawOps·Twilio류)에 위임 (capex 0, opex 종량제)
  - STT/LLM/시나리오/TTS = 기존 자체 스택 그대로 재사용 (stt.transcribe, engine.run_turn, tts._synth)
  => 이 파일은 "전화 이벤트"를 기존 웹 음성 루프와 1:1로 이어주는 얇은 어댑터일 뿐이다.

연계 방식 (A) 턴제 음성 — MVP 권장:
  통화연결 -> (인사 재생) -> 발화 녹음 -> /stt -> run_turn -> 답변 -> 재생 -> 다시 녹음 ...
  -> run_turn 이 transferred=True 면 상담사 SIP 호전환.

주의: Vercel 서버리스는 상태가 없으므로, 실제 운영에서는 SESSION 저장소를
      Redis/Upstash/KV 등 외부 스토어로 교체해야 한다(아래 _Session 참고).

각 CPaaS 마다 웹훅 payload/응답 스키마가 다르다. provider 별 매핑은
  _parse_event() 와 _actions_*() 어댑터 함수만 바꾸면 되도록 격리했다.
"""
from __future__ import annotations
import os, sys, json, time
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

# 기존 자체 스택 재사용 (import 실패해도 빌드가 죽지 않도록 지연/보호)
try:
    from engine import run_turn            # LLM + 시나리오 + 툴 + 상담사전환
except Exception:
    run_turn = None
try:
    from stt import transcribe             # 오디오 -> 텍스트 (Gemini)
except Exception:
    transcribe = None
try:
    from assist import run_assist          # 통화 후 요약/TA/QA
except Exception:
    run_assist = None

CPAAS = os.environ.get("CPAAS_PROVIDER", "sim")       # sim | generic | clawops | twilio
GREETING = os.environ.get("CALLBOT_GREETING", "안녕하세요, 콜봇 상담센터입니다. 무엇을 도와드릴까요?")
AGENT_SIP = os.environ.get("CALLBOT_AGENT_SIP", "sip:agent@pbx.local")   # 호전환 대상

# ★ 비용 안전장치: 실제 통화(발신)는 CPAAS_LIVE=1 일 때만 나간다.
#   기본값(미설정)에서는 발신 API를 호출하지 않고 payload만 반환하는 dry-run → 통신비 0원.
#   즉, 배포/연계 테스트 단계에서는 절대 과금되지 않는다. 라이브 전환 시에만 켠다.
LIVE = os.environ.get("CPAAS_LIVE", "0") == "1"


# ────────────────────────── 세션 저장 (운영시 외부 KV로 교체) ──────────────────────────
class _Session:
    """call_id -> {messages, scenario, phone, started}. 데모용 in-memory.
    운영: Upstash Redis 등으로 교체 (서버리스 콜드스타트 시 유실 방지)."""
    _mem: dict = {}

    @classmethod
    def get(cls, cid):
        return cls._mem.get(cid)

    @classmethod
    def put(cls, cid, data):
        cls._mem[cid] = data

    @classmethod
    def drop(cls, cid):
        cls._mem.pop(cid, None)


# ────────────────────────── CPaaS 이벤트 파싱 (provider 어댑터) ──────────────────────────
def _parse_event(body: dict) -> dict:
    """CPaaS 웹훅 payload -> 표준 이벤트로 정규화.
    표준 이벤트: {type, call_id, from, to, scenario, audio_b64, mime, recording_url}
      type ∈ {answered, speech, completed}
    ↓ provider 별 필드명만 여기서 매핑하면 나머지 로직은 공통.
    """
    if CPAAS == "clawops":
        # 예시(문서 확인 후 확정): ClawOps 콜 이벤트 필드 매핑
        et = body.get("event")
        typ = {"call.answered": "answered", "call.recording": "speech",
               "call.completed": "completed"}.get(et, et)
        return {
            "type": typ,
            "call_id": body.get("callId") or body.get("call_id"),
            "from": body.get("from"), "to": body.get("to"),
            "scenario": (body.get("metadata") or {}).get("scenario", "refund"),
            "audio_b64": body.get("audio"), "mime": body.get("mime", "audio/wav"),
            "recording_url": body.get("recordingUrl"),
        }
    # generic (자체 게이트웨이/테스트용) — 이미 표준 스키마로 보낸다고 가정
    return {
        "type": body.get("type", "answered"),
        "call_id": body.get("call_id", "demo-call"),
        "from": body.get("from", "01000000000"),
        "to": body.get("to", ""),
        "scenario": body.get("scenario", "refund"),
        "audio_b64": body.get("audio_b64"), "mime": body.get("mime", "audio/webm"),
        "recording_url": body.get("recording_url"),
        # sim/무료 테스트: 이미 텍스트로 전사된 발화를 직접 주입(오디오·STT 없이 전 구간 검증)
        "text": body.get("text"),
    }


# ────────────────────────── CPaaS 액션 응답 (provider 어댑터) ──────────────────────────
def _act_say_then_listen(text: str) -> dict:
    """봇 발화 재생 후, 고객 발화를 다시 녹음(듣기)하라는 지시.
    generic 스키마. provider 별로 TwiML/JSON 등으로 변환해 반환하면 됨."""
    return {"actions": [
        {"action": "say", "text": text, "tts": "self"},   # tts=self: 우리 /api/tts 사용(또는 CPaaS 내장)
        {"action": "record", "endpoint": "/api/voice", "vad": True, "maxSeconds": 15},
    ]}


def _act_transfer() -> dict:
    return {"actions": [
        {"action": "say", "text": "상담사에게 연결해 드리겠습니다. 잠시만 기다려 주세요."},
        {"action": "dial", "sip": AGENT_SIP},
    ]}


def _act_hangup(text: str = "이용해 주셔서 감사합니다.") -> dict:
    return {"actions": [{"action": "say", "text": text}, {"action": "hangup"}]}


# ────────────────────────── 핵심: 전화 한 턴 처리 (웹 루프 재사용) ──────────────────────────
def handle_event(ev: dict) -> dict:
    cid = ev["call_id"]

    # 1) 통화 연결 -> 세션 생성 + 인사
    if ev["type"] == "answered":
        _Session.put(cid, {"messages": [], "scenario": ev["scenario"],
                           "phone": ev["from"], "started": time.time()})
        return _act_say_then_listen(GREETING)

    # 2) 고객 발화 도착 -> STT -> run_turn -> 답변
    if ev["type"] == "speech":
        sess = _Session.get(cid) or {"messages": [], "scenario": ev["scenario"], "phone": ev["from"]}

        # (a) 오디오 -> 텍스트 : 기존 stt 재사용 (또는 sim 텍스트 주입)
        user_text = ev.get("text") or ""          # sim/무료 테스트 경로: 텔레포니·과금 없음
        if not user_text and ev.get("audio_b64") and transcribe:
            user_text = transcribe(ev["audio_b64"], ev.get("mime", "audio/webm"))
        elif not user_text and ev.get("recording_url"):
            # CPaaS가 URL만 주는 경우: 다운로드 후 transcribe (운영시 구현)
            user_text = _transcribe_url(ev["recording_url"])

        if not user_text:
            return _act_say_then_listen("죄송합니다, 잘 못 들었어요. 다시 말씀해 주시겠어요?")

        # (b) 대화 진행 : 기존 run_turn 재사용 (시나리오·툴·상담사전환 포함)
        sess["messages"].append({"role": "user", "content": user_text})
        if run_turn:
            r = run_turn(sess["messages"], phone=sess.get("phone", ""), scenario=sess.get("scenario", "refund"))
            sess["messages"] = r["messages"]
            _Session.put(cid, sess)
            if r.get("transferred"):
                return _act_transfer()            # 상담사 실제 호전환(SIP)
            return _act_say_then_listen(r["reply"])
        return _act_say_then_listen("현재 응대 엔진 점검 중입니다. 상담사에게 연결해 드릴게요.")

    # 3) 통화 종료 -> 후처리(요약/TA/QA) -> 통계/사유코드 연계 (Phase 2)
    if ev["type"] == "completed":
        sess = _Session.get(cid)
        result = None
        if sess and run_assist:
            convo = "\n".join(f'{m["role"]}: {m["content"]}' for m in sess.get("messages", []))
            try:
                result = run_assist("summary", convo)   # {summary, points, action}
            except Exception:
                result = None
        _persist_call_result(cid, sess, result)         # TODO: 대시보드/통계/사유코드 저장 연계
        _Session.drop(cid)
        return {"ok": True, "summary": result}

    return {"ok": True, "ignored": ev["type"]}


def _transcribe_url(url: str) -> str:
    """CPaaS 녹음 URL 다운로드 -> base64 -> transcribe. 운영시 구현."""
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
    """통화결과를 기존 통계/사유코드/실적보고로 연계하는 훅 (Phase 2, 특허 ③).
    운영: DB/시트에 세션ID·번호(마스킹)·시나리오·요약·사유코드 저장 -> 대시보드 집계."""
    # TODO: 저장소 연동. 지금은 로깅만.
    try:
        print("[voice] call done", cid, json.dumps(result, ensure_ascii=False))
    except Exception:
        pass


# ────────────────────────── 아웃바운드 캠페인 트리거 (Phase 2, 발굴·안부) ──────────────────────────
def trigger_campaign(numbers, scenario="care", meta=None):
    """번호 리스트로 일괄 발신 요청을 생성 -> CPaaS 발신 API로 전송.
    각 통화가 연결되면 위 handle_event(answered) 루프로 들어온다.
    실제 발신은 CPaaS SDK 호출로 대체 (아래는 요청 페이로드 생성까지)."""
    meta = meta or {}
    calls = [{"to": n, "from": os.environ.get("CALLBOT_CALLER_ID", "070-0000-0000"),
              "webhook": "/api/voice", "metadata": {"scenario": scenario, **meta},
              # 규제 준수: 수신거부(DNC)·사전동의 확인 후에만 발신
              "consent_required": True}
             for n in numbers]
    if not LIVE:
        # ★ dry-run: 실제 발신 안 함 → 과금 0. 생성된 발신 페이로드만 확인용으로 반환.
        return {"mode": "dry-run(무과금)", "queued": 0, "would_call": len(calls), "calls": calls}
    # ↓ CPAAS_LIVE=1 일 때만 실제 발신 (여기서부터 종량 과금 발생)
    # TODO: CPaaS 발신 API 호출 (ClawOps: POST /calls / Twilio: POST /Calls 등)
    return {"mode": "live", "queued": len(calls), "calls": calls}


# ────────────────────────── HTTP 핸들러 ──────────────────────────
class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self._send({"ok": True, "endpoint": "voice-webhook", "provider": CPAAS,
                    "engine": bool(run_turn), "stt": bool(transcribe),
                    "note": "CPaaS 통화 이벤트를 POST 로 받아 기존 stt/run_turn/tts 로 처리하는 어댑터"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or "{}")
            # 캠페인 트리거: {"op":"campaign","numbers":[...],"scenario":"care"}
            if body.get("op") == "campaign":
                self._send(trigger_campaign(body.get("numbers", []),
                                            body.get("scenario", "care"),
                                            body.get("meta")))
                return
            ev = _parse_event(body)
            self._send(handle_event(ev))
        except Exception as e:
            self._send({"error": str(e)}, 500)
