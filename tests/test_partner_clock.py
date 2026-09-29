# -*- coding: utf-8 -*-
"""귀속 장부 시계 회귀 — 의존성 0, 네트워크 미사용.

배경 — `time.time()` 의 해상도는 플랫폼마다 다르고 Windows 에서는 **15.6ms** 다.
연속 호출이 같은 값을 돌려주므로, 장부가 시계를 그대로 찍으면 같은 틱에 들어온
담당 변경이 `[t, t)` — **길이 0 인 귀속 기간**을 만든다. 이 기간은 겹침·빈틈
불변식을 통과하면서도 어떤 시점으로도 조회되지 않아서, "그때 누구 담당이었냐"에
직전 담당이 아니라 **다음 담당**이 답으로 나온다. 정산 분쟁에서 지는 장부다.

검증 대상
  1) `_stamp()` 는 거친 시계에서도 같은 값을 두 번 주지 않는다.
  2) `_stamp()` 는 시계가 뒤로 가도(NTP 보정) 되감지 않는다 — append-only 순서.
  3) 같은 틱의 담당 변경이 이전 기간을 조회 불가로 만들지 않는다.
  4) 조회 시계(`_now()`)는 장부보다 과거를 가리키지 않고, 눈금을 옮기지도 않는다.
  5) 길이 0 인 기간이 생기면(명시적 시각 주입) 불변식 검사가 드러낸다.
  6) 조회 경로는 자기 시계를 장부 질의에 강요하지 않는다 — 다만 호출자가 시점을
     명시하면 그 시점으로 묻는다.

실행: python3 -m pytest tests/test_partner_clock.py
"""
import os
import sys
import time as _real_time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import partners       # noqa: E402
import caller_id      # noqa: E402

GATE = "PARTNER_RBAC_LIVE"


class CoarseClock(object):
    """해상도가 거친 시계 — 연속 호출이 **같은 값**을 돌려준다(Windows 15.6ms 재현)."""

    def __init__(self, t):
        self.t = float(t)

    def time(self):
        return self.t

    def __getattr__(self, name):        # gmtime·strftime 등은 진짜 time 에 위임
        return getattr(_real_time, name)


class ClockBase(unittest.TestCase):
    def setUp(self):
        self._gate = os.environ.get(GATE)
        os.environ.pop(GATE, None)
        self._time = partners.time
        partners._clear_for_tests()

    def tearDown(self):
        partners.time = self._time
        partners._clear_for_tests()
        caller_id._clear_for_tests()
        if self._gate is None:
            os.environ.pop(GATE, None)
        else:
            os.environ[GATE] = self._gate

    def freeze(self, t=None):
        """장부 시계를 한 틱에 묶는다. 반환값이 그 틱의 벽시계 값."""
        t = _real_time.time() if t is None else float(t)
        partners.time = CoarseClock(t)
        return t


# --------------------------------------------------------------------------
# 1~2) 장부 시각은 겹치지도 되감기지도 않는다
# --------------------------------------------------------------------------
class TestStamp(ClockBase):
    def test_coarse_clock_does_not_repeat_a_stamp(self):
        self.freeze()
        stamps = [partners._stamp() for _ in range(5)]
        self.assertEqual(len(set(stamps)), 5)
        self.assertEqual(stamps, sorted(stamps))

    def test_stamp_does_not_go_backwards_when_the_clock_does(self):
        t = self.freeze()
        first = partners._stamp()
        self.freeze(t - 3600)               # NTP 보정으로 한 시간 뒤로
        self.assertGreater(partners._stamp(), first)

    def test_stamp_follows_the_wall_clock_when_it_moves_on(self):
        t = self.freeze()
        partners._stamp()
        self.freeze(t + 60)
        self.assertAlmostEqual(partners._stamp(), t + 60, places=6)


# --------------------------------------------------------------------------
# 3~4) 같은 틱의 담당 변경 — 이전 기간이 사라지지 않는다
# --------------------------------------------------------------------------
class TestSameTickHandover(ClockBase):
    def _ledger(self):
        partners.create_partner("ch-alpha", "알파채널")
        partners.create_partner("ch-beta", "베타채널")
        partners.attach("acme", "partner_managed", partner_id="ch-alpha")

    def test_previous_period_is_still_queryable(self):
        self.freeze()
        self._ledger()
        opened = partners._ACCOUNTS["acme"]["periods"][0]["from_ts"]
        partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                          reason="담당 이관")
        cur = partners.attribution_at("acme", opened)
        self.assertIsNotNone(cur)
        self.assertEqual(cur["partner_id"], "ch-alpha")   # 이관 전 담당이 답이다

    def test_no_zero_length_period_is_created(self):
        self.freeze()
        self._ledger()
        partners.reassign("acme", "partner_managed", partner_id="ch-beta")
        first = partners._ACCOUNTS["acme"]["periods"][0]
        self.assertGreater(first["to_ts"], first["from_ts"])
        self.assertEqual(partners.check_invariants(), [])

    def test_scope_at_the_handover_instant_returns_the_new_partner(self):
        self.freeze()
        self._ledger()
        partners.reassign("acme", "partner_managed", partner_id="ch-beta")
        at = partners._ACCOUNTS["acme"]["periods"][1]["from_ts"]
        self.assertEqual(partners.attribution_at("acme", at)["partner_id"], "ch-beta")

    def test_read_clock_never_precedes_the_ledger(self):
        self.freeze()
        self._ledger()
        opened = partners._ACCOUNTS["acme"]["periods"][0]["from_ts"]
        self.assertGreaterEqual(partners._now(), opened)
        # 기본 인자 조회도 방금 연 기간을 찾아낸다
        self.assertEqual(partners.attribution_at("acme")["partner_id"], "ch-alpha")

    def test_reading_does_not_advance_the_ledger_clock(self):
        self.freeze()
        first = partners._stamp()
        for _ in range(5):
            partners._now()
        self.assertAlmostEqual(partners._stamp(), first + partners._CLOCK_TICK,
                               places=6)


# --------------------------------------------------------------------------
# 5) 길이 0 인 기간은 드러난다
# --------------------------------------------------------------------------
class TestInvariant(ClockBase):
    def test_zero_length_period_is_reported(self):
        t = _real_time.time()
        partners.create_partner("ch-alpha", "알파채널")
        partners.create_partner("ch-beta", "베타채널")
        partners.attach("acme", "partner_managed", partner_id="ch-alpha", now=t)
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t)
        problems = partners.check_invariants()
        self.assertTrue(any("길이 0" in p for p in problems), problems)

    def test_normal_ledger_has_no_problems(self):
        t = _real_time.time()
        partners.create_partner("ch-alpha", "알파채널")
        partners.create_partner("ch-beta", "베타채널")
        partners.attach("acme", "partner_managed", partner_id="ch-alpha", now=t)
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t + 1)
        self.assertEqual(partners.check_invariants(), [])


# --------------------------------------------------------------------------
# 6) 조회 경로는 자기 시계를 장부에 강요하지 않는다
# --------------------------------------------------------------------------
class TestReadPathClock(ClockBase):
    """발신번호 목록의 `now` 는 **등록 만료 계산용** 시계다. 그것을 귀속 장부
    질의 시점으로 그대로 넘기면, 두 시계가 조금만 어긋나도 장부에 멀쩡히 있는
    고객사가 화면에서 사라진다(권한 통제가 아니라 사고다).
    """

    def setUp(self):
        super(TestReadPathClock, self).setUp()
        # 장부 시계를 조회 시계보다 앞세운다 — 어긋남을 크게 만들어 고정한다
        self.t = self.freeze(_real_time.time() + 60)
        partners.create_partner("ch-alpha", "알파채널")
        partners.attach("acme", "partner_managed", partner_id="ch-alpha")
        partners.attach("selfco", "direct")
        caller_id._clear_for_tests()
        caller_id.register("acme", "02-1234-5678", label="대표")
        caller_id.register("selfco", "02-9876-5432", label="대표")
        os.environ[GATE] = "1"

    def _tids(self, rows):
        return sorted({r["tenant_id"] for r in rows})

    def test_account_is_not_lost_when_the_ledger_clock_runs_ahead(self):
        rows = caller_id.list_numbers(role="partner_admin", actor_partner_id="ch-alpha")
        self.assertEqual(self._tids(rows), ["acme"])

    def test_explicit_point_in_time_is_still_honoured(self):
        # 귀속 전 시점으로 물으면 그 시점의 사실대로 비어 있다
        rows = caller_id.list_numbers(now=self.t - 3600, role="partner_admin",
                                      actor_partner_id="ch-alpha")
        self.assertEqual(rows, [])

    def test_owner_is_unaffected(self):
        rows = caller_id.list_numbers(role="owner")
        self.assertEqual(self._tids(rows), ["acme", "selfco"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
