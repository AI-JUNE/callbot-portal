# -*- coding: utf-8 -*-
"""고객에게 말해지는 수치는 **지어내지 않는다** 회귀 — 27·28차. 네트워크 미사용.

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

28차가 더하는 것 — 같은 경로에 남아 있던 수치 셋
  5) 접수 툴의 ETA(`eta_days: 3`·`eta_minutes: 25`)도 지어낸 값이었다. 접수
     **사실**은 그대로 보고하고 수치만 '모른다'로 남긴다.
  6) `PROMPT_OVERDUE` 가 「현재 미납금은 37,000원입니다」를 봇이 말할 문장으로
     들고 있었다(이 시나리오는 툴을 쓰지 않아 어디서도 조회되지 않는다).
  7) 콘솔·react 의 대본표는 **봇이 하는 말의 문서**다 — 프롬프트와 어긋나면 거짓이다.

실행: python3 -m pytest tests/test_no_invented_numbers.py -q
"""
import io
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


class TestWriteToolEta(unittest.TestCase):
    """28차: 접수 툴의 ETA 도 같은 경로였다.

    `confirm_refund` 는 `eta_days: 3`, `request_redelivery` 는 `eta_minutes: 25`
    를 돌려줬다. 환불 소요일수를 아는 것은 결제·정산 시스템, 재배달 도착 시각을
    아는 것은 배송 추적인데 데모 백엔드는 둘 중 아무것도 보지 않는다 —
    고객은 '3일'·'25분'을 **약속으로** 듣는다(돈·기다림에 관한 약속이라
    대기 순번보다 무르기 어렵다).
    """

    def setUp(self):
        self.b = order_backend.DemoOrderBackend()

    def test_acceptance_fact_is_still_reported(self):
        """사실(접수됨·접수번호)은 그대로 — 수치만 뺀다."""
        r = self.b.confirm_refund({"order_id": "x", "refund_amount": 1})
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["refund_id"], "RF-DEMO1234")
        d = self.b.request_redelivery({"order_id": "x", "items": []})
        self.assertEqual(d["status"], "accepted")
        self.assertEqual(d["redelivery_id"], "RD-DEMO1234")

    def test_refund_eta_is_not_invented(self):
        r = self.b.confirm_refund({"order_id": "x", "refund_amount": 1})
        self.assertIsNone(r["eta_days"])
        self.assertIs(r["eta_days_known"], False)

    def test_redelivery_eta_is_not_invented(self):
        d = self.b.request_redelivery({"order_id": "x", "items": []})
        self.assertIsNone(d["eta_minutes"])
        self.assertIs(d["eta_minutes_known"], False)

    def test_unknown_state_is_stated_not_hidden(self):
        """칸을 지우면 '모른다'가 아니라 '안 알려준다'가 된다(순번과 같은 판단)."""
        for out in (self.b.confirm_refund({}), self.b.request_redelivery({})):
            self.assertIn("알 수 없", out["note"])
            self.assertIn("고객에게 말하지 말 것", out["note"])

    def test_no_duration_number_survives_in_the_payload(self):
        """접수번호 밖에 숫자가 남으면 모델이 그것을 기간으로 읽는다."""
        for out in (self.b.confirm_refund({"order_id": "x", "refund_amount": 1}),
                    self.b.request_redelivery({"order_id": "x", "items": []})):
            for k, v in out.items():
                if isinstance(v, bool):
                    continue                   # 사실(모름 표기)은 수치가 아니다
                self.assertNotIsInstance(v, (int, float), k)
            text = json.dumps(out, ensure_ascii=False)
            # 남은 숫자는 자기 자신이 데모임을 밝히는 접수번호뿐이다.
            self.assertEqual(re.findall(r"\d+", text.replace("DEMO1234", "")), [])

    def test_source_marker_still_rides_along(self):
        for tool, args in (("confirm_refund", {"order_id": "x", "refund_amount": 1}),
                           ("request_redelivery", {"order_id": "x", "items": []})):
            self.assertEqual(self.b.dispatch(tool, args).get("data_source"), "demo")


class TestOverdueScriptNumbers(unittest.TestCase):
    """28차: 연체 안내 대본이 **고정 미납금액**을 말하게 돼 있었다.

    `PROMPT_OVERDUE` 는 '현재 미납금은 37,000원입니다'를 봇이 말할 문장으로
    들고 있었고, 이 시나리오는 툴을 쓰지 않으므로(`use_tools=False`) 그 금액은
    어디서도 조회되지 않는다 — 누구에게 걸어도 같은 숫자를 말하고 그 말은
    「은행이 알려 준 내 미납금」으로 들린다.
    """

    MONEY = re.compile(r"\d[\d,]*\s*원")

    def test_prompt_states_no_amount(self):
        self.assertIsNone(self.MONEY.search(engine.PROMPT_OVERDUE))
        self.assertNotIn("37,000", engine.PROMPT_OVERDUE)

    def test_prompt_tells_the_bot_what_to_say_instead(self):
        """문장을 지우기만 하면 모델이 빈자리를 채운다 — 명시해야 닫힌다."""
        self.assertIn("말하지 않는다", engine.PROMPT_OVERDUE)
        self.assertIn("확인되는 대로 안내드리겠습니다", engine.PROMPT_OVERDUE)

    def test_prompt_does_not_claim_a_lookup_that_never_happened(self):
        """'오늘 오전 조회된 내용' — 조회 경로가 없다(출처를 지어내지 않는다)."""
        self.assertNotIn("오늘 오전", engine.PROMPT_OVERDUE)

    def test_no_scenario_prompt_quotes_an_amount(self):
        for name in sorted(n for n in dir(engine) if n.startswith("PROMPT_")):
            with self.subTest(prompt=name):
                self.assertIsNone(self.MONEY.search(getattr(engine, name)))

    def test_overdue_scenario_still_uses_this_prompt(self):
        """대본을 고쳤어도 그 대본이 실제로 쓰이는지 확인한다."""
        src = io.open(os.path.join(ROOT, "api", "_engine.py"), encoding="utf-8").read()
        self.assertIn('scenario=="overdue": sysp=PROMPT_OVERDUE', src)


class TestConsoleScriptTableAgrees(unittest.TestCase):
    """화면의 대본표는 **봇이 하는 말의 문서**다 — 프롬프트와 어긋나면 거짓이다."""

    MONEY = re.compile(r"\d[\d,]*\s*원")
    PAGES = (("admin.html", "\n overdue:{", "\n  edge:["),
             ("react.html", "\n overdue:{", "\n};"))

    def _block(self, page, start, end):
        text = io.open(os.path.join(ROOT, "public", page), encoding="utf-8").read()
        self.assertIn(start, text, page)
        seg = text.split(start, 1)[1]
        return seg.split(end, 1)[0]

    def test_overdue_table_quotes_no_amount(self):
        for page, start, end in self.PAGES:
            with self.subTest(page=page):
                block = self._block(page, start, end)
                self.assertNotIn("37,000", block)
                self.assertIsNone(self.MONEY.search(block))

    def test_overdue_table_says_the_amount_is_not_looked_up(self):
        """숫자만 지우면 '왜 금액을 말하지 않는지'를 아무도 모른다."""
        block = self._block(*self.PAGES[0])
        self.assertIn("확인되는 대로 안내드리겠습니다", block)

    def test_overdue_table_does_not_promise_a_retrieval_we_do_not_have(self):
        """29차: 대본표가 「RAG 문서검색 후 … 생성형 답변」이라고 적고 있었다.

        연체 경로는 `use_tools=False` 이고 근거 문서를 검색하는 코드가 없다 —
        바로 아래 「배치 테스트」가 이 대본을 **실제 엔진에 보내** 검증하므로
        이 표는 설계도가 아니라 동작 문서로 읽힌다(없는 근거를 약속한다).
        """
        block = self._block(*self.PAGES[0])
        self.assertNotIn("RAG 문서검색 후", block)
        # 문구는 사람이 다듬는다 — 79b4f68(문안 정리)이 「근거 문서 미연결」을
        # 「근거 문서 미연동 상태」로 바꾸면서 이 회귀가 **적색인 채로** 커밋됐다.
        # 게이트가 봐야 하는 것은 철자가 아니라 **사실**이다: 표가 근거 문서가
        # 연결되지 않았다고 말하고 있는가(33차).
        self.assertRegex(block, "근거 문서[^']*미연(?:결|동)")

    def test_flow_diagram_marks_the_retrieval_node_as_unwired(self):
        """같은 화면이 서로 다른 말을 하면 안 된다 — 흐름도의 RAG 노드도 미연결."""
        for page, start, end in self.PAGES:
            with self.subTest(page=page):
                block = self._block(page, start, end)
                for seg in block.split("'RAG'")[1:]:
                    self.assertIn("미연결", seg.split("]", 1)[0], page)


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


class TestGeneralQuestionNumbers(unittest.TestCase):
    """29차: 수치가 박혀 있지 않아도 **답하라고 지시하면** 모델이 지어낸다.

    `PROMPT_OVERDUE` 는 「원리금/원금균등 차이, 인지세, 만기 전 상환 비용 등:
    간결·**정확하게** 2문장 이내로 답한다」를 들고 있었다. 28차가 세운 수치
    규칙은 '미납 금액·연체일수·납기일·조회 시각'만 열거했으므로 이 자리를
    덮지 못했고, 두 줄이 서로 어긋나 있었다. 인지세는 약정 금액 구간별 정액에
    은행·고객 분담이 걸리고 만기 전 상환 비용은 상품·경과기간별 요율이다 —
    둘 다 **고객 돈**이고, 이 경로는 툴도 근거 문서 검색도 쓰지 않는다.
    """

    RATE = re.compile(r"\d[\d,.]*\s*(?:%|퍼센트|원)")

    def test_general_questions_are_answered_as_concepts_only(self):
        self.assertIn("제도·용어의 뜻만", engine.PROMPT_OVERDUE)
        self.assertNotIn("간결·정확하게 2문장 이내로 답한다", engine.PROMPT_OVERDUE)

    def test_rates_and_taxes_are_explicitly_out_of_the_bot_mouth(self):
        self.assertIn("요율·세액·금액·기간", engine.PROMPT_OVERDUE)
        self.assertIn("확인 후 안내드리겠습니다", engine.PROMPT_OVERDUE)

    def test_the_number_rule_is_not_a_closed_list(self):
        """열거만 하면 목록 밖의 수치(수수료율·세액)가 그대로 빠져나간다."""
        self.assertIn("이에 한정하지 않는다", engine.PROMPT_OVERDUE)
        self.assertIn("수수료율", engine.PROMPT_OVERDUE)

    def test_every_scenario_prompt_carries_the_rule(self):
        """같은 공백이 세 군데였다 — 새 시나리오가 규칙 없이 늘지 않게 고정한다."""
        names = sorted(n for n in dir(engine) if n.startswith("PROMPT_"))
        self.assertGreaterEqual(len(names), 5)
        for name in names + ["_sys"]:
            text = (engine._sys("01012345678") if name == "_sys"
                    else getattr(engine, name))
            with self.subTest(prompt=name):
                self.assertTrue("말하지 않는다" in text or "지어내지" in text, name)

    def test_no_prompt_quotes_a_rate_or_an_amount(self):
        for name in sorted(n for n in dir(engine) if n.startswith("PROMPT_")):
            with self.subTest(prompt=name):
                self.assertIsNone(self.RATE.search(getattr(engine, name)))

    def test_overdue_turn_sends_the_rule_and_no_tools(self):
        """프롬프트 상수만 보면 '실제로 그 문장이 모델에 가는가'는 모른다."""
        seen = {}

        def fake_call(model, payload):
            seen.update(payload)
            return say("정확한 요율과 금액은 확인 후 안내드리겠습니다.")

        saved = engine._call
        engine._call = fake_call
        try:
            out = engine.run_turn([{"role": "user", "content": "인지세가 얼마인가요?"}],
                                  phone="01012345678", scenario="overdue")
        finally:
            engine._call = saved
        sysp = seen["systemInstruction"]["parts"][0]["text"]
        self.assertNotIn("tools", seen)        # 조회 경로가 아예 없다
        self.assertIn("제도·용어의 뜻만", sysp)
        self.assertIn("말하지 않는다", sysp)
        self.assertIsNone(self.RATE.search(sysp))
        self.assertTrue(out["reply"])


class TestPromptRule(unittest.TestCase):

    def test_system_prompt_forbids_unknown_numbers(self):
        sysp = engine._sys("01012345678")
        self.assertIn("툴 결과만 인용", sysp)
        self.assertIn("대기 순번", sysp)

    def test_prompt_still_tells_the_bot_to_transfer(self):
        self.assertIn("escalate_to_agent", engine._sys("01012345678"))


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
