# -*- coding: utf-8 -*-
"""39차 — '진행 중'을 뜻하는 저장소의 시간 만료 회귀.

고친 결함 두 건은 같은 모양이다 — **상한은 있는데 만료가 없었다**.
상한(`mem_caps` 게이트)은 메모리가 무한히 자라는 것을 막지만, "지금 진행 중"을
뜻하는 숫자는 상한 안에서도 거짓을 말한다.

  1) `_call_metrics._Store._open` → `calls.in_progress`
     종료 웹훅(`finish`)이 유실되면 그 관측치가 영구히 남아 콘솔 「진행 중」
     타일이 **수 시간 전에 끝난 통화**를 지금 통화 중으로 그렸다.
  2) `_escalation.EscalationQueue._tickets` → `stats().queued`
     `queued` 를 벗어나게 하는 배선(실 상담원 배정)이 아직 없어서 이 인스턴스가
     만든 **모든** 전환 티켓이 「대기 중」으로 남았다(단조 증가).

검증 축:
  A) 수집기 만료 — 만료되고, 말이 오가는 통화는 만료되지 않고, 지속시간·사유를
     **지어내지 않고**(종료 표본에 섞지 않는다) 버린 건수를 드러낸다.
  B) 전환 큐 만료 — 미처리 티켓은 제 어휘(`abandoned`)로 전이하고 티켓을 지우지
     않으며, 전이 기록의 actor 가 `timeout` 이라 사람이 처리한 것과 구별된다.
  C) 노출 — `/api/ops_stats` 가 두 카운터(`stale_opens`·`expired`)와 만료 기준을
     함께 싣는다(세어 두고 보여주지 않으면 센 것이 아니다).
  D) 릴리스 게이트 19단계 `stale_state` — 실제로 결함을 잡는가 + 과차단하지 않는가.

네트워크·LLM 미사용. 실행: python -m pytest tests/test_stale_state.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _call_metrics as cm      # noqa: E402
import _escalation as esc       # noqa: E402
import ops_stats                # noqa: E402
import verify                   # noqa: E402

T0 = 1_700_000_000.0


class _Env(object):
    """환경변수 임시 설정(호출 시점 판독 확인용)."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# --------------------------------------------------------------------------
# A) 실측 수집기 — 종료 이벤트가 오지 않은 관측치
# --------------------------------------------------------------------------
class TestOpenCallExpiry(unittest.TestCase):
    def setUp(self):
        cm.reset()

    def tearDown(self):
        cm.reset()

    def test_open_call_expires_and_is_counted(self):
        cm.start("lost", ts=T0)
        self.assertEqual(cm.summary(now=T0 + 10)["calls"]["in_progress"], 1)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 1)
        self.assertEqual(s["calls"]["in_progress"], 0)
        self.assertEqual(s["collector"]["stale_opens"], 1)

    def test_expired_open_is_not_turned_into_a_finished_call(self):
        """지속시간·종료사유를 지어내야 하므로 종료 표본으로 옮기지 않는다."""
        cm.start("lost", ts=T0)
        cm.mark_turn("lost", ts=T0 + 5)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 60)
        self.assertEqual(s["sample_size"], 0)
        self.assertEqual(s["calls"]["total"], 0)
        self.assertIsNone(s["avg_duration_sec"])
        self.assertEqual(s["calls"]["bot_completed"], 0)
        self.assertEqual(s["calls"]["failed"], 0)   # '실패'로 분류하지도 않는다

    def test_talking_call_is_not_expired(self):
        """말이 오가는 동안은 진행 중이다(긴 통화를 끊어 세지 않는다)."""
        cm.start("long", ts=T0)
        for i in range(1, 5):
            cm.mark_turn("long", ts=T0 + i * (cm.OPEN_TTL_SEC - 10))
        s = cm.summary(now=T0 + 4 * (cm.OPEN_TTL_SEC - 10) + 10)
        self.assertEqual(s["calls"]["in_progress"], 1)
        self.assertEqual(s["collector"]["stale_opens"], 0)

    def test_mark_outcome_also_refreshes(self):
        cm.start("t", ts=T0)
        cm.mark_outcome("t", "transferred", ts=T0 + cm.OPEN_TTL_SEC - 1)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 1)
        self.assertEqual(s["calls"]["in_progress"], 1)

    def test_boundary_is_strict(self):
        """정확히 TTL 인 순간은 아직 만료가 아니다(경계에서 흔들리지 않는다)."""
        cm.start("edge", ts=T0)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC)
        self.assertEqual(s["calls"]["in_progress"], 1)

    def test_clock_going_backwards_does_not_expire(self):
        cm.start("back", ts=T0)
        s = cm.summary(now=T0 - 10_000)
        self.assertEqual(s["calls"]["in_progress"], 1)
        self.assertEqual(s["collector"]["stale_opens"], 0)

    def test_finish_after_expiry_is_an_orphan_not_an_invented_call(self):
        cm.start("late", ts=T0)
        cm.summary(now=T0 + cm.OPEN_TTL_SEC + 1)        # 만료
        self.assertIsNone(cm.finish("late", ts=T0 + cm.OPEN_TTL_SEC + 2))
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 3)
        self.assertEqual(s["sample_size"], 0)
        self.assertEqual(s["collector"]["orphan_finishes"], 1)
        self.assertEqual(s["collector"]["stale_opens"], 1)

    def test_expired_slots_do_not_evict_a_fresh_call(self):
        """만료분이 상한 자리를 차지한 채 **방금 걸려 온 통화**를 밀어내지 않는다."""
        for i in range(cm.MAX_OPEN):
            cm.start("old-%d" % i, ts=T0 + i)
        before = cm.summary(now=T0 + cm.MAX_OPEN)["collector"]["dropped"]
        cm.start("fresh", ts=T0 + cm.OPEN_TTL_SEC + cm.MAX_OPEN + 10)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + cm.MAX_OPEN + 11)
        self.assertEqual(s["calls"]["in_progress"], 1)
        self.assertEqual(s["collector"]["dropped"], before)   # 축출 없음
        self.assertEqual(s["collector"]["stale_opens"], cm.MAX_OPEN)

    def test_redelivered_answered_refreshes_without_double_counting(self):
        cm.start("dup", ts=T0)
        cm.start("dup", ts=T0 + cm.OPEN_TTL_SEC - 1)      # 재배달
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 1)
        self.assertEqual(s["calls"]["in_progress"], 1)
        cm.mark_turn("dup", ts=T0 + cm.OPEN_TTL_SEC)
        cm.finish("dup", ts=T0 + cm.OPEN_TTL_SEC + 5)
        s = cm.summary(now=T0 + cm.OPEN_TTL_SEC + 6)
        self.assertEqual(s["sample_size"], 1)
        # 시작 시각은 첫 관측을 유지한다(재배달이 통화시간을 깎지 않는다)
        self.assertEqual(s["avg_duration_sec"], round(cm.OPEN_TTL_SEC + 5, 1))

    def test_reset_clears_the_counter(self):
        cm.start("x", ts=T0)
        cm.summary(now=T0 + cm.OPEN_TTL_SEC + 1)
        cm.reset()
        self.assertEqual(cm.summary(now=T0)["collector"]["stale_opens"], 0)


class TestOpenTtlConfig(unittest.TestCase):
    def setUp(self):
        cm.reset()

    def tearDown(self):
        cm.reset()

    def test_read_at_call_time(self):
        """배포 후 조정이 **재배포 없이** 반영된다(import 시점에 얼리지 않는다)."""
        with _Env(CALLBOT_METRICS_OPEN_TTL="60"):
            self.assertEqual(cm.open_ttl(), 60.0)
            cm.start("short", ts=T0)
            self.assertEqual(cm.summary(now=T0 + 61)["calls"]["in_progress"], 0)
        self.assertEqual(cm.open_ttl(), cm.OPEN_TTL_SEC)

    def test_bad_values_fall_back_to_default(self):
        """만료가 **사라지는 쪽으로** 실패하지 않는다."""
        for bad in ("", "   ", "0", "-5", "abc", "1,800"):
            with _Env(CALLBOT_METRICS_OPEN_TTL=bad):
                self.assertEqual(cm.open_ttl(), cm.OPEN_TTL_SEC, bad)

    def test_ttl_is_reported(self):
        with _Env(CALLBOT_METRICS_OPEN_TTL="120"):
            self.assertEqual(cm.stats()["open_ttl_sec"], 120)

    def test_same_scale_as_voice_session_ttl(self):
        """세션이 이미 만료된 통화를 '진행 중'으로 셀 수는 없다(같은 눈금)."""
        import voice
        self.assertEqual(cm.OPEN_TTL_SEC, voice.SESSION_TTL)
        self.assertEqual(esc.OPEN_TTL_SEC, voice.SESSION_TTL)


# --------------------------------------------------------------------------
# B) 전환 큐 — 아무도 집어가지 않은 티켓
# --------------------------------------------------------------------------
class TestTicketExpiry(unittest.TestCase):
    def setUp(self):
        self.q = esc.EscalationQueue()

    def _age(self, tid, seconds):
        self.q._tickets[tid]["ts"] = self.q._tickets[tid]["ts"] - seconds

    def test_queued_ticket_expires_to_abandoned(self):
        tk = self.q.enqueue("s1", "request", "연결 요청")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        s = self.q.stats()
        self.assertEqual(s["queued"], 0)
        self.assertEqual(s["abandoned"], 1)
        self.assertEqual(s["expired"], 1)

    def test_assigned_ticket_expires_too(self):
        tk = self.q.enqueue("s1", "request")
        self.q.transition(tk["id"], "assigned", actor="agent-01")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        s = self.q.stats()
        self.assertEqual(s["assigned"], 0)
        self.assertEqual(s["abandoned"], 1)

    def test_ticket_is_not_deleted(self):
        """장부에서 지우면 '전환이 있었다'는 사실까지 사라진다."""
        tk = self.q.enqueue("s1", "request")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        s = self.q.stats()
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["dropped"], 0)
        self.assertEqual([t["id"] for t in self.q.list()], [tk["id"]])

    def test_transition_is_recorded_with_timeout_actor(self):
        """사람이 이탈 처리한 것과 **구별된다**."""
        tk = self.q.enqueue("s1", "request")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        self.q.sweep()
        last = self.q.audit_log()[-1]
        self.assertEqual((last["ticket"], last["from"], last["to"]),
                         (tk["id"], "queued", "abandoned"))
        self.assertIn(esc.TIMEOUT_ACTOR, last["note"])
        human = self.q.enqueue("s2", "request")
        self.q.transition(human["id"], "abandoned", actor="agent-01")
        self.assertNotIn(esc.TIMEOUT_ACTOR, self.q.audit_log()[-1]["note"])

    def test_expiry_is_not_counted_twice(self):
        tk = self.q.enqueue("s1", "request")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        for _ in range(3):
            self.q.sweep()
        self.assertEqual(self.q.stats()["expired"], 1)

    def test_fresh_tickets_are_untouched(self):
        tk = self.q.enqueue("s1", "request")
        self.q.sweep()
        self.assertEqual(self.q.stats()["queued"], 1)
        self.assertEqual(self.q.stats()["expired"], 0)
        self.assertEqual(self.q._tickets[tk["id"]]["state"], "queued")

    def test_boundary_is_strict(self):
        tk = self.q.enqueue("s1", "request")
        self._age(tk["id"], esc.OPEN_TTL_SEC)
        self.assertEqual(self.q.stats()["queued"], 1)

    def test_list_reflects_expiry(self):
        """목록과 집계가 서로 다른 말을 하지 않는다."""
        tk = self.q.enqueue("s1", "request")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        self.assertEqual(self.q.list("queued"), [])
        self.assertEqual(len(self.q.list("abandoned")), 1)

    def test_terminal_states_are_left_alone(self):
        tk = self.q.enqueue("s1", "request")
        self.q.transition(tk["id"], "assigned")
        self.q.transition(tk["id"], "resolved")
        self._age(tk["id"], esc.OPEN_TTL_SEC * 10)
        s = self.q.stats()
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["expired"], 0)

    def test_broken_timestamp_does_not_raise(self):
        tk = self.q.enqueue("s1", "request")
        self.q._tickets[tk["id"]]["ts"] = "어제"
        self.assertEqual(self.q.sweep(), 0)
        self.assertEqual(self.q.stats()["queued"], 1)

    def test_expiry_frees_capacity_without_dropping_waiting(self):
        """고친 것의 효과: 상한에 닿아도 '기다리는 고객'이 유실로 찍히지 않는다.

        고치기 전에는 오래된 `queued` 가 영원히 대기로 남아, 상한 초과 시
        `_evict` 가 **대기 중 티켓**을 버리고 `dropped_waiting` 을 올렸다.
        """
        q = esc.EscalationQueue()
        for i in range(esc.MAX_TICKETS):
            tk = q.enqueue("s", "request")
            q._tickets[tk["id"]]["ts"] -= esc.OPEN_TTL_SEC + 10
        q.enqueue("fresh", "request")
        s = q.stats()
        self.assertEqual(s["dropped_waiting"], 0)
        self.assertEqual(s["queued"], 1)
        self.assertGreater(s["expired"], 0)
        self.assertLessEqual(s["total"], esc.MAX_TICKETS)

    def test_ttl_read_at_call_time_and_reported(self):
        with _Env(CALLBOT_ESC_OPEN_TTL="30"):
            self.assertEqual(esc.open_ttl(), 30.0)
            self.assertEqual(self.q.stats()["open_ttl_sec"], 30)
            tk = self.q.enqueue("s1", "request")
            self._age(tk["id"], 31)
            self.assertEqual(self.q.stats()["queued"], 0)
        self.assertEqual(esc.open_ttl(), esc.OPEN_TTL_SEC)

    def test_bad_ttl_values_fall_back_to_default(self):
        for bad in ("", " ", "0", "-1", "nope"):
            with _Env(CALLBOT_ESC_OPEN_TTL=bad):
                self.assertEqual(esc.open_ttl(), esc.OPEN_TTL_SEC, bad)

    def test_note_has_no_personal_data(self):
        """만료 사유는 **우리가 만든 고정 문구**다(발화·번호가 섞이지 않는다)."""
        tk = self.q.enqueue("s1", "request", "고객 010-1234-5678 환불 요청")
        self._age(tk["id"], esc.OPEN_TTL_SEC + 1)
        self.q.sweep()
        blob = repr(self.q.audit_log())
        self.assertNotIn("010-1234-5678", blob)
        self.assertNotIn("1234", blob)


# --------------------------------------------------------------------------
# C) 노출 — /api/ops_stats
# --------------------------------------------------------------------------
class TestExposure(unittest.TestCase):
    def setUp(self):
        cm.reset()
        esc.QUEUE.__init__()

    def tearDown(self):
        cm.reset()
        esc.QUEUE.__init__()

    def test_collector_keys_match_the_registry(self):
        self.assertEqual(set(cm.stats()), set(ops_stats.MEASURED_COLLECTOR_KEYS))

    def test_unavailable_fallback_has_the_same_keys(self):
        """수집기가 죽어도 콘솔이 **없는 칸**을 읽지 않는다."""
        import builtins
        real = builtins.__import__

        def boom(name, *a, **kw):
            if name == "_call_metrics":
                raise RuntimeError("down")
            return real(name, *a, **kw)
        builtins.__import__ = boom
        try:
            u = ops_stats._measured("today")
        finally:
            builtins.__import__ = real
        self.assertEqual(u["data_source"], "unavailable")
        self.assertEqual(set(u["collector"]), set(ops_stats.MEASURED_COLLECTOR_KEYS))
        self.assertEqual(u["collector"]["stale_opens"], 0)
        self.assertEqual(u["collector"]["scope"], "instance")

    def test_measured_block_carries_stale_opens(self):
        cm.start("lost", ts=T0)
        cm.SINK._sweep_open(T0 + cm.OPEN_TTL_SEC + 1)
        m = ops_stats._measured("today")
        self.assertEqual(m["collector"]["stale_opens"], 1)
        self.assertGreater(m["collector"]["open_ttl_sec"], 0)

    def test_escalation_block_carries_expired(self):
        tk = esc.QUEUE.enqueue("s1", "request")
        esc.QUEUE._tickets[tk["id"]]["ts"] -= esc.OPEN_TTL_SEC + 1
        d = ops_stats.get_ops_summary()["escalation"]
        self.assertEqual(d["expired"], 1)
        self.assertEqual(d["queued"], 0)
        self.assertEqual(d["abandoned"], 1)
        self.assertGreater(d["open_ttl_sec"], 0)

    def test_ticket_list_view_agrees_with_stats(self):
        tk = esc.QUEUE.enqueue("s1", "request")
        esc.QUEUE._tickets[tk["id"]]["ts"] -= esc.OPEN_TTL_SEC + 1
        v = ops_stats.get_escalations(state="open")
        self.assertEqual(v["matched"], 0)          # 대기 중으로 세지 않는다
        self.assertEqual(v["stats"]["expired"], 1)
        allv = ops_stats.get_escalations()
        self.assertEqual([t["state"] for t in allv["tickets"]], ["abandoned"])
        self.assertGreaterEqual(allv["tickets"][0]["age_sec"], int(esc.OPEN_TTL_SEC))

    def test_console_shows_both_counters(self):
        """세어 두고 보여주지 않으면 센 것이 아니다 — 콘솔에도 자리가 있어야 한다."""
        with open(os.path.join(ROOT, "public", "admin.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("col.stale_opens", html)
        self.assertIn("['expired','시간만료']", html)
        self.assertIn("open_ttl_sec", html)
        # 하드코딩 수치 금지 — 만료 기준은 서버 값에서만 온다
        self.assertNotIn("30분을 넘기면", html)


# --------------------------------------------------------------------------
# D) 릴리스 게이트 19단계 `stale_state`
# --------------------------------------------------------------------------
GOOD = '''
TTL_SEC = 1800.0


def ttl():
    return TTL_SEC


class S:
    def __init__(self):
        self._open = {}

    def _sweep(self, now):
        for k in [k for k, v in self._open.items() if now - v > ttl()]:
            self._open.pop(k, None)

    def put(self, k, v):
        self._sweep(0)
        self._open[k] = v


STORE = S()
'''


class TestStaleStateGate(unittest.TestCase):
    KEY = ("fake.py", "S._open")

    def _patch(self, entry=None):
        """등록부를 한 칸으로 바꿔 둔다(실제 등록부는 아래 실저장소 검사가 본다)."""
        old_s, old_m = verify.STALE_STORES, verify.MEM_STORES
        verify.STALE_STORES = {self.KEY: (entry or ("TTL_SEC", "_sweep", "사유"))}
        verify.MEM_STORES = dict(old_m)
        verify.MEM_STORES[self.KEY] = ("MAX", "상한")

        def restore():
            verify.STALE_STORES, verify.MEM_STORES = old_s, old_m
        self.addCleanup(restore)

    def _run(self, text):
        return verify.check_stale_state({"fake.py": text})

    def test_good_source_passes(self):
        self._patch()
        ok, msg = self._run(GOOD)
        self.assertTrue(ok, msg)

    def test_missing_ttl_symbol_fails(self):
        self._patch()
        ok, msg = self._run(GOOD.replace("TTL_SEC = 1800.0", "OTHER = 1"))
        self.assertFalse(ok)
        self.assertIn("TTL_SEC", msg)

    def test_non_positive_ttl_fails(self):
        self._patch()
        ok, msg = self._run(GOOD.replace("TTL_SEC = 1800.0", "TTL_SEC = 0"))
        self.assertFalse(ok)
        self.assertIn("양수", msg)

    def test_missing_sweep_fails(self):
        self._patch()
        ok, msg = self._run(GOOD.replace("def _sweep(self, now):", "def _other(self, now):"))
        self.assertFalse(ok)
        self.assertIn("_sweep", msg)

    def test_sweep_that_ignores_the_ttl_fails(self):
        """상한만 보고 시간을 보지 않는 정리는 만료가 아니다."""
        self._patch()
        bad = GOOD.replace("if now - v > ttl()", "if len(self._open) > 500")
        ok, msg = self._run(bad)
        self.assertFalse(ok)
        self.assertIn("읽지 않는다", msg)

    def test_unwired_sweep_fails(self):
        """아무도 부르지 않는 정리 함수는 만료가 없는 것과 같다."""
        self._patch()
        ok, msg = self._run(GOOD.replace("        self._sweep(0)\n", ""))
        self.assertFalse(ok)
        self.assertIn("부르지 않는다", msg)

    def test_direct_ttl_read_also_passes(self):
        """판독 래퍼가 없어도 된다(한 홉은 편의이지 요구가 아니다)."""
        self._patch()
        ok, msg = self._run(GOOD.replace("> ttl()", "> TTL_SEC"))
        self.assertTrue(ok, msg)

    def test_store_missing_from_cap_registry_fails(self):
        """상한과 만료는 **둘 다** 필요하다."""
        self._patch()
        verify.MEM_STORES.pop(self.KEY, None)
        ok, msg = self._run(GOOD)
        self.assertFalse(ok)
        self.assertIn("MEM_STORES", msg)

    def test_stale_registry_entries_fail(self):
        self._patch()
        ok, msg = self._run("x = 1\n")          # 저장소 선언 자체가 없다
        self.assertFalse(ok)
        self.assertIn("잔재", msg)
        ok2, msg2 = verify.check_stale_state({})   # 파일이 없다
        self.assertFalse(ok2)
        self.assertIn("잔재", msg2)

    def test_unregistered_stores_are_not_checked(self):
        """과차단 금지 — 등록하지 않은 저장소는 보지 않는다."""
        self._patch()
        ok, msg = verify.check_stale_state({"fake.py": GOOD, "other.py": GOOD})
        self.assertTrue(ok, msg)

    def test_syntax_error_is_left_to_py_compile(self):
        self._patch()
        ok, msg = self._run("def (:\n")
        self.assertTrue(ok, msg)

    # --- 실저장소 ---------------------------------------------------------
    def test_repository_passes_today(self):
        ok, msg = verify.check_stale_state(verify.api_sources())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.STALE_STORES), msg)

    def test_registry_matches_reality(self):
        srcs = verify.api_sources()
        for (fname, store), (ttl, sweep, why) in verify.STALE_STORES.items():
            self.assertIn(fname, srcs)
            self.assertIn("%s = " % ttl, srcs[fname])
            self.assertIn("def %s(" % sweep, srcs[fname])
            self.assertTrue(why.strip(), "사유가 비어 있다: %s" % store)
            self.assertIn((fname, store), verify.MEM_STORES,
                          "상한 등록부에도 있어야 한다: %s" % store)

    def test_the_real_defects_would_be_caught_today(self):
        """저장소 파일 그대로 — 고친 자리를 되돌리면 게이트가 운다."""
        srcs = verify.api_sources()
        srcs["_call_metrics.py"] = srcs["_call_metrics.py"].replace(
            "            self._sweep_open(t)\n", "").replace(
            "            self._sweep_open(t)", "")
        ok, msg = verify.check_stale_state(srcs)
        self.assertFalse(ok)
        self.assertIn("_sweep_open", msg)

        srcs2 = verify.api_sources()
        srcs2["_escalation.py"] = srcs2["_escalation.py"].replace(
            "OPEN_TTL_SEC = 1800.0", "OPEN_TTL_SEC = 0")
        ok2, msg2 = verify.check_stale_state(srcs2)
        self.assertFalse(ok2)
        self.assertIn("양수", msg2)

    def test_main_reports_this_check_not_a_stub(self):
        import contextlib
        import io
        _ok, msg = verify.check_stale_state(verify.api_sources())
        buf = io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py"]
        try:
            with contextlib.redirect_stdout(buf):
                verify.main()
        finally:
            sys.argv = argv
        out = buf.getvalue()
        self.assertIn("stale_state", out)
        self.assertIn(msg, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
