# -*- coding: utf-8 -*-
"""api/partners.py 잔여 방어 분기 회귀 — 네트워크 미사용, 의존성 0.

이미 83% 가 덮여 있다. 남은 것은 **장애·모순 입력 때만 도는 방어 분기**로,
평시에 실행되지 않아 회귀가 없으면 "있는 줄만 알았지 실제로는 터지는" 상태가
된다. 정산 분쟁의 근거가 되는 장부라서, 방어가 실제로 도는지 고정한다.

검증 대상
  1) 장부 입력 경계  — 이름 60자 상한, 미등록 파트너로 담당 변경, 종료 시점 역행
  2) 기간 조회 경계  — 조회 구간 역전 거부, 구간 밖 기간은 건너뛴다
  3) 불변식 검사     — 겹침·빈틈·역행 기간을 **실제로 잡아낸다**(변이 검증)
  4) 감사 격리       — 감사 모듈이 없거나 터져도 요청이 죽지 않는다
  5) 주체 식별 격리  — actor 산출 실패가 요청을 죽이지 않는다(익명으로 진행)
  6) HTTP 잔여 경로  — POST 오리진 차단, 잘못된 tenant 형식 400, 계약일 미래 400
  7) 오류→입력 지목  — `_field_for` 매핑표(사용자가 고칠 곳을 지목한다)

실행: python3 -m pytest tests/test_partners_edge.py -q
"""
import io
import os
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import partners           # noqa: E402
import _ratelimit         # noqa: E402

ENVS = ("PARTNER_RBAC_LIVE", "CALLBOT_API_KEY", "CALLBOT_STRICT",
        "CALLBOT_DEBUG_ERRORS")
ORIGIN = {"origin": "https://callbot-portal.vercel.app"}
DAY = 86400.0


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
    path = "/api/partners" + ("?" + query if query else "")
    inst = partners.handler.__new__(partners.handler)
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
    (inst.do_GET if method == "GET" else inst.do_POST)()
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)
        _ratelimit.reset()
        partners._clear_for_tests()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        self._audit = partners._audit
        self.t0 = 1_700_000_000.0

    def tearDown(self):
        partners._audit = self._audit
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        partners._clear_for_tests()

    def _boom(self, *a, **k):
        raise NetworkTouched("파트너 대장이 네트워크를 건드렸다")

    def mk(self, pid="ch-alpha", name="테스트 파트너"):
        return partners.create_partner(pid, name, now=self.t0)

    def acct(self, tid="acme", channel="partner_referral", pid="ch-alpha", **kw):
        kw.setdefault("now", self.t0)
        return partners.attach(tid, channel, partner_id=pid, **kw)


# ==========================================================================
# 1) 장부 입력 경계
# ==========================================================================
class TestLedgerInputLimits(Base):
    def test_partner_name_over_60_chars_rejected(self):
        with self.assertRaises(ValueError) as cm:
            partners.create_partner("ch-alpha", "가" * 61, now=self.t0)
        self.assertIn("60자", str(cm.exception))
        self.assertEqual(partners.list_partners(), [])   # 거부된 시도는 남지 않는다

    def test_partner_name_exactly_60_chars_allowed(self):
        p = partners.create_partner("ch-alpha", "가" * 60, now=self.t0)
        self.assertEqual(len(p["name"]), 60)

    def test_reassign_to_unregistered_partner_raises_keyerror(self):
        self.mk()
        self.acct()
        with self.assertRaises(KeyError):
            partners.reassign("acme", "partner_managed", partner_id="ch-ghost",
                              now=self.t0 + DAY)
        # 장부는 변하지 않는다 — 실패한 변경이 기간을 열지 않는다
        acc = partners._ACCOUNTS["acme"]
        self.assertEqual(len(acc["periods"]), 1)
        self.assertIsNone(acc["periods"][0]["to_ts"])

    def test_detach_before_attachment_start_rejected(self):
        self.mk()
        self.acct()
        with self.assertRaises(ValueError) as cm:
            partners.detach("acme", now=self.t0 - DAY)
        self.assertIn("시작일", str(cm.exception))
        self.assertIsNone(partners._ACCOUNTS["acme"]["periods"][0]["to_ts"])

    def test_detach_at_exact_start_is_allowed(self):
        self.mk()
        self.acct()
        partners.detach("acme", now=self.t0)
        self.assertEqual(partners._ACCOUNTS["acme"]["periods"][0]["to_ts"], self.t0)


# ==========================================================================
# 2) 기간 조회 경계
# ==========================================================================
class TestPeriodQueryBounds(Base):
    def setUp(self):
        Base.setUp(self)
        self.mk()
        self.mk("ch-beta", "베타채널")
        self.acct()                                             # [t0, t0+10d)
        partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                          now=self.t0 + 10 * DAY)               # [t0+10d, ...)

    def test_reversed_window_rejected(self):
        with self.assertRaises(ValueError) as cm:
            partners.attribution_periods("acme", frm=self.t0 + DAY, to=self.t0)
        self.assertIn("빠릅니다", str(cm.exception))

    def test_window_after_a_closed_period_skips_it(self):
        out = partners.attribution_periods("acme", frm=self.t0 + 10 * DAY)
        self.assertEqual([p["partner_id"] for p in out], ["ch-beta"])

    def test_window_before_a_later_period_skips_it(self):
        out = partners.attribution_periods("acme", to=self.t0 + 10 * DAY)
        self.assertEqual([p["partner_id"] for p in out], ["ch-alpha"])

    def test_window_spanning_the_handover_returns_both(self):
        out = partners.attribution_periods("acme", frm=self.t0 + 9 * DAY,
                                           to=self.t0 + 11 * DAY)
        self.assertEqual([p["partner_id"] for p in out], ["ch-alpha", "ch-beta"])

    def test_equal_bounds_is_not_reversed(self):
        self.assertEqual(partners.attribution_periods("acme", frm=self.t0,
                                                      to=self.t0), [])

    def test_query_returns_copies_not_the_ledger(self):
        out = partners.attribution_periods("acme")
        out[0]["partner_id"] = "ch-tampered"
        self.assertEqual(partners._ACCOUNTS["acme"]["periods"][0]["partner_id"],
                         "ch-alpha")


# ==========================================================================
# 3) 불변식 검사 — 깨진 장부를 실제로 잡아내는지(변이 검증)
# ==========================================================================
class TestInvariantDetection(Base):
    def setUp(self):
        Base.setUp(self)
        self.mk()
        self.acct()

    def periods(self):
        return partners._ACCOUNTS["acme"]["periods"]

    def test_clean_ledger_has_no_problems(self):
        self.assertEqual(partners.check_invariants(), [])

    def test_backwards_period_is_reported(self):
        self.periods()[0]["to_ts"] = self.t0 - DAY          # 종료가 시작보다 빠름
        probs = partners.check_invariants()
        self.assertTrue(any("종료가 시작보다 빠름" in p for p in probs), probs)
        self.assertTrue(all(p.startswith("acme:") for p in probs), probs)

    def test_gap_between_periods_is_reported(self):
        ps = self.periods()
        ps[0]["to_ts"] = self.t0 + DAY
        ps.append(dict(ps[0], from_ts=self.t0 + 5 * DAY, to_ts=None))   # 4일 빈틈
        probs = partners.check_invariants()
        self.assertTrue(any("빈틈/겹침" in p for p in probs), probs)

    def test_overlapping_periods_are_reported(self):
        ps = self.periods()
        ps[0]["to_ts"] = self.t0 + 5 * DAY
        ps.append(dict(ps[0], from_ts=self.t0 + DAY, to_ts=None))       # 겹침
        probs = partners.check_invariants()
        self.assertTrue(any("빈틈/겹침" in p for p in probs), probs)

    def test_open_period_followed_by_another_is_reported(self):
        ps = self.periods()
        ps.append(dict(ps[0], from_ts=self.t0 + DAY, to_ts=None))
        probs = partners.check_invariants()
        self.assertTrue(any("닫히지 않은 기간" in p for p in probs), probs)

    def test_summary_surfaces_integrity_problems(self):
        self.periods()[0]["to_ts"] = self.t0 - DAY
        self.assertTrue(partners.summary()["integrity"])


# ==========================================================================
# 4~5) 감사·주체 식별 격리 — 부가 기능 장애가 요청을 죽이지 않는다
# ==========================================================================
class _BrokenAudit(object):
    def record_request(self, *a, **k):
        raise RuntimeError("감사 저장소 장애 /var/secret/audit.log")

    def actor(self, *a, **k):
        raise RuntimeError("헤더 저장소 장애")


class _CountingAudit(object):
    def __init__(self):
        self.calls = []

    def record_request(self, headers, path, method, result, status, request_id=None):
        self.calls.append((method, result, status))

    def actor(self, headers):
        return {"type": "origin", "id": "portal"}


class TestAuditIsolation(Base):
    def test_no_audit_module_still_serves(self):
        partners._audit = None
        r = call("GET")
        self.assertEqual(r.status, 200)
        self.assertIsNone(partners.handler._actor(self._inst()))

    def _inst(self):
        inst = partners.handler.__new__(partners.handler)
        inst.headers = FakeHeaders(dict(ORIGIN))
        return inst

    def test_audit_failure_does_not_break_the_request(self):
        partners._audit = _BrokenAudit()
        r = call("GET")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])

    def test_audit_failure_message_does_not_leak_into_response(self):
        partners._audit = _BrokenAudit()
        r = call("POST", body={"op": "create", "partner_id": "ch-alpha",
                               "name": "테스트 파트너"})
        self.assertEqual(r.status, 200)
        self.assertNotIn("audit.log", json.dumps(r.body(), ensure_ascii=False))

    def test_actor_failure_falls_back_to_anonymous(self):
        partners._audit = _BrokenAudit()
        self.assertIsNone(partners.handler._actor(self._inst()))
        r = call("POST", body={"op": "create", "partner_id": "ch-alpha",
                               "name": "테스트 파트너"})
        self.assertEqual(r.status, 200)          # 익명으로라도 진행된다

    def test_actor_is_a_fingerprint_not_a_key(self):
        partners._audit = _CountingAudit()
        self.assertEqual(partners.handler._actor(self._inst()), "origin:portal")

    def test_audit_records_the_real_outcome(self):
        fake = _CountingAudit()
        partners._audit = fake
        call("GET", query="op=drop")             # 400
        call("GET")                              # 200
        self.assertIn(("GET", "error", 400), fake.calls)
        self.assertIn(("GET", "allow", 200), fake.calls)


# ==========================================================================
# 6) HTTP 잔여 경로
# ==========================================================================
class TestHttpRemaining(Base):
    def test_post_from_foreign_origin_denied_before_any_write(self):
        r = call("POST", body={"op": "create", "partner_id": "ch-alpha",
                               "name": "테스트 파트너"},
                 headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertFalse(r.body()["ok"])
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"),
                            "https://evil.example")
        self.assertEqual(partners.list_partners(), [])    # 장부 무변화

    def test_attribution_with_malformed_tenant_400(self):
        r = call("GET", query="op=attribution&tenant=ACME!!")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "tenant")

    def test_attach_with_future_contract_date_400_on_contracted_at(self):
        call("POST", body={"op": "create", "partner_id": "ch-alpha",
                           "name": "테스트 파트너"})
        r = call("POST", body={"op": "attach", "tenant_id": "acme",
                               "channel": "partner_referral",
                               "partner_id": "ch-alpha",
                               "contracted_at": 4_000_000_000})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "contracted_at")
        self.assertEqual(partners.list_accounts(), [])

    def test_attach_with_past_contract_date_is_recorded(self):
        call("POST", body={"op": "create", "partner_id": "ch-alpha",
                           "name": "테스트 파트너"})
        r = call("POST", body={"op": "attach", "tenant_id": "acme",
                               "channel": "partner_referral",
                               "partner_id": "ch-alpha",
                               "contracted_at": int(self.t0)})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["account"]["contracted_at"].startswith("2023-11"))

    def test_attach_with_non_integer_contract_date_400(self):
        call("POST", body={"op": "create", "partner_id": "ch-alpha",
                           "name": "테스트 파트너"})
        r = call("POST", body={"op": "attach", "tenant_id": "acme",
                               "channel": "partner_referral",
                               "partner_id": "ch-alpha",
                               "contracted_at": "어제"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "contracted_at")

    def test_reassign_to_unknown_partner_over_http_points_at_partner_id(self):
        call("POST", body={"op": "create", "partner_id": "ch-alpha",
                           "name": "테스트 파트너"})
        call("POST", body={"op": "attach", "tenant_id": "acme",
                           "channel": "partner_referral", "partner_id": "ch-alpha"})
        r = call("POST", body={"op": "reassign", "tenant_id": "acme",
                               "channel": "partner_managed",
                               "partner_id": "ch-ghost"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "partner_id")


# ==========================================================================
# 7) 오류 문구 → 어느 입력이 틀렸는지(매핑표 고정)
# ==========================================================================
class TestFieldMapping(Base):
    def test_contact_maps_to_owner(self):
        self.assertEqual(partners._field_for("attach", "연락처는 받지 않습니다"),
                         "owner")

    def test_channel_words_map_to_channel_on_attribution_ops(self):
        for op in ("attach", "reassign"):
            self.assertEqual(partners._field_for(op, "'직접 계약' 경로에는..."),
                             "channel")
            self.assertEqual(partners._field_for(op, "허용값: direct"), "channel")

    def test_channel_words_map_to_status_elsewhere(self):
        self.assertEqual(partners._field_for("detach", "허용값: active"), "status")

    def test_contract_date_maps_to_contracted_at(self):
        self.assertEqual(partners._field_for("attach", "계약일이 미래입니다"),
                         "contracted_at")

    def test_name_maps_to_name(self):
        self.assertEqual(partners._field_for("create", "파트너 이름은 필수입니다"),
                         "name")
        self.assertEqual(partners._field_for("create", "파트너 이름은 60자 이내"),
                         "name")

    def test_partner_words_map_to_partner_id(self):
        self.assertEqual(partners._field_for("attach", "파트너 지정이 필요합니다"),
                         "partner_id")
        for op in ("create", "suspend", "resume"):
            self.assertEqual(partners._field_for(op, "무슨 말인지 모를 오류"),
                             "partner_id")

    def test_unclassified_attribution_errors_fall_back_to_tenant(self):
        self.assertEqual(partners._field_for("attach", "이미 등록된 고객사입니다"),
                         "tenant_id")
        self.assertEqual(partners._field_for("detach", "이미 종료된 고객사입니다"),
                         "tenant_id")

    def test_every_mapped_field_is_a_real_input(self):
        fields = {"owner", "channel", "status", "contracted_at", "name",
                  "partner_id", "tenant_id"}
        for op in partners.OPS:
            for msg in ("연락처", "경로", "허용값", "계약일", "이름", "파트너", "기타"):
                self.assertIn(partners._field_for(op, msg), fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)
