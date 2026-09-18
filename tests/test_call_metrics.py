# -*- coding: utf-8 -*-
"""api/call_metrics.py 실측 통화 지표 회귀 테스트 + ops_stats·voice 배선.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

검증 대상 (COMMERCIAL_READINESS '운영 대시보드 실데이터 연결 준비')
  1) 집계 산식 — 사유별 집계·비율·평균, 기간 창 밖 제외
  2) **없는 수치를 만들지 않는다** — 표본 0건이면 비율은 null, 데모로 채우지 않는다
  3) 부분 표본 정직성 — 관측 창이 기간보다 짧으면 partial=true
  4) 중복·유실 이벤트 — answered 재배달은 1건, 시작 못 본 종료는 세지 않고 카운터로 드러냄
  5) 입력검증 — 미지 outcome·빈 call_id 거부(조용히 바꾸지 않는다)
  6) 개인정보 — 통화ID 원문·번호가 저장소·응답 어디에도 없다
  7) 오류를 삼키지 않는다 — 수집 실패는 통화를 끊지 않되 errors 카운터로 드러난다
  8) 배선 — voice 통화 흐름이 실제로 집계되고, ops_stats 가 데모와 분리해 내보낸다

실행: python3 -m pytest tests/test_call_metrics.py -q
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import call_metrics as cm    # noqa: E402
import ops_stats             # noqa: E402
import voice                 # noqa: E402

T0 = 1_700_000_000.0


class _NoNetwork(object):
    """테스트가 외부로 나가지 않는 것을 강제(과금 0)."""

    def __enter__(self):
        import urllib.request
        self._real = urllib.request.urlopen

        def _boom(*a, **kw):
            raise AssertionError("테스트가 네트워크를 호출했다")
        urllib.request.urlopen = _boom
        return self

    def __exit__(self, *a):
        import urllib.request
        urllib.request.urlopen = self._real
        return False


class Base(unittest.TestCase):
    def setUp(self):
        cm.reset()

    def tearDown(self):
        cm.reset()


# --------------------------------------------------------------------------
# 1) 집계 산식
# --------------------------------------------------------------------------
class TestAggregate(Base):
    def _seed(self):
        cm.start("a", scenario="refund", ts=T0)
        cm.mark_turn("a")
        cm.mark_turn("a")
        cm.finish("a", outcome="bot_completed", ts=T0 + 30)
        cm.start("b", scenario="refund", ts=T0)
        cm.mark_outcome("b", "transferred")
        cm.finish("b", ts=T0 + 90)

    def test_counts_by_outcome(self):
        self._seed()
        s = cm.summary(now=T0 + 100)
        self.assertEqual(s["sample_size"], 2)
        self.assertEqual(s["calls"]["bot_completed"], 1)
        self.assertEqual(s["calls"]["transferred"], 1)
        self.assertEqual(s["calls"]["abandoned"], 0)

    def test_rates_and_averages(self):
        self._seed()
        s = cm.summary(now=T0 + 100)
        self.assertEqual(s["auto_rate"], 0.5)
        self.assertEqual(s["transfer_rate"], 0.5)
        self.assertEqual(s["avg_duration_sec"], 60.0)
        self.assertEqual(s["avg_turns"], 1.0)

    def test_in_progress_counted_separately(self):
        cm.start("open-1", ts=T0)
        s = cm.summary(now=T0 + 10)
        self.assertEqual(s["calls"]["in_progress"], 1)
        # 진행 중 통화는 종료 표본에 섞이지 않는다(끝나지 않은 것을 셀 수 없다)
        self.assertEqual(s["sample_size"], 0)
        self.assertEqual(s["calls"]["total"], 0)

    def test_outside_period_window_excluded(self):
        cm.start("old", ts=T0)
        cm.mark_turn("old")
        cm.finish("old", ts=T0 + 10)
        later = T0 + 10 + cm.PERIOD_SEC["today"] + 60
        self.assertEqual(cm.summary("today", now=later)["sample_size"], 0)
        self.assertEqual(cm.summary("week", now=later)["sample_size"], 1)

    def test_default_outcome_from_observation(self):
        # 사유를 못 받은 종료: 응대 턴이 있으면 bot_completed, 없으면 abandoned
        cm.start("t", ts=T0)
        cm.mark_turn("t")
        cm.finish("t", ts=T0 + 5)
        cm.start("n", ts=T0)
        cm.finish("n", ts=T0 + 5)
        s = cm.summary(now=T0 + 10)
        self.assertEqual(s["calls"]["bot_completed"], 1)
        self.assertEqual(s["calls"]["abandoned"], 1)

    def test_unknown_period_falls_back_to_today(self):
        for weird in ("yyy", None, 3, ["week"], {"a": 1}):
            self.assertEqual(cm.summary(period=weird, now=T0)["period"], "today")

    def test_negative_duration_clamped(self):
        # 시계 역행·이벤트 순서 뒤바뀜이 음수 통화시간을 만들지 않는다
        cm.start("z", ts=T0 + 100)
        cm.mark_turn("z")
        cm.finish("z", ts=T0)
        self.assertEqual(cm.summary(now=T0 + 200)["avg_duration_sec"], 0.0)


# --------------------------------------------------------------------------
# 2) 없는 수치를 만들지 않는다 (§8 허위 수치 금지)
# --------------------------------------------------------------------------
class TestNoFabrication(Base):
    def test_empty_sample_rates_are_null(self):
        s = cm.summary(now=T0)
        self.assertEqual(s["sample_size"], 0)
        for k in ("auto_rate", "transfer_rate", "avg_duration_sec", "avg_turns"):
            self.assertIsNone(s[k], k)

    def test_empty_sample_counts_are_zero_not_demo(self):
        s = cm.summary(now=T0)
        for k, v in s["calls"].items():
            self.assertEqual(v, 0, k)
        # 데모 기준선 수치가 실측 자리에 새어 들어오지 않는다
        blob = json.dumps(s)
        for demo in ("214", "1486", "6120", "0.72"):
            self.assertNotIn(demo, blob)

    def test_measured_marked_as_measured(self):
        self.assertEqual(cm.summary(now=T0)["data_source"], "measured")


# --------------------------------------------------------------------------
# 3) 부분 표본 정직성
# --------------------------------------------------------------------------
class TestPartialWindow(Base):
    def test_short_window_is_partial(self):
        cm.start("a", ts=T0)
        cm.mark_turn("a")
        cm.finish("a", ts=T0 + 60)
        s = cm.summary("month", now=T0 + 120)
        self.assertTrue(s["partial"])
        self.assertLess(s["window_sec"], cm.PERIOD_SEC["month"])

    def test_full_window_not_partial(self):
        cm.start("a", ts=T0)
        cm.mark_turn("a")
        cm.finish("a", ts=T0 + 60)
        s = cm.summary("today", now=T0 + cm.PERIOD_SEC["today"] + 10)
        self.assertFalse(s["partial"])

    def test_empty_is_always_partial(self):
        self.assertTrue(cm.summary("today", now=T0)["partial"])

    def test_window_never_exceeds_period(self):
        cm.start("a", ts=T0)
        cm.finish("a", ts=T0 + 1)
        s = cm.summary("today", now=T0 + 10 * cm.PERIOD_SEC["today"])
        self.assertLessEqual(s["window_sec"], cm.PERIOD_SEC["today"])


# --------------------------------------------------------------------------
# 4) 중복·유실 이벤트
# --------------------------------------------------------------------------
class TestDuplicateAndOrphan(Base):
    def test_redelivered_answered_is_one_call(self):
        cm.start("dup", ts=T0)
        cm.start("dup", ts=T0 + 5)     # 웹훅 재배달
        cm.mark_turn("dup")
        cm.finish("dup", ts=T0 + 30)
        s = cm.summary(now=T0 + 60)
        self.assertEqual(s["sample_size"], 1)
        # 시작 시각은 첫 관측을 유지한다(재배달이 통화시간을 깎지 않는다)
        self.assertEqual(s["avg_duration_sec"], 30.0)

    def test_duplicate_finish_not_double_counted(self):
        cm.start("d", ts=T0)
        cm.mark_turn("d")
        cm.finish("d", ts=T0 + 10)
        cm.finish("d", ts=T0 + 11)     # 재배달
        s = cm.summary(now=T0 + 60)
        self.assertEqual(s["sample_size"], 1)
        self.assertEqual(s["collector"]["orphan_finishes"], 1)

    def test_orphan_finish_not_invented(self):
        # 시작을 못 본 통화를 '있었던 것으로' 세면 지속시간·턴수를 지어내야 한다
        self.assertIsNone(cm.finish("ghost"))
        s = cm.summary(now=T0)
        self.assertEqual(s["sample_size"], 0)
        self.assertEqual(s["collector"]["orphan_finishes"], 1)

    def test_mark_turn_on_unknown_call_is_false(self):
        self.assertFalse(cm.SINK.mark_turn("nope"))
        self.assertFalse(cm.SINK.mark_outcome("nope", "transferred"))

    def test_turn_counter_capped(self):
        cm.start("loop", ts=T0)
        for _ in range(cm.MAX_TURNS + 50):
            cm.mark_turn("loop")
        cm.finish("loop", ts=T0 + 1)
        self.assertEqual(cm.summary(now=T0 + 2)["avg_turns"], float(cm.MAX_TURNS))

    def test_open_calls_capped(self):
        for i in range(cm.MAX_OPEN + 5):
            cm.start("open-%d" % i, ts=T0 + i)
        s = cm.summary(now=T0 + 10_000)
        self.assertLessEqual(s["calls"]["in_progress"], cm.MAX_OPEN)
        self.assertGreater(s["collector"]["dropped"], 0)   # 버린 사실을 숨기지 않는다

    def test_records_capped(self):
        for i in range(cm.MAX_RECORDS + 10):
            cm.start("c-%d" % i, ts=T0 + i)
            cm.mark_turn("c-%d" % i)
            cm.finish("c-%d" % i, ts=T0 + i + 1)
        s = cm.summary("month", now=T0 + cm.MAX_RECORDS + 20)
        self.assertLessEqual(s["sample_size"], cm.MAX_RECORDS)
        self.assertGreater(s["collector"]["dropped"], 0)

    def test_retention_prunes_old(self):
        cm.start("ancient", ts=T0)
        cm.mark_turn("ancient")
        cm.finish("ancient", ts=T0 + 1)
        s = cm.summary("month", now=T0 + cm.RETENTION_SEC + 10_000)
        self.assertEqual(s["sample_size"], 0)
        self.assertGreater(s["collector"]["dropped"], 0)


# --------------------------------------------------------------------------
# 5) 입력검증 — 조용히 바꾸지 않는다
# --------------------------------------------------------------------------
class TestValidation(Base):
    def test_unknown_outcome_rejected(self):
        cm.start("v", ts=T0)
        with self.assertRaises(ValueError):
            cm.SINK.mark_outcome("v", "weird")
        with self.assertRaises(ValueError):
            cm.SINK.finish("v", outcome="weird")
        # 거부된 시도는 집계를 바꾸지 않는다
        self.assertEqual(cm.summary(now=T0 + 1)["calls"]["in_progress"], 1)

    def test_blank_call_id_rejected(self):
        for bad in ("", "   ", None, 12345, ["a"]):
            with self.assertRaises(ValueError):
                cm.digest(bad)

    def test_outcome_whitelist_shape(self):
        self.assertEqual(set(cm.OUTCOMES),
                         {"bot_completed", "transferred", "abandoned", "failed"})

    def test_period_names_match_ops_stats(self):
        # 드리프트 차단: 대시보드 기간 정의가 둘로 갈라지면 수치가 어긋난다
        self.assertEqual(set(cm.PERIOD_SEC), set(ops_stats.PERIODS))


# --------------------------------------------------------------------------
# 6) 개인정보
# --------------------------------------------------------------------------
class TestPrivacy(Base):
    def test_call_id_not_stored_in_plaintext(self):
        cm.start("01012345678", scenario="refund", ts=T0)
        cm.mark_turn("01012345678")
        cm.finish("01012345678", ts=T0 + 5)
        blob = json.dumps(cm.SINK._done, ensure_ascii=False)
        self.assertNotIn("01012345678", blob)
        self.assertNotIn("2345678", blob)

    def test_summary_exposes_no_identifiers(self):
        cm.start("call-홍길동-01099998888", scenario="refund", ts=T0)
        cm.mark_turn("call-홍길동-01099998888")
        cm.finish("call-홍길동-01099998888", ts=T0 + 5)
        blob = json.dumps(cm.summary(now=T0 + 10), ensure_ascii=False)
        self.assertNotIn("홍길동", blob)
        self.assertNotIn("01099998888", blob)

    def test_numeric_label_rejected(self):
        # 시나리오·테넌트 자리에 번호가 들어와도 저장되지 않는다
        cm.start("lbl", scenario="01012345678", tenant="900101-1234567", ts=T0)
        cm.mark_turn("lbl")
        cm.finish("lbl", ts=T0 + 1)
        rec = cm.SINK._done[-1]
        self.assertIsNone(rec["scenario"])
        self.assertIsNone(rec["tenant"])

    def test_normal_label_kept(self):
        cm.start("lbl2", scenario="wellbeing", tenant="tenant-a", ts=T0)
        cm.mark_turn("lbl2")
        cm.finish("lbl2", ts=T0 + 1)
        rec = cm.SINK._done[-1]
        self.assertEqual(rec["scenario"], "wellbeing")
        self.assertEqual(rec["tenant"], "tenant-a")

    def test_digest_is_stable_and_distinct(self):
        self.assertEqual(cm.digest("a"), cm.digest(" a "))
        self.assertNotEqual(cm.digest("a"), cm.digest("b"))
        self.assertEqual(len(cm.digest("a")), 12)

    def test_salt_not_exposed(self):
        os.environ["CALLBOT_METRICS_SALT"] = "s3cret-salt"
        try:
            cm.start("salted", ts=T0)
            cm.mark_turn("salted")
            cm.finish("salted", ts=T0 + 1)
            blob = json.dumps(cm.summary(now=T0 + 2), ensure_ascii=False)
            self.assertNotIn("s3cret-salt", blob)
        finally:
            os.environ.pop("CALLBOT_METRICS_SALT", None)


# --------------------------------------------------------------------------
# 7) 오류를 삼키지 않는다
# --------------------------------------------------------------------------
class TestErrorsSurfaced(Base):
    def test_wrapper_swallows_but_counts(self):
        before = cm.summary(now=T0)["collector"]["errors"]
        cm.mark_outcome("nope", "weird")     # ValueError 를 래퍼가 삼킨다
        after = cm.summary(now=T0)["collector"]["errors"]
        self.assertEqual(after, before + 1)

    def test_bad_call_id_via_wrapper_does_not_raise(self):
        self.assertIsNone(cm.start(None))
        self.assertIsNone(cm.finish(""))
        self.assertGreater(cm.summary(now=T0)["collector"]["errors"], 0)

    def test_collector_scope_declared(self):
        # 인스턴스 메모리라는 한계를 응답이 스스로 밝힌다
        c = cm.summary(now=T0)["collector"]
        self.assertEqual(c["scope"], "instance")
        self.assertEqual(c["retention_sec"], cm.RETENTION_SEC)


# --------------------------------------------------------------------------
# 8) 배선 — voice / ops_stats
# --------------------------------------------------------------------------
class TestWiring(Base):
    def _ev(self, typ, cid, **kw):
        base = {"type": typ, "call_id": cid, "from": "01012345678", "to": "07012341234",
                "scenario": "refund", "tenant": None}
        base.update(kw)
        return base

    def test_voice_call_lifecycle_is_measured(self):
        with _NoNetwork():
            voice.handle_event(self._ev("answered", "w1"))
            self.assertEqual(cm.summary()["calls"]["in_progress"], 1)
            voice.handle_event(self._ev("completed", "w1"))
        s = cm.summary()
        self.assertEqual(s["sample_size"], 1)
        self.assertEqual(s["calls"]["in_progress"], 0)

    def test_voice_speech_turn_counted(self):
        real = voice.run_turn
        voice.run_turn = lambda msgs, **kw: {"messages": list(msgs) + [
            {"role": "assistant", "content": "네"}], "reply": "네", "transferred": False}
        try:
            with _NoNetwork():
                voice.handle_event(self._ev("answered", "w2"))
                voice.handle_event(self._ev("speech", "w2", text="환불해 주세요"))
                voice.handle_event(self._ev("completed", "w2"))
        finally:
            voice.run_turn = real
        s = cm.summary()
        self.assertEqual(s["calls"]["bot_completed"], 1)
        self.assertEqual(s["avg_turns"], 1.0)

    def test_voice_transfer_marked(self):
        real = voice.run_turn
        voice.run_turn = lambda msgs, **kw: {"messages": list(msgs), "reply": "연결",
                                             "transferred": True}
        try:
            with _NoNetwork():
                voice.handle_event(self._ev("answered", "w3"))
                voice.handle_event(self._ev("speech", "w3", text="상담사 바꿔주세요"))
                voice.handle_event(self._ev("completed", "w3"))
        finally:
            voice.run_turn = real
        s = cm.summary()
        self.assertEqual(s["calls"]["transferred"], 1)
        self.assertEqual(s["calls"]["bot_completed"], 0)

    def test_twilio_failed_status_is_failed(self):
        with _NoNetwork():
            voice.handle_twilio({"CallId": "tw1", "From": "01012345678"})
            voice.handle_twilio({"CallId": "tw1", "CallStatus": "no-answer"})
        self.assertEqual(cm.summary()["calls"]["failed"], 1)

    def test_metrics_failure_does_not_break_call(self):
        # 수집기가 통째로 터져도 통화 처리는 계속된다(수집 실패 < 통화 성공)
        real = voice.call_metrics
        class _Broken(object):
            def __getattr__(self, name):
                def _boom(*a, **kw):
                    raise RuntimeError("collector down")
                return _boom
        voice.call_metrics = _Broken()
        try:
            with _NoNetwork():
                out = voice.handle_event(self._ev("answered", "w4"))
        finally:
            voice.call_metrics = real
        self.assertIn("actions", out)

    def test_ops_stats_exposes_measured_separately(self):
        cm.start("o1", ts=T0)
        cm.mark_turn("o1")
        cm.finish("o1", outcome="bot_completed", ts=T0 + 10)
        s = ops_stats.get_ops_summary()
        self.assertEqual(s["data_source"], "demo")        # 헤드라인은 여전히 데모
        self.assertEqual(s["measured"]["data_source"], "measured")
        # 실측이 데모 수치를 덮어쓰지 않는다
        self.assertEqual(s["calls"]["today"], ops_stats.DEMO_BASELINE["calls_today"])

    def test_ops_stats_measured_schema_fixed_when_collector_down(self):
        import builtins
        real = builtins.__import__

        def _boom(name, *a, **kw):
            if name == "call_metrics":
                raise RuntimeError("down")
            return real(name, *a, **kw)
        builtins.__import__ = _boom
        try:
            m = ops_stats._measured("today")
        finally:
            builtins.__import__ = real
        self.assertEqual(m["data_source"], "unavailable")
        for k in ops_stats.MEASURED_CALL_KEYS:
            self.assertIsInstance(m["calls"][k], int)
        for k in ops_stats.MEASURED_RATE_KEYS:
            self.assertIsNone(m[k])

    def test_ops_stats_measured_definition_present(self):
        d = ops_stats.get_ops_summary()["measured"]["definition"]
        self.assertEqual(set(d), set(cm.OUTCOMES))
        for v in d.values():
            self.assertTrue(v.strip())

    def test_ops_stats_period_passed_through(self):
        s = ops_stats.get_ops_summary(period="week")
        self.assertEqual(s["measured"]["period"], "week")

    def test_ops_stats_summary_is_json_serialisable(self):
        json.dumps(ops_stats.get_ops_summary(), ensure_ascii=False)

    def test_reading_does_not_mutate(self):
        cm.start("ro", ts=T0)
        cm.mark_turn("ro")
        cm.finish("ro", outcome="bot_completed", ts=T0 + 5)
        a = cm.summary(now=T0 + 10)
        b = cm.summary(now=T0 + 10)
        self.assertEqual(a, b)
        # 조회가 게이트를 켜지 않는다
        self.assertFalse(ops_stats.gate_flags()["cpaas_live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
