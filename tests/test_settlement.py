# -*- coding: utf-8 -*-
"""api/settlement.py 파트너 정산 리포트 회귀 테스트.

검증 대상 (COMMERCIAL_READINESS '파트너 채널 → 정산 리포트')
  1) 요율은 설정값   — 하드코딩 없음, 미설정이면 금액을 만들지 않는다
  2) 요율표 검증     — 잘못된 카드는 조용히 고치지 않고 문제로 드러낸다
  3) 실적 수집       — 주입된 관측치만, 입력검증·상한·보관, 통화를 죽이지 않는 래퍼
  4) 기간 배분       — 귀속 변경일 분할, **쪼개도 합이 보존**, 미귀속 조각 노출
  5) 수수료 산출     — pct·건당·복합·반올림, 근거 문자열, 계산 불가 시 null
  6) 합계            — 빈 줄을 0 으로 메우지 않음, complete 플래그, attention
  7) 부작용 없음     — 조회가 장부·요율·버킷을 바꾸지 않는다
  8) CSV             — 컬럼 고정·수식주입 방어·빈 칸·BOM·CRLF·파일명
  9) HTTP 계약       — 403/200/400/405 봉투, no-store, CORS 되비침 금지, 네트워크 미사용
 10) 개인정보·날조    — 원문 이름·번호 미노출, 없는 수치를 만들지 않는다

실행: python3 -m pytest tests/test_settlement.py -q
"""
import io
import os
import re
import sys
import json
import random
import calendar
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import partners            # noqa: E402
import settlement as st    # noqa: E402
import _ratelimit          # noqa: E402

ENVS = ("PARTNER_RATE_CARD", "PARTNER_RBAC_LIVE", "CALLBOT_API_KEY",
        "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS")
ORIGIN = {"origin": "https://callbot-portal.vercel.app"}
DAY = 86400.0
# 2026-08-10 12:00 KST
AUG10 = float(calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0)))
SEP05 = float(calendar.timegm((2026, 9, 5, 3, 0, 0, 0, 0, 0)))


class NetworkTouched(AssertionError):
    pass


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class Resp(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))

    def text(self):
        return self.wfile.data.decode("utf-8")


def call(method="GET", query="", body=None, headers=None):
    r = Resp()
    path = "/api/settlement" + ("?" + query if query else "")
    inst = st.handler.__new__(st.handler)
    h = FakeHeaders(headers if headers is not None else dict(ORIGIN))
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    if body is not None:
        h["content-length"] = str(len(payload))
        h["content-type"] = "application/json"
    inst.headers = h
    inst.wfile = r.wfile
    inst.path = path
    inst.rfile = io.BytesIO(payload)
    inst.send_response = lambda c: setattr(r, "status", c)
    inst.send_header = lambda k, v: r.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    getattr(inst, "do_" + method)()
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)
        _ratelimit.reset()
        partners._clear_for_tests()
        st._clear_for_tests()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        partners._clear_for_tests()
        st._clear_for_tests()

    def _boom(self, *a, **k):
        raise NetworkTouched("정산 리포트가 네트워크를 건드렸다")

    # -- 사전 조건 헬퍼 -----------------------------------------------------
    def ledger(self, tid="acme", pid="ch-alpha", channel="partner_managed",
               name="알파채널", at=AUG10 - 30 * DAY, owner=""):
        if pid and pid not in [p["id"] for p in partners.list_partners()]:
            partners.create_partner(pid, name, now=at)
        return partners.attach(tid, channel, partner_id=pid, owner=owner,
                               contracted_at=at, now=at)

    def card(self, rates, version="test-1"):
        st.set_rate_card({"version": version, "rates": rates})


# ==========================================================================
# 1) 요율은 설정값 — 하드코딩이 없다
# ==========================================================================
class TestRateCardIsConfiguration(Base):
    def test_default_card_is_empty(self):
        card, probs, source = st.rate_card()
        self.assertEqual(card["rates"], {})
        self.assertEqual(source, "none")
        self.assertTrue(probs, "미설정을 문제로 드러내야 한다")
        self.assertIn("승인", " ".join(probs))

    def test_source_has_no_hardcoded_rate(self):
        """요율 숫자가 코드에 박히면 계약이 바뀌어도 청구서가 안 바뀐다."""
        src = io.open(os.path.join(ROOT, "api", "settlement.py"),
                      encoding="utf-8").read()
        src = src.split('if __name__ ==')[0]
        self.assertFalse(re.search(r'commission_pct"?\s*[:=]\s*[\d]', src))
        self.assertFalse(re.search(r'unit_fee_krw"?\s*[:=]\s*[\d]', src))
        self.assertEqual(st.EMPTY_CARD["rates"], {})

    def test_env_card_is_read_at_call_time(self):
        os.environ["PARTNER_RATE_CARD"] = json.dumps(
            {"version": "v-env", "rates": {"ch-alpha/*": {"commission_pct": 10}}})
        card, probs, source = st.rate_card()
        self.assertEqual((card["version"], source, probs), ("v-env", "env", []))
        os.environ["PARTNER_RATE_CARD"] = json.dumps(
            {"version": "v-env2", "rates": {"ch-alpha/*": {"commission_pct": 11}}})
        self.assertEqual(st.rate_card()[0]["version"], "v-env2")

    def test_broken_env_json_is_reported_not_swallowed(self):
        os.environ["PARTNER_RATE_CARD"] = "{not json"
        card, probs, source = st.rate_card()
        self.assertEqual(card["rates"], {})
        self.assertEqual(source, "env")
        self.assertTrue(any("JSON" in p for p in probs))

    def test_runtime_override_takes_precedence(self):
        os.environ["PARTNER_RATE_CARD"] = json.dumps(
            {"version": "env", "rates": {"*/*": {"unit_fee_krw": 50}}})
        self.card({"*/*": {"unit_fee_krw": 70}}, version="runtime")
        card, _p, source = st.rate_card()
        self.assertEqual((card["version"], source), ("runtime", "runtime"))

    def test_set_rate_card_rejects_invalid(self):
        with self.assertRaises(ValueError):
            st.set_rate_card({"version": "x", "rates": {"ch-a/*": {"commission_pct": 101}}})

    def test_api_cannot_change_rates(self):
        r = call("POST", body={"version": "hack", "rates": {}})
        self.assertEqual(r.status, 405)
        self.assertEqual(r.body()["code"], "METHOD_NOT_ALLOWED")
        self.assertEqual(r.header("Allow"), "GET, OPTIONS")
        self.assertFalse(st.rate_card()[0]["rates"])


# ==========================================================================
# 2) 요율표 검증 — 조용히 고치지 않는다
# ==========================================================================
class TestRateCardValidation(Base):
    def test_rejects_out_of_range_pct(self):
        for bad in (-1, 100.5, "abc", True):
            _c, probs = st.validate_rate_card(
                {"version": "v", "rates": {"ch-a/*": {"commission_pct": bad}}})
            self.assertTrue(probs, bad)

    def test_rejects_non_integer_unit_fee(self):
        _c, probs = st.validate_rate_card(
            {"version": "v", "rates": {"ch-a/*": {"unit_fee_krw": 12.5}}})
        self.assertTrue(probs)

    def test_rejects_empty_rule(self):
        _c, probs = st.validate_rate_card({"version": "v", "rates": {"ch-a/*": {}}})
        self.assertTrue(any("필요" in p for p in probs))

    def test_rejects_bad_key_shape(self):
        for bad in ("ch-a", "ch a/*", "/*", "ch-a/partner managed", "a" * 50 + "/*"):
            _c, probs = st.validate_rate_card(
                {"version": "v", "rates": {bad: {"commission_pct": 10}}})
            self.assertTrue(probs, bad)

    def test_currency_and_vat_checked(self):
        card, probs = st.validate_rate_card(
            {"version": "v", "currency": "USD", "vat": "maybe", "rates": {}})
        self.assertTrue(any("KRW" in p for p in probs))
        self.assertTrue(any("vat" in p for p in probs))
        self.assertEqual(card["vat"], "excluded")

    def test_missing_version_is_a_problem(self):
        _c, probs = st.validate_rate_card({"rates": {"ch-a/*": {"commission_pct": 1}}})
        self.assertTrue(any("version" in p for p in probs))

    def test_non_dict_card(self):
        card, probs = st.validate_rate_card("요율 15%")
        self.assertEqual(card["rates"], {})
        self.assertTrue(probs)

    def test_lookup_precedence(self):
        card, _p = st.validate_rate_card({"version": "v", "rates": {
            "ch-a/partner_managed": {"commission_pct": 20},
            "ch-a/*": {"commission_pct": 15},
            "*/partner_referral": {"commission_pct": 10},
            "*/*": {"commission_pct": 5},
        }})
        self.assertEqual(st.lookup_rate(card, "ch-a", "partner_managed")[1],
                         "ch-a/partner_managed")
        self.assertEqual(st.lookup_rate(card, "ch-a", "inbound")[1], "ch-a/*")
        self.assertEqual(st.lookup_rate(card, "ch-b", "partner_referral")[1],
                         "*/partner_referral")
        self.assertEqual(st.lookup_rate(card, "ch-b", "event")[1], "*/*")

    def test_lookup_miss_returns_none(self):
        card, _p = st.validate_rate_card(
            {"version": "v", "rates": {"ch-a/*": {"commission_pct": 15}}})
        self.assertEqual(st.lookup_rate(card, "ch-b", "event"), (None, ""))


# ==========================================================================
# 3) 실적 수집
# ==========================================================================
class TestUsage(Base):
    def test_accumulates_into_day_bucket(self):
        st.record_usage("acme", ts=AUG10, calls=3, minutes=5.5, revenue_krw=1000)
        b = st.record_usage("acme", ts=AUG10 + 3600, calls=2, minutes=1.5,
                            revenue_krw=500)
        self.assertEqual((b["calls"], b["revenue_krw"], b["records"]), (5, 1500, 2))

    def test_kst_day_boundary(self):
        """한국 날짜로 끊는다 — UTC 로 끊으면 말일 밤 9시간이 옆 달로 샌다."""
        kst_late = float(calendar.timegm((2026, 8, 31, 20, 0, 0, 0, 0, 0)))  # 9/1 05:00 KST
        st.record_usage("acme", ts=kst_late, calls=1)
        self.assertEqual(st.usage_coverage("2026-08", now=SEP05)["calls"], 0)
        self.assertEqual(st.usage_coverage("2026-09", now=SEP05)["calls"], 1)

    def test_rejects_bad_input(self):
        bad = [
            dict(tenant_id="한글"), dict(tenant_id="acme", calls=-1),
            dict(tenant_id="acme", calls=st.MAX_CALLS_PER_RECORD + 1),
            dict(tenant_id="acme", minutes=-0.1),
            dict(tenant_id="acme", revenue_krw=-5),
            dict(tenant_id="acme", revenue_krw=True),
            dict(tenant_id="acme", ts=0),
        ]
        for kw in bad:
            kw.setdefault("ts", AUG10)
            with self.assertRaises((ValueError, TypeError), msg=str(kw)):
                st.record_usage(**kw)

    def test_safe_wrapper_never_raises_but_counts(self):
        self.assertIsNone(st.record_usage_safe("한글", ts=AUG10))
        self.assertEqual(st.usage_coverage("2026-08", now=SEP05)["collector"]["errors"], 1)

    def test_bucket_cap_evicts_oldest(self):
        orig = st.MAX_USAGE_BUCKETS
        st.MAX_USAGE_BUCKETS = 2
        try:
            st.record_usage("a", ts=AUG10, calls=1)
            st.record_usage("b", ts=AUG10 + DAY, calls=1)
            st.record_usage("c", ts=AUG10 + 2 * DAY, calls=1)
            cov = st.usage_coverage("2026-08", now=SEP05)
            self.assertEqual(cov["buckets"], 2)
            self.assertEqual(cov["collector"]["dropped"], 1)
        finally:
            st.MAX_USAGE_BUCKETS = orig

    def test_retention_prunes_old(self):
        st.record_usage("acme", ts=AUG10 - 500 * DAY, calls=9)
        st.record_usage("acme", ts=AUG10, calls=1)     # prune 을 유발
        self.assertEqual(st.usage_coverage("2026-08", now=SEP05)["calls"], 1)

    def test_coverage_empty_is_none_source(self):
        cov = st.usage_coverage("2026-08", now=SEP05)
        self.assertEqual(cov["data_source"], "none")
        self.assertEqual((cov["calls"], cov["buckets"], cov["tenants"]), (0, 0, 0))

    def test_returned_bucket_is_a_copy(self):
        b = st.record_usage("acme", ts=AUG10, calls=1)
        b["calls"] = 9999
        self.assertEqual(st.usage_coverage("2026-08", now=SEP05)["calls"], 1)


# ==========================================================================
# 4) 기간 배분 — 쪼개도 합이 보존된다
# ==========================================================================
class TestAllocation(Base):
    def test_allocate_preserves_sum(self):
        rnd = random.Random(7)
        for _ in range(200):
            total = rnd.randint(0, 5000)
            ws = [rnd.random() for _ in range(rnd.randint(1, 6))]
            parts = st.allocate_int(total, ws)
            self.assertEqual(sum(parts), total)
            self.assertTrue(all(p >= 0 for p in parts))

    def test_allocate_zero_weights(self):
        self.assertEqual(sum(st.allocate_int(10, [0, 0])), 10)

    def test_allocate_empty(self):
        self.assertEqual(st.allocate_int(10, []), [])

    def test_midday_reassign_splits_and_preserves_totals(self):
        self.ledger("acme", "ch-alpha", "partner_managed")
        partners.create_partner("ch-beta", "베타채널", now=AUG10 - DAY)
        partners.reassign("acme", "partner_managed", "ch-beta", now=AUG10)  # 정오 교체
        st.record_usage("acme", ts=AUG10 - 3600, calls=100, minutes=200.0,
                        revenue_krw=1000000)
        st.record_usage("acme", ts=AUG10 + 3600, calls=100, minutes=200.0,
                        revenue_krw=1000000)
        rep = st.report("2026-08", now=SEP05)
        pids = sorted(l["partner_id"] for l in rep["lines"])
        self.assertEqual(pids, ["ch-alpha", "ch-beta"])
        self.assertEqual(sum(l["calls"] for l in rep["lines"]), 200)
        self.assertEqual(sum(l["revenue_krw"] for l in rep["lines"]), 2000000)
        self.assertTrue(all(l["split"] for l in rep["lines"]))

    def test_usage_before_ledger_entry_is_unattributed(self):
        self.ledger("acme", at=AUG10)          # 8/10 부터 장부에 있음
        st.record_usage("acme", ts=AUG10 - 5 * DAY, calls=4)
        rep = st.report("2026-08", now=SEP05)
        got = {l["attribution"] for l in rep["lines"]}
        self.assertIn("unattributed", got)
        self.assertEqual(sum(l["calls"] for l in rep["lines"]), 4)

    def test_unknown_tenant_is_not_dropped(self):
        st.record_usage("ghost", ts=AUG10, calls=7)
        rep = st.report("2026-08", now=SEP05)
        self.assertEqual(len(rep["lines"]), 1)
        self.assertEqual(rep["lines"][0]["status"], "unattributed")
        self.assertIsNone(rep["lines"][0]["commission_krw"])
        self.assertEqual(rep["attention"][0]["tenant_id"], "ghost")

    def test_detached_account_keeps_prior_period(self):
        self.ledger("acme", at=AUG10 - 10 * DAY)
        partners.detach("acme", now=AUG10)
        st.record_usage("acme", ts=AUG10 - 2 * DAY, calls=10)
        self.card({"ch-alpha/*": {"unit_fee_krw": 100}})
        rep = st.report("2026-08", now=SEP05)
        line = [l for l in rep["lines"] if l["partner_id"] == "ch-alpha"][0]
        self.assertEqual(line["commission_krw"], 1000)


# ==========================================================================
# 5) 수수료 산출
# ==========================================================================
class TestCommission(Base):
    def one_line(self, rep, tid="acme"):
        got = [l for l in rep["lines"] if l["tenant_id"] == tid]
        self.assertEqual(len(got), 1, rep["lines"])
        return got[0]

    def test_percent_of_revenue(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=10, revenue_krw=1000000)
        self.card({"ch-alpha/*": {"commission_pct": 12.5}})
        line = self.one_line(st.report("2026-08", now=SEP05))
        self.assertEqual(line["commission_krw"], 125000)
        self.assertIn("12.5%", line["basis"])

    def test_unit_fee_per_call_without_revenue(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=37)
        self.card({"ch-alpha/*": {"unit_fee_krw": 120}})
        line = self.one_line(st.report("2026-08", now=SEP05))
        self.assertEqual((line["status"], line["commission_krw"]), ("ok", 4440))

    def test_combined_rule(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=10, revenue_krw=100000)
        self.card({"ch-alpha/*": {"commission_pct": 10, "unit_fee_krw": 50}})
        line = self.one_line(st.report("2026-08", now=SEP05))
        self.assertEqual(line["commission_krw"], 10000 + 500)

    def test_rounding_half_up_to_won(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=1, revenue_krw=1005)
        self.card({"ch-alpha/*": {"commission_pct": 50}})   # 502.5 → 503
        self.assertEqual(self.one_line(st.report("2026-08", now=SEP05))["commission_krw"], 503)

    def test_rate_missing_leaves_amount_null(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=10, revenue_krw=1000)
        rep = st.report("2026-08", now=SEP05)
        line = self.one_line(rep)
        self.assertEqual(line["status"], "rate_missing")
        self.assertIsNone(line["commission_krw"])
        self.assertIn("승인", line["basis"])
        self.assertFalse(rep["totals"]["complete"])

    def test_revenue_missing_leaves_amount_null(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=10)        # 매출 미보고
        self.card({"ch-alpha/*": {"commission_pct": 15}})
        line = self.one_line(st.report("2026-08", now=SEP05))
        self.assertEqual(line["status"], "revenue_missing")
        self.assertIsNone(line["commission_krw"])

    def test_direct_contract_has_no_commission(self):
        partners.attach("selfco", "direct", contracted_at=AUG10 - DAY, now=AUG10 - DAY)
        st.record_usage("selfco", ts=AUG10, calls=5, revenue_krw=1000)
        self.card({"*/*": {"commission_pct": 30}})
        line = self.one_line(st.report("2026-08", now=SEP05), "selfco")
        self.assertEqual(line["status"], "direct")
        self.assertIsNone(line["commission_krw"])

    def test_partial_revenue_is_flagged(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=5, revenue_krw=1000)
        st.record_usage("acme", ts=AUG10 + DAY, calls=5)     # 다음 날은 매출 미보고
        self.card({"ch-alpha/*": {"commission_pct": 10}})
        line = self.one_line(st.report("2026-08", now=SEP05))
        self.assertTrue(line["revenue_partial"])


# ==========================================================================
# 6) 합계 — 없는 수치를 만들지 않는다
# ==========================================================================
class TestTotals(Base):
    def test_empty_month_has_no_numbers(self):
        rep = st.report("2026-08", now=SEP05)
        self.assertEqual(rep["lines"], [])
        self.assertIsNone(rep["totals"]["commission_krw"])
        self.assertIsNone(rep["totals"]["revenue_krw"])
        self.assertEqual(rep["totals"]["calls"], 0)
        self.assertEqual(rep["usage"]["data_source"], "none")

    def test_totals_sum_only_computed_lines(self):
        self.ledger("acme")
        self.ledger("beta", pid="ch-beta", name="베타채널")
        st.record_usage("acme", ts=AUG10, calls=10, revenue_krw=100000)
        st.record_usage("beta", ts=AUG10, calls=10)         # 매출 없음 → 산출 불가
        self.card({"*/*": {"commission_pct": 10}})
        rep = st.report("2026-08", now=SEP05)
        self.assertEqual(rep["totals"]["commission_krw"], 10000)
        self.assertEqual(rep["totals"]["commission_lines"], 1)
        self.assertFalse(rep["totals"]["complete"])
        self.assertEqual(len(rep["attention"]), 1)

    def test_status_is_always_draft(self):
        self.assertEqual(st.report("2026-08", now=SEP05)["status"], "draft")
        self.assertIn("승인", st.report("2026-08", now=SEP05)["note"])

    def test_in_progress_month_marked(self):
        self.assertTrue(st.report("2026-09", now=SEP05)["in_progress"])
        self.assertFalse(st.report("2026-08", now=SEP05)["in_progress"])

    def test_default_month_is_previous(self):
        self.assertEqual(st.report(now=SEP05)["month"], "2026-08")
        self.assertEqual(st.previous_month(float(calendar.timegm(
            (2026, 1, 15, 0, 0, 0, 0, 0, 0)))), "2025-12")

    def test_by_partner_rollup(self):
        self.ledger("acme")
        self.ledger("beta", pid="ch-alpha")
        st.record_usage("acme", ts=AUG10, calls=10, revenue_krw=100000)
        st.record_usage("beta", ts=AUG10, calls=20, revenue_krw=200000)
        self.card({"ch-alpha/*": {"commission_pct": 10}})
        rep = st.report("2026-08", now=SEP05)
        roll = [p for p in rep["by_partner"] if p["partner_id"] == "ch-alpha"][0]
        self.assertEqual((roll["tenants"], roll["calls"], roll["commission_krw"]),
                         (2, 30, 30000))
        self.assertEqual(roll["partner_name"], "알파채널")

    def test_partner_filter(self):
        self.ledger("acme")
        st.record_usage("acme", ts=AUG10, calls=1)
        st.record_usage("ghost", ts=AUG10, calls=1)
        self.assertEqual([l["tenant_id"] for l in
                          st.report("2026-08", "ch-alpha", now=SEP05)["lines"]], ["acme"])
        self.assertEqual([l["tenant_id"] for l in
                          st.report("2026-08", "-", now=SEP05)["lines"]], ["ghost"])

    def test_invalid_month_rejected(self):
        for bad in ("2026-13", "26-08", "2026/08", "", "abcd-ef"):
            with self.assertRaises(ValueError, msg=bad):
                st.month_range(bad)


# ==========================================================================
# 7) 부작용 없음
# ==========================================================================
class TestNoSideEffects(Base):
    def test_report_does_not_mutate_ledger_or_usage(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=5, revenue_krw=1000)
        self.card({"ch-alpha/*": {"commission_pct": 10}})
        before = json.dumps(partners.list_accounts(), ensure_ascii=False)
        cov_before = st.usage_coverage("2026-08", now=SEP05)
        rep = st.report("2026-08", now=SEP05)
        rep["lines"][0]["calls"] = 999
        self.assertEqual(json.dumps(partners.list_accounts(), ensure_ascii=False), before)
        self.assertEqual(st.usage_coverage("2026-08", now=SEP05), cov_before)
        self.assertEqual(st.report("2026-08", now=SEP05)["lines"][0]["calls"], 5)

    def test_report_does_not_change_rate_card(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=1)
        st.report("2026-08", now=SEP05)
        self.assertEqual(st.rate_card()[0]["rates"], {})

    def test_reading_does_not_enable_gates(self):
        st.summary(now=SEP05)
        st.report("2026-08", now=SEP05)
        self.assertFalse(partners.rbac_live())
        self.assertIsNone(os.environ.get("PARTNER_RATE_CARD"))


# ==========================================================================
# 8) CSV
# ==========================================================================
class TestCsv(Base):
    def test_header_is_fixed(self):
        rep = st.report("2026-08", now=SEP05)
        first = st.to_csv(rep).split("\r\n")[0]
        self.assertEqual(first, ",".join(st.CSV_COLUMNS))

    def test_formula_injection_neutralised(self):
        partners.create_partner("ch-alpha", "=cmd|' /c calc'!A1", now=AUG10 - DAY)
        partners.attach("acme", "partner_managed", "ch-alpha",
                        contracted_at=AUG10 - DAY, now=AUG10 - DAY)
        st.record_usage("acme", ts=AUG10, calls=1)
        text = st.to_csv(st.report("2026-08", now=SEP05))
        self.assertIn("'=cmd", text)
        self.assertNotIn(",=cmd", text)

    def test_cell_guard_covers_all_prefixes(self):
        for ch in ("=", "+", "-", "@"):
            self.assertTrue(st.csv_cell(ch + "x").startswith("'"))
        self.assertEqual(st.csv_cell(None), "")
        self.assertNotIn("\n", st.csv_cell("a\nb"))

    def test_missing_amount_is_blank_not_zero(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=3)          # 요율 미설정
        rows = st.to_csv(st.report("2026-08", now=SEP05)).strip().split("\r\n")
        cells = next(csv_reader(rows[1]))
        self.assertEqual(cells[st.CSV_COLUMNS.index("수수료(원)")], "")
        self.assertEqual(cells[st.CSV_COLUMNS.index("매출(원)")], "")
        self.assertEqual(cells[st.CSV_COLUMNS.index("상태")], "요율미설정")

    def test_filename_is_safe_and_marked_draft(self):
        name = st.csv_filename({"month": "2026-08"})
        self.assertEqual(name, "settlement_2026-08_draft.csv")
        self.assertNotIn("/", st.csv_filename({"month": "../../etc"}))

    def test_export_http_has_bom_and_disposition(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=1)
        r = call("GET", "op=export&month=2026-08")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.wfile.data.startswith(b"\xef\xbb\xbf"), "엑셀 한글 깨짐 방지 BOM")
        self.assertIn("attachment", r.header("Content-Disposition"))
        self.assertEqual(r.header("X-Settlement-Status"), "draft")
        self.assertEqual(r.header("Cache-Control"), "no-store")


def csv_reader(line):
    import csv as _csv
    return _csv.reader([line])


# ==========================================================================
# 9) HTTP 계약
# ==========================================================================
class TestHttp(Base):
    def test_summary_ok(self):
        r = call("GET")
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertTrue(b["ok"])
        self.assertFalse(b["rate_card"]["configured"])
        self.assertEqual(b["status"], "draft-only")
        self.assertEqual(r.header("Cache-Control"), "no-store")

    def test_foreign_origin_denied(self):
        r = call("GET", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"),
                            "https://evil.example")

    def test_report_endpoint(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=2)
        r = call("GET", "op=report&month=2026-08")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["month"], "2026-08")

    def test_bad_month_400_with_field(self):
        r = call("GET", "op=report&month=2026-99")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "month")

    def test_future_month_rejected(self):
        r = call("GET", "op=report&month=2099-01")
        self.assertEqual(r.status, 400)
        self.assertIn("미래", r.body()["details"][0]["reason"])

    def test_bad_partner_400(self):
        r = call("GET", "op=report&partner=%ED%95%9C%EA%B8%80")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "partner")

    def test_bad_op_400(self):
        r = call("GET", "op=drop_table")
        self.assertEqual(r.status, 400)

    def test_ratecard_endpoint_marks_readonly(self):
        r = call("GET", "op=ratecard")
        self.assertEqual(r.status, 200)
        self.assertFalse(r.body()["editable_via_api"])

    def test_usage_endpoint(self):
        st.record_usage("acme", ts=AUG10, calls=4)
        r = call("GET", "op=usage&month=2026-08")
        self.assertEqual(r.body()["calls"], 4)

    def test_write_methods_rejected(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(call(m, body={"x": 1}).status, 405, m)

    def test_request_id_header(self):
        r = call("GET")
        self.assertTrue(r.header("X-Request-Id"))

    def test_options_preflight(self):
        r = Resp()
        inst = st.handler.__new__(st.handler)
        inst.headers = FakeHeaders(dict(ORIGIN))
        inst.wfile = r.wfile
        inst.path = "/api/settlement"
        inst.send_response = lambda c: setattr(r, "status", c)
        inst.send_header = lambda k, v: r.sent.append((k, str(v)))
        inst.end_headers = lambda: None
        inst.do_OPTIONS()
        self.assertEqual(r.status, 204)


# ==========================================================================
# 10) 개인정보·날조 금지
# ==========================================================================
class TestPrivacyAndHonesty(Base):
    def test_owner_name_is_masked_everywhere(self):
        self.ledger(owner="홍길동")
        st.record_usage("acme", ts=AUG10, calls=1)
        rep = st.report("2026-08", now=SEP05)
        blob = json.dumps(rep, ensure_ascii=False) + st.to_csv(rep)
        self.assertNotIn("홍길동", blob)
        self.assertIn("홍*동", blob)

    def test_phone_like_owner_is_refused_upstream(self):
        with self.assertRaises(ValueError):
            self.ledger(owner="010-1234-5678")

    def test_no_phone_numbers_in_output(self):
        self.ledger()
        st.record_usage("acme", ts=AUG10, calls=1)
        rep = st.report("2026-08", now=SEP05)
        blob = json.dumps(rep, ensure_ascii=False) + st.to_csv(rep)
        self.assertFalse(re.search(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}", blob))
        self.assertFalse(re.search(r"\d{6}[-]\d{7}", blob))

    def test_no_demo_numbers_when_empty(self):
        """대장·실적이 비면 어떤 금액도 나오지 않는다(샘플 데이터 금지)."""
        rep = st.report("2026-08", now=SEP05)
        text = json.dumps(rep, ensure_ascii=False)
        self.assertNotIn("demo", text)
        self.assertEqual(rep["by_partner"], [])
        self.assertIsNone(rep["totals"]["commission_krw"])

    def test_summary_declares_limits(self):
        s = st.summary(now=SEP05)
        self.assertIn("승인", s["persistence"])
        self.assertEqual(s["usage"]["collector"]["scope"], "instance")

    def test_banned_brand_names_absent(self):
        src = io.open(os.path.join(ROOT, "api", "settlement.py"),
                      encoding="utf-8").read()
        for bad in ("농협", "라피치", "IBK", "날리지큐브", "보이스봇", "신세계", "하나은행"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
