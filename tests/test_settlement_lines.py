# -*- coding: utf-8 -*-
"""정산 리포트 · 테넌트 과금 라인(`api/_settle_lines.py`, `/api/settlement?op=lines`) 회귀.

돈 화면이라 기준이 하나 더 있다 — **실측이 아닌 수치는 반드시 표식을 달고 나간다.**
  1) SETTLEMENT_SOURCE 미설정 = 데모: 응답 머리·모든 줄·CSV 비고에 「데모 데이터」 표식
  2) ledger 출처는 이 인스턴스의 관측치만 — 표본이 없으면 빈 표(데모 수치로 채우지 않는다)
  3) 금액을 모르면 0 이 아니라 null / CSV 빈 칸
  4) 접근 가드: 외부 오리진 403 · STRICT 모드 API 키 없으면 401 · POST 405
  5) CSV 이스케이프: 수식 주입 무력화 + 구분자·따옴표·개행 감싸기 (서버 규칙 = 화면 규칙)
  6) 입력 검증: month·tenant·format 은 `details[].field` 로 지목
  7) 콘솔 「정산 리포트(테넌트)」 배선·배지·CSV 조립 규칙
실행: python3 -m pytest tests/test_settlement_lines.py -q
"""
import calendar
import io
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import partners            # noqa: E402
import settlement as st    # noqa: E402
import _settle_lines as sl  # noqa: E402
import _ratelimit          # noqa: E402

ENVS = ("SETTLEMENT_SOURCE", "CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS", "PARTNER_RATE_CARD")
ORIGIN = {"origin": "https://callbot-portal.vercel.app"}
AUG10 = float(calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0)))


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class Resp(object):
    def __init__(self):
        self.status, self.sent, self.data = None, [], b""

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.data.decode("utf-8"))

    def text(self):
        return self.data.decode("utf-8")


def call(method="GET", query="", headers=None):
    r = Resp()
    inst = st.handler.__new__(st.handler)
    inst.headers = FakeHeaders(headers if headers is not None else dict(ORIGIN))
    inst.path = "/api/settlement" + ("?" + query if query else "")
    inst.rfile = io.BytesIO(b"")

    class W(object):
        def write(self, b):
            r.data += b
    inst.wfile = W()
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

        def boom(*a, **k):
            raise AssertionError("network touched")
        urllib.request.urlopen = boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        st._clear_for_tests()
        partners._clear_for_tests()


# --------------------------------------------------------------------------
# 순수 로직
# --------------------------------------------------------------------------
class Pure(Base):
    def test_source_switch(self):
        self.assertEqual(sl.source({}), ("demo", ""))
        self.assertEqual(sl.source({"SETTLEMENT_SOURCE": " Ledger "}), ("ledger", ""))
        src, note = sl.source({"SETTLEMENT_SOURCE": "postgres"})
        self.assertEqual(src, "demo")
        self.assertIn("알 수 없어", note)

    def test_demo_rows_are_labelled_everywhere(self):
        out = sl.build("2026-08", {})
        self.assertTrue(out["demo"])
        self.assertEqual(out["source"], "demo")
        self.assertTrue(out["lines"])
        for l in out["lines"]:
            self.assertTrue(l["demo"], l)
            self.assertEqual(l["status"], "demo")
            self.assertEqual(l["note"], "데모 데이터")
            self.assertEqual(l["period"], "2026-08")
            self.assertIn(l["unit"], sl.UNITS)
        self.assertEqual(out["status"], "draft")
        self.assertIn("데모 데이터", out["note"])

    def test_demo_tenant_filter(self):
        out = sl.build("2026-08", {}, tenant_id="demo-tenant-b")
        self.assertEqual({l["tenant_id"] for l in out["lines"]}, {"demo-tenant-b"})
        self.assertEqual(sl.build("2026-08", {}, tenant_id="nobody")["lines"], [])

    def test_units_match_core_billable_unit(self):
        self.assertEqual(set(sl.UNITS), {"voice_seconds", "voice_units", "sessions", "llm_prompt_tokens",
                                         "llm_completion_tokens", "stt_seconds", "tts_seconds"})

    def test_ledger_empty_is_empty_not_demo(self):
        out = sl.build("2026-08", {"SETTLEMENT_SOURCE": "ledger"}, buckets=[])
        self.assertFalse(out["demo"])
        self.assertEqual(out["lines"], [])
        self.assertEqual(out["totals"], {"lines": 0, "tenants": 0, "count": 0, "amount_krw": None,
                                         "amount_complete": False})

    def test_ledger_lines_amount_only_when_known(self):
        buckets = [{"tenant_id": "acme", "calls": 3, "minutes": 2.2, "revenue_krw": 1000},
                   {"tenant_id": "acme", "calls": 1, "minutes": 0.5, "revenue_krw": None},
                   {"tenant_id": "zed", "calls": 2, "minutes": 1.0, "revenue_krw": None},
                   {"tenant_id": "", "calls": 9, "minutes": 9.0, "revenue_krw": 9}]
        lines = sl.ledger_lines("2026-08", buckets)
        by = {(l["tenant_id"], l["unit"]): l for l in lines}
        self.assertEqual(set(by), {("acme", "voice_units"), ("acme", "sessions"),
                                   ("zed", "voice_units"), ("zed", "sessions")})
        self.assertEqual((by[("acme", "voice_units")]["count"], by[("acme", "voice_units")]["amount_krw"],
                          by[("acme", "voice_units")]["status"]), (3, 1000, "ok"))
        self.assertEqual((by[("acme", "sessions")]["count"], by[("acme", "sessions")]["amount_krw"]), (4, None))
        self.assertEqual(by[("zed", "voice_units")]["status"], "amount_missing")
        self.assertFalse(any(l["demo"] for l in lines))
        t = sl.totals(lines)
        self.assertEqual((t["amount_krw"], t["amount_complete"], t["tenants"]), (1000, False, 2))

    def test_ledger_tenant_filter(self):
        buckets = [{"tenant_id": "acme", "calls": 1, "minutes": 1.0}, {"tenant_id": "zed", "calls": 1, "minutes": 1.0}]
        self.assertEqual({l["tenant_id"] for l in sl.ledger_lines("2026-08", buckets, "zed")}, {"zed"})


class Csv(Base):
    def test_formula_injection_neutralised(self):
        for bad in ("=cmd|' /C calc'!A0", "+1+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertTrue(sl.csv_cell(bad).startswith("'") or sl.csv_cell(bad).startswith('"\''), bad)
        self.assertEqual(sl.csv_cell("=1"), "'=1")

    def test_quoting(self):
        self.assertEqual(sl.csv_cell('a,b'), '"a,b"')
        self.assertEqual(sl.csv_cell('say "hi"'), '"say ""hi"""')
        self.assertEqual(sl.csv_cell("l1\nl2"), '"l1\nl2"')
        self.assertEqual(sl.csv_cell("=a,b"), "\"'=a,b\"", "수식 방어와 감싸기를 함께")
        self.assertEqual(sl.csv_cell(None), "")
        self.assertEqual(sl.csv_cell("x\x00y"), "xy")

    def test_to_csv_blank_for_unknown_amount_and_demo_note(self):
        out = sl.build("2026-08", {})
        text = sl.to_csv(out)
        rows = text.split("\r\n")
        self.assertEqual(rows[0], ",".join(sl.CSV_HEADER))
        self.assertEqual(len(rows), len(out["lines"]) + 2, "CRLF 종료")
        for r in rows[1:-1]:
            self.assertTrue(r.endswith(",demo,데모 데이터"), r)
        sess = [r for r in rows[1:-1] if ",세션 수," in r][0]
        self.assertIn(",,demo,", sess, "모르는 금액은 0 이 아니라 빈 칸")
        self.assertEqual(sl.csv_filename(out), "settlement_lines_2026-08_demo.csv")

    def test_to_csv_escapes_tenant(self):
        out = sl.build("2026-08", {"SETTLEMENT_SOURCE": "ledger"},
                       buckets=[{"tenant_id": "=evil,\"x", "calls": 1, "minutes": 1.0}])
        text = sl.to_csv(out)
        self.assertIn("\"'=evil,\"\"x\"", text)


# --------------------------------------------------------------------------
# HTTP 계약 · 접근 가드
# --------------------------------------------------------------------------
class Http(Base):
    def test_demo_mode_default(self):
        r = call("GET", "op=lines&month=2026-08")
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertTrue(b["ok"] and b["demo"])
        self.assertEqual(b["source"], "demo")
        self.assertEqual(b["month"], "2026-08")
        self.assertTrue(all(l["demo"] for l in b["lines"]))
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertEqual(b["columns"], ["period", "tenant_id", "count", "unit", "amount_krw", "status", "note"])

    def test_ledger_mode_uses_recorded_usage_only(self):
        os.environ["SETTLEMENT_SOURCE"] = "ledger"
        r = call("GET", "op=lines&month=2026-08")
        self.assertEqual(r.status, 200)
        self.assertEqual((r.body()["demo"], r.body()["lines"]), (False, []), "표본 없으면 빈 표")
        st.record_usage("acme", ts=AUG10, calls=5, minutes=12.4, revenue_krw=30000)
        b = call("GET", "op=lines&month=2026-08").body()
        self.assertEqual([(l["tenant_id"], l["unit"], l["count"], l["amount_krw"]) for l in b["lines"]],
                         [("acme", "voice_units", 13, 30000), ("acme", "sessions", 5, None)])
        self.assertEqual(call("GET", "op=lines&month=2026-09").body()["lines"], [], "다른 달에는 보이지 않는다")
        self.assertNotIn("demo-tenant", r.text())

    def test_unknown_source_falls_to_demo_with_note(self):
        os.environ["SETTLEMENT_SOURCE"] = "bigquery"
        b = call("GET", "op=lines&month=2026-08").body()
        self.assertTrue(b["demo"])
        self.assertIn("알 수 없어", b["source_note"])

    def test_csv_download(self):
        r = call("GET", "op=lines&month=2026-08&format=csv")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("Content-Type").startswith("text/csv"))
        self.assertIn('filename="settlement_lines_2026-08_demo.csv"', r.header("Content-Disposition"))
        self.assertTrue(r.text().startswith("﻿기간,테넌트,건수,과금 단위,금액(원),상태,비고"), "엑셀용 BOM")
        self.assertIn("데모 데이터", r.text())

    def test_validation_fields(self):
        r = call("GET", "op=lines&month=2026-99")
        self.assertEqual((r.status, r.body()["details"][0]["field"]), (400, "month"))
        r = call("GET", "op=lines&month=2026-08&tenant=Bad%20Tenant!")
        self.assertEqual((r.status, r.body()["details"][0]["field"]), (400, "tenant"))
        r = call("GET", "op=lines&month=2026-08&format=xlsx")
        self.assertEqual(r.status, 400)
        self.assertIn("format", json.dumps(r.body()["details"]))

    def test_tenant_filter_http(self):
        b = call("GET", "op=lines&month=2026-08&tenant=DEMO-TENANT-A").body()
        self.assertEqual({l["tenant_id"] for l in b["lines"]}, {"demo-tenant-a"})
        self.assertEqual(b["tenant_id"], "demo-tenant-a")

    def test_foreign_origin_denied(self):
        r = call("GET", "op=lines", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertNotIn("demo-tenant", r.text())

    def test_strict_requires_api_key(self):
        os.environ["CALLBOT_STRICT"] = "1"
        os.environ["CALLBOT_API_KEY"] = "k-test"
        r = call("GET", "op=lines", headers={})
        self.assertEqual(r.status, 401)
        self.assertNotIn("demo-tenant", r.text())
        r = call("GET", "op=lines&month=2026-08", headers={"x-api-key": "k-test"})
        self.assertEqual(r.status, 200)
        self.assertNotIn("k-test", r.text())

    def test_post_is_405(self):
        r = call("POST", "op=lines")
        self.assertEqual(r.status, 405)

    def test_no_network(self):
        call("GET", "op=lines&month=2026-08&format=csv")   # urlopen 이 불리면 AssertionError


# --------------------------------------------------------------------------
# 콘솔 화면
# --------------------------------------------------------------------------
class Console(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = io.open(os.path.join(ROOT, "public", "admin.html"), encoding="utf-8").read()
        i = cls.h.find("/* ---- 정산 리포트(테넌트 과금 라인)")
        assert i > 0, "테넌트 정산 화면 스크립트가 없다"
        cls.js = cls.h[i:cls.h.find("/* ---- 정산 리포트(파트너)", i)]
        assert len(cls.js) > 100
        i = cls.h.find('<section id="view-settlelines"')
        assert i > 0
        cls.sec = cls.h[i:cls.h.find("</section>", i)]

    def test_wiring(self):
        self.assertIn('data-v="settlelines"', self.h)
        self.assertIn("settlelines:['정산 리포트(테넌트)'", self.h)
        self.assertIn("['settlelines','정산 리포트(테넌트)']", self.h)
        self.assertIn("'/api/settlement?op=lines", self.js)
        for fid in ("scMonth", "scTenant"):
            self.assertIn('for="%s"' % fid, self.sec, "라벨 연결: " + fid)
        self.assertIn('role="alert"', self.sec)

    def test_demo_badge_and_states(self):
        self.assertIn("데모 데이터", self.js, "데모 배지")
        self.assertIn("d.demo", self.js)
        self.assertIn('role="status"', self.js, "로딩 표시")
        self.assertIn("아직 집계된 과금 라인이 없습니다", self.js, "빈 상태 + 다음 행동")
        self.assertIn("scFieldErr(", self.js, "인라인 검증")
        self.assertIn("scBtns(true)", self.js, "중복 클릭 방지")
        self.assertIn("요청을 처리하지 못했습니다", self.js, "네트워크 오류 안내")

    def test_reads_only_server_keys(self):
        import re
        sample = sl.build("2026-08", {})
        keys = set(sample) | set(sample["lines"][0]) | set(sample["totals"])
        for k in set(re.findall(r"\bl\.([a-z_]+)", self.js)):
            self.assertIn(k, keys, "화면이 읽는 줄 필드가 서버 응답에 없다: " + k)

    def test_client_csv_rules_match_server(self):
        self.assertIn("function scCsvCell(", self.js)
        self.assertIn("/^[=+\\-@\\t\\r]/", self.js, "수식 주식 방어 — 서버 csv_cell 과 같은 문자 집합")
        self.assertIn('\\uFEFF', self.js, "엑셀용 BOM")
        self.assertIn("'데모 데이터'", self.js, "CSV 에도 표식")
        self.assertIn("amount_krw==null?''", self.js.replace(" ", ""), "모르는 금액은 빈 칸")

    def test_no_hardcoded_amounts_in_console(self):
        import re
        self.assertFalse(re.search(r"\d{1,3}(,\d{3}){2,}", self.js), "화면이 금액을 스스로 만들지 않는다")
        self.assertNotIn("demo-tenant", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
