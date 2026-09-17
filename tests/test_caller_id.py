# -*- coding: utf-8 -*-
"""api/caller_id.py 발신번호 등록 상태 관리 회귀 테스트.

검증 대상 (COMMERCIAL_READINESS 'Callbot 전용 — 발신번호 등록 상태 관리')
  1) 번호 규칙    — 국내 형식만 통과, 국제·특수·빈값 거부, +82 정규화
  2) 마스킹       — voice._mask_phone 과 같은 규칙(드리프트 차단), 원문 미노출
  3) 상태 전이    — 전이표 준수, 증빙 없는 승인 불가, 낡은 증빙 거부
  4) 만료 계산    — 저장 상태가 verified 여도 지나면 expired, 임박 경고, 갱신 복구
  5) 게이트       — 조회·등록이 CPAAS_LIVE 를 켜지 않는다, outbound_ready 는 승인과 별개
  6) 봉인         — 키 있으면 원문 봉인, 없으면 보관 안 함, 중지 시 암호 파기
  7) HTTP 계약    — 403/200/400 봉투·요청ID·no-store·CORS 되비침 금지, 네트워크 미사용

실행: python3 -m pytest tests/test_caller_id.py -q
"""
import os
import sys
import json
import time
import base64
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import caller_id          # noqa: E402
import pii_vault          # noqa: E402
import voice              # noqa: E402
import _ratelimit         # noqa: E402

KEY = base64.b64encode(b"C" * 32).decode()
ENVS = ("CPAAS_LIVE", "PII_MASTER_KEY", "PII_MASTER_KEY_OLD",
        "CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS")
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
    h = FakeHeaders(headers or {"origin": "https://callbot-portal.vercel.app"})
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    if body is not None:
        h["content-length"] = str(len(payload))
        h["content-type"] = "application/json"
    inst.headers = h
    inst.wfile = r.wfile
    inst.path = path
    inst.rfile = __import__("io").BytesIO(payload)
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
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        self.now = 1_700_000_000.0

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        caller_id._clear_for_tests()

    def _boom(self, *a, **k):
        raise NetworkTouched("발신번호 대장이 네트워크를 건드렸다")

    def reg(self, number="010-1234-5678", tenant="demo"):
        return caller_id.register(tenant, number, "대표", actor="t", now=self.now)

    def ok_evidence(self):
        return self.now - 3 * 86400


# ==========================================================================
# 1) 번호 규칙
# ==========================================================================
class TestNumberRules(Base):
    def test_valid_domestic_numbers(self):
        for n in ("010-1234-5678", "01012345678", "02-123-4567", "031-123-4567",
                  "070-1234-5678", "1588-1234", "080-123-4567", "+82 10-1234-5678"):
            self.assertTrue(caller_id.normalize_number(n).isdigit(), n)

    def test_rejected_numbers(self):
        for n in ("", "   ", "abc", "+1-202-555-0100", "119", "1234",
                  "0000000000", "0101234567890123", None, "010-1234-567"):
            with self.assertRaises(ValueError, msg=repr(n)):
                caller_id.normalize_number(n)

    def test_plus82_normalizes_to_leading_zero(self):
        self.assertEqual(caller_id.normalize_number("+82-10-1234-5678"), "01012345678")

    def test_emergency_and_short_codes_rejected(self):
        for n in ("112", "119", "182", "1", "15"):
            with self.assertRaises(ValueError, msg=n):
                caller_id.normalize_number(n)


# ==========================================================================
# 2) 마스킹
# ==========================================================================
class TestMasking(Base):
    def test_mask_matches_voice_rule(self):
        """voice 로그와 같은 규칙 — 두 화면이 다른 번호처럼 보이면 안 된다."""
        for n in ("01012345678", "0212345678", "1588123", "123", ""):
            self.assertEqual(caller_id.mask_number(n), voice._mask_phone(n), n)

    def test_view_has_no_raw_number(self):
        r = self.reg()
        self.assertNotIn("1234", json.dumps(r, ensure_ascii=False).replace("010****5678", ""))
        self.assertNotIn("number_sealed", r)
        self.assertNotIn("number_digits_masked_key", r)

    def test_history_has_no_raw_number(self):
        self.reg()
        self.assertNotIn("1234", json.dumps(caller_id.history(), ensure_ascii=False)
                         .replace("010****5678", ""))


# ==========================================================================
# 3) 상태 전이
# ==========================================================================
class TestTransitions(Base):
    def test_register_starts_pending_not_ready(self):
        r = self.reg()
        self.assertEqual(r["status"], "pending")
        self.assertFalse(r["outbound_ready"])
        self.assertEqual(r["days_left"], None)

    def test_duplicate_active_number_rejected(self):
        self.reg()
        with self.assertRaises(ValueError):
            self.reg()

    def test_duplicate_after_revoke_is_allowed_and_reuses_id(self):
        r = self.reg()
        caller_id.revoke(r["id"], now=self.now)
        again = self.reg()
        self.assertEqual(again["id"], r["id"])
        self.assertEqual(again["status"], "pending")

    def test_verify_requires_evidence(self):
        r = self.reg()
        for bad in (None, "", "아무거나", "위임장 사본"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                caller_id.verify(r["id"], bad, self.ok_evidence(), now=self.now)
        self.assertEqual(caller_id.view(caller_id._NUMBERS[r["id"]], self.now)["status"],
                         "pending")

    def test_stale_evidence_rejected(self):
        r = self.reg()
        with self.assertRaises(ValueError):
            caller_id.verify(r["id"], EVID, self.now - 200 * 86400, now=self.now)

    def test_future_evidence_rejected(self):
        r = self.reg()
        with self.assertRaises(ValueError):
            caller_id.verify(r["id"], EVID, self.now + 10 * 86400, now=self.now)

    def test_missing_issued_at_rejected(self):
        r = self.reg()
        for bad in (None, "", "어제", 0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                caller_id.verify(r["id"], EVID, bad, now=self.now)

    def test_verify_sets_expiry_and_ready(self):
        r = self.reg()
        v = caller_id.verify(r["id"], EVID, self.ok_evidence(), valid_days=365, now=self.now)
        self.assertEqual(v["status"], "verified")
        self.assertTrue(v["outbound_ready"])
        self.assertEqual(v["days_left"], 365)

    def test_valid_days_bounds(self):
        r = self.reg()
        for bad in (0, -1, 2000, "abc", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                caller_id.verify(r["id"], EVID, self.ok_evidence(), valid_days=bad, now=self.now)

    def test_transition_from_wrong_state_rejected(self):
        r = self.reg()
        caller_id.verify(r["id"], EVID, self.ok_evidence(), now=self.now)
        with self.assertRaises(ValueError):
            caller_id.verify(r["id"], EVID, self.ok_evidence(), now=self.now)   # 이미 verified
        with self.assertRaises(ValueError):
            caller_id.reject(r["id"], now=self.now)

    def test_revoked_number_is_not_ready(self):
        r = self.reg()
        caller_id.verify(r["id"], EVID, self.ok_evidence(), now=self.now)
        out = caller_id.revoke(r["id"], actor="t")
        self.assertEqual(out["status"], "revoked")
        self.assertFalse(out["outbound_ready"])
        self.assertIsNone(out["days_left"], "중지된 번호에 잔여일이 남으면 안 된다")

    def test_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            caller_id.revoke("CID-9999")

    def test_unknown_op_rejected(self):
        r = self.reg()
        with self.assertRaises(ValueError):
            caller_id._apply(r["id"], "approve_all")

    def test_tenant_id_validated(self):
        for bad in ("", "  ", "A"*41, "한글", "has space", "-lead"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                caller_id.register(bad, "010-1234-5678", now=self.now)

    def test_max_numbers_enforced(self):
        caller_id.MAX_NUMBERS, orig = 2, caller_id.MAX_NUMBERS
        try:
            caller_id.register("demo", "010-1111-2222", now=self.now)
            caller_id.register("demo", "010-3333-4444", now=self.now)
            with self.assertRaises(ValueError):
                caller_id.register("demo", "010-5555-6666", now=self.now)
        finally:
            caller_id.MAX_NUMBERS = orig


# ==========================================================================
# 4) 만료 계산
# ==========================================================================
class TestExpiry(Base):
    def setUp(self):
        Base.setUp(self)
        self.r = self.reg()
        caller_id.verify(self.r["id"], EVID, self.ok_evidence(), valid_days=100, now=self.now)
        self.rec = caller_id._NUMBERS[self.r["id"]]

    def test_expired_is_computed_not_stored(self):
        late = self.now + 101 * 86400
        self.assertEqual(self.rec["status"], "verified")            # 저장값은 그대로
        self.assertEqual(caller_id.view(self.rec, late)["status"], "expired")
        self.assertFalse(caller_id.view(self.rec, late)["outbound_ready"])

    def test_boundary_exact_expiry_is_expired(self):
        self.assertEqual(caller_id.view(self.rec, self.rec["expires_at"])["status"], "expired")
        self.assertEqual(caller_id.view(self.rec, self.rec["expires_at"] - 1)["status"],
                         "verified")

    def test_expiring_soon_window(self):
        self.assertFalse(caller_id.view(self.rec, self.now)["expiring_soon"])
        soon = self.now + 71 * 86400        # 남은 29일
        self.assertTrue(caller_id.view(self.rec, soon)["expiring_soon"])

    def test_renew_revives_expired(self):
        late = self.now + 200 * 86400
        out = caller_id.renew(self.r["id"], EVID, late - 86400, valid_days=365, now=late)
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["days_left"], 365)

    def test_renew_needs_fresh_evidence(self):
        late = self.now + 200 * 86400
        with self.assertRaises(ValueError):
            caller_id.renew(self.r["id"], EVID, self.ok_evidence(), now=late)

    def test_summary_surfaces_expiring_and_expired(self):
        soon = self.now + 80 * 86400
        s = caller_id.summary(soon)
        self.assertEqual(len(s["expiring_soon"]), 1)
        self.assertEqual(s["outbound_ready_count"], 1)
        late = self.now + 200 * 86400
        s2 = caller_id.summary(late)
        self.assertEqual(s2["counts"]["expired"], 1)
        self.assertEqual(s2["outbound_ready_count"], 0)

    def test_list_sorts_urgent_first(self):
        other = caller_id.register("demo", "010-9999-8888", now=self.now)
        caller_id.verify(other["id"], EVID, self.ok_evidence(), valid_days=3000 % 1825 or 900,
                         now=self.now)
        rows = caller_id.list_numbers(now=self.now)
        self.assertEqual(rows[0]["id"], self.r["id"])   # 100일 남은 쪽이 앞

    def test_list_filters(self):
        caller_id.register("other", "010-9999-8888", now=self.now)
        self.assertEqual(len(caller_id.list_numbers(tenant="other", now=self.now)), 1)
        self.assertEqual(len(caller_id.list_numbers(status="verified", now=self.now)), 1)
        self.assertEqual(len(caller_id.list_numbers(status="expired", now=self.now)), 0)


# ==========================================================================
# 5) 게이트 — 조회가 실발신을 켜지 않는다
# ==========================================================================
class TestGate(Base):
    def test_summary_does_not_flip_cpaas(self):
        self.reg()
        caller_id.summary(self.now)
        caller_id.list_numbers(now=self.now)
        self.assertIsNone(os.environ.get("CPAAS_LIVE"))
        self.assertFalse(caller_id.cpaas_live())

    def test_cpaas_live_is_exact_one(self):
        for v, want in (("1", True), ("true", False), ("on", False), ("0", False), ("", False)):
            os.environ["CPAAS_LIVE"] = v
            self.assertEqual(caller_id.cpaas_live(), want, v)

    def test_outbound_ready_is_not_permission_to_dial(self):
        r = self.reg()
        v = caller_id.verify(r["id"], EVID, self.ok_evidence(), now=self.now)
        self.assertTrue(v["outbound_ready"])
        self.assertFalse(caller_id.cpaas_live())
        self.assertIn("[승인 필요]", caller_id.summary(self.now)["activation_note"])

    def test_policy_is_static_and_documents_legal_basis(self):
        p = caller_id.policy()
        self.assertEqual(set(p["transitions"][0]), {"op", "label", "from", "to",
                                                    "needs_evidence"})
        self.assertIn("전기통신사업법", p["legal"])
        self.assertEqual(len(p["transitions"]), len(caller_id.TRANSITIONS))


# ==========================================================================
# 6) 봉인
# ==========================================================================
class TestSealing(Base):
    def test_without_key_number_is_not_stored(self):
        r = self.reg()
        rec = caller_id._NUMBERS[r["id"]]
        self.assertIsNone(rec["number_sealed"])
        self.assertEqual(rec["protection"], "unavailable")
        self.assertIsNone(caller_id.reveal_number(r["id"], actor="t"))

    def test_with_key_number_is_sealed_and_revealable(self):
        os.environ["PII_MASTER_KEY"] = KEY
        r = self.reg()
        rec = caller_id._NUMBERS[r["id"]]
        self.assertTrue(pii_vault.is_envelope(rec["number_sealed"]))
        self.assertNotIn("12345678", rec["number_sealed"])
        self.assertEqual(caller_id.reveal_number(r["id"], actor="t"), "01012345678")
        self.assertEqual(caller_id.history()[-1]["action"], "reveal")

    def test_evidence_is_sealed_too(self):
        os.environ["PII_MASTER_KEY"] = KEY
        r = self.reg()
        v = caller_id.verify(r["id"], EVID, self.ok_evidence(), now=self.now)
        self.assertEqual(v["evidence_stored"], "sealed")
        self.assertTrue(pii_vault.is_envelope(caller_id._NUMBERS[r["id"]]["evidence_sealed"]))

    def test_revoke_shreds_number(self):
        os.environ["PII_MASTER_KEY"] = KEY
        r = self.reg()
        caller_id.revoke(r["id"], actor="t")
        rec = caller_id._NUMBERS[r["id"]]
        self.assertTrue(pii_vault.is_shredded(rec["number_sealed"]))
        self.assertEqual(rec["protection"], "shredded")
        self.assertIsNone(caller_id.reveal_number(r["id"], actor="t"))

    def test_reveal_unknown_raises(self):
        with self.assertRaises(KeyError):
            caller_id.reveal_number("CID-9999")


# ==========================================================================
# 7) HTTP 계약
# ==========================================================================
class TestHttp(Base):
    def test_get_summary_200_envelope(self):
        r = call("GET")
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertTrue(b["ok"])
        self.assertIn("counts", b)
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertTrue(r.header("X-Request-Id"))

    def test_cors_is_not_reflected(self):
        r = call("GET", headers={"origin": "https://evil.example"})
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"), "https://evil.example")

    def test_foreign_origin_denied(self):
        r = call("GET", headers={"origin": "https://evil.example",
                                 "user-agent": "Mozilla/5.0"})
        self.assertEqual(r.status, 403)
        self.assertIs(r.body()["ok"], False)

    def test_get_ops(self):
        self.reg()
        for op, key in (("list", "numbers"), ("policy", "evidence_types"),
                        ("history", "history")):
            r = call("GET", query="op=" + op)
            self.assertEqual(r.status, 200, op)
            self.assertIn(key, r.body(), op)

    def test_bad_op_400(self):
        r = call("GET", query="op=drop_all")
        self.assertEqual(r.status, 400)
        self.assertFalse(r.body()["ok"])

    def test_bad_status_filter_400_names_field(self):
        r = call("GET", query="op=list&status=whatever")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "status")

    def test_post_register_then_verify(self):
        r = call("POST", body={"op": "register", "tenant_id": "demo",
                               "number": "010-1234-5678", "label": "대표"})
        self.assertEqual(r.status, 200)
        cid = r.body()["number"]["id"]
        self.assertEqual(r.body()["number"]["status"], "pending")
        r2 = call("POST", body={"op": "verify", "id": cid, "evidence_type": EVID,
                                "issued_at": time.time() - 86400, "valid_days": 180})
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.body()["number"]["status"], "verified")
        self.assertIn("[승인 필요]", r2.body()["activation_note"])

    def test_post_bad_number_400_names_field(self):
        r = call("POST", body={"op": "register", "tenant_id": "demo",
                               "number": "+1-202-555-0100"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "number")

    def test_post_bad_tenant_400_names_field(self):
        r = call("POST", body={"op": "register", "tenant_id": "한글",
                               "number": "010-1234-5678"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "tenant_id")

    def test_post_unknown_op_400(self):
        r = call("POST", body={"op": "nuke"})
        self.assertEqual(r.status, 400)

    def test_post_unknown_id_400_names_field(self):
        r = call("POST", body={"op": "revoke", "id": "CID-9999"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "id")

    def test_post_stale_evidence_400_names_field(self):
        r = call("POST", body={"op": "register", "tenant_id": "demo",
                               "number": "010-1234-5678"})
        cid = r.body()["number"]["id"]
        r2 = call("POST", body={"op": "verify", "id": cid, "evidence_type": EVID,
                                "issued_at": time.time() - 200 * 86400})
        self.assertEqual(r2.status, 400)
        self.assertEqual(r2.body()["details"][0]["field"], "evidence_type")

    def test_post_missing_body_400(self):
        r = call("POST", body={})
        self.assertEqual(r.status, 400)

    def test_error_body_has_no_internal_paths(self):
        r = call("POST", body={"op": "register", "tenant_id": "demo", "number": "xx"})
        blob = json.dumps(r.body(), ensure_ascii=False)
        self.assertNotIn("Traceback", blob)
        self.assertNotIn("/api/", blob.replace("/api/caller_id", ""))

    def test_options_preflight(self):
        r = Resp()
        inst = caller_id.handler.__new__(caller_id.handler)
        inst.headers = FakeHeaders({"origin": "https://callbot-portal.vercel.app"})
        inst.wfile = r.wfile
        inst.path = "/api/caller_id"
        inst.send_response = lambda c: setattr(r, "status", c)
        inst.send_header = lambda k, v: r.sent.append((k, str(v)))
        inst.end_headers = lambda: None
        inst.do_OPTIONS()
        self.assertEqual(r.status, 204)

    def test_no_network_touched(self):
        """여기까지 예외 없이 왔다는 것 자체가 urlopen 미호출의 증거."""
        self.reg()
        call("GET")
        call("GET", query="op=list")


if __name__ == "__main__":
    unittest.main()
