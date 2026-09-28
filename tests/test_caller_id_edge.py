# -*- coding: utf-8 -*-
"""api/caller_id.py 잔여 방어 분기 회귀 — 네트워크 미사용, 의존성 0.

88% 가 이미 덮여 있다. 남은 것은 **장애·모순 입력 때만 도는 방어 분기**다.
발신번호 대장은 실발신 승인 근거가 되므로, 방어가 실제로 도는지 고정한다.

검증 대상
  1) 유효기간 입력  — 정수가 아니면 승인 자체가 거부된다(잘못된 만료일 방지)
  2) 스코프 격리    — partners 모듈이 없거나 터져도 목록 조회가 죽지 않고
                      **아무것도 조용히 사라지지 않는다**
  3) 감사 격리      — 감사 모듈 부재·장애가 요청을 죽이지 않고 문구도 새지 않는다
  4) 주체 식별 격리 — actor 산출 실패 시 익명으로 진행(요청은 산다)
  5) HTTP 잔여 경로 — 잘못된 tenant 형식 400, POST 오리진 차단(무변화)

실행: python3 -m pytest tests/test_caller_id_edge.py -q
"""
import io
import os
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import caller_id          # noqa: E402
import partners           # noqa: E402
import _ratelimit         # noqa: E402

ENVS = ("CPAAS_LIVE", "PARTNER_RBAC_LIVE", "PII_MASTER_KEY",
        "CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS")
ORIGIN = {"origin": "https://callbot-portal.vercel.app"}
EVID = "통신서비스 이용증명원"


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
    path = "/api/caller_id" + ("?" + query if query else "")
    inst = caller_id.handler.__new__(caller_id.handler)
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
        caller_id._clear_for_tests()
        partners._clear_for_tests()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        self._audit = caller_id._audit
        self._partners_mod = sys.modules.get("partners")
        self.now = 1_700_000_000.0

    def tearDown(self):
        if self._partners_mod is None:
            sys.modules.pop("partners", None)
        else:
            sys.modules["partners"] = self._partners_mod
        caller_id._audit = self._audit
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        caller_id._clear_for_tests()
        partners._clear_for_tests()

    def _boom(self, *a, **k):
        raise NetworkTouched("발신번호 대장이 네트워크를 건드렸다")

    def reg(self, tid="acme", num="010-1234-5678"):
        return caller_id.register(tid, num, "대표 상담", now=self.now)["id"]

    def inst(self):
        h = caller_id.handler.__new__(caller_id.handler)
        h.headers = FakeHeaders(dict(ORIGIN))
        return h


# ==========================================================================
# 1) 유효기간 입력 — 잘못된 값으로 만료일이 만들어지지 않는다
# ==========================================================================
class TestValidDaysInput(Base):
    def test_non_integer_valid_days_rejected(self):
        cid = self.reg()
        for bad in ("일년", None, [365], {}, object()):
            with self.assertRaises(ValueError) as cm:
                caller_id.verify(cid, EVID, self.now - 86400, valid_days=bad,
                                 now=self.now)
            self.assertIn("정수", str(cm.exception), repr(bad))
            # 거부가 상태를 바꾸지 않으므로 다음 값도 같은 지점에서 걸린다
            self.assertEqual(caller_id._NUMBERS[cid]["status"], "pending")

    def test_rejected_approval_leaves_the_ledger_untouched(self):
        """회귀: 거부된 승인 신청이 대장을 바꾸면 안 된다.

        결함(2026-09-28 수정): `_apply` 가 `valid_days` 검증보다 먼저
        `status="verified"` 를 써서, 거부된 신청이 **승인 완료 + 만료 없음**
        (`expires_at=0` 이라 만료 계산도 걸리지 않음)으로 남았다. 즉
        `outbound_ready=True` — 실발신 승인 근거가 잘못된 입력으로 켜졌고,
        예외로 빠져나가 이력에도 남지 않았다.
        """
        cid = self.reg()
        before = dict(caller_id._NUMBERS[cid])
        hist_before = len(caller_id.history(limit=50))
        with self.assertRaises(ValueError):
            caller_id.verify(cid, EVID, self.now - 86400, valid_days="일년",
                             note="승인 메모", now=self.now)
        rec = caller_id._NUMBERS[cid]
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["expires_at"], 0.0)
        self.assertEqual(rec["verified_at"], 0.0)
        self.assertEqual(rec["note"], before["note"])          # 메모도 덮이지 않는다
        self.assertEqual(rec["evidence_type"], "")             # 증빙도 기록되지 않는다
        self.assertFalse(caller_id.view(rec, self.now)["outbound_ready"])
        self.assertEqual(len(caller_id.history(limit=50)), hist_before)

    def test_retry_after_rejection_still_works(self):
        cid = self.reg()
        with self.assertRaises(ValueError):
            caller_id.verify(cid, EVID, self.now - 86400, valid_days=0,
                             now=self.now)
        v = caller_id.verify(cid, EVID, self.now - 86400, valid_days=365,
                             now=self.now)
        self.assertEqual(v["status"], "verified")
        self.assertEqual(v["days_left"], 365)

    def test_out_of_range_rejection_does_not_approve_either(self):
        cid = self.reg()
        with self.assertRaises(ValueError):
            caller_id.verify(cid, EVID, self.now - 86400, valid_days=99999,
                             now=self.now)
        self.assertEqual(caller_id._NUMBERS[cid]["status"], "pending")

    def test_numeric_string_valid_days_is_accepted(self):
        cid = self.reg()
        v = caller_id.verify(cid, EVID, self.now - 86400, valid_days="30",
                             now=self.now)
        self.assertEqual(v["days_left"], 30)

    def test_out_of_range_valid_days_rejected(self):
        for bad in (0, -1, 1826):
            cid = self.reg("t%d" % abs(int(bad)))
            with self.assertRaises(ValueError) as cm:
                caller_id.verify(cid, EVID, self.now - 86400, valid_days=bad,
                                 now=self.now)
            self.assertIn("1~1825", str(cm.exception))

    def test_http_non_integer_valid_days_points_at_valid_days(self):
        self.reg()
        r = call("POST", body={"op": "verify", "id": "CID-0001",
                               "evidence_type": EVID,
                               "issued_at": int(self.now - 86400),
                               "valid_days": "일년"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "valid_days")


# ==========================================================================
# 2) 파트너 스코프 격리 — 없거나 터져도 목록이 사라지지 않는다
# ==========================================================================
class _BrokenPartners(object):
    def scope_tenants(self, *a, **k):
        raise RuntimeError("장부 저장소 장애")


class TestScopeIsolation(Base):
    def setUp(self):
        Base.setUp(self)
        self.reg("acme")
        self.reg("selfco", "010-9876-5432")

    def test_missing_partners_module_does_not_filter(self):
        sys.modules["partners"] = None          # import partners → ImportError
        self.assertIsNone(caller_id._scope_tenants(["acme"], "partner_admin",
                                                   "ch-alpha", self.now))
        rows = caller_id.list_numbers(role="partner_admin",
                                      actor_partner_id="ch-alpha", now=self.now)
        self.assertEqual({r["tenant_id"] for r in rows}, {"acme", "selfco"})

    def test_broken_partners_module_does_not_filter(self):
        sys.modules["partners"] = _BrokenPartners()
        self.assertIsNone(caller_id._scope_tenants(["acme"], "partner_admin",
                                                   "ch-alpha", self.now))
        rows = caller_id.list_numbers(role="partner_admin",
                                      actor_partner_id="ch-alpha", now=self.now)
        self.assertEqual(len(rows), 2)          # 조용히 사라지지 않는다

    def test_healthy_partners_module_returns_a_verdict(self):
        s = caller_id._scope_tenants(["acme"], "partner_admin", "ch-alpha", self.now)
        self.assertIsNotNone(s)
        self.assertFalse(s["applied"])          # 승인 전이므로 미적용
        self.assertIn("[승인 필요]", s["note"])

    def test_scope_failure_does_not_break_http_list(self):
        sys.modules["partners"] = _BrokenPartners()
        r = call("GET", query="op=list")
        self.assertEqual(r.status, 200)
        self.assertEqual(len(r.body()["numbers"]), 2)


# ==========================================================================
# 3~4) 감사·주체 식별 격리
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
        caller_id._audit = None
        self.assertIsNone(caller_id.handler._actor(self.inst()))
        r = call("GET")
        self.assertEqual(r.status, 200)

    def test_audit_failure_does_not_break_the_request(self):
        caller_id._audit = _BrokenAudit()
        r = call("GET", query="op=policy")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])

    def test_audit_failure_message_does_not_leak(self):
        caller_id._audit = _BrokenAudit()
        r = call("POST", body={"op": "register", "tenant_id": "acme",
                               "number": "010-1234-5678"})
        self.assertEqual(r.status, 200)
        self.assertNotIn("audit.log", json.dumps(r.body(), ensure_ascii=False))

    def test_actor_failure_falls_back_to_anonymous(self):
        caller_id._audit = _BrokenAudit()
        self.assertIsNone(caller_id.handler._actor(self.inst()))
        r = call("POST", body={"op": "register", "tenant_id": "acme",
                               "number": "010-1234-5678"})
        self.assertEqual(r.status, 200)

    def test_actor_is_a_fingerprint_not_a_key(self):
        caller_id._audit = _CountingAudit()
        self.assertEqual(caller_id.handler._actor(self.inst()), "origin:portal")

    def test_audit_records_the_real_outcome(self):
        fake = _CountingAudit()
        caller_id._audit = fake
        call("GET", query="op=nope")            # 400
        call("GET")                             # 200
        self.assertIn(("GET", "error", 400), fake.calls)
        self.assertIn(("GET", "allow", 200), fake.calls)


# ==========================================================================
# 5) HTTP 잔여 경로
# ==========================================================================
class TestHttpRemaining(Base):
    def test_malformed_tenant_query_400(self):
        r = call("GET", query="op=list&tenant=ACME!!")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "tenant")

    def test_well_formed_tenant_query_filters(self):
        self.reg("acme")
        self.reg("selfco", "010-9876-5432")
        b = call("GET", query="op=list&tenant=acme").body()
        self.assertEqual([n["tenant_id"] for n in b["numbers"]], ["acme"])

    def test_post_from_foreign_origin_denied_before_any_write(self):
        r = call("POST", body={"op": "register", "tenant_id": "acme",
                               "number": "010-1234-5678"},
                 headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertFalse(r.body()["ok"])
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"),
                            "https://evil.example")
        self.assertEqual(caller_id.list_numbers(), [])      # 대장 무변화

    def test_denied_post_does_not_turn_on_the_live_gate(self):
        call("POST", body={"op": "register", "tenant_id": "acme",
                           "number": "010-1234-5678"},
             headers={"origin": "https://evil.example"})
        self.assertIsNone(os.environ.get("CPAAS_LIVE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
