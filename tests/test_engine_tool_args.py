# -*- coding: utf-8 -*-
"""툴 인자가 dict 가 아닐 때 — `_engine` 가드·감사·디스패치, `chat` 입력검증.

23차가 남긴 제안의 회귀다. 툴 인자(`functionCall.args`)는 **모델 출력**이고, 대화
이력(`messages[].tool_calls[].input`·`tool` 본문)은 **클라이언트 입력**이다. 둘 다
우리가 타입을 보장할 수 없는 바깥 값인데 `inp.get(...)`·`tc["input"]` 로 바로 읽고
있었다. 모델이 배열을 주거나 클라이언트가 `input` 을 빼면

  · 가드 **안에서** AttributeError/KeyError 가 터져 요청이 500 으로 끝나고,
  · 감사 append 는 가드 반환 뒤이므로 **위험 툴 시도가 흔적 없이 사라지고**,
  · 사용자 입력 오류가 내부 오류로 보고돼 모니터링 알림 노이즈가 된다.

19차가 `_amount`(금액류 바깥 값)에서 고친 것과 같은 계열이다 — 해석 불가는 예외가
아니라 **차단 판정**으로 돌려준다(fail-safe).

검증 대상
  1) `_guard` — 어떤 타입이 와도 예외 대신 차단 판정. 환불 2단계 확인이 비켜가지 않는다
  2) `_audit` — 비정상 인자로도 **기록은 남는다**(기록이 사라지는 쪽이 더 나쁘다)
  3) `dispatch` — 계약(dict) 위반은 빈 인자로 **갈아 끼워 실행하지 않는다**
  4) `_mem`·`_to_contents` — 객체가 아닌 툴 본문·`input` 누락에 죽지 않는다
  5) `chat.validate_messages` — 어느 칸이 왜 틀렸는지 400 으로 지목한다(1차 방어)

LLM·네트워크 미호출(`_call` 대역 + urlopen 감시).

실행: python -m pytest tests/test_engine_tool_args.py -q
"""
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _engine as engine            # noqa: E402
import _errors                      # noqa: E402
import _order_backend as ob         # noqa: E402
import chat                         # noqa: E402

# 모델·클라이언트가 실제로 줄 수 있는 '객체가 아닌' 값들
NOT_OBJECTS = ([], ["order_id", "SSG-1"], "SSG-1", 5, 0, 1.5, True, False, None)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        self._restore = []

        def watchdog(*a, **kw):
            raise AssertionError("네트워크 호출 금지")

        self.patch(urllib.request, "urlopen", watchdog)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)


def mem(**kw):
    m = {"order_id": None, "max_refund": None, "quoted_amount": None,
         "awaiting": False, "affirm": False, "transferred": False}
    m.update(kw)
    return m


# ==========================================================================
# 1) 가드는 판정으로 끝난다 — 예외로 죽지 않는다
# ==========================================================================
class TestGuardNeverRaises(NoNetwork):

    def test_non_object_args_are_blocked_for_every_tool(self):
        tools = [t["name"] for t in engine.TOOLS]
        self.assertIn("confirm_refund", tools, "툴 목록이 바뀌면 이 회귀를 갱신할 것")
        for name in tools:
            for inp in NOT_OBJECTS:
                ok, reason, esc = engine._guard(name, inp, mem(affirm=True, awaiting=True,
                                                               quoted_amount=1000))
                self.assertFalse(ok, "%s / %r" % (name, inp))
                self.assertIn("형식 오류", reason)
                self.assertFalse(esc, "형식 오류는 상담사 전환이 아니라 재시도 대상이다")

    def test_two_step_refund_confirmation_cannot_be_bypassed_by_a_list(self):
        """예전에는 여기서 AttributeError 가 났다 — 판정도 기록도 없이 500."""
        m = mem(affirm=True, awaiting=True, quoted_amount=159000, max_refund=159000)
        ok, _r, _e = engine._guard("confirm_refund", ["user_confirmed", True], m)
        self.assertFalse(ok)

    def test_dict_args_still_work(self):
        m = mem(affirm=True, awaiting=True, quoted_amount=1000, max_refund=1000)
        ok, reason, _e = engine._guard("confirm_refund",
                                       {"refund_amount": 1000, "user_confirmed": True}, m)
        self.assertTrue(ok, reason)


# ==========================================================================
# 2) 감사기록은 남는다
# ==========================================================================
class TestAuditSurvives(NoNetwork):

    def test_audit_records_a_block_for_non_object_args(self):
        for inp in NOT_OBJECTS:
            row = engine._audit("confirm_refund", inp, mem(), False, "툴 인자 형식 오류")
            self.assertEqual(row["decision"], "block", repr(inp))
            self.assertIsNone(row["order_id"])
            self.assertIsNone(row["refund_amount"])
            self.assertFalse(row["user_confirmed"])
            json.dumps(row, ensure_ascii=False)        # 직렬화 가능해야 응답에 실린다

    def test_run_turn_leaves_an_audit_row_instead_of_a_500(self):
        """통화 경로 전체 — 모델이 배열을 줘도 요청이 살고 시도가 기록된다."""
        responses = [
            {"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "confirm_refund", "args": ["refund_amount", 159000]}}]}}],
             "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}},
            {"candidates": [{"content": {"parts": [{"text": "확인해 보겠습니다."}]}}],
             "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        r = engine.run_turn([{"role": "user", "content": "네 환불해 주세요"}], scenario="refund")
        self.assertEqual([a["decision"] for a in r["audit"]], ["block"])
        self.assertIn("형식 오류", r["audit"][0]["reason"])
        self.assertFalse(r["transferred"], "형식 오류로 상담사를 부르지 않는다")
        self.assertEqual(r["reply"], "확인해 보겠습니다.")
        blocked = [x for x in r["log"] if x["turn"] == "guard"]
        self.assertEqual(len(blocked), 1)

    def test_run_turn_does_not_execute_the_tool_with_emptied_args(self):
        """빈 인자로 갈아 끼워 실행하면 모델이 묻지 않은 질문에 답을 주게 된다.

        `quote_refund([])` 가 그대로 돌면 `awaiting=True` 와 견적 0원이 기억되어
        금액 재확인이 **0원 기준**으로 바뀐다 — 가드를 열어 주는 방향의 사고다.
        """
        responses = [
            {"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "quote_refund", "args": []}}]}}],
             "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "다시 확인할게요."}]}}],
             "usageMetadata": {}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        r = engine.run_turn([{"role": "user", "content": "환불해 주세요"}], scenario="refund")
        self.assertEqual([x["turn"] for x in r["log"]], ["guard", "bot"])
        self.assertNotIn("tool", [x["turn"] for x in r["log"]])


class TestParseKeepsMalformedArgs(NoNetwork):
    """`_parse` 가 거짓인 비객체를 `{}` 로 갈아 끼우면 가드가 볼 기회가 없다."""

    def parse(self, args, present=True):
        fc = {"name": "quote_refund"}
        if present:
            fc["args"] = args
        resp = {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]}
        return engine._parse(resp)[1][0]["args"]

    def test_falsy_non_objects_are_not_turned_into_empty_objects(self):
        for v in ([], "", 0, False, 0.0):
            self.assertEqual(self.parse(v), v, repr(v))
            self.assertFalse(isinstance(self.parse(v), dict), repr(v))

    def test_absent_or_null_args_mean_no_arguments(self):
        self.assertEqual(self.parse(None, present=False), {})
        self.assertEqual(self.parse(None), {})

    def test_objects_pass_through(self):
        self.assertEqual(self.parse({"order_id": "SSG-1"}), {"order_id": "SSG-1"})


# ==========================================================================
# 3) 디스패치 계약
# ==========================================================================
class TestDispatchContract(NoNetwork):

    def test_non_object_input_is_refused_not_coerced(self):
        b = ob.DemoOrderBackend()
        called = []
        b.confirm_refund = lambda inp: called.append(inp) or {"status": "accepted"}
        for inp in (["a"], "a", 5, 1.5, True):
            out = b.dispatch("confirm_refund", inp)
            self.assertEqual(out, {"error": "invalid input"}, repr(inp))
        self.assertEqual(called, [], "쓰기 툴이 빈 인자로 나가지 않는다")

    def test_empty_and_missing_input_still_runs(self):
        b = ob.DemoOrderBackend()
        self.assertTrue(b.dispatch("lookup_recent_order", None)["found"])
        self.assertTrue(b.dispatch("lookup_recent_order", {})["found"])

    def test_unknown_tool_is_still_rejected_first(self):
        b = ob.DemoOrderBackend()
        self.assertIn("unknown", b.dispatch("rm_rf", ["x"])["error"])


# ==========================================================================
# 4) 대화 이력의 바깥 값
# ==========================================================================
class TestHistoryTypes(NoNetwork):

    def test_tool_body_that_is_not_an_object_does_not_crash_memory(self):
        """`/api/chat` 은 대화 이력을 클라이언트가 보낸다 — `"[]"` 로 500 이 났다."""
        for body in ("[]", "[1,2]", "3", '"found"', "null", "true"):
            m = engine._mem([{"role": "tool", "name": "lookup_recent_order", "content": body}])
            self.assertIsNone(m["order_id"], body)
        m = engine._mem([{"role": "tool", "name": "quote_refund", "content": "[]"}])
        self.assertTrue(m["awaiting"])
        self.assertIsNone(m["quoted_amount"], "견적 금액을 모르는 상태로 남는다")

    def test_object_tool_body_still_remembered(self):
        m = engine._mem([{"role": "tool", "name": "quote_refund",
                          "content": json.dumps({"refund_amount": 1234})}])
        self.assertEqual(m["quoted_amount"], 1234)

    def test_non_dict_history_items_are_skipped(self):
        for msgs in ([None], ["hello"], [["role", "user"]], "not a list"):
            engine._mem(msgs)
            engine._to_contents(msgs)

    def test_tool_call_without_input_does_not_raise(self):
        out = engine._to_contents([{"role": "assistant", "content": "",
                                    "tool_calls": [{"name": "quote_refund"}]}])
        self.assertEqual(out[0]["parts"][0]["functionCall"]["args"], {})

    def test_malformed_tool_calls_are_skipped(self):
        out = engine._to_contents([{"role": "assistant", "content": "안녕",
                                    "tool_calls": ["quote_refund", None]}])
        self.assertEqual(out[0]["parts"], [{"text": "안녕"}])
        out = engine._to_contents([{"role": "assistant", "content": "안녕",
                                    "tool_calls": {"name": "x"}}])
        self.assertEqual(out[0]["parts"], [{"text": "안녕"}])


# ==========================================================================
# 5) 라우트가 먼저 지목한다 — 사용자 입력 오류는 400 이다
# ==========================================================================
class TestChatValidation(NoNetwork):

    def bad(self, msgs):
        with self.assertRaises(_errors.ValidationError) as cm:
            chat.validate_messages(msgs)
        return cm.exception

    def test_missing_tool_call_input_is_400_with_the_field(self):
        e = self.bad([{"role": "assistant", "content": "",
                       "tool_calls": [{"name": "quote_refund"}]}])
        self.assertEqual(e.status, 400)
        self.assertEqual(e.details[0]["field"], "messages[0].tool_calls[0].input")

    def test_non_object_tool_call_input_is_400(self):
        for v in ([], "x", 5, None, True):
            e = self.bad([{"role": "assistant", "content": "",
                           "tool_calls": [{"name": "quote_refund", "input": v}]}])
            self.assertEqual(e.details[0]["field"], "messages[0].tool_calls[0].input", repr(v))

    def test_the_index_points_at_the_offending_call(self):
        e = self.bad([{"role": "user", "content": "안녕"},
                      {"role": "assistant", "content": "",
                       "tool_calls": [{"name": "a", "input": {}}, {"name": "b"}]}])
        self.assertEqual(e.details[0]["field"], "messages[1].tool_calls[1].input")

    def test_too_many_tool_calls_is_capped_before_the_item_loop(self):
        e = self.bad([{"role": "assistant", "content": "",
                       "tool_calls": [{"name": "a", "input": {}}] * (chat.MAX_TOOL_CALLS + 1)}])
        self.assertEqual(e.details[0]["field"], "messages[0].tool_calls")

    def test_well_formed_history_still_passes(self):
        msgs = [{"role": "user", "content": "환불해 주세요"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "quote_refund", "name": "quote_refund",
                                 "input": {"order_id": "SSG-1"}}]},
                {"role": "tool", "name": "quote_refund", "content": "{\"refund_amount\": 1}"}]
        self.assertIs(chat.validate_messages(msgs), msgs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
