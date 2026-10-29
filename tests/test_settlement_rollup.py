# -*- coding: utf-8 -*-
"""파트너별 집계(`report().by_partner`)의 **고객사 수** 회귀 — 2026-10-29(41차) 결함.

무엇이 틀렸었나
  `by_partner[*]["tenants"]` 는 줄마다 1 을 더해 **줄 수**를 적었다. 그런데 한
  고객사가 한 파트너 밑에서 줄을 두 개 만드는 일은 흔하다 —

    · 달 안에서 유입경로가 바뀌면 `split_bucket` 이 기간별로 쪼개고,
      키 `(고객사, 파트너, 경로, 귀속)` 이 달라져 줄이 둘로 선다.
    · 등록 전 구간은 `unattributed` 줄로 따로 서고, 파트너 없는 줄은
      `direct` 와 **같은 `-` 칸**에 모인다.

  그래서 고객사 1곳이 「고객사 2」로 적혔고, **같은 응답의**
  `totals.tenants`(집합으로 센다)와 다른 말을 했다. 청구 근거 화면에서 두
  숫자가 다르면 어느 쪽도 믿을 수 없다.

고친 뒤의 규약
  · `tenants` = 그 파트너 칸에 든 **서로 다른 고객사 수**
  · `lines`   = 그 칸의 줄 수(종전 값의 뜻 — 없애지 않고 제 이름을 준다)
  · 어떤 조합에서도 `sum(tenants) >= totals.tenants` 이고
    `sum(lines) == totals.lines`

네트워크 미사용. 실행: python3 -m pytest tests/test_settlement_rollup.py -q
"""
import calendar
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import partners            # noqa: E402
import settlement as st    # noqa: E402

DAY = 86400.0
AUG10 = float(calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0)))
SEP05 = float(calendar.timegm((2026, 9, 5, 3, 0, 0, 0, 0, 0)))


class NetworkTouched(AssertionError):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("PARTNER_RATE_CARD")
        os.environ.pop("PARTNER_RATE_CARD", None)
        partners._clear_for_tests()
        st._clear_for_tests()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        if self._saved is None:
            os.environ.pop("PARTNER_RATE_CARD", None)
        else:
            os.environ["PARTNER_RATE_CARD"] = self._saved
        partners._clear_for_tests()
        st._clear_for_tests()

    def _boom(self, *a, **k):
        raise NetworkTouched("정산 집계가 네트워크를 건드렸다")

    # -- 헬퍼 --------------------------------------------------------------
    def roll(self, rep, key):
        want = None if key == "-" else key
        rows = [p for p in rep["by_partner"] if p["partner_id"] == want]
        self.assertEqual(len(rows), 1, "파트너 칸 %s 가 하나가 아니다" % key)
        return rows[0]

    def assert_consistent(self, rep):
        """응답 안의 두 숫자가 같은 말을 하는가."""
        rolls = rep["by_partner"]
        self.assertEqual(sum(p["lines"] for p in rolls), rep["totals"]["lines"])
        self.assertGreaterEqual(sum(p["tenants"] for p in rolls),
                                rep["totals"]["tenants"])
        for p in rolls:
            self.assertLessEqual(p["tenants"], p["lines"])
            self.assertGreaterEqual(p["tenants"], 1)


class TenantCountIsDistinct(Base):
    def test_경로가_바뀐_고객사는_고객사_1곳이다(self):
        """같은 파트너 밑에서 유입경로만 바뀌어도 줄은 둘이 된다."""
        partners.create_partner("ch-alpha", "알파채널", now=AUG10 - 40 * DAY)
        partners.attach("acme", "partner_managed", partner_id="ch-alpha",
                        contracted_at=AUG10 - 30 * DAY, now=AUG10 - 30 * DAY)
        partners.reassign("acme", "partner_referral", partner_id="ch-alpha",
                          now=AUG10)
        st.record_usage("acme", ts=AUG10 - 3600, calls=4, minutes=10.0)
        st.record_usage("acme", ts=AUG10 + 3600, calls=6, minutes=20.0)

        rep = st.report("2026-08", now=SEP05)
        row = self.roll(rep, "ch-alpha")
        self.assertEqual(row["lines"], 2, rep["lines"])
        self.assertEqual(row["tenants"], 1, "고객사 1곳인데 %d 로 적혔다" % row["tenants"])
        self.assertEqual(row["calls"], 10)
        self.assertEqual(rep["totals"]["tenants"], 1)
        self.assert_consistent(rep)

    def test_등록_전_구간은_같은_칸에서_겹쳐_세지_않는다(self):
        """직접 계약 고객사 1곳 = `direct` 줄 + `unattributed` 줄(둘 다 `-` 칸)."""
        partners.attach("solo", "direct", contracted_at=AUG10, now=AUG10)
        st.record_usage("solo", ts=AUG10 - 5 * DAY, calls=3)   # 등록 전 → 미귀속
        st.record_usage("solo", ts=AUG10 + DAY, calls=7)       # 등록 후 → 직접

        rep = st.report("2026-08", now=SEP05)
        row = self.roll(rep, "-")
        self.assertEqual(sorted(d["attribution"] for d in rep["lines"]),
                         ["direct", "unattributed"])
        self.assertEqual(row["lines"], 2)
        self.assertEqual(row["tenants"], 1, "고객사 1곳인데 %d 로 적혔다" % row["tenants"])
        self.assertEqual(row["calls"], 10)
        self.assertEqual(rep["totals"]["tenants"], 1)
        self.assert_consistent(rep)

    def test_서로_다른_고객사는_그대로_센다(self):
        """과소보고를 만들지 않았다 — 고객사가 둘이면 둘이다."""
        partners.create_partner("ch-alpha", "알파채널", now=AUG10 - 40 * DAY)
        for tid in ("acme", "beta"):
            partners.attach(tid, "partner_managed", partner_id="ch-alpha",
                            contracted_at=AUG10 - 30 * DAY, now=AUG10 - 30 * DAY)
            st.record_usage(tid, ts=AUG10, calls=5)

        rep = st.report("2026-08", now=SEP05)
        row = self.roll(rep, "ch-alpha")
        self.assertEqual((row["tenants"], row["lines"], row["calls"]), (2, 2, 10))
        self.assertEqual(rep["totals"]["tenants"], 2)
        self.assert_consistent(rep)

    def test_파트너_칸이_섞여도_각_칸이_제_고객사만_센다(self):
        partners.create_partner("ch-alpha", "알파채널", now=AUG10 - 40 * DAY)
        partners.create_partner("ch-beta", "베타채널", now=AUG10 - 40 * DAY)
        partners.attach("acme", "partner_managed", partner_id="ch-alpha",
                        contracted_at=AUG10 - 30 * DAY, now=AUG10 - 30 * DAY)
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=AUG10)
        st.record_usage("acme", ts=AUG10 - 3600, calls=2)
        st.record_usage("acme", ts=AUG10 + 3600, calls=3)
        partners.attach("solo", "direct", contracted_at=AUG10 - 30 * DAY,
                        now=AUG10 - 30 * DAY)
        st.record_usage("solo", ts=AUG10, calls=1)

        rep = st.report("2026-08", now=SEP05)
        for key in ("ch-alpha", "ch-beta", "-"):
            self.assertEqual(self.roll(rep, key)["tenants"], 1, key)
        self.assertEqual(rep["totals"]["tenants"], 2)
        self.assert_consistent(rep)

    def test_빈_표에는_칸이_없다(self):
        rep = st.report("2026-08", now=SEP05)
        self.assertEqual(rep["by_partner"], [])
        self.assertEqual(rep["totals"]["tenants"], 0)

    def test_칸의_키가_사라지지_않았다(self):
        """하위호환 — 종전 칸이 전부 남아 있고 `lines` 가 늘었다."""
        partners.attach("solo", "direct", contracted_at=AUG10 - 30 * DAY,
                        now=AUG10 - 30 * DAY)
        st.record_usage("solo", ts=AUG10, calls=1)
        row = self.roll(st.report("2026-08", now=SEP05), "-")
        for k in ("partner_id", "partner_name", "tenants", "lines", "calls",
                  "minutes", "revenue_krw", "commission_krw", "needs_attention"):
            self.assertIn(k, row)


# --------------------------------------------------------------------------
# 라우트 — 소켓 E2E
#
# 화면이 읽는 것은 함수 반환값이 아니라 **라우트 응답**이다. 라우팅·가드·
# 직렬화를 거친 뒤에도 두 숫자가 같은 말을 하는지 본다.
# 루프백 + 임의 포트, 읽기 전용(과금 경로 없음).
# --------------------------------------------------------------------------
E2E_ENVS = ("CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_ALLOWED_ORIGINS",
            "PARTNER_RBAC_LIVE", "PARTNER_RATE_CARD")


class _Server(object):
    def __init__(self, handler):
        import http.server
        import threading
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def get(self, path):
        import json
        import urllib.error
        req = urllib.request.Request(self.origin + path, method="GET")
        req.add_header("Origin", self.origin)
        req.add_header("Sec-Fetch-Site", "same-origin")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class RollupOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in E2E_ENVS}
        for k in E2E_ENVS:
            os.environ.pop(k, None)
        cls.srv = _Server(st.handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        partners._clear_for_tests()
        st._clear_for_tests()

    def tearDown(self):
        partners._clear_for_tests()
        st._clear_for_tests()

    def test_응답의_두_숫자가_같은_말을_한다(self):
        partners.attach("solo", "direct", contracted_at=AUG10, now=AUG10)
        st.record_usage("solo", ts=AUG10 - 5 * DAY, calls=3)
        st.record_usage("solo", ts=AUG10 + DAY, calls=7)

        status, d = self.srv.get("?op=report&month=2026-08")
        self.assertEqual(status, 200, d)
        self.assertTrue(d["ok"])
        row = [p for p in d["by_partner"] if p["partner_id"] is None][0]
        self.assertEqual(row["tenants"], 1, d["by_partner"])
        self.assertEqual(row["lines"], 2, d["by_partner"])
        self.assertEqual(d["totals"]["tenants"], 1)
        self.assertEqual(d["totals"]["lines"], 2)
        self.assertEqual(sum(p["lines"] for p in d["by_partner"]),
                         d["totals"]["lines"])

    def test_빈_달도_거짓_없이_말한다(self):
        status, d = self.srv.get("?op=report&month=2026-08")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["by_partner"], [])
        self.assertEqual((d["totals"]["tenants"], d["totals"]["lines"]), (0, 0))

    def test_조회가_장부를_바꾸지_않는다(self):
        partners.attach("solo", "direct", contracted_at=AUG10, now=AUG10)
        st.record_usage("solo", ts=AUG10 + DAY, calls=7)
        first = self.srv.get("?op=report&month=2026-08")[1]["by_partner"]
        second = self.srv.get("?op=report&month=2026-08")[1]["by_partner"]
        self.assertEqual(first, second)


class ConsoleShowsBoth(Base):
    """화면이 두 숫자를 **구분해서** 그린다(한 숫자를 둘로 쓰지 않는다)."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(ROOT, "public", "admin.html"),
                  encoding="utf-8") as fh:
            self.html = fh.read()

    def test_머리글과_칸이_짝을_이룬다(self):
        self.assertIn("<th>고객사</th><th>줄</th>", self.html)
        self.assertIn("slNum(p.tenants)", self.html)
        self.assertIn("slNum(p.lines)", self.html)
        # 옛 머리글(줄 수를 고객사로 부르던 자리)이 남아 있지 않다
        self.assertNotIn("<th>고객사 줄</th>", self.html)

    def test_하드코딩_수치가_없다(self):
        """표는 서버 응답만 그린다 — 가짜 수치를 넣지 않았다."""
        start = self.html.index("d.by_partner.map")
        chunk = self.html[start:start + 700]
        self.assertNotIn("고객사 2", chunk)
        self.assertIn("p.tenants", chunk)


if __name__ == "__main__":
    unittest.main(verbosity=2)
