# -*- coding: utf-8 -*-
"""상담사 전환 장부(`api/_escalation.py`)의 **보관 계약** 회귀 — 26차.

`tests/test_escalation.py` 가 정책·상태 전이·엔진 배선을 덮는다면, 여기서는
"장부가 자기가 적어 둔 말대로 동작하는가"를 본다. 모듈 문서는 두 가지를
주장하고 있었는데 둘 다 사실이 아니었다:

  1) "실개인정보 저장 없음 / 감사 기록은 메타만" — `summary` 는 LLM 이 만든
     자유 문장이고 시스템 프롬프트에 발신번호가 들어 있다. 200자로 자르기만
     했으므로 번호·주민번호·카드번호가 그대로 남을 수 있었다.
  2) 큐·감사 목록에 **상한이 없었다** — 웜 인스턴스가 전환을 처리할수록 자라서
     결국 진행 중인 통화가 메모리로 죽는다. 저장소의 다른 스토어는 전부 상한과
     유실 카운터를 갖고 있다(`_call_metrics`·`_audit`·`wellbeing.FAILED`).

그리고 휘발성 표기: 숫자만 내보내면 소비자는 영속 큐로 읽는다. `stats()` 가
`scope`/`volatile` 로 사실을 말하고 `/api/ops_stats` 가 그것을 전달하는지 본다.

네트워크 미사용(LLM 은 대역). 실행: python3 tests/test_escalation_store.py
"""
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _escalation as escalation  # noqa: E402
import _engine as engine  # noqa: E402
import ops_stats  # noqa: E402


def say(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


def call_tool(name, args):
    return {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


# ==========================================================================
# 1) 개인정보 — 가릴 수 없는 것은 보관하지 않는다
# ==========================================================================
class TestMasking(unittest.TestCase):

    def setUp(self):
        self.q = escalation.EscalationQueue()

    def test_phone_in_summary_is_masked(self):
        t = self.q.enqueue("s", "request", "고객 010-1234-5678 님 환불 요청")
        self.assertNotIn("010-1234-5678", t["summary"])
        self.assertIn("환불 요청", t["summary"])

    def test_rrn_and_card_in_summary_are_masked(self):
        t = self.q.enqueue("s", "request", "900101-1234567 / 4111 1111 1111 1111")
        blob = json.dumps(t, ensure_ascii=False)
        self.assertNotIn("1234567", blob)
        self.assertNotIn("4111 1111 1111 1111", blob)

    def test_masking_runs_before_truncation(self):
        """자른 뒤에 마스킹하면 경계에서 잘린 번호를 패턴으로 못 알아본다."""
        pad = "가" * (escalation.MAX_SUMMARY - 5)
        t = self.q.enqueue("s", "request", pad + "01012345678 뒤쪽")
        self.assertNotIn("01012345678", t["summary"])
        self.assertLessEqual(len(t["summary"]), escalation.MAX_SUMMARY)

    def test_labels_are_masked_and_capped(self):
        t = self.q.enqueue("sess 010-1234-5678", "x" * 300, "", "y" * 300)
        self.assertNotIn("010-1234-5678", t["session_id"])
        self.assertEqual(len(t["reason"]), escalation.MAX_LABEL)
        self.assertEqual(len(t["scenario"]), escalation.MAX_LABEL)

    def test_label_and_summary_accept_none(self):
        self.assertEqual(escalation._label(None), "")
        self.assertEqual(escalation._summary(None), "")

    def test_audit_note_is_masked(self):
        t = self.q.enqueue("s", "request")
        self.q.transition(t["id"], "assigned", actor="agent 010-1234-5678")
        self.assertNotIn("010-1234-5678",
                         json.dumps(self.q.audit_log(), ensure_ascii=False))

    def test_unmaskable_summary_is_dropped_not_stored_raw(self):
        """마스킹 자체가 불가능하면(모듈 부재·장애) 요약을 **싣지 않는다**.

        폴백에서 원문을 통과시키면 '마스킹한다'고 적힌 자리에 원문이 남는다
        (조용한 강등). 버린 사실은 `summary_dropped` 로 드러난다.
        """
        saved = escalation._scrub
        escalation._scrub = None
        try:
            t = self.q.enqueue("s", "request", "010-1234-5678 환불")
        finally:
            escalation._scrub = saved
        self.assertEqual(t["summary"], "")
        self.assertEqual(self.q.stats()["summary_dropped"], 1)

    def test_scrub_failure_does_not_break_enqueue(self):
        saved = escalation._scrub
        escalation._scrub = lambda v: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            t = self.q.enqueue("s", "request", "요약")
        finally:
            escalation._scrub = saved
        self.assertEqual(t["summary"], "")
        self.assertEqual(t["state"], "queued")      # 티켓 자체는 남는다

    def test_engine_ticket_never_carries_caller_number(self):
        saved_call, saved_q = engine._call, engine._ESC_QUEUE
        engine._ESC_QUEUE = self.q
        resp = [call_tool("escalate_to_agent",
                          {"reason": "request", "summary": "발신 01099998888 고객"}),
                say("연결하겠습니다.")]
        engine._call = lambda m, p: resp.pop(0)
        try:
            engine.run_turn([{"role": "user", "content": "상담사요"}],
                            phone="01099998888", scenario="handoff")
        finally:
            engine._call, engine._ESC_QUEUE = saved_call, saved_q
        self.assertNotIn("01099998888", json.dumps(self.q.list(), ensure_ascii=False))


# ==========================================================================
# 2) 상한 — 무한히 자라지 않고, 버린 것을 센다
# ==========================================================================
class TestLimits(unittest.TestCase):

    def setUp(self):
        self.q = escalation.EscalationQueue()

    def _fill(self, n, close=False):
        for _ in range(n):
            tk = self.q.enqueue("s", "request")
            if close:
                self.q.transition(tk["id"], "abandoned")

    def test_tickets_are_capped(self):
        self._fill(escalation.MAX_TICKETS + 20, close=True)
        s = self.q.stats()
        self.assertLessEqual(s["total"], escalation.MAX_TICKETS)
        self.assertEqual(s["capacity"], escalation.MAX_TICKETS)
        self.assertEqual(s["dropped"], 20)

    def test_closed_tickets_are_evicted_before_waiting_ones(self):
        """대기 중인 티켓을 먼저 버리면 '기다리는 고객'이 사라진다."""
        waiting = [self.q.enqueue("s", "request")["id"]
                   for _ in range(10)]
        self._fill(escalation.MAX_TICKETS + 30, close=True)
        alive = {t["id"] for t in self.q.list()}
        for tid in waiting:
            self.assertIn(tid, alive, tid)
        self.assertEqual(self.q.stats()["dropped_waiting"], 0)

    def test_dropping_a_waiting_ticket_is_counted_separately(self):
        """종결분이 없으면 대기 티켓을 버리게 된다 — 그 사실을 따로 센다."""
        self._fill(escalation.MAX_TICKETS + 3)
        s = self.q.stats()
        self.assertEqual(s["dropped"], 3)
        self.assertEqual(s["dropped_waiting"], 3)

    def test_audit_log_is_capped_and_loss_is_visible(self):
        over = 5
        for _ in range(escalation.MAX_AUDIT + over):
            self.q.enqueue("s", "request")          # 전이 기록 1건/티켓
        self.assertLessEqual(len(self.q.audit_log()), escalation.MAX_AUDIT)
        self.assertEqual(self.q.stats()["audit_dropped"], over)

    def test_oldest_is_evicted_first(self):
        first = self.q.enqueue("s", "request")["id"]
        self.q.transition(first, "abandoned")
        self._fill(escalation.MAX_TICKETS, close=True)
        self.assertNotIn(first, {t["id"] for t in self.q.list()})

    def test_ids_stay_unique_after_eviction(self):
        """버린 자리 번호를 재사용하면 서로 다른 통화가 같은 티켓 번호를 갖는다."""
        self._fill(escalation.MAX_TICKETS + 5, close=True)
        ids = [t["id"] for t in self.q.list()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("ESC-0001", ids)


# ==========================================================================
# 3) 기록 실패·휘발성을 드러내는가
# ==========================================================================
class TestHonestReporting(unittest.TestCase):

    def setUp(self):
        self.q = escalation.EscalationQueue()

    def test_stats_declares_volatility(self):
        s = self.q.stats()
        self.assertEqual(s["scope"], "instance")
        self.assertIs(s["volatile"], True)
        self.assertIsInstance(s["started_ts"], int)

    def test_note_failure_counts_and_keeps_type_name_only(self):
        self.q.note_failure("RuntimeError")
        s = self.q.stats()
        self.assertEqual(s["record_errors"], 1)
        self.assertEqual(self.q.last_error, "RuntimeError")

    def test_engine_logs_unrecorded_transfer(self):
        """큐가 죽어도 통화는 계속한다 — 그러나 '기록되지 않았다'를 적는다.

        예전에는 `if tk:` 로 성공만 적었다. 고객에게는 이미 상담사 연결을
        약속했는데 티켓이 없고, 응답 로그에서도 그 사실이 보이지 않았다.
        """
        class Broken:
            def enqueue(self, **kw):
                raise RuntimeError("queue down")

        saved_call, saved_q = engine._call, engine._ESC_QUEUE
        engine._ESC_QUEUE = Broken()
        resp = [call_tool("escalate_to_agent", {"reason": "request", "summary": "s"}),
                say("연결하겠습니다.")]
        engine._call = lambda m, p: resp.pop(0)
        try:
            r = engine.run_turn([{"role": "user", "content": "상담사요"}],
                                scenario="handoff")
        finally:
            engine._call, engine._ESC_QUEUE = saved_call, saved_q
        esc = [e for e in r["log"] if e["turn"] == "escalation"]
        self.assertEqual(len(esc), 1)
        self.assertIs(esc[0]["recorded"], False)
        self.assertIsNone(esc[0]["ticket"])
        self.assertTrue(r["transferred"])           # 통화 흐름은 그대로

    def test_guard_path_also_logs_unrecorded_transfer(self):
        """가드 차단 → 전환 경로도 같은 규약이어야 한다(두 자리 중 한쪽만 고치면 반쪽)."""
        class Broken:
            def enqueue(self, **kw):
                raise RuntimeError("queue down")

        saved_call, saved_q = engine._call, engine._ESC_QUEUE
        engine._ESC_QUEUE = Broken()
        resp = [call_tool("confirm_refund", {"order_id": "SSG-1",
                                             "refund_amount": 159000,
                                             "user_confirmed": True}),
                say("상담사에게 연결하겠습니다.")]
        engine._call = lambda m, p: resp.pop(0)
        try:
            r = engine.run_turn([{"role": "user", "content": "네 환불해 주세요"}],
                                scenario="refund")
        finally:
            engine._call, engine._ESC_QUEUE = saved_call, saved_q
        esc = [e for e in r["log"] if e["turn"] == "escalation"]
        self.assertEqual(len(esc), 1)
        self.assertIs(esc[0]["recorded"], False)
        self.assertIn("견적", esc[0]["reason"])     # 차단 사유가 남는다
        self.assertTrue(r["transferred"])

    def test_queue_records_failure_when_enqueue_raises(self):
        class Flaky(escalation.EscalationQueue):
            def enqueue(self, **kw):
                raise RuntimeError("boom")

        q = Flaky()
        saved = engine._ESC_QUEUE
        engine._ESC_QUEUE = q
        try:
            self.assertIsNone(engine._esc_enqueue("request", "s", "handoff"))
        finally:
            engine._ESC_QUEUE = saved
        self.assertEqual(q.stats()["record_errors"], 1)
        self.assertEqual(q.last_error, "RuntimeError")

    def test_missing_queue_returns_none_without_raising(self):
        """큐 모듈 자체가 없으면 셀 곳도 없다 — 그때는 `/api/ops_stats` 의
        `escalation.source="unavailable"` 과 이 통화의 `recorded:false` 가 말한다."""
        saved = engine._ESC_QUEUE
        engine._ESC_QUEUE = None
        try:
            self.assertIsNone(engine._esc_enqueue("request", "s", "handoff"))
        finally:
            engine._ESC_QUEUE = saved

    def test_ops_stats_exposes_loss_and_scope(self):
        s = ops_stats.get_ops_summary()["escalation"]
        for k in ("dropped", "dropped_waiting", "record_errors"):
            self.assertIn(k, s)
            self.assertIsInstance(s[k], int)
        self.assertEqual(s["scope"], "instance")
        self.assertIs(s["volatile"], True)

    def test_scope_survives_source_failure(self):
        """소스가 죽어도 '인스턴스 메모리'라는 성질은 변하지 않는다."""
        n = ops_stats._norm_stats(None, ops_stats.ESCALATION_KEYS,
                                  ops_stats.ESCALATION_META)
        self.assertEqual(n["source"], "unavailable")
        self.assertEqual(n["scope"], "instance")
        self.assertIs(n["volatile"], True)
        for k in ops_stats.ESCALATION_KEYS:
            self.assertEqual(n[k], 0)

    def test_console_says_queue_is_volatile(self):
        """화면이 숫자만 보여주면 심사·운영자가 영속 큐로 읽는다."""
        html = open(os.path.join(ROOT, "public", "admin.html"),
                    encoding="utf-8").read()
        i = html.find("상담원 연결 큐 · 녹취/감사 현황")
        self.assertGreater(i, 0)
        card = html[i:i + 1200]
        self.assertIn("재시작", card)

    def test_module_still_has_no_network_import(self):
        src = open(os.path.join(ROOT, "api", "_escalation.py"),
                   encoding="utf-8").read()
        for banned in ("urllib", "requests", "socket", "http.client"):
            self.assertNotIn(banned, src, banned)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
