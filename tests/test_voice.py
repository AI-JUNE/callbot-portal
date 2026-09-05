# -*- coding: utf-8 -*-
"""CPaaS 음성 웹훅(api/voice.py) 회귀 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' 항목의 다음 순서 = voice)

  1) 인증        — 웹훅 토큰(헤더·?t=), 미설정/오토큰 거부
  2) 입력검증    — 본문 상한 413, 잘못된 JSON 400, 비객체 400, Content-Length 이상
  3) 이벤트 파싱 — clawops 매핑 · 기본(sim) 매핑 · metadata 오염 내성
  4) 상태 전이   — answered/speech/completed/미지원, 세션 생성·유지·파기
  5) 중복 이벤트 — answered 재배달이 대화를 지우지 않을 것,
                   같은 녹음 재배달이 STT·LLM·도구를 두 번 실행하지 않을 것
  6) VoiceML    — 인사/재청취/전환/종료 분기, XML 이스케이프(속성 주입 포함)
  7) 안전        — CPAAS_LIVE 기본 OFF(캠페인 dry-run), 통화 로그에 원문 번호 미기록,
                   네트워크 호출 없음(urlopen 감시 대역)

LLM·STT·CPaaS 는 호출하지 않는다. run_turn·transcribe·urlopen 을 대역으로 바꾸고,
대역이 풀린 채 네트워크를 타면 즉시 실패하도록 감시한다.

실행: python3 -m pytest tests/test_voice.py  또는  python3 tests/test_voice.py
"""
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import voice  # noqa: E402
import _ratelimit  # noqa: E402

try:
    import _audit  # noqa: E402
except Exception:  # pragma: no cover
    _audit = None


# --------------------------------------------------------------------------
# BaseHTTPRequestHandler 대역 — 네트워크 없이 응답을 수집한다.
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class _Reader(object):
    def __init__(self, body):
        self.body = body

    def read(self, n):
        return self.body[:n]


class FakeHandler(object):
    def __init__(self, headers=None, body=b"", path="/api/voice"):
        self.headers = FakeHeaders(headers or {})
        self.path = path
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()
        self.rfile = _Reader(body)

    # 실제 핸들러의 응답 헬퍼를 그대로 빌려 쓴다(응답 규약까지 검증하기 위해).
    _send = voice.handler._send
    _send_xml = voice.handler._send_xml

    # BaseHTTPRequestHandler 인터페이스
    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    # 편의
    def header(self, k):
        for a, b in self.sent:
            if a.lower() == k.lower():
                return b
        return None

    def text(self):
        return self.wfile.data.decode("utf-8")

    def json(self):
        return json.loads(self.text())


def post(body, headers=None, path="/api/voice", raw=None):
    """JSON 웹훅 POST 를 실행하고 응답 핸들러를 돌려준다."""
    if raw is None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = {"content-length": str(len(raw)), "content-type": "application/json"}
    h.update({k.lower(): v for k, v in (headers or {}).items()})
    fh = FakeHandler(h, raw, path)
    voice.handler.do_POST(fh)
    return fh


def form_post(fields, headers=None, path="/api/voice"):
    """CPaaS 폼(TwiML/VoiceML) POST."""
    from urllib.parse import urlencode
    raw = urlencode(fields).encode("utf-8")
    h = {"content-length": str(len(raw)),
         "content-type": "application/x-www-form-urlencoded"}
    h.update({k.lower(): v for k, v in (headers or {}).items()})
    fh = FakeHandler(h, raw, path)
    voice.handler.do_POST(fh)
    return fh


def get(path="/api/voice", headers=None):
    fh = FakeHandler({k.lower(): v for k, v in (headers or {}).items()}, b"", path)
    voice.handler.do_GET(fh)
    return fh


SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


class VoiceBase(unittest.TestCase):
    """공통 준비 — 세션·로그·요청제한 초기화, 네트워크 차단, 환경변수 복원."""

    def setUp(self):
        voice._Session._mem = {}
        del voice.RECENT[:]
        _ratelimit.reset()
        if _audit is not None:
            _audit.reset()
        self._env = dict(os.environ)
        os.environ["CALLBOT_LOG"] = "off"   # 이 테스트 동안만 stdout 소음 제거(tearDown 에서 복원)
        self._mod = {k: getattr(voice, k) for k in
                     ("CPAAS", "LIVE", "run_turn", "transcribe", "run_assist")}
        # 네트워크 감시: 테스트가 실제로 밖으로 나가면 즉시 실패한다.
        self._urlopen = urllib.request.urlopen
        self.network_calls = []

        def blocked(*a, **k):
            self.network_calls.append(a[0] if a else None)
            raise AssertionError("테스트가 네트워크를 호출했다: %r" % (a[:1],))

        urllib.request.urlopen = blocked

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._mod.items():
            setattr(voice, k, v)
        os.environ.clear()
        os.environ.update(self._env)
        voice._Session._mem = {}
        del voice.RECENT[:]
        _ratelimit.reset()

    # 대역 설치 헬퍼 -------------------------------------------------------
    def stub_engine(self, reply="네, 도와드릴게요.", transferred=False):
        calls = []

        def run_turn(messages, phone="", scenario="refund"):
            calls.append({"messages": list(messages), "phone": phone, "scenario": scenario})
            msgs = list(messages) + [{"role": "assistant", "content": reply}]
            return {"messages": msgs, "reply": reply, "transferred": transferred}

        voice.run_turn = run_turn
        return calls

    def stub_stt(self, text="환불하고 싶어요"):
        calls = []

        def transcribe(audio_b64, mime="audio/webm"):
            calls.append((audio_b64, mime))
            return text

        voice.transcribe = transcribe
        return calls

    def stub_transcribe_url(self, text="환불하고 싶어요"):
        """_transcribe_url 자체를 대역으로 — 녹음 URL 다운로드를 하지 않는다."""
        calls = []
        orig = voice._transcribe_url

        def fake(url):
            calls.append(url)
            return text

        voice._transcribe_url = fake
        self.addCleanup(lambda: setattr(voice, "_transcribe_url", orig))
        return calls


# ==========================================================================
# 1) 인증 — 웹훅 토큰
# ==========================================================================
class TestWebhookAuth(VoiceBase):
    def test_no_origin_no_token_denied(self):
        """토큰 미설정 + 브라우저 신호 없음 = 401. 무인증 공개가 아니다."""
        os.environ.pop("CPAAS_WEBHOOK_TOKEN", None)
        os.environ.pop("CALLBOT_API_KEY", None)
        r = post({"type": "answered"})
        self.assertEqual(r.status, 401)

    def test_token_header_allows(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        r = post({"type": "answered"}, headers={"X-Webhook-Token": "tok-secret"})
        self.assertEqual(r.status, 200)

    def test_token_query_allows(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        r = post({"type": "answered"}, path="/api/voice?t=tok-secret")
        self.assertEqual(r.status, 200)

    def test_wrong_token_denied(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        r = post({"type": "answered"}, headers={"X-Webhook-Token": "nope"})
        self.assertEqual(r.status, 401)

    def test_deny_body_has_no_secret(self):
        """거부 응답에 토큰·설정값이 새지 않는다."""
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        r = post({"type": "answered"}, headers={"X-Webhook-Token": "nope"})
        self.assertNotIn("tok-secret", r.text())

    def test_same_origin_console_allowed(self):
        """콘솔(동일 오리진)에서 온 상태 조회는 통과한다."""
        r = get("/api/voice", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.json()["ok"])

    def test_deny_is_audited(self):
        if _audit is None:
            self.skipTest("_audit 없음")
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        post({"type": "answered"}, headers={"X-Webhook-Token": "nope"})
        results = [e.get("result") for e in _audit.recent(10)]
        self.assertIn("deny", results)


# ==========================================================================
# 2) 입력검증
# ==========================================================================
class TestInputValidation(VoiceBase):
    def test_oversized_body_413(self):
        fh = FakeHandler({"content-length": str(voice.MAX_WEBHOOK_BODY + 1),
                          "content-type": "application/json",
                          "sec-fetch-site": "same-origin"}, b"{}", "/api/voice")
        voice.handler.do_POST(fh)
        self.assertEqual(fh.status, 413)

    def test_bad_json_400(self):
        r = post(None, headers=SAME_ORIGIN, raw=b"{not json")
        self.assertEqual(r.status, 400)

    def test_non_object_body_400(self):
        r = post(None, headers=SAME_ORIGIN, raw=b"[1,2,3]")
        self.assertEqual(r.status, 400)

    def test_bad_content_length_400(self):
        fh = FakeHandler({"content-length": "abc", "sec-fetch-site": "same-origin"},
                         b"", "/api/voice")
        voice.handler.do_POST(fh)
        self.assertEqual(fh.status, 400)

    def test_empty_body_treated_as_answered(self):
        """본문이 비어도 500 이 아니다(기본 매핑)."""
        fh = FakeHandler({"content-length": "0", "sec-fetch-site": "same-origin"},
                         b"", "/api/voice")
        voice.handler.do_POST(fh)
        self.assertEqual(fh.status, 200)

    def test_error_envelope_shape(self):
        r = post(None, headers=SAME_ORIGIN, raw=b"{not json")
        b = r.json()
        self.assertFalse(b["ok"])
        for k in ("error", "code", "status"):
            self.assertIn(k, b)


# ==========================================================================
# 3) 이벤트 파싱
# ==========================================================================
class TestParseEvent(VoiceBase):
    def test_clawops_event_mapping(self):
        voice.CPAAS = "clawops"
        ev = voice._parse_event({"event": "call.answered", "callId": "c1",
                                 "from": "01011112222", "to": "07012345678",
                                 "metadata": {"scenario": "care"}})
        self.assertEqual(ev["type"], "answered")
        self.assertEqual(ev["call_id"], "c1")
        self.assertEqual(ev["scenario"], "care")

    def test_clawops_recording_is_speech(self):
        voice.CPAAS = "clawops"
        ev = voice._parse_event({"event": "call.recording", "callId": "c1",
                                 "recordingUrl": "https://cdn/x.wav"})
        self.assertEqual(ev["type"], "speech")
        self.assertEqual(ev["recording_url"], "https://cdn/x.wav")

    def test_clawops_completed(self):
        voice.CPAAS = "clawops"
        self.assertEqual(voice._parse_event({"event": "call.completed"})["type"], "completed")

    def test_clawops_unknown_event_passes_through(self):
        voice.CPAAS = "clawops"
        self.assertEqual(voice._parse_event({"event": "call.dtmf"})["type"], "call.dtmf")

    def test_clawops_metadata_not_dict_is_tolerated(self):
        """회귀: metadata 가 문자열·배열로 와도 500 이 되지 않는다."""
        voice.CPAAS = "clawops"
        for bad in ("oops", [1, 2], 7, None):
            ev = voice._parse_event({"event": "call.answered", "callId": "c",
                                     "metadata": bad})
            self.assertEqual(ev["scenario"], "refund")

    def test_default_mapping_defaults(self):
        voice.CPAAS = "sim"
        ev = voice._parse_event({})
        self.assertEqual(ev["type"], "answered")
        self.assertEqual(ev["scenario"], "refund")

    def test_default_mapping_text_passthrough(self):
        voice.CPAAS = "sim"
        ev = voice._parse_event({"type": "speech", "text": "환불이요", "call_id": "c9"})
        self.assertEqual((ev["type"], ev["text"], ev["call_id"]), ("speech", "환불이요", "c9"))


# ==========================================================================
# 4) 상태 전이 (JSON 이벤트 경로)
# ==========================================================================
class TestEventStateMachine(VoiceBase):
    def test_answered_creates_session_and_greets(self):
        out = voice.handle_event({"type": "answered", "call_id": "c1",
                                  "from": "01011112222", "scenario": "care"})
        self.assertEqual(out["actions"][0]["action"], "say")
        self.assertEqual(out["actions"][1]["action"], "record")
        self.assertIsNotNone(voice._Session.get("c1"))

    def test_speech_runs_turn_and_keeps_history(self):
        calls = self.stub_engine("환불 접수했어요.")
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                                  "scenario": "refund", "text": "환불이요"})
        self.assertEqual(out["actions"][0]["text"], "환불 접수했어요.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(voice._Session.get("c1")["messages"][-1]["content"], "환불 접수했어요.")

    def test_speech_uses_stt_for_audio(self):
        self.stub_engine()
        stt = self.stub_stt("녹음된 발화")
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                            "scenario": "refund", "audio_b64": "AAAA", "mime": "audio/webm"})
        self.assertEqual(len(stt), 1)
        self.assertEqual(voice._Session.get("c1")["messages"][0]["content"], "녹음된 발화")

    def test_speech_empty_reprompts_without_engine(self):
        calls = self.stub_engine()
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                                  "scenario": "refund", "text": ""})
        self.assertIn("다시 말씀해", out["actions"][0]["text"])
        self.assertEqual(calls, [])

    def test_transfer_action_on_transferred(self):
        self.stub_engine("상담사 연결", transferred=True)
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                                  "scenario": "refund", "text": "사람 바꿔줘"})
        self.assertEqual(out["actions"][-1]["action"], "dial")

    def test_engine_absent_is_graceful(self):
        voice.run_turn = None
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                                  "scenario": "refund", "text": "여보세요"})
        self.assertEqual(out["actions"][0]["action"], "say")

    def test_completed_drops_session(self):
        self.stub_engine()
        voice.run_assist = None
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        out = voice.handle_event({"type": "completed", "call_id": "c1"})
        self.assertTrue(out["ok"])
        self.assertIsNone(voice._Session.get("c1"))

    def test_completed_summary_failure_does_not_break_call(self):
        """요약(LLM)이 터져도 통화 종료 처리는 성공한다."""
        def boom(task, text):
            raise RuntimeError("llm down")

        voice.run_assist = boom
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        out = voice.handle_event({"type": "completed", "call_id": "c1"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["summary"])

    def test_unknown_type_ignored(self):
        out = voice.handle_event({"type": "dtmf", "call_id": "c1", "from": "010", "scenario": "refund"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["ignored"], "dtmf")

    def test_missing_call_id_does_not_crash(self):
        out = voice.handle_event({"type": "answered", "call_id": None,
                                  "from": "010", "scenario": "refund"})
        self.assertEqual(out["actions"][0]["action"], "say")


# ==========================================================================
# 5) 중복 이벤트 (웹훅 재시도 내성)
# ==========================================================================
class TestDuplicateEvents(VoiceBase):
    def test_duplicate_answered_keeps_history(self):
        """회귀: answered 재배달이 진행 중인 대화를 지우면 안 된다."""
        self.stub_engine("네 확인했습니다.")
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                            "scenario": "refund", "text": "주문 조회요"})
        before = len(voice._Session.get("c1")["messages"])
        self.assertTrue(before > 0)
        voice.handle_event({"type": "answered", "call_id": "c1", "from": "010", "scenario": "refund"})
        self.assertEqual(len(voice._Session.get("c1")["messages"]), before)

    def test_duplicate_recording_does_not_rerun_engine(self):
        """회귀: 같은 RecordingUrl 재배달이 STT·LLM·도구를 두 번 실행하면 안 된다.
        (환불 같은 도구가 중복 실행되면 실제 손해가 난다)"""
        calls = self.stub_engine("환불 접수했어요.")
        stt = self.stub_transcribe_url("환불해주세요")
        voice.handle_twilio({"CallId": "c1", "From": "01011112222", "CallStatus": "in-progress"})
        first = voice.handle_twilio({"CallId": "c1", "From": "01011112222",
                                     "RecordingUrl": "https://cdn/rec-1.wav",
                                     "RecordingDuration": "3"})
        second = voice.handle_twilio({"CallId": "c1", "From": "01011112222",
                                      "RecordingUrl": "https://cdn/rec-1.wav",
                                      "RecordingDuration": "3"})
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "엔진이 두 번 실행됐다")
        self.assertEqual(len(stt), 1, "STT 가 두 번 실행됐다")

    def test_new_recording_after_duplicate_is_processed(self):
        """중복 방지가 정상 후속 발화까지 막지는 않는다."""
        calls = self.stub_engine("네.")
        self.stub_transcribe_url("들려요")
        voice.handle_twilio({"CallId": "c1", "From": "010", "CallStatus": "in-progress"})
        voice.handle_twilio({"CallId": "c1", "From": "010",
                             "RecordingUrl": "https://cdn/1.wav", "RecordingDuration": "3"})
        voice.handle_twilio({"CallId": "c1", "From": "010",
                             "RecordingUrl": "https://cdn/1.wav", "RecordingDuration": "3"})
        voice.handle_twilio({"CallId": "c1", "From": "010",
                             "RecordingUrl": "https://cdn/2.wav", "RecordingDuration": "3"})
        self.assertEqual(len(calls), 2)


# ==========================================================================
# 6) VoiceML(TwiML) 분기와 이스케이프
# ==========================================================================
class TestVoiceML(VoiceBase):
    def test_first_leg_greets_and_records(self):
        out = voice.handle_twilio({"CallId": "c1", "From": "01011112222", "CallStatus": "in-progress"})
        self.assertIn("<Say", out)
        self.assertIn("<Record", out)
        self.assertIn("action=", out)

    def test_call_end_hangs_up_and_drops_session(self):
        voice.handle_twilio({"CallId": "c1", "From": "010", "CallStatus": "in-progress"})
        for st in ("completed", "canceled", "busy", "no-answer", "failed"):
            voice._Session.put("c1", {"messages": [], "scenario": "refund", "phone": "010"})
            out = voice.handle_twilio({"CallId": "c1", "From": "010", "CallStatus": st})
            self.assertIn("<Hangup/>", out)
            self.assertIsNone(voice._Session.get("c1"))

    def test_dial_completed_ends_call(self):
        out = voice.handle_twilio({"CallId": "c1", "From": "010", "DialCallStatus": "completed"})
        self.assertIn("<Hangup/>", out)

    def test_dial_failed_returns_to_bot(self):
        out = voice.handle_twilio({"CallId": "c1", "From": "010", "DialCallStatus": "no-answer"})
        self.assertIn("<Record", out)

    def test_zero_length_recording_reprompts(self):
        self.stub_engine()
        for dur in ("", "0", "0.0"):
            out = voice.handle_twilio({"CallId": "c1", "From": "010",
                                       "RecordingUrl": "https://cdn/x.wav",
                                       "RecordingDuration": dur})
            self.assertIn("다시 말씀해", out)

    def test_empty_transcript_reprompts(self):
        calls = self.stub_engine()
        self.stub_transcribe_url("")
        out = voice.handle_twilio({"CallId": "c1", "From": "010",
                                   "RecordingUrl": "https://cdn/x.wav",
                                   "RecordingDuration": "4"})
        self.assertIn("다시 말씀해", out)
        self.assertEqual(calls, [])

    def test_transfer_dials_agent_phone(self):
        os.environ["CALLBOT_AGENT_PHONE"] = "07099998888"
        self.stub_engine("연결할게요", transferred=True)
        self.stub_transcribe_url("사람 바꿔줘")
        out = voice.handle_twilio({"CallId": "c1", "From": "010", "To": "07012345678",
                                   "RecordingUrl": "https://cdn/x.wav",
                                   "RecordingDuration": "4"})
        self.assertIn("<Dial", out)
        self.assertIn("07099998888", out)

    def test_transfer_without_agent_number_apologizes(self):
        os.environ.pop("CALLBOT_AGENT_PHONE", None)
        self.stub_engine("연결할게요", transferred=True)
        self.stub_transcribe_url("사람 바꿔줘")
        out = voice.handle_twilio({"CallId": "c1", "From": "010",
                                   "RecordingUrl": "https://cdn/x.wav",
                                   "RecordingDuration": "4"})
        self.assertNotIn("<Dial", out)
        self.assertIn("<Hangup/>", out)

    def test_engine_absent_says_maintenance(self):
        voice.run_turn = None
        self.stub_transcribe_url("여보세요")
        out = voice.handle_twilio({"CallId": "c1", "From": "010",
                                   "RecordingUrl": "https://cdn/x.wav",
                                   "RecordingDuration": "4"})
        self.assertIn("점검", out)

    def test_say_text_is_escaped(self):
        os.environ["CALLBOT_GREETING"] = "<b>안녕</b> & 반갑습니다"
        out = voice._say_then_record(os.environ["CALLBOT_GREETING"], "c1")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&amp;", out)

    def test_call_id_cannot_inject_xml_attribute(self):
        """회귀: CallId 는 외부 입력이다. 따옴표로 속성을 덧붙일 수 없어야 한다."""
        evil = 'c1" onhangup="evil'
        out = voice._say_then_record("안녕", evil)
        self.assertNotIn('onhangup="evil', out)
        self.assertNotIn('c1" ', out)

    def test_caller_id_cannot_inject_xml_attribute(self):
        """회귀: 발신번호(To) 도 외부 입력이다."""
        out = voice._say_then_dial("연결", "07011112222", 'x" bad="1', "c1")
        self.assertNotIn('bad="1"', out)
        self.assertIn("&quot;", out)

    def test_action_url_percent_encodes_query(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "t o&k"
        url = voice._action_url("c 1&x")
        self.assertNotIn(" ", url)
        self.assertIn("&amp;", url)          # XML 속성 안에서 & 는 &amp;
        self.assertNotIn("&t=", url)

    def test_action_url_without_token(self):
        os.environ.pop("CPAAS_WEBHOOK_TOKEN", None)
        url = voice._action_url("c1")
        self.assertTrue(url.endswith("?cid=c1"))

    def test_xml_is_well_formed(self):
        from xml.dom.minidom import parseString
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok"
        for xml in (voice._say_then_record('안녕 <hi> & "quote"', 'c"1'),
                    voice._say_then_hangup("감사합니다"),
                    voice._say_then_dial("연결", "070", 'a"b', "c1")):
            parseString(xml)   # 파싱 실패하면 예외


# ==========================================================================
# 7) 안전 — 과금·개인정보·네트워크
# ==========================================================================
class TestSafety(VoiceBase):
    def test_campaign_is_dry_run_by_default(self):
        voice.LIVE = False
        r = voice.trigger_campaign(["01011112222", "01033334444"], "care")
        self.assertEqual(r["queued"], 0)
        self.assertEqual(r["would_call"], 2)
        self.assertIn("dry-run", r["mode"])

    def test_campaign_marks_consent_required(self):
        voice.LIVE = False
        r = voice.trigger_campaign(["01011112222"])
        self.assertTrue(r["calls"][0]["consent_required"])

    def test_campaign_over_http_is_dry_run(self):
        voice.LIVE = False
        r = post({"op": "campaign", "numbers": ["01011112222"], "scenario": "care"},
                 headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["queued"], 0)

    def test_live_flag_defaults_off_in_source(self):
        """기본값이 OFF 인지 소스로 확인 — 실발신은 승인 사항이다."""
        src = open(os.path.join(ROOT, "api", "voice.py"), encoding="utf-8").read()
        self.assertIn('os.environ.get("CPAAS_LIVE", "0") == "1"', src)

    def test_call_log_masks_phone_number(self):
        """회귀: /api/voice?op=log 는 콘솔이 폴링하는 경로다. 원문 번호를 남기지 않는다."""
        voice._log_call({"from": "010-1111-2222", "ev": "통화연결", "text": ""})
        self.assertEqual(voice.RECENT[0]["from"], "010****2222")

    def test_log_endpoint_does_not_leak_raw_number(self):
        voice.handle_twilio({"CallId": "c1", "From": "01011112222", "CallStatus": "in-progress"})
        r = get("/api/voice?op=log", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertNotIn("01011112222", r.text())
        self.assertTrue(r.json()["recent"])

    def test_mask_short_and_empty_values(self):
        self.assertEqual(voice._mask_phone(""), "")
        self.assertEqual(voice._mask_phone("sip:agent@pbx.local"), "")
        self.assertEqual(voice._mask_phone("1234"), "****")

    def test_log_is_capped(self):
        for i in range(80):
            voice._log_call({"from": "01011112222", "ev": "고객발화", "text": str(i)})
        self.assertLessEqual(len(voice.RECENT), 60)

    def test_status_endpoint_exposes_no_secrets(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-secret"
        os.environ["CALLBOT_API_KEY"] = "key-secret"
        r = get("/api/voice", headers=SAME_ORIGIN)
        self.assertNotIn("tok-secret", r.text())
        self.assertNotIn("key-secret", r.text())

    def test_no_network_during_suite(self):
        """대역이 제대로 걸려 있는지 자체 확인."""
        self.stub_engine()
        self.stub_transcribe_url("안녕")
        voice.handle_twilio({"CallId": "c1", "From": "010",
                             "RecordingUrl": "https://cdn/x.wav", "RecordingDuration": "3"})
        self.assertEqual(self.network_calls, [])

    def test_transcribe_url_swallows_network_failure(self):
        """녹음 다운로드 실패가 통화를 죽이지 않는다(빈 문자열 반환)."""
        self.assertEqual(voice._transcribe_url("https://cdn/x.wav"), "")


# ==========================================================================
# 8) HTTP 표면
# ==========================================================================
class TestHttpSurface(VoiceBase):
    def test_options_preflight(self):
        fh = FakeHandler(SAME_ORIGIN, b"", "/api/voice")
        voice.handler.do_OPTIONS(fh)
        self.assertEqual(fh.status, 204)
        self.assertIsNotNone(fh.header("Access-Control-Allow-Origin"))

    def test_get_status_shape(self):
        r = get("/api/voice", headers=SAME_ORIGIN)
        b = r.json()
        for k in ("ok", "endpoint", "provider", "live", "engine", "stt"):
            self.assertIn(k, b)

    def test_form_post_returns_xml(self):
        r = form_post({"CallId": "c1", "From": "01011112222", "CallStatus": "in-progress"},
                      headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertIn("xml", (r.header("Content-Type") or "").lower())
        self.assertIn("<Response>", r.text())

    def test_json_event_returns_json(self):
        self.stub_engine()
        r = post({"type": "answered", "call_id": "c1", "from": "01011112222"},
                 headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertIn("actions", r.json())

    def test_speech_event_over_http(self):
        self.stub_engine("확인했습니다.")
        post({"type": "answered", "call_id": "c1", "from": "01011112222"}, headers=SAME_ORIGIN)
        r = post({"type": "speech", "call_id": "c1", "from": "01011112222", "text": "환불이요"},
                 headers=SAME_ORIGIN)
        self.assertEqual(r.json()["actions"][0]["text"], "확인했습니다.")

    def test_rate_limit_kicks_in(self):
        """웹훅 등급 한도를 넘으면 429 + Retry-After."""
        _ratelimit.reset()
        seen = None
        for _ in range(200):
            r = post({"type": "dtmf", "call_id": "c1"}, headers=SAME_ORIGIN)
            if r.status == 429:
                seen = r
                break
        self.assertIsNotNone(seen, "웹훅 한도가 걸리지 않았다")
        self.assertIsNotNone(seen.header("Retry-After"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
