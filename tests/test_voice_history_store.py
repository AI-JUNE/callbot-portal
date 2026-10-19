# -*- coding: utf-8 -*-
"""세션이 **저장하는** 대화 이력의 상한 회귀 (32차) — 네트워크 미사용.

왜 이 파일이 필요한가
  30차는 세션 **건수**에 상한을 뒀고(몇 통화를 들고 있나), 31차는 **모델에
  보내는 창**에 상한을 뒀다(한 턴에 얼마를 보내나). 그 사이에 한 칸이 비어
  있었다 — 세션이 **들고 있는** 이력이다. 31차 보고서가 한계 ②로 적어 둔
  자리이고, 그동안 한 통화의 전사 원문은 통화가 끝날 때까지(종료 이벤트가
  오지 않으면 `SESSION_TTL` 30분까지) 전부 메모리에 쌓였다. 영속 저장소가
  [승인 필요]인 동안 **보관 기간은 코드가 적는 것**인데 여기에는 적혀 있지
  않았다.

  31차가 저장 쪽을 손대지 않은 이유는 fail-open 이다: 가드의 기억은 전체
  이력에서 계산되므로 이력을 버리면 견적·한도가 함께 사라지고, 한도만 사라진
  조합은 **차단이 아니라 승인** 쪽으로 틀린다. 그래서 버리지 않고 접어서
  넘긴다(`_engine.trim_history` → `mem_prior`). 이 파일은 그 배선이 실제
  이벤트 경로에서 돌아가는지, 그리고 자른 사실이 조용히 지나가지 않는지를
  고정한다.

실행: python3 -m pytest tests/test_voice_history_store.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice  # noqa: E402
import _engine as engine  # noqa: E402

from test_voice_session import Base as SessionBase  # noqa: E402
from test_voice import SAME_ORIGIN, get  # noqa: E402

TRIM_KEYS = ("expired", "evicted", "trimmed", "trimmed_msgs")


class Base(SessionBase):
    def setUp(self):
        super().setUp()
        voice.SESSION_STATS.update({k: 0 for k in TRIM_KEYS})
        voice.SESSION_STATS["last_reason"] = None
        # 대역을 끼우는 모듈 전역은 **되돌린다** — 남겨 두면 다른 파일의 회귀가
        # 조용히 다른 것을 검사하게 된다(순서에 따라 통과/실패가 바뀐다).
        self._tu = voice._transcribe_url
        for k in ("CALLBOT_HISTORY_MAX", "CALLBOT_HISTORY_MAX_CHARS"):
            os.environ.pop(k, None)

    def tearDown(self):
        voice._transcribe_url = self._tu
        voice.SESSION_STATS.update({k: 0 for k in TRIM_KEYS})
        super().tearDown()

    def stub_engine(self, extra=None, reply="네"):
        """턴마다 호출 인자를 기록하는 대역. `extra` 는 이력에 덧붙일 메시지."""
        seen = []

        def run_turn(messages, phone="", scenario="refund", **kw):
            seen.append({"messages": list(messages),
                         "mem_prior": kw.get("mem_prior"),
                         "session_ref": kw.get("session_ref")})
            msgs = list(messages) + [{"role": "assistant", "content": reply}]
            if extra and len(seen) == 1:
                msgs += list(extra)
            return {"messages": msgs, "reply": reply, "transferred": False}

        voice.run_turn = run_turn
        return seen

    def speak(self, n, cid="c1"):
        """같은 통화를 n턴 이어간다(실제 이벤트 경로)."""
        for i in range(n):
            voice.handle_event({"type": "speech", "call_id": cid, "from": "01011112222",
                                "scenario": "refund", "text": "발화 %d" % i})

    def quote_rows(self, amount=15900):
        """견적 1건이 들어 있는 홉(functionCall + functionResponse)."""
        return [{"role": "assistant", "content": "",
                 "tool_calls": [{"id": "quote_refund", "name": "quote_refund",
                                 "input": {}}]},
                {"role": "tool", "tool_call_id": "quote_refund",
                 "name": "quote_refund",
                 "content": "{\"refund_amount\": %d}" % amount}]


# ==========================================================================
# 1) 저장 상한 — 통화가 길어져도 세션이 무한히 자라지 않는다
# ==========================================================================
class TestStoredHistoryCap(Base):
    def test_a_long_call_does_not_grow_without_bound(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "8"
        self.stub_engine()
        self.speak(60)
        kept = voice._Session._mem["c1"]["messages"]
        self.assertLessEqual(len(kept), 8, "저장 이력에 상한이 없다")

    def test_the_cap_is_actually_reached(self):
        """상한에 닿지 않으면 위 테스트는 아무것도 보지 않는다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "8"
        self.stub_engine()
        self.speak(30)
        self.assertGreater(voice.SESSION_STATS["trimmed_msgs"], 0)

    def test_the_newest_turns_are_the_ones_kept(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.stub_engine()
        self.speak(20)
        kept = voice._Session._mem["c1"]["messages"]
        self.assertIn("발화 19", repr(kept))
        self.assertNotIn("발화 0", repr(kept))

    def test_a_short_call_is_stored_whole(self):
        """과차단도 결함이다 — 짧은 통화는 자르지 않는다."""
        self.stub_engine()
        self.speak(3)
        self.assertEqual(len(voice._Session._mem["c1"]["messages"]), 6)
        self.assertEqual(voice.SESSION_STATS["trimmed_msgs"], 0)

    def test_the_twilio_path_is_wired_too(self):
        """두 호출부 중 하나만 고치면 절반만 지켜진 규칙이 된다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "6"
        self.stub_engine()
        voice._transcribe_url = lambda u: "환불해 주세요"
        for i in range(20):
            voice.handle_twilio({"CallId": "t1", "From": "01011112222",
                                 "RecordingUrl": "https://cdn.example.com/%d.wav" % i,
                                 "RecordingDuration": "3"})
        self.assertLessEqual(len(voice._Session._mem["t1"]["messages"]), 6)

    def test_the_store_cap_uses_the_same_scale_as_the_model_window(self):
        """두 상한이 따로 놀면 실제 보관 기간이 코드에서 읽히지 않는다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "5"
        self.stub_engine()
        self.speak(20)
        kept = voice._Session._mem["c1"]["messages"]
        self.assertEqual(engine._window(kept)[1], 0, "저장분이 창보다 크다")


# ==========================================================================
# 2) fail-open 금지 — 자른 이력의 가드 기억은 접혀서 다음 턴에 넘어간다
# ==========================================================================
class TestFoldedGuardMemory(Base):
    def test_the_quote_is_carried_past_the_cut(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "6"
        seen = self.stub_engine(extra=self.quote_rows())
        self.speak(20)
        last = seen[-1]
        self.assertIsNotNone(last["mem_prior"], "접힌 기억이 넘어가지 않았다")
        self.assertEqual(last["mem_prior"]["quoted_amount"], 15900)
        self.assertTrue(last["mem_prior"]["awaiting"])

    def test_the_guard_still_blocks_a_mismatching_amount_late_in_the_call(self):
        """창 밖으로 밀려난 견적과 다른 금액은 통화 후반에도 막힌다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "6"
        seen = self.stub_engine(extra=self.quote_rows())
        self.speak(20)
        # 마지막 발화가 명시 동의여야 금액 재확인 단계까지 내려간다(2단계 관문).
        voice.handle_event({"type": "speech", "call_id": "c1", "from": "01011112222",
                            "scenario": "refund", "text": "네 동의합니다"})
        sess = voice._Session._mem["c1"]
        mem = engine._mem(sess["messages"], prior=sess["mem"])
        ok, reason, esc = engine._guard(
            "confirm_refund",
            {"order_id": "A", "refund_amount": 99000, "user_confirmed": True}, mem)
        self.assertFalse(ok, "견적을 잊고 승인했다 — fail-open")
        self.assertIn("불일치", reason)
        self.assertTrue(esc)
        self.assertTrue(seen)

    def test_the_folded_memory_holds_no_speech(self):
        """접힌 쪽에는 금액·판정만 남는다(개인정보가 따라오지 않는다)."""
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.stub_engine(extra=self.quote_rows())
        for i in range(20):
            voice.handle_event({"type": "speech", "call_id": "c1", "from": "01011112222",
                                "scenario": "refund",
                                "text": "주민번호 900101-1234567 입니다"})
        folded = voice._Session._mem["c1"]["mem"]
        self.assertEqual(set(folded), set(engine._MEM_KEYS))
        self.assertNotIn("900101", repr(folded))

    def test_a_fresh_session_starts_without_a_prior(self):
        """세션이 사라진 통화는 기억도 없이 시작한다(차단 쪽으로 실패한다)."""
        seen = self.stub_engine()
        self.speak(1)
        self.assertIsNone(seen[0]["mem_prior"])


# ==========================================================================
# 3) 조용하지 않다 — 자른 사실이 집계·로그·응답에 남는다
# ==========================================================================
class TestTrimIsNotSilent(Base):
    def test_counters_are_exposed_over_http(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.stub_engine()
        self.speak(20)
        d = get("/api/voice", headers=SAME_ORIGIN).json()
        self.assertEqual(d["sessions"]["trimmed"], 1)
        self.assertGreater(d["sessions"]["trimmed_msgs"], 0)

    def test_the_log_line_is_once_per_call_and_counts_only(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        rows = []
        self._emit = voice._log.emit
        voice._log.emit = lambda r: rows.append(r)
        try:
            self.stub_engine()
            self.speak(20)
        finally:
            voice._log.emit = self._emit
        trims = [r for r in rows if r.get("event") == "history_trim"]
        self.assertEqual(len(trims), 1, "통화 1건에 1줄")
        self.assertEqual(set(trims[0]) & {"text", "messages", "call"}, set())
        self.assertGreater(trims[0]["dropped"], 0)
        self.assertNotIn("발화", repr(trims[0]))

    def test_two_calls_count_two_trims(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.stub_engine()
        self.speak(20, cid="c1")
        self.speak(20, cid="c2")
        self.assertEqual(voice.SESSION_STATS["trimmed"], 2)

    def test_the_done_log_still_reports_the_whole_call(self):
        """상한은 보관량의 상한이지 **사실**의 상한이 아니다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        rows = []
        self.stub_engine()
        voice.run_assist = None
        self.speak(20)
        emit = voice._log.emit
        voice._log.emit = lambda r: rows.append(r)
        try:
            voice.handle_event({"type": "completed", "call_id": "c1",
                                "from": "01011112222", "scenario": "refund"})
        finally:
            voice._log.emit = emit
        done = [r for r in rows if r.get("event") == "call_done"][0]
        self.assertGreaterEqual(done["turn_count"], 40)
        self.assertLessEqual(done["kept"], 4)

    def test_the_summary_says_it_is_partial(self):
        """요약은 남은 창만 본다 — 말하지 않으면 통화 전체 요약으로 읽힌다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.stub_engine()
        voice.run_assist = lambda task, text: {"summary": "요약"}
        self.speak(20)
        out = voice.handle_event({"type": "completed", "call_id": "c1",
                                  "from": "01011112222", "scenario": "refund"})
        self.assertTrue(out["summary_partial"])
        self.assertGreater(out["summary_dropped_messages"], 0)

    def test_a_short_call_summary_is_not_marked_partial(self):
        self.stub_engine()
        voice.run_assist = lambda task, text: {"summary": "요약"}
        self.speak(2)
        out = voice.handle_event({"type": "completed", "call_id": "c1",
                                  "from": "01011112222", "scenario": "refund"})
        self.assertNotIn("summary_partial", out)

    def test_counting_failure_does_not_kill_the_call(self):
        """집계는 통화보다 덜 중요하다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        emit = voice._log.emit

        def boom(_r):
            raise RuntimeError("로그 장애")

        voice._log.emit = boom
        try:
            self.stub_engine()
            self.speak(20)
        finally:
            voice._log.emit = emit
        self.assertIn("c1", voice._Session._mem)


# ==========================================================================
# 4) 전환 티켓의 통화 참조 — 실통화를 `sim-` 으로 적지 않는다
# ==========================================================================
class TestSessionRef(Base):
    def test_the_ref_is_a_digest_not_the_raw_call_id(self):
        ref = voice._session_ref("CA-01011112222-7")
        self.assertNotIn("01011112222", ref)
        self.assertTrue(ref.startswith("call-"))

    def test_the_ref_is_stable_within_a_call(self):
        """턴마다 달라지면 티켓을 통화로 묶을 수 없다."""
        self.assertEqual(voice._session_ref("c1"), voice._session_ref("c1"))
        self.assertNotEqual(voice._session_ref("c1"), voice._session_ref("c2"))

    def test_the_ref_matches_the_metrics_digest(self):
        """지표·로그·티켓을 한 인스턴스 안에서 맞출 수 있어야 한다."""
        import _call_metrics
        self.assertEqual(voice._session_ref("c1"),
                         "call-%s" % _call_metrics.digest("c1"))

    def test_without_a_digest_it_does_not_claim_to_be_a_simulation(self):
        saved = voice.call_metrics
        voice.call_metrics = None
        try:
            ref = voice._session_ref("c1")
        finally:
            voice.call_metrics = saved
        self.assertEqual(ref, "call-unknown")
        self.assertNotIn("sim", ref)

    def test_the_ref_is_handed_to_the_engine(self):
        seen = self.stub_engine()
        self.speak(1)
        self.assertEqual(seen[0]["session_ref"], voice._session_ref("c1"))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
