# -*- coding: utf-8 -*-
"""과금 라인 합계의 단위 회귀 (38차) — 네트워크 미사용.

왜 이 파일이 필요한가
  `/api/settlement?op=lines` 는 **청구 근거 화면**이다. 이 모듈은 금액을 모를 때
  0 으로 채우지 않는다 — 「0 원도 주장이고, 틀린 주장이 청구서로 가면 분쟁이
  된다」가 모듈 첫 문장이다. 그런데 `totals()["count"]` 는 그 원칙을 어겼다:

      건수 = 1,240(통화 과금 단위) + 310(세션 수) + 865(통화 과금 단위) + …
           = 2,655  ← 아무 뜻도 없는 숫자

  줄마다 단위가 다르다(통화 과금 단위·세션 수·토큰 수·STT 초…). 서로 더할 수
  없는 것을 더한 숫자가 정산 화면 맨 위에 **과금 수량처럼** 서 있었다.
  콘솔도 그 값을 「건수 2,655」 칩으로 그렸다.

  이제 단위별 합계(`counts_by_unit`)를 내보내고, 단위가 섞이면 `count` 는 0 이
  아니라 **null** 이다(더할 수 없는 것에는 합계가 없다 — 금액과 같은 규약).

실행: python3 -m pytest tests/test_settle_lines_totals.py -q
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _settle_lines as sl  # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


class TestMixedUnitsHaveNoSingleCount(unittest.TestCase):
    def test_demo_totals_do_not_add_units_together(self):
        """이번 결함 그대로 — 데모 표의 「건수」는 더해지지 않는다."""
        out = sl.build("2026-08", {})
        t = out["totals"]
        self.assertTrue(t["units_mixed"])
        self.assertIsNone(t["count"])
        self.assertEqual(t["counts_by_unit"],
                         {"voice_units": 1240 + 865, "sessions": 310 + 198 + 42})
        # 섞어 더한 옛 숫자가 어디에도 남아 있지 않다
        self.assertNotIn(2655, list(t["counts_by_unit"].values()))

    def test_ledger_totals_split_by_unit(self):
        buckets = [{"tenant_id": "acme", "calls": 3, "minutes": 2.2, "revenue_krw": 1000},
                   {"tenant_id": "zed", "calls": 2, "minutes": 1.0, "revenue_krw": None}]
        t = sl.totals(sl.ledger_lines("2026-08", buckets))
        self.assertEqual(t["counts_by_unit"], {"voice_units": 3 + 1, "sessions": 3 + 2})
        self.assertIsNone(t["count"])
        self.assertTrue(t["units_mixed"])

    def test_every_unit_key_is_known(self):
        """단위별 합계의 키는 라벨을 가진 단위다(화면이 이름을 붙일 수 있다)."""
        for env in ({}, {"SETTLEMENT_SOURCE": "demo"}):
            out = sl.build("2026-08", env)
            for unit in out["totals"]["counts_by_unit"]:
                self.assertIn(unit, sl.UNITS, unit)


class TestSingleUnitKeepsItsTotal(unittest.TestCase):
    """단위가 하나면 합계가 뜻을 갖는다 — 그때는 종전처럼 더한다."""

    def test_one_unit_table_sums(self):
        lines = [sl._line("2026-08", "a", "sessions", 5, None, False),
                 sl._line("2026-08", "b", "sessions", 7, None, False)]
        t = sl.totals(lines)
        self.assertEqual((t["count"], t["units_mixed"]), (12, False))
        self.assertEqual(t["counts_by_unit"], {"sessions": 12})

    def test_tenant_filter_can_collapse_to_one_unit(self):
        out = sl.build("2026-08", {}, tenant_id="demo-tenant-c")
        self.assertEqual(out["totals"]["counts_by_unit"], {"sessions": 42})
        self.assertEqual(out["totals"]["count"], 42)
        self.assertFalse(out["totals"]["units_mixed"])

    def test_empty_table_still_reports_zero(self):
        t = sl.totals([])
        self.assertEqual((t["count"], t["counts_by_unit"], t["units_mixed"]),
                         (0, {}, False))


class TestOtherTotalsUnchanged(unittest.TestCase):
    """금액 쪽 규약은 건드리지 않았다."""

    def test_amount_rules_hold(self):
        lines = [sl._line("2026-08", "a", "voice_units", 2, 1000, False),
                 sl._line("2026-08", "a", "sessions", 2, None, False)]
        t = sl.totals(lines)
        self.assertEqual((t["amount_krw"], t["amount_complete"]), (1000, False))
        self.assertEqual((t["lines"], t["tenants"]), (2, 1))

    def test_no_amount_at_all_is_null_not_zero(self):
        t = sl.totals([sl._line("2026-08", "a", "sessions", 2, None, False)])
        self.assertIsNone(t["amount_krw"])

    def test_csv_body_unchanged(self):
        """CSV 는 줄 단위다 — 합계 줄이 없으므로 이번 변경과 무관하다."""
        body = sl.to_csv(sl.build("2026-08", {}))
        self.assertEqual(body.count("\r\n"), len(sl._DEMO_ROWS) + 1)
        self.assertNotIn("2655", body)


class TestConsoleDrawsPerUnitCounts(unittest.TestCase):
    """콘솔이 서버 응답만 그린다 — 섞어 더한 칩을 만들지 않는다."""

    def setUp(self):
        with open(ADMIN, encoding="utf-8") as f:
            self.html = f.read()

    def test_helper_is_wired(self):
        self.assertIn("function scCountTags(", self.html)
        self.assertIn("scCountTags(t,d.units)", self.html)

    def test_old_single_count_chip_is_gone(self):
        self.assertNotIn("'건수 '+scNum(t.count)+'</span>'", self.html)

    def test_helper_reads_counts_by_unit(self):
        body = re.search(r"function scCountTags\(t,units\)\{(.+?)\n(?=function )",
                         self.html, re.S)
        self.assertIsNotNone(body)
        src = body.group(1)
        self.assertIn("counts_by_unit", src)
        self.assertIn("scEsc(", src)                     # 단위 라벨도 이스케이프
        self.assertNotIn("innerHTML", src)

    def test_helper_has_no_hardcoded_numbers(self):
        body = re.search(r"function scCountTags\(t,units\)\{(.+?)\n(?=function )",
                         self.html, re.S).group(1)
        self.assertEqual(re.findall(r"\b\d{2,}\b", body), [])


if __name__ == "__main__":
    unittest.main()
