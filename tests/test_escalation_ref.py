# -*- coding: utf-8 -*-
"""상담사 전환 티켓의 통화 참조 회귀 (32차) — LLM·네트워크 미호출.

왜 이 파일이 필요한가
  `_engine._esc_enqueue` 는 티켓의 `session_id` 를 `"sim-%s" % scenario` **한
  가지**로 적었다. 그 라벨은 호출부가 `/api/chat`·`sim_call` 뿐일 때 쓴 것인데,
  실통화(`/api/voice`)가 같은 경로로 들어오면서 그대로 물려받았다. 결과가 둘 다
  나쁘다.

    (1) 한 인스턴스의 모든 티켓이 `sim-refund` 다 — **어느 통화인지 알 수 없다.**
        고객에게 "상담사에게 연결해 드리겠습니다"라고 말한 그 통화를 되짚을
        근거가 장부에 없다(전환 티켓은 그 약속의 유일한 기록이다).
    (2) 라벨이 `sim-` 이라 **실통화 전환이 시연으로 읽힌다** — 운영자가 데모가
        만든 티켓으로 보고 넘긴다.

  17·19·30차가 반복해 지적한 '규칙이 절반만 지켜진' 모양이다(sim 경로용 라벨을
  실경로가 그대로 쓴다). 통화ID 는 CPaaS 가 주는 외부 값이라 번호가 섞여 들어올
  수 있으므로 원문이 아니라 **지문**을 적는다(지표 수집기와 같은 값).

실행: python3 -m pytest tests/test_escalation_ref.py -q
"""
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _engine as engine  # noqa: E402
import _escalation  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._restore = []
        _escalation.QUEUE.__init__()          # 장부 비우기(같은 싱글턴)

        def watchdog(*a, **kw):
            raise AssertionError("네트워크 호출 금지")

        self.patch(urllib.request, "urlopen", watchdog)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        _escalation.QUEUE.__init__()

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def transfer_turn(self, **kw):
        """모델이 escalate_to_agent 를 부르는 1홉 통화."""
        responses = [
            {"candidates": [{"content": {"parts": [{"functionCall": {
                "name": "escalate_to_agent",
                "args": {"reason": "request", "summary": "상담사 요청"}}}]}}],
             "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "연결해 드릴게요."}]}}],
             "usageMetadata": {}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        return engine.run_turn([{"role": "user", "content": "상담사 바꿔 주세요"}],
                               scenario="refund", **kw)


class TestRefLabel(unittest.TestCase):
    def test_a_given_ref_is_used(self):
        self.assertEqual(engine._esc_ref("call-abc123", "refund"), "call-abc123")

    def test_no_ref_falls_back_to_the_sim_label(self):
        """기존 호출부(`/api/chat`·sim)는 그대로 — 그쪽은 실제로 시연이다."""
        self.assertEqual(engine._esc_ref(None, "refund"), "sim-refund")
        self.assertEqual(engine._esc_ref(None, ""), "sim-call")

    def test_junk_refs_do_not_become_the_label(self):
        for bad in ("", "   ", 5, [], {}):
            self.assertEqual(engine._esc_ref(bad, "refund"), "sim-refund", repr(bad))

    def test_whitespace_is_trimmed(self):
        self.assertEqual(engine._esc_ref("  call-x  ", "refund"), "call-x")


class TestTicketCarriesTheCall(Base):
    def test_the_ticket_records_the_call_reference(self):
        r = self.transfer_turn(session_ref="call-abc123def456")
        tickets = _escalation.QUEUE.list()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["session_id"], "call-abc123def456")
        self.assertTrue(r["transferred"])

    def test_a_live_call_is_not_labelled_as_a_simulation(self):
        self.transfer_turn(session_ref="call-abc123def456")
        self.assertNotIn("sim", _escalation.QUEUE.list()[0]["session_id"])

    def test_two_calls_produce_two_distinguishable_tickets(self):
        """같은 라벨이면 티켓을 통화로 나눌 수 없다(예전 동작)."""
        self.transfer_turn(session_ref="call-aaa")
        self.transfer_turn(session_ref="call-bbb")
        refs = {t["session_id"] for t in _escalation.QUEUE.list()}
        self.assertEqual(refs, {"call-aaa", "call-bbb"})

    def test_the_guard_path_also_records_the_reference(self):
        """가드가 막아 전환하는 쪽(돈이 걸린 경로)도 같은 참조를 남긴다."""
        responses = [
            {"candidates": [{"content": {"parts": [{"functionCall": {
                "name": "confirm_refund",
                "args": {"order_id": "A", "refund_amount": 99000,
                         "user_confirmed": True}}}]}}], "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "상담사 연결할게요."}]}}],
             "usageMetadata": {}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        engine.run_turn([{"role": "user", "content": "네 동의합니다"}],
                        scenario="refund", session_ref="call-guard1",
                        mem_prior={"awaiting": True, "quoted_amount": 15900})
        self.assertEqual(_escalation.QUEUE.list()[0]["session_id"], "call-guard1")

    def test_the_sim_path_is_unchanged(self):
        self.transfer_turn()
        self.assertEqual(_escalation.QUEUE.list()[0]["session_id"], "sim-refund")

    def test_the_reference_carries_no_phone_number(self):
        """참조는 지문이어야 한다 — 원문 통화ID 를 넘기면 번호가 장부에 남는다."""
        import voice
        ref = voice._session_ref("CA-01011112222-7")
        self.transfer_turn(session_ref=ref)
        blob = repr(_escalation.QUEUE.list()[0])
        self.assertNotIn("01011112222", blob)

    def test_the_ticket_still_records_when_the_ref_is_junk(self):
        """참조가 이상해도 티켓은 남는다 — 전환이 기록되지 않는 쪽이 더 나쁘다."""
        self.transfer_turn(session_ref=12345)
        self.assertEqual(len(_escalation.QUEUE.list()), 1)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
