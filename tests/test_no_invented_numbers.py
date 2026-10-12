# -*- coding: utf-8 -*-
"""고객에게 말해지는 수치는 **지어내지 않는다** 회귀 — 27차. 네트워크 미사용.

26차가 남긴 후보: 데모 주문 백엔드의 `escalate_to_agent` 가
`{"transferred": True, "queue_position": 2}` 를 돌려줬다. 이 값은 툴 결과이므로
① LLM 컨텍스트로 들어가고 ② 모델은 "주문/정책/금액은 툴 결과만 인용" 하도록
지시받았으며 ③ `/api/chat` 응답의 `log[].out` 으로 화면까지 나간다. 그래서
고객은 근거 없는 **"대기 두 번째입니다"** 를 듣게 된다 — 화면의 가짜 KPI 보다
나쁘다(귀로 듣고 믿는다). 순번을 아는 것은 상담 시스템(CTI)뿐이고, 우리 큐는
인스턴스 메모리 sim 장부라 그 숫자를 순번이라고 부를 수도 없다.

여기서 고정하는 것
  1) 전환 툴 결과에 **순번 숫자가 없다**(모르는 것은 모른다고 말한다).
  2) 데모 응답은 **출처를 표시한다**(`data_source="demo"`) — 받는 쪽이 실측이
     아님을 알 수 있게. 단 오류 봉투에는 붙지 않는다(계약은 고객사와 같게).
  3) 실제 숫자는 **장부에서만** 나온다 — 전환 1건 뒤 큐의 `queued` 는 1 이고,
     그 값은 `/api/ops_stats` 가 노출한다(지어낸 숫자와 섞이지 않는다).
  4) 시스템 프롬프트가 "툴이 알려주지 않은 수치는 말하지 않는다"를 지시한다.

실행: python3 -m pytest tests/test_no_invented_numbers.py -q
"""
import os
import re
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _engine as engine          # noqa: E402
import _escalation as escalation  # noqa: E402
import _order_backend as order_backend  # noqa: E402


def say(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


def call_tool(name, args):
    return {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


class TestEscalateResult(unittest.TestCase):

    def setUp(self):
        self.b = order_backend.DemoOrderBackend()

    def test_transfer_fact_is_still_reported(self):
        out = self.b.escalate_to_agent({"reason": "request", "summary": "s"})
        self.assertIs(out["transferred"], True)

    def test_no_queue_position_is_invented(self):
        out = self.b.escalate_to_agent({"reason": "request", "summary": "s"})
        self.assertIsNone(out["queue_position"])
        self.assertIs(out["queue_position_known"], False)

    def test_result_carries_no_number_at_all(self):
        """숫자 한 칸이라도 남으면 모델이 그것을 순번으로 읽는다."""
        out = self.b.escalate_to_agent({"reason": "request", "summary": "s"})
        for k, v in out.items():
            if isinstance(v, bool):
                continue                       # 사실(전환됨/모름)은 숫자가 아니다
            self.assertNotIsInstance(v, (int, float), k)
        self.assertIsNone(re.search(r"\d", json.dumps(out, ensure_ascii=False)))

    def test_unknown_state_is_stated_not_hidden(self):
        """칸을 지우면 '모른다'가 아니라 '안 알려준다'가 된다 — 사유를 남긴다."""
        out = self.b.escalate_to_agent({})
        self.assertIn("note", out)
        self.assertIn("알 수 없", out["note"])


class TestDemoSourceMarker(unittest.TestCase):

    def setUp(self):
        self.b = order_backend.DemoOrderBackend()

    def test_dispatch_marks_demo_data(self):
        for tool in ("lookup_recent_order", "get_refund_policy", "quote_refund",
                     "confirm_refund", "request_redelivery", "escalate_to_agent"):
            out = self.b.dispatch(tool, {"order_id": "x", "missing_items": [],
                                         "items": [], "refund_amount": 1,
                                         "user_confirmed": True, "reason": "r",
                                         "summary": "s", "issue_type": "missing"})
            self.assertEqual(out.get("data_source"), "demo", tool)

    def test_error_envelopes_are_not_marked(self):
        """거부 통지는 데모 데이터가 아니다(모양은 고객사 백엔드와 같아야 한다)."""
        self.assertEqual(self.b.dispatch("explode", {}), {"error": "unknown explode"})
        self.assertEqual(self.b.dispatch("confirm_refund", ["a"]),
                         {"error": "invalid input"})

    def test_live_backend_is_not_marked_as_demo(self):
        """고객사 응답에 우리가 'demo' 를 덧붙이면 그쪽이 거짓이 된다."""
        self.assertIs(order_backend.HttpOrderBackend.dispatch,
                      order_backend.OrderBackend.dispatch)


class TestNumbersComeFromTheLedger(unittest.TestCase):
    """지어낸 숫자를 뺐으니, 실제 숫자는 장부에서 나와야 한다."""

    def setUp(self):
        self.q = escalation.EscalationQueue()
        self._call, self._q = engine._call, engine._ESC_QUEUE
        engine._ESC_QUEUE = self.q

    def tearDown(self):
        engine._call, engine._ESC_QUEUE = self._call, self._q

    def _run(self):
        resp = [call_tool("escalate_to_agent",
                          {"reason": "request", "summary": "상담사 연결 요청"}),
                say("상담사에게 연결하겠습니다.")]
        engine._call = lambda m, p: resp.pop(0)
        return engine.run_turn([{"role": "user", "content": "상담사 바꿔주세요"}],
                               phone="01012345678", scenario="refund")

    def test_chat_log_has_no_invented_position(self):
        out = self._run()
        tool_turns = [t for t in out["log"] if t.get("turn") == "tool"]
        self.assertTrue(tool_turns)
        self.assertIsNone(tool_turns[0]["out"]["queue_position"])
        self.assertEqual(tool_turns[0]["out"]["data_source"], "demo")

    def test_queue_count_is_real_and_one(self):
        self._run()
        self.assertEqual(self.q.stats()["queued"], 1)
        self.assertEqual(self.q.stats()["scope"], "instance")

    def test_transfer_is_recorded_with_the_ticket(self):
        out = self._run()
        esc = [t for t in out["log"] if t.get("turn") == "escalation"]
        self.assertEqual(len(esc), 1)
        self.assertIs(esc[0]["recorded"], True)
        self.assertTrue(esc[0]["ticket"].startswith("ESC-"))


class TestPromptRule(unittest.TestCase):

    def test_system_prompt_forbids_unknown_numbers(self):
        sysp = engine._sys("01012345678")
        self.assertIn("툴 결과만 인용", sysp)
        self.assertIn("대기 순번", sysp)

    def test_prompt_still_tells_the_bot_to_transfer(self):
        self.assertIn("escalate_to_agent", engine._sys("01012345678"))


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
