# -*- coding: utf-8 -*-
"""상담사 에스컬레이션 회귀 테스트 — 의존성 0, 네트워크 미사용.

검증 대상
  (COMMERCIAL_READINESS '상담사 에스컬레이션 실동작 검증(sim 시나리오 회귀 테스트)'
   + '테스트 커버리지' 항목의 escalation·engine)

  1) 정책(EscalationPolicy) — 4개 트리거의 우선순위·경계값
  2) 큐(EscalationQueue)   — 상태 전이 계약·불변식·감사 기록
  3) 엔진 배선(engine)     — 툴 전환·가드 전환·홉 소진 전환이 실제로 티켓을 남기는가
  4) sim 시나리오          — handoff 스크립트가 전환으로 끝나는가(텔레포니 0원)
  5) 안전                  — LLM 호출 없음(네트워크 차단), 개인정보(발신번호) 미기록,
                             큐 장애가 통화를 죽이지 않음

LLM 은 호출하지 않는다. engine._call 을 대역으로 교체하고, 교체가 풀린 상태에서
네트워크를 타면 즉시 실패하도록 감시 대역을 함께 건다.

실행: python3 tests/test_escalation.py
"""
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import escalation  # noqa: E402
import engine  # noqa: E402
import sim_call  # noqa: E402


# --------------------------------------------------------------------------
# LLM 응답 대역 — Gemini generateContent 응답 모양만 흉내낸다.
# --------------------------------------------------------------------------
def say(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5}}


def call_tool(name, args, text=""):
    parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": name, "args": args}}]
    return {"candidates": [{"content": {"parts": parts}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5}}


class EngineHarness(unittest.TestCase):
    """engine._call 을 스크립트된 응답 큐로 교체하고, 큐를 매번 새로 만든다."""

    def setUp(self):
        self.q = escalation.EscalationQueue()
        self._saved_queue = engine._ESC_QUEUE
        self._saved_call = engine._call
        engine._ESC_QUEUE = self.q
        self.responses = []
        self.seen_payloads = []

        def fake_call(model, payload):
            self.seen_payloads.append(payload)
            if not self.responses:
                raise AssertionError("대역 응답 소진 — 예상보다 많은 LLM 호출")
            return self.responses.pop(0)

        engine._call = fake_call

    def tearDown(self):
        engine._call = self._saved_call
        engine._ESC_QUEUE = self._saved_queue


# ==========================================================================
# 1) 정책
# ==========================================================================
class TestPolicy(unittest.TestCase):

    def setUp(self):
        self.pol = escalation.EscalationPolicy()

    def test_handoff_keyword_triggers_request(self):
        for text in ["상담사 바꿔 주세요", "그냥 사람이랑 얘기할게요", "직원 연결해줘",
                     "책임자 나오라고 해", "진짜 사람 좀 바꿔요"]:
            hit = self.pol.evaluate(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit["reason"], "request", text)

    def test_keyword_match_ignores_spaces(self):
        # 전사 결과의 띄어쓰기는 불안정하다 — 공백 제거 후 부분일치여야 한다
        self.assertEqual(self.pol.evaluate("상 담 사 바꿔주세요")["reason"], "request")
        self.assertEqual(self.pol.evaluate("사 람 이 랑 얘기")["reason"], "request")

    def test_sensitive_outranks_request(self):
        # 같은 문장에 두 키워드가 있으면 민감 주제가 이긴다(응대 범위 밖 → 즉시 전환)
        hit = self.pol.evaluate("상담원 불러요, 소송할 겁니다")
        self.assertEqual(hit["reason"], "sensitive")
        self.assertIn("소송", hit["detail"])

    def test_sensitive_keywords(self):
        for text in ["법적 대응할게요", "고소하겠습니다", "언론에 제보할 거예요",
                     "신고할 거예요", "금감원에 알리겠습니다"]:
            self.assertEqual(self.pol.evaluate(text)["reason"], "sensitive", text)

    def test_low_confidence_boundary(self):
        # 임계값 미만만 저신뢰. 경계값(==)은 통과여야 한다.
        self.assertIsNone(self.pol.evaluate("주문 조회요", confidence=escalation.CONF_THRESHOLD))
        hit = self.pol.evaluate("주문 조회요", confidence=escalation.CONF_THRESHOLD - 0.01)
        self.assertEqual(hit["reason"], "low_confidence")

    def test_confidence_none_is_not_low(self):
        # 신뢰도 미제공(None)을 저신뢰로 오판하면 멀쩡한 통화가 전부 전환된다
        self.assertIsNone(self.pol.evaluate("주문 조회요", confidence=None))

    def test_fallback_streak_boundary(self):
        lim = escalation.FALLBACK_LIMIT
        self.assertIsNone(self.pol.evaluate("음...", confidence=0.9, fallback_streak=lim))
        hit = self.pol.evaluate("음...", confidence=0.9, fallback_streak=lim + 1)
        self.assertEqual(hit["reason"], "repeated_fallback")

    def test_normal_utterance_does_not_escalate(self):
        for text in ["주문 조회요", "언제 도착하나요", "네 알겠습니다", ""]:
            self.assertIsNone(self.pol.evaluate(text, confidence=0.9, fallback_streak=0), text)

    def test_none_text_is_safe(self):
        self.assertIsNone(self.pol.evaluate(None, confidence=0.9))

    def test_custom_thresholds_are_honored(self):
        strict = escalation.EscalationPolicy(conf_threshold=0.9, fallback_limit=0)
        self.assertEqual(strict.evaluate("주문 조회요", confidence=0.8)["reason"], "low_confidence")
        self.assertEqual(strict.evaluate("음...", confidence=1.0, fallback_streak=1)["reason"],
                         "repeated_fallback")

    def test_trigger_priority_order(self):
        # request > low_confidence: 저신뢰여도 명시 요청이면 이유는 'request' 여야
        # 상담사에게 넘어가는 맥락이 정확해진다.
        hit = self.pol.evaluate("상담사요", confidence=0.1)
        self.assertEqual(hit["reason"], "request")


# ==========================================================================
# 2) 큐
# ==========================================================================
class TestQueue(unittest.TestCase):

    def setUp(self):
        self.q = escalation.EscalationQueue()

    def test_enqueue_starts_queued_with_id(self):
        t = self.q.enqueue("sess-1", "request", "요약")
        self.assertEqual(t["state"], "queued")
        self.assertTrue(t["id"].startswith("ESC-"))
        self.assertEqual(t["session_id"], "sess-1")

    def test_ids_are_unique_and_sequential(self):
        ids = [self.q.enqueue("s", "request")["id"] for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids, sorted(ids))

    def test_summary_is_truncated(self):
        # 상담내용 원문이 통째로 쌓이지 않도록 요약은 200자에서 잘린다
        t = self.q.enqueue("s", "request", "가" * 500)
        self.assertEqual(len(t["summary"]), 200)

    def test_summary_none_becomes_empty(self):
        self.assertEqual(self.q.enqueue("s", "request", None)["summary"], "")

    def test_happy_path_transitions(self):
        t = self.q.enqueue("s", "request")
        t = self.q.transition(t["id"], "assigned", actor="agent-01")
        self.assertEqual(t["state"], "assigned")
        t = self.q.transition(t["id"], "resolved", actor="agent-01")
        self.assertEqual(t["state"], "resolved")

    def test_abandon_from_either_open_state(self):
        a = self.q.enqueue("s", "request")
        self.assertEqual(self.q.transition(a["id"], "abandoned")["state"], "abandoned")
        b = self.q.enqueue("s", "request")
        self.q.transition(b["id"], "assigned")
        self.assertEqual(self.q.transition(b["id"], "abandoned")["state"], "abandoned")

    def test_terminal_states_are_final(self):
        for terminal in ("resolved", "abandoned"):
            t = self.q.enqueue("s", "request")
            self.q.transition(t["id"], "assigned")
            if terminal == "resolved":
                self.q.transition(t["id"], "resolved")
            else:
                self.q.transition(t["id"], "abandoned")
            for nxt in ("queued", "assigned", "resolved", "abandoned"):
                with self.assertRaises(ValueError):
                    self.q.transition(t["id"], nxt)

    def test_skipping_assigned_is_rejected(self):
        # queued → resolved 는 '상담사가 받지도 않았는데 해결됨'이 되므로 막는다
        t = self.q.enqueue("s", "request")
        with self.assertRaises(ValueError):
            self.q.transition(t["id"], "resolved")
        self.assertEqual(self.q.list()[0]["state"], "queued")

    def test_unknown_state_is_rejected(self):
        t = self.q.enqueue("s", "request")
        with self.assertRaises(ValueError):
            self.q.transition(t["id"], "escalated_again")

    def test_unknown_ticket_raises(self):
        with self.assertRaises(KeyError):
            self.q.transition("ESC-9999", "assigned")

    def test_list_filters_and_orders(self):
        a = self.q.enqueue("s1", "request")
        b = self.q.enqueue("s2", "sensitive")
        self.q.transition(b["id"], "assigned")
        self.assertEqual([t["id"] for t in self.q.list()], [a["id"], b["id"]])
        self.assertEqual([t["id"] for t in self.q.list(state="queued")], [a["id"]])
        self.assertEqual([t["id"] for t in self.q.list(state="assigned")], [b["id"]])
        self.assertEqual(self.q.list(state="resolved"), [])

    def test_stats_counts_every_state(self):
        self.assertEqual(self.q.stats(),
                         {"queued": 0, "assigned": 0, "resolved": 0, "abandoned": 0, "total": 0})
        t = self.q.enqueue("s", "request")
        self.q.enqueue("s", "sensitive")
        self.q.transition(t["id"], "assigned")
        self.q.transition(t["id"], "resolved")
        s = self.q.stats()
        self.assertEqual((s["queued"], s["assigned"], s["resolved"], s["total"]), (1, 0, 1, 2))

    def test_audit_records_every_transition(self):
        t = self.q.enqueue("s", "request")
        self.q.transition(t["id"], "assigned", actor="agent-01")
        self.q.transition(t["id"], "resolved", actor="agent-01")
        log = self.q.audit_log()
        self.assertEqual([(e["from"], e["to"]) for e in log],
                         [(None, "queued"), ("queued", "assigned"), ("assigned", "resolved")])
        self.assertTrue(all(e["ticket"] == t["id"] for e in log))

    def test_rejected_transition_leaves_no_audit_trace(self):
        t = self.q.enqueue("s", "request")
        before = len(self.q.audit_log())
        with self.assertRaises(ValueError):
            self.q.transition(t["id"], "resolved")
        self.assertEqual(len(self.q.audit_log()), before)

    def test_audit_log_is_append_only_snapshot(self):
        # 반환값을 건드려도 내부 기록은 바뀌지 않아야 한다(감사 기록 변조 방지)
        self.q.enqueue("s", "request")
        snap = self.q.audit_log()
        snap.append({"forged": True})
        snap[0]["to"] = "tampered"
        self.assertEqual(len(self.q.audit_log()), 1)
        self.assertEqual(self.q.audit_log()[0]["to"], "queued")

    def test_returned_ticket_is_a_copy(self):
        t = self.q.enqueue("s", "request")
        t["state"] = "resolved"
        self.assertEqual(self.q.list()[0]["state"], "queued")

    def test_no_delete_or_update_api(self):
        # append-only 계약: 삭제·수정 메서드가 생기면 이 테스트가 깨져야 한다
        for name in ("delete", "remove", "purge", "clear", "update", "set_state"):
            self.assertFalse(hasattr(self.q, name), name)

    def test_module_queue_is_shared_instance(self):
        self.assertIsInstance(escalation.QUEUE, escalation.EscalationQueue)


# ==========================================================================
# 3) 엔진 배선
# ==========================================================================
class TestEngineEscalation(EngineHarness):

    def test_tool_escalation_creates_ticket_and_transfers(self):
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "고객이 상담사 연결 요청"}),
            say("상담사에게 연결해 드릴게요."),
        ]
        r = engine.run_turn([{"role": "user", "content": "상담사 바꿔 주세요"}], scenario="handoff")
        self.assertTrue(r["transferred"])
        tickets = self.q.list()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["reason"], "request")
        self.assertEqual(tickets[0]["scenario"], "handoff")
        self.assertIn({"turn": "escalation", "ticket": tickets[0]["id"], "reason": "request"}, r["log"])

    def test_transferred_survives_next_turn_via_history(self):
        # 전환 사실은 messages 에 남은 tool 결과로 복원돼야 한다(서버리스=무상태)
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "s"}),
            say("연결해 드릴게요."),
        ]
        r1 = engine.run_turn([{"role": "user", "content": "상담사 바꿔 주세요"}], scenario="handoff")
        msgs = r1["messages"] + [{"role": "user", "content": "네"}]
        self.responses = [say("잠시만 기다려 주세요.")]
        r2 = engine.run_turn(msgs, scenario="handoff")
        self.assertTrue(r2["transferred"])

    def test_guard_block_without_quote_escalates(self):
        # 견적 없이 환불 확정 시도 → 접수 차단 + 상담사 전환 + 감사로그
        self.responses = [
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 159000,
                                         "user_confirmed": True}),
            say("상담사에게 연결해 드릴게요."),
        ]
        r = engine.run_turn([{"role": "user", "content": "네 환불해 주세요"}], scenario="refund")
        self.assertTrue(r["transferred"])
        self.assertEqual(len(self.q.list()), 1)
        self.assertEqual(self.q.list()[0]["reason"], "guard")
        self.assertEqual([a["decision"] for a in r["audit"]], ["block"])

    def test_guard_block_amount_mismatch_escalates(self):
        msgs = [
            {"role": "user", "content": "환불해 주세요"},
            {"role": "tool", "name": "get_refund_policy",
             "content": json.dumps({"eligible": True, "max_refund": 159000})},
            {"role": "tool", "name": "quote_refund",
             "content": json.dumps({"refund_amount": 159000})},
            {"role": "user", "content": "네 50만 원 환불해 주세요"},
        ]
        self.responses = [
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 500000,
                                         "user_confirmed": True}),
            say("확인이 필요해 상담사에게 연결하겠습니다."),
        ]
        r = engine.run_turn(msgs, scenario="refund_over")
        self.assertTrue(r["transferred"])
        self.assertEqual(len(self.q.list()), 1)
        self.assertIn("불일치", r["audit"][0]["reason"])

    def test_guard_block_without_consent_does_not_escalate(self):
        # 동의 미확인은 되물으면 되는 상황 — 상담사 전환까지 갈 일이 아니다
        self.responses = [
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 159000,
                                         "user_confirmed": False}),
            say("환불 금액을 먼저 확인해 드릴게요."),
        ]
        r = engine.run_turn([{"role": "user", "content": "환불이요"}], scenario="refund")
        self.assertFalse(r["transferred"])
        self.assertEqual(self.q.list(), [])
        self.assertEqual(r["audit"][0]["decision"], "block")

    def test_hop_exhaustion_transfers(self):
        # 툴 호출만 반복하며 답을 못 내면 무한루프 대신 상담사 전환으로 끝난다
        self.responses = [call_tool("lookup_recent_order", {"phone": "01012345678"})
                          for _ in range(5)]
        r = engine.run_turn([{"role": "user", "content": "주문이요"}], max_hops=5)
        self.assertTrue(r["transferred"])
        self.assertIn("상담사", r["reply"])

    def test_normal_turn_creates_no_ticket(self):
        self.responses = [say("어제 주문은 배송완료 상태입니다.")]
        r = engine.run_turn([{"role": "user", "content": "배송 조회요"}], scenario="order")
        self.assertFalse(r["transferred"])
        self.assertEqual(self.q.list(), [])

    def test_ticket_does_not_store_caller_number(self):
        # 개인정보 최소화: 발신번호가 티켓에 흘러들면 안 된다
        phone = "01099998888"
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "연결 요청"}),
            say("연결하겠습니다."),
        ]
        engine.run_turn([{"role": "user", "content": "상담사요"}], phone=phone, scenario="handoff")
        self.assertNotIn(phone, json.dumps(self.q.list(), ensure_ascii=False))
        self.assertNotIn(phone, json.dumps(self.q.audit_log(), ensure_ascii=False))

    def test_ticket_summary_is_truncated_from_engine(self):
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "나" * 400}),
            say("연결하겠습니다."),
        ]
        engine.run_turn([{"role": "user", "content": "상담사요"}], scenario="handoff")
        self.assertEqual(len(self.q.list()[0]["summary"]), 200)

    def test_queue_failure_does_not_break_the_call(self):
        # 큐 장애로 통화가 끊기면 안 된다(가용성 우선, best-effort 기록)
        class Broken:
            def enqueue(self, **kw):
                raise RuntimeError("queue down")

        engine._ESC_QUEUE = Broken()
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "s"}),
            say("연결하겠습니다."),
        ]
        r = engine.run_turn([{"role": "user", "content": "상담사요"}], scenario="handoff")
        self.assertTrue(r["transferred"])
        self.assertEqual(r["reply"], "연결하겠습니다.")

    def test_missing_queue_module_is_tolerated(self):
        engine._ESC_QUEUE = None
        self.assertIsNone(engine._esc_enqueue("request", "s", "handoff"))
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "s"}),
            say("연결하겠습니다."),
        ]
        r = engine.run_turn([{"role": "user", "content": "상담사요"}], scenario="handoff")
        self.assertTrue(r["transferred"])

    def test_tool_free_scenarios_cannot_call_tools(self):
        # 전용 프롬프트 경로(툴 미사용)에 함수 선언이 새어나가면 안 된다
        for scenario in ("integrity", "overdue"):
            self.responses = [say("네, 안내드리겠습니다.")]
            engine.run_turn([{"role": "user", "content": "여보세요"}], scenario=scenario)
            self.assertNotIn("tools", self.seen_payloads[-1], scenario)

    def test_cs_scenario_declares_escalation_tool(self):
        self.responses = [say("네, 확인해 드릴게요.")]
        engine.run_turn([{"role": "user", "content": "주문이요"}], scenario="refund")
        names = [f["name"] for f in self.seen_payloads[-1]["tools"][0]["functionDeclarations"]]
        self.assertIn("escalate_to_agent", names)


# ==========================================================================
# 4) sim 시나리오 회귀 (텔레포니 0원)
# ==========================================================================
class TestSimHandoff(EngineHarness):

    def test_handoff_script_ends_in_transfer(self):
        self.responses = [
            call_tool("escalate_to_agent", {"reason": "request", "summary": "상담사 연결 요청"}),
            say("상담사에게 연결해 드리겠습니다."),
        ]
        out = sim_call.simulate("handoff")
        self.assertTrue(out["ok"])
        self.assertTrue(out["transferred"])
        self.assertEqual(out["billing"], "0원(텔레포니 미발생)")
        # 첫 발화에서 전환됐으므로 뒤 스크립트는 소비되지 않는다
        self.assertEqual(len(out["turns"]), 1)
        self.assertEqual(len(self.q.list()), 1)

    def test_handoff_script_contains_policy_keywords(self):
        # 정책 키워드와 sim 스크립트가 따로 놀면 회귀를 못 잡는다
        pol = escalation.EscalationPolicy()
        for utter in sim_call.SCRIPTS["handoff"]:
            self.assertEqual(pol.evaluate(utter)["reason"], "request", utter)

    def test_unknown_scenario_falls_back_to_refund(self):
        self.responses = [say("네, 확인해 드릴게요.")] * len(sim_call.SCRIPTS["refund"])
        out = sim_call.simulate("no-such-scenario")
        self.assertEqual(len(out["turns"]), len(sim_call.SCRIPTS["refund"]))
        self.assertFalse(out["transferred"])


# ==========================================================================
# 5) 안전 — 네트워크 미사용
# ==========================================================================
class TestNoNetwork(unittest.TestCase):

    def test_call_requires_api_key(self):
        # 키 없이 호출하면 네트워크를 타기 전에 실패한다(테스트 환경에서 실호출 방지)
        saved = {k: os.environ.pop(k, None) for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY")}
        try:
            with self.assertRaises(RuntimeError):
                engine._call("gemini-2.5-flash", {})
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_escalation_module_has_no_network_import(self):
        src = open(os.path.join(ROOT, "api", "escalation.py"), encoding="utf-8").read()
        for banned in ("urllib", "requests", "socket", "http.client"):
            self.assertNotIn(banned, src, banned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
