# -*- coding: utf-8 -*-
"""api/settlement.py 잔여 방어 분기 회귀 — 네트워크 미사용, 의존성 0.

97% 가 이미 덮여 있다. 남은 것은 **입력 모순·모듈 부재 때만 도는 방어 분기**다.
정산 리포트는 정산 분쟁의 근거이므로, 평시에 돌지 않는 방어 코드를 남겨두지 않는다.

검증 대상
  1) 요율표 검증 잔여 — rates 비객체·항목 과다(500 초과)·규칙 비객체·숫자 변환 불가 타입
  2) 요율 출처(_card_source) — runtime/env/none 세 갈래
  3) 요율 조회 — 빈 partner_id 에 대한 잘못된 매칭 키 스킵
  4) 감사 격리 — 감사 모듈 부재·장애가 요청을 죽이지 않는다
  5) HTTP 잔여 경로 — POST 가 외부 오리진이면 405 이전에 403 으로 먼저 막힌다

실행: python3 -m pytest tests/test_settlement_edge.py -q
"""
import io
import os
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import partners             # noqa: E402
import settlement as st     # noqa: E402
import _ratelimit           # noqa: E402

ENVS = ("PARTNER_RATE_CARD", "PARTNER_RBAC_LIVE", "CALLBOT_API_KEY",
        "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS")
ORIGIN = {"origin": "https://callbot-portal.vercel.app"}


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
        self._audit = st._audit
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        st._audit = self._audit
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


# ==========================================================================
# 1) 요율표 검증 잔여 분기
# ==========================================================================
class TestRateCardValidationEdge(Base):
    def test_as_decimal_rejects_unsupported_type(self):
        for bad in ([1, 2], {"a": 1}, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                st._as_decimal(bad)

    def test_commission_pct_unsupported_type_reported_not_swallowed(self):
        card, probs = st.validate_rate_card(
            {"version": "v1", "rates": {"acme/voice": {"commission_pct": [1, 2]}}})
        self.assertEqual(card["rates"], {})
        self.assertTrue(any("acme/voice" in p for p in probs), probs)

    def test_rates_not_a_dict_is_reported_and_discarded(self):
        card, probs = st.validate_rate_card({"version": "v1", "rates": "요율표 아님"})
        self.assertEqual(card["rates"], {})
        self.assertTrue(any("rates" in p and "객체" in p for p in probs), probs)

    def test_too_many_rate_entries_rejected(self):
        rates = {"p%d/voice" % i: {"unit_fee_krw": 10} for i in range(501)}
        card, probs = st.validate_rate_card({"version": "v1", "rates": rates})
        self.assertEqual(card["rates"], {})
        self.assertTrue(any("500" in p for p in probs), probs)

    def test_rule_not_a_dict_is_reported_and_key_skipped(self):
        card, probs = st.validate_rate_card(
            {"version": "v1", "rates": {"acme/voice": "규칙 아님",
                                        "acme/*": {"unit_fee_krw": 5}}})
        self.assertNotIn("acme/voice", card["rates"])
        self.assertIn("acme/*", card["rates"])
        self.assertTrue(any("acme/voice" in p and "객체" in p for p in probs), probs)


# ==========================================================================
# 1-b) 날짜 라벨 — day_index ↔ day_label 이 서로 되돌린다
# ==========================================================================
class TestDayLabel(Base):
    def test_day_label_round_trips_with_day_index(self):
        # 2026-08-10 12:00 KST
        import calendar
        ts = float(calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0)))
        idx = st.day_index(ts)
        self.assertEqual(st.day_label(idx), "2026-08-10")


# ==========================================================================
# 2) 요율 출처 — runtime / env / none
# ==========================================================================
class TestCardSource(Base):
    def test_runtime_when_override_set(self):
        st.set_rate_card({"version": "v1", "rates": {}})
        self.assertEqual(st._card_source(), "runtime")

    def test_none_when_nothing_set(self):
        st.set_rate_card(None)
        os.environ.pop("PARTNER_RATE_CARD", None)
        self.assertEqual(st._card_source(), "none")

    def test_env_when_only_env_set(self):
        st.set_rate_card(None)
        os.environ["PARTNER_RATE_CARD"] = json.dumps({"version": "v-env", "rates": {}})
        self.assertEqual(st._card_source(), "env")


# ==========================================================================
# 3) 요율 조회 — 빈 partner_id 의 오매칭 키 스킵
# ==========================================================================
class TestLookupRateEdge(Base):
    def test_empty_partner_id_skips_malformed_keys_and_uses_wildcard(self):
        card = {"rates": {"*/voice": {"unit_fee_krw": 100}}}
        rule, key = st.lookup_rate(card, "", "voice")
        self.assertEqual(key, "*/voice")
        self.assertEqual(rule["unit_fee_krw"], 100)

    def test_empty_partner_id_with_no_wildcard_finds_nothing(self):
        card = {"rates": {"acme/voice": {"unit_fee_krw": 100}}}
        rule, key = st.lookup_rate(card, "", "voice")
        self.assertIsNone(rule)
        self.assertEqual(key, "")


# ==========================================================================
# 4) 감사 격리
# ==========================================================================
class _BrokenAudit(object):
    def record_request(self, *a, **k):
        raise RuntimeError("감사 저장소 장애 /var/secret/audit.log")


class TestAuditIsolation(Base):
    def test_no_audit_module_still_serves(self):
        st._audit = None
        r = call("GET")
        self.assertEqual(r.status, 200)

    def test_audit_failure_does_not_break_the_request(self):
        st._audit = _BrokenAudit()
        r = call("GET")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])

    def test_audit_failure_message_does_not_leak_into_response(self):
        st._audit = _BrokenAudit()
        r = call("GET")
        self.assertNotIn("audit.log", r.wfile.data.decode("utf-8"))


# ==========================================================================
# 5) HTTP 잔여 경로
# ==========================================================================
class TestHttpRemaining(Base):
    def test_post_from_foreign_origin_denied_before_405(self):
        r = call("POST", body={"x": 1}, headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"),
                            "https://evil.example")

    def test_post_from_same_origin_gets_405_readonly(self):
        r = call("POST", body={"x": 1})
        self.assertEqual(r.status, 405)


if __name__ == "__main__":
    unittest.main(verbosity=2)
