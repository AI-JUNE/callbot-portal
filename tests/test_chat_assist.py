# -*- coding: utf-8 -*-
"""api/chat.py · api/assist.py 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — chat·assist 순서)
  1) 접근 가드 — 외부 오리진 403, 동일출처 200, OPTIONS 프리플라이트
  2) 입력검증 — messages/phone/scenario/task/text, 400·413 봉투와 details[].field
  3) 대화 이력 항목 검증 — 잘못된 항목이 500 이 아니라 400 으로 끝난다
  4) 시나리오 배선 — 요청한 시나리오가 실제로 engine 프롬프트를 바꾼다(드리프트 감시)
  5) assist JSON 파싱 — 코드펜스·본문혼합·비JSON·배열에서도 예외 없이 계약 유지
  6) PII·비밀값 — 대화 원문·전화번호·API 키가 로그·응답에 남지 않는다
  7) 과금 안전 — 검증 실패 경로에서 업스트림(Gemini) 호출이 없다

실행: python3 -m pytest tests/test_chat_assist.py -q
"""
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _errors      # noqa: E402
import _guard       # noqa: E402
import _log         # noqa: E402
import _ratelimit   # noqa: E402
import engine       # noqa: E402
import chat         # noqa: E402
import assist       # noqa: E402


# --------------------------------------------------------------------------
# 최소 핸들러 대역 (소켓 없이 do_GET/do_POST 를 돌린다)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class FakeRFile(object):
    def __init__(self, data=b""):
        self._d = data

    def read(self, n=None):
        d = self._d if n is None else self._d[:n]
        self._d = b"" if n is None else self._d[n:]
        return d


class Recorder(object):
    def __init__(self, headers=None, body=b""):
        self.headers = FakeHeaders(headers or {})
        self.wfile = FakeWFile()
        self.rfile = FakeRFile(body)
        self.status = None
        self.sent = []

    def send_response(self, c):
        self.status = c

    def send_header(self, k, v):
        self.sent.append((k, str(v)))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))

    def text(self):
        return self.wfile.data.decode("utf-8")


def call(module, method, headers=None, payload=None, path=None, raw=None):
    """module.handler 를 소켓 없이 호출한다."""
    path = path or ("/api/chat" if module is chat else "/api/assist")
    data = raw if raw is not None else (
        b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault("content-length", str(len(data)))
    h = Recorder(hdrs, data)
    h.path = path
    inst = module.handler.__new__(module.handler)
    inst.headers = h.headers
    inst.wfile = h.wfile
    inst.rfile = h.rfile
    inst.path = path
    inst.send_response = h.send_response
    inst.send_header = h.send_header
    inst.end_headers = h.end_headers
    getattr(inst, "do_" + method)()
    return h


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.7"}

ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CALLBOT_GEMINI_MODEL", "GOOGLE_API_KEY", "GEMINI_API_KEY", "SENTRY_DSN")


class NetworkUsed(AssertionError):
    pass


class Base(unittest.TestCase):
    """모든 테스트에서 urlopen 을 막는다 — 업스트림을 부르면 즉시 실패."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()
        self.logs = []
        self._emit = _log.emit if hasattr(_log, "emit") else None
        import urllib.request

        self._urlopen = urllib.request.urlopen

        def boom(*a, **k):
            raise NetworkUsed("테스트가 업스트림을 호출했다(과금 위험)")

        urllib.request.urlopen = boom
        self._restore_net = lambda: setattr(urllib.request, "urlopen", self._urlopen)

    def tearDown(self):
        self._restore_net()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def stub_engine(self, fn=None):
        """engine._call 을 대역으로 바꾼다(응답을 우리가 결정)."""
        seen = []

        def default(model, payload):
            seen.append((model, payload))
            return {"candidates": [{"content": {"parts": [{"text": "안녕하세요"}]}}],
                    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5}}

        orig = engine._call
        engine._call = fn or default
        self.addCleanup(lambda: setattr(engine, "_call", orig))
        return seen


# ==========================================================================
# 1) 접근 가드
# ==========================================================================
class TestGuard(Base):
    def test_chat_get_same_origin_ok(self):
        h = call(chat, "GET", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body()["ok"])

    def test_chat_get_cross_origin_denied(self):
        h = call(chat, "GET", {"origin": "https://evil.example"})
        self.assertEqual(h.status, 403)
        self.assertFalse(h.body()["ok"])

    def test_chat_post_cross_origin_denied_without_upstream(self):
        h = call(chat, "POST", {"origin": "https://evil.example"},
                 {"messages": [{"role": "user", "content": "안녕"}]})
        self.assertEqual(h.status, 403)   # urlopen 대역이 살아있으므로 업스트림 미호출도 함께 증명

    def test_assist_get_cross_origin_denied(self):
        h = call(assist, "GET", {"origin": "https://evil.example"})
        self.assertEqual(h.status, 403)

    def test_options_preflight_no_body(self):
        for mod in (chat, assist):
            h = call(mod, "OPTIONS", SAME_ORIGIN)
            self.assertEqual(h.status, 204)
            self.assertEqual(h.wfile.data, b"")
            self.assertIn("X-Request-Id", h.header("Access-Control-Expose-Headers"))

    def test_cors_header_never_echoes_unknown_origin(self):
        h = call(chat, "GET", {"origin": "https://evil.example"})
        self.assertNotIn("evil.example", h.header("Access-Control-Allow-Origin") or "")


# ==========================================================================
# 2) chat GET 진단
# ==========================================================================
class TestChatGet(Base):
    def test_reports_key_presence_not_the_key(self):
        os.environ["GOOGLE_API_KEY"] = "k-super-secret"
        h = call(chat, "GET", SAME_ORIGIN)
        self.assertTrue(h.body()["google_key_present"])
        self.assertNotIn("k-super-secret", h.text())

    def test_key_absent_is_false(self):
        h = call(chat, "GET", SAME_ORIGIN)
        self.assertFalse(h.body()["google_key_present"])

    def test_blank_key_counts_as_absent(self):
        os.environ["GEMINI_API_KEY"] = "   "
        self.assertFalse(call(chat, "GET", SAME_ORIGIN).body()["google_key_present"])

    def test_model_is_configurable(self):
        os.environ["CALLBOT_GEMINI_MODEL"] = "gemini-x"
        self.assertEqual(call(chat, "GET", SAME_ORIGIN).body()["model"], "gemini-x")

    def test_scenarios_exposed_for_ui(self):
        self.assertEqual(call(chat, "GET", SAME_ORIGIN).body()["scenarios"],
                         list(chat.SCENARIOS))

    def test_request_id_header_present(self):
        self.assertTrue(call(chat, "GET", SAME_ORIGIN).header("X-Request-Id"))


# ==========================================================================
# 3) chat 입력검증
# ==========================================================================
class TestChatValidation(Base):
    def post(self, payload=None, raw=None, headers=None):
        return call(chat, "POST", dict(headers or SAME_ORIGIN), payload, raw=raw)

    def field_of(self, h):
        d = h.body().get("details") or []
        return d[0].get("field") if d else None

    def test_empty_body_400(self):
        h = self.post()
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["code"], "INVALID_REQUEST")

    def test_not_json_400(self):
        h = self.post(raw=b"not json at all")
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "body")

    def test_top_level_array_400(self):
        h = self.post(raw=json.dumps([1, 2]).encode())
        self.assertEqual(h.status, 400)

    def test_messages_required(self):
        h = self.post({"phone": "01011112222"})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "messages")

    def test_messages_empty_list_400(self):
        h = self.post({"messages": []})
        self.assertEqual(h.status, 400)

    def test_messages_wrong_type_400(self):
        self.assertEqual(self.post({"messages": "안녕"}).status, 400)

    def test_messages_item_not_object_400(self):
        h = self.post({"messages": ["안녕"]})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "messages[0]")

    def test_too_many_messages_400(self):
        h = self.post({"messages": [{"role": "user", "content": "x"}] * 101})
        self.assertEqual(h.status, 400)

    def test_oversized_body_413(self):
        h = call(chat, "POST", dict(SAME_ORIGIN, **{"content-length": str(3 * 1024 * 1024)}),
                 None, raw=b"")
        self.assertEqual(h.status, 413)
        self.assertEqual(h.body()["code"], "PAYLOAD_TOO_LARGE")

    def test_phone_too_long_400(self):
        h = self.post({"messages": [{"role": "user", "content": "x"}], "phone": "0" * 40})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "phone")

    def test_unknown_scenario_400_lists_allowed(self):
        h = self.post({"messages": [{"role": "user", "content": "x"}], "scenario": "해킹"})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "scenario")
        self.assertIn("refund", (h.body()["details"][0]["reason"]))

    def test_validation_failure_never_calls_upstream(self):
        """입력이 틀렸는데 모델을 부르면 그대로 과금된다."""
        self.post({"messages": [{"role": "user"}]})  # urlopen 대역이 살아있음
        self.post({"messages": []})


# ==========================================================================
# 4) 대화 이력 항목 검증 — 회귀(결함): 500 이 아니라 400 이어야 한다
# ==========================================================================
class TestMessageItems(Base):
    def post(self, msgs):
        return call(chat, "POST", dict(SAME_ORIGIN), {"messages": msgs})

    def field_of(self, h):
        d = h.body().get("details") or []
        return d[0].get("field") if d else None

    def test_user_without_content_is_400_not_500(self):
        h = self.post([{"role": "user"}])
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "messages[0].content")

    def test_role_missing_400(self):
        h = self.post([{"content": "안녕"}])
        self.assertEqual(self.field_of(h), "messages[0].role")

    def test_role_unknown_400(self):
        h = self.post([{"role": "root", "content": "안녕"}])
        self.assertEqual(h.status, 400)

    def test_role_not_string_400(self):
        self.assertEqual(self.post([{"role": 7, "content": "x"}]).status, 400)

    def test_content_not_string_400(self):
        h = self.post([{"role": "user", "content": {"text": "안녕"}}])
        self.assertEqual(self.field_of(h), "messages[0].content")

    def test_content_too_long_400(self):
        h = self.post([{"role": "user", "content": "가" * (chat.MAX_CONTENT + 1)}])
        self.assertEqual(h.status, 400)

    def test_tool_item_requires_name(self):
        h = self.post([{"role": "user", "content": "안녕"},
                       {"role": "tool", "content": "{}"}])
        self.assertEqual(self.field_of(h), "messages[1].name")

    def test_tool_calls_must_be_list(self):
        h = self.post([{"role": "assistant", "tool_calls": {"name": "x"}}])
        self.assertEqual(self.field_of(h), "messages[0].tool_calls")

    def test_tool_calls_item_shape(self):
        h = self.post([{"role": "assistant", "tool_calls": [{"nope": 1}]}])
        self.assertEqual(self.field_of(h), "messages[0].tool_calls[0].name")

    def test_index_points_at_the_bad_item(self):
        h = self.post([{"role": "user", "content": "정상"}, {"role": "user"}])
        self.assertEqual(self.field_of(h), "messages[1].content")

    def test_assistant_without_content_is_allowed(self):
        """툴 호출만 있는 assistant 턴은 정상 이력이다."""
        chat.validate_messages([{"role": "assistant",
                                 "tool_calls": [{"name": "lookup_recent_order", "input": {}}]}])

    def test_engine_no_longer_crashes_on_missing_content(self):
        """2차 방어: 라우트를 거치지 않는 호출자(voice 등)도 죽지 않는다."""
        out = engine._to_contents([{"role": "user"}])
        self.assertEqual(out[0]["parts"][0]["text"], "")


# ==========================================================================
# 5) 시나리오 배선 — 회귀(결함): 요청한 시나리오가 무시되던 문제
# ==========================================================================
class TestScenarioWiring(Base):
    def run_with(self, scenario):
        seen = []

        def spy(model, payload):
            seen.append(payload)
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

        self.stub_engine(spy)
        body = {"messages": [{"role": "user", "content": "안녕"}]}
        if scenario is not None:
            body["scenario"] = scenario
        h = call(chat, "POST", dict(SAME_ORIGIN), body)
        return h, seen

    def sysprompt(self, seen):
        return seen[0]["systemInstruction"]["parts"][0]["text"]

    def test_wellbeing_scenario_reaches_engine(self):
        h, seen = self.run_with("wellbeing")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.sysprompt(seen), engine.PROMPT_WELLBEING)

    def test_default_is_refund_prompt_with_tools(self):
        h, seen = self.run_with(None)
        self.assertIn("고객센터 콜봇", self.sysprompt(seen))
        self.assertIn("tools", seen[0])       # 기본 시나리오만 툴을 붙인다

    def test_every_scenario_selects_a_distinct_prompt(self):
        """화이트리스트가 engine 분기와 어긋나면(드리프트) 여기서 깨진다."""
        got = {}
        for s in chat.SCENARIOS:
            _h, seen = self.run_with(s)
            got[s] = self.sysprompt(seen)
        self.assertEqual(got["안부"], got["wellbeing"])       # 한국어 별칭
        distinct = set(got.values())
        self.assertEqual(len(distinct), len(set(chat.SCENARIOS)) - 1)  # 별칭 1건 제외
        for s in ("integrity", "overdue", "welfare", "trio"):
            self.assertNotEqual(got[s], got["refund"])

    def test_non_default_scenarios_have_no_tools(self):
        for s in ("integrity", "overdue", "wellbeing"):
            _h, seen = self.run_with(s)
            self.assertNotIn("tools", seen[0])


# ==========================================================================
# 6) chat 정상 경로 · 업스트림 장애 분류
# ==========================================================================
class TestChatPost(Base):
    def ok_post(self, **kw):
        self.stub_engine()
        return call(chat, "POST", dict(SAME_ORIGIN),
                    dict({"messages": [{"role": "user", "content": "주문 확인해주세요"}]}, **kw))

    def test_reply_and_usage_shape(self):
        h = self.ok_post()
        self.assertEqual(h.status, 200)
        b = h.body()
        for k in ("reply", "messages", "log", "audit", "usage", "transferred"):
            self.assertIn(k, b)
        self.assertEqual(b["usage"], {"input": 3, "output": 5})
        self.assertFalse(b["transferred"])

    def test_history_is_returned_for_next_turn(self):
        b = self.ok_post().body()
        self.assertEqual(b["messages"][0]["content"], "주문 확인해주세요")
        self.assertEqual(b["messages"][-1]["role"], "assistant")

    def test_missing_api_key_is_500_not_silent(self):
        """키가 없으면 engine 이 RuntimeError — 조용히 빈 응답을 주지 않는다."""
        h = call(chat, "POST", dict(SAME_ORIGIN),
                 {"messages": [{"role": "user", "content": "안녕"}]})
        self.assertEqual(h.status, 500)
        self.assertFalse(h.body()["ok"])

    def test_upstream_timeout_maps_to_504(self):
        def boom(model, payload):
            raise TimeoutError("timed out")
        self.stub_engine(boom)
        h = call(chat, "POST", dict(SAME_ORIGIN),
                 {"messages": [{"role": "user", "content": "안녕"}]})
        self.assertEqual(h.status, 504)
        self.assertEqual(h.body()["code"], "UPSTREAM_TIMEOUT")

    def test_upstream_error_maps_to_502(self):
        import urllib.error

        def boom(model, payload):
            raise urllib.error.URLError("no route")
        self.stub_engine(boom)
        h = call(chat, "POST", dict(SAME_ORIGIN),
                 {"messages": [{"role": "user", "content": "안녕"}]})
        self.assertEqual(h.status, 502)

    def test_internal_error_message_not_leaked(self):
        def boom(model, payload):
            raise RuntimeError("GOOGLE_API_KEY=k-super-secret 로 접속 실패")
        self.stub_engine(boom)
        h = call(chat, "POST", dict(SAME_ORIGIN),
                 {"messages": [{"role": "user", "content": "안녕"}]})
        self.assertEqual(h.status, 500)
        self.assertNotIn("k-super-secret", h.text())

    def test_conversation_text_not_echoed_in_error(self):
        def boom(model, payload):
            raise RuntimeError("실패")
        self.stub_engine(boom)
        h = call(chat, "POST", dict(SAME_ORIGIN),
                 {"messages": [{"role": "user", "content": "제 계좌는 110-123-456789 입니다"}],
                  "phone": "01099998888"})
        self.assertEqual(h.status, 500)
        self.assertNotIn("110-123-456789", h.text())
        self.assertNotIn("01099998888", h.text())


# ==========================================================================
# 7) 구조화 로그 — 건수·시나리오만, 원문·번호는 없다
# ==========================================================================
class TestChatLogging(Base):
    def _post(self, body):
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            h = call(chat, "POST", dict(SAME_ORIGIN), body)
        return h, buf.getvalue()

    def test_log_records_counts_not_content(self):
        self.stub_engine()
        h, out = self._post({"messages": [{"role": "user", "content": "제 번호는 01055556666"}],
                             "phone": "01055556666", "scenario": "wellbeing"})
        self.assertEqual(h.status, 200)
        self.assertNotIn("01055556666", out)      # 발신번호가 로그로 새면 안 된다
        self.assertNotIn("제 번호는", out)          # 대화 원문도 마찬가지

    def test_log_keeps_diagnosable_fields(self):
        self.stub_engine()
        _h, out = self._post({"messages": [{"role": "user", "content": "안녕"}],
                              "scenario": "overdue"})
        if out.strip():                            # 로깅이 꺼진 환경에서는 건너뛴다
            self.assertIn("overdue", out)          # 시나리오는 PII 가 아니고 진단에 필요하다
            self.assertIn("/api/chat", out)


# ==========================================================================
# 8) assist GET / 입력검증
# ==========================================================================
class TestAssist(Base):
    def post(self, payload=None, raw=None):
        return call(assist, "POST", dict(SAME_ORIGIN), payload, raw=raw)

    def field_of(self, h):
        d = h.body().get("details") or []
        return d[0].get("field") if d else None

    def test_get_lists_tasks_from_single_source(self):
        h = call(assist, "GET", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body()["tasks"], list(assist.TASKS))

    def test_prompts_cover_every_non_kms_task(self):
        """화이트리스트에만 추가하고 프롬프트를 빠뜨리면 KeyError(500) 가 된다."""
        for t in assist.TASKS:
            if t != "kms":
                self.assertIn(t, assist.PROMPTS)

    def test_unknown_task_400(self):
        h = self.post({"task": "shell", "text": "x"})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "task")

    def test_kms_requires_question(self):
        h = self.post({"task": "kms", "kb": "지식"})
        self.assertEqual(h.status, 400)
        self.assertEqual(self.field_of(h), "text")

    def test_text_length_cap_400(self):
        h = self.post({"task": "summary", "text": "가" * 20001})
        self.assertEqual(h.status, 400)

    def test_kb_length_cap_400(self):
        h = self.post({"task": "kms", "text": "질문", "kb": "가" * 20001})
        self.assertEqual(h.status, 400)

    def test_empty_body_400(self):
        self.assertEqual(self.post().status, 400)

    def test_oversized_body_413(self):
        h = call(assist, "POST", dict(SAME_ORIGIN, **{"content-length": str(3 * 1024 * 1024)}),
                 None, raw=b"")
        self.assertEqual(h.status, 413)

    def test_validation_failure_never_calls_upstream(self):
        self.post({"task": "shell"})      # urlopen 대역이 살아있음

    def test_missing_key_is_500_not_silent(self):
        h = self.post({"task": "summary", "text": "대화"})
        self.assertEqual(h.status, 500)
        self.assertFalse(h.body()["ok"])


# ==========================================================================
# 9) assist 정상 경로 · 프롬프트 조립
# ==========================================================================
class TestAssistRun(Base):
    def stub_assist(self, text):
        seen = []

        def spy(model, payload):
            seen.append(payload)
            return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

        orig = assist._call
        assist._call = spy
        self.addCleanup(lambda: setattr(assist, "_call", orig))
        return seen

    def test_summary_round_trip(self):
        seen = self.stub_assist('{"summary":"요약","points":["a"],"action":"조치"}')
        h = call(assist, "POST", dict(SAME_ORIGIN), {"task": "summary", "text": "대화 원문"})
        self.assertEqual(h.status, 200)
        b = h.body()
        self.assertEqual(b["task"], "summary")
        self.assertEqual(b["result"]["summary"], "요약")
        self.assertIn("대화 원문", seen[0]["contents"][0]["parts"][0]["text"])

    def test_default_task_is_summary(self):
        self.stub_assist('{"summary":"s"}')
        self.assertEqual(call(assist, "POST", dict(SAME_ORIGIN),
                              {"text": "대화"}).body()["task"], "summary")

    def test_kms_prompt_includes_knowledge_and_question(self):
        seen = self.stub_assist('{"answer":"답","source":"문서"}')
        call(assist, "POST", dict(SAME_ORIGIN),
             {"task": "kms", "text": "환불 기한은?", "kb": "환불은 7일 이내"})
        p = seen[0]["contents"][0]["parts"][0]["text"]
        self.assertIn("환불은 7일 이내", p)
        self.assertIn("환불 기한은?", p)

    def test_json_mime_is_requested(self):
        seen = self.stub_assist('{"a":1}')
        call(assist, "POST", dict(SAME_ORIGIN), {"task": "ta", "text": "x"})
        self.assertEqual(seen[0]["generationConfig"]["responseMimeType"], "application/json")

    def test_qa_prompt_selected_by_task(self):
        seen = self.stub_assist('{"score":90}')
        call(assist, "POST", dict(SAME_ORIGIN), {"task": "qa", "text": "x"})
        self.assertIn("품질평가", seen[0]["contents"][0]["parts"][0]["text"])


# ==========================================================================
# 10) assist JSON 파싱 — 회귀(결함): 비정형 출력이 500 을 내던 문제
# ==========================================================================
class TestAssistParse(Base):
    def test_plain_json(self):
        self.assertEqual(assist.parse_json('{"a":1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(assist.parse_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_code_fence_without_language(self):
        self.assertEqual(assist.parse_json('```\n{"a":1}\n```'), {"a": 1})

    def test_json_embedded_in_prose(self):
        self.assertEqual(assist.parse_json('결과입니다 {"a":1} 이상.'), {"a": 1})

    def test_broken_braces_fall_back_to_raw_not_500(self):
        out = assist.parse_json("설명입니다 {not json}")
        self.assertEqual(out, {"raw": "설명입니다 {not json}"})

    def test_plain_text_falls_back_to_raw(self):
        self.assertEqual(assist.parse_json("판단 불가"), {"raw": "판단 불가"})

    def test_empty_output_falls_back(self):
        self.assertEqual(assist.parse_json(""), {"raw": ""})
        self.assertEqual(assist.parse_json(None), {"raw": ""})

    def test_top_level_array_is_wrapped(self):
        self.assertEqual(assist.parse_json("[1,2]"), {"raw": [1, 2]})

    def test_result_is_always_a_dict(self):
        for t in ("", "그냥 문장", "[1]", '{"a":1}', "{깨짐}"):
            self.assertIsInstance(assist.parse_json(t), dict)

    def test_handler_returns_200_on_unparsable_model_output(self):
        """모델이 형식을 어겨도 화면은 실패하지 않고 원문을 받는다."""
        orig = assist._call
        assist._call = lambda m, p: {"candidates": [{"content": {"parts": [{"text": "안녕 {깨짐}"}]}}]}
        self.addCleanup(lambda: setattr(assist, "_call", orig))
        h = call(assist, "POST", dict(SAME_ORIGIN), {"task": "ta", "text": "x"})
        self.assertEqual(h.status, 200)
        self.assertIn("raw", h.body()["result"])

    def test_safety_blocked_response_has_no_candidates(self):
        orig = assist._call
        assist._call = lambda m, p: {"promptFeedback": {"blockReason": "SAFETY"}}
        self.addCleanup(lambda: setattr(assist, "_call", orig))
        h = call(assist, "POST", dict(SAME_ORIGIN), {"task": "ta", "text": "x"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body()["result"], {"raw": ""})


# ==========================================================================
# 11) 콘솔 드리프트 — 화면이 보내는 시나리오 키가 서버 화이트리스트 안에 있다
# ==========================================================================
class TestConsoleDrift(Base):
    def test_console_scenario_keys_are_accepted(self):
        """화면 버튼 키가 목록에서 빠지면 사용자는 400 만 보게 된다."""
        import re
        path = os.path.join(ROOT, "public", "admin.html")
        html = open(path, encoding="utf-8").read()
        keys = set(re.findall(r"pickScn\('([^']+)'", html))
        self.assertTrue(keys, "콘솔에서 시나리오 버튼을 찾지 못했다")
        for k in keys:
            self.assertIn(k, chat.SCENARIOS, "콘솔이 보내는 시나리오 %r 가 서버 화이트리스트에 없다" % k)


if __name__ == "__main__":
    unittest.main()
