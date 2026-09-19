# -*- coding: utf-8 -*-
"""api/partners.py 파트너(채널) 귀속 대장 회귀 테스트.

검증 대상 (COMMERCIAL_READINESS '파트너 채널')
  1) 파트너 개념   — partner_id nullable, 없으면 직접 계약, 중복·한도·상태 전이
  2) 귀속 근거     — 기간으로 쌓기(덮어쓰기 금지)·빈틈/겹침 없음·과거 시점 조회
  3) 경로 정합성   — direct 에 파트너 금지 / partner_* 에 파트너 필수
  4) 개인정보      — 담당자 이름 마스킹, 연락처 저장 거부, 응답 어디에도 원문 없음
  5) 역할 권한     — partner_admin 은 자기 파트너·해당 시점 고객사만, 직접계약 불가
  6) 활성화 게이트 — enforce 는 PARTNER_RBAC_LIVE 전까지 호출 자체가 실패
  7) HTTP 계약     — 403/200/400/404 봉투·요청ID·no-store·CORS 되비침 금지, 네트워크 미사용
  8) 수치 날조 금지 — 대장이 비면 0 이고 임의 실적·수수료를 만들지 않는다

실행: python3 -m pytest tests/test_partners.py -q
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
        self.t0 = 1_700_000_000.0

    def tearDown(self):
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

    def mk(self, pid="ch-alpha", name="테스트 파트너", owner=""):
        return partners.create_partner(pid, name, owner=owner, now=self.t0)

    def acct(self, tid="acme", channel="partner_referral", pid="ch-alpha", **kw):
        kw.setdefault("now", self.t0)
        return partners.attach(tid, channel, partner_id=pid, **kw)


# ==========================================================================
# 1) 파트너 개념 — nullable partner_id
# ==========================================================================
class TestPartnerConcept(Base):
    def test_create_defaults_active(self):
        p = self.mk()
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["accounts"], 0)
        self.assertTrue(p["created_at"].endswith("Z"))

    def test_duplicate_partner_rejected(self):
        self.mk()
        with self.assertRaises(ValueError):
            self.mk()

    def test_partner_id_format_enforced(self):
        for bad in ("", "  ", "한글", "a" * 41, "-lead", "x y", "ch/alpha"):
            with self.assertRaises(ValueError, msg=bad):
                partners.create_partner(bad, "이름", now=self.t0)

    def test_partner_id_case_normalized(self):
        """대소문자만 다른 ID 가 별개 파트너로 갈라지면 귀속이 둘로 쪼개진다."""
        p = partners.create_partner("CH-Alpha", "이름", now=self.t0)
        self.assertEqual(p["id"], "ch-alpha")
        with self.assertRaises(ValueError):
            partners.create_partner("ch-ALPHA", "다른 이름", now=self.t0)

    def test_tenant_id_case_normalized(self):
        a = partners.attach("ACME", "direct", now=self.t0)
        self.assertEqual(a["tenant_id"], "acme")
        with self.assertRaises(ValueError):
            partners.attach("Acme", "direct", now=self.t0)

    def test_name_required(self):
        with self.assertRaises(ValueError):
            partners.create_partner("ch-x", "   ", now=self.t0)

    def test_partner_limit(self):
        orig = partners.MAX_PARTNERS
        partners.MAX_PARTNERS = 2
        try:
            self.mk("p1", "A")
            self.mk("p2", "B")
            with self.assertRaises(ValueError):
                self.mk("p3", "C")
        finally:
            partners.MAX_PARTNERS = orig

    def test_direct_account_has_null_partner(self):
        a = partners.attach("selfco", "direct", now=self.t0)
        self.assertIsNone(a["partner_id"])
        self.assertEqual(a["attribution"], "direct")

    def test_suspend_resume_transitions(self):
        self.mk()
        s = partners.set_partner_status("ch-alpha", "suspended", now=self.t0)
        self.assertEqual(s["status"], "suspended")
        with self.assertRaises(ValueError):       # 같은 상태로 재전이 금지
            partners.set_partner_status("ch-alpha", "suspended", now=self.t0)
        r = partners.set_partner_status("ch-alpha", "active", now=self.t0)
        self.assertEqual(r["status"], "active")

    def test_unknown_partner_status_rejected(self):
        self.mk()
        with self.assertRaises(ValueError):
            partners.set_partner_status("ch-alpha", "deleted", now=self.t0)

    def test_unknown_partner_keyerror(self):
        with self.assertRaises(KeyError):
            partners.set_partner_status("nope", "suspended", now=self.t0)

    def test_attach_to_unknown_partner_rejected(self):
        with self.assertRaises(KeyError):
            self.acct(pid="ghost")

    def test_partner_account_count_reflects_current_only(self):
        self.mk()
        self.mk("ch-beta", "다른 파트너")
        self.acct()
        self.assertEqual(partners.list_partners()[0]["accounts"], 1)
        partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                          now=self.t0 + DAY)
        by_id = {p["id"]: p for p in partners.list_partners()}
        self.assertEqual(by_id["ch-alpha"]["accounts"], 0)
        self.assertEqual(by_id["ch-beta"]["accounts"], 1)

    def test_list_partners_filtered_and_sorted(self):
        self.mk("ch-z", "Z")
        self.mk("ch-a", "A")
        partners.set_partner_status("ch-z", "suspended", now=self.t0)
        self.assertEqual([p["id"] for p in partners.list_partners()], ["ch-a", "ch-z"])
        self.assertEqual([p["id"] for p in partners.list_partners("active")], ["ch-a"])


# ==========================================================================
# 2) 귀속 근거 — 기간으로 쌓는다
# ==========================================================================
class TestAttribution(Base):
    def test_reassign_keeps_previous_period(self):
        self.mk()
        self.mk("ch-beta", "다른 파트너")
        self.acct()
        t1 = self.t0 + 100 * DAY
        a = partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t1)
        self.assertEqual(a["changes"], 2)
        self.assertEqual(len(a["periods"]), 2)
        self.assertEqual(a["periods"][0]["partner_id"], "ch-alpha")

    def test_past_attribution_survives_reassign(self):
        """정산 분쟁의 핵심 — '그때 누구 담당이었나'에 답해야 한다."""
        self.mk()
        self.mk("ch-beta", "다른 파트너")
        self.acct()
        t1 = self.t0 + 100 * DAY
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t1)
        self.assertEqual(partners.attribution_at("acme", self.t0 + DAY)["partner_id"],
                         "ch-alpha")
        self.assertEqual(partners.attribution_at("acme", t1 + DAY)["partner_id"],
                         "ch-beta")

    def test_period_boundary_is_exclusive_at_end(self):
        self.mk()
        self.mk("ch-beta", "B")
        self.acct()
        t1 = self.t0 + 10 * DAY
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t1)
        self.assertEqual(partners.attribution_at("acme", t1)["partner_id"], "ch-beta")
        self.assertEqual(partners.attribution_at("acme", t1 - 1)["partner_id"], "ch-alpha")

    def test_no_gap_or_overlap(self):
        self.mk()
        self.mk("ch-beta", "B")
        self.acct()
        partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                          now=self.t0 + DAY)
        partners.reassign("acme", "direct", now=self.t0 + 2 * DAY)
        self.assertEqual(partners.check_invariants(), [])

    def test_invariant_check_detects_corruption(self):
        """변이 검증 — 불변식 검사가 실제로 깨진 장부를 잡는지."""
        self.mk()
        self.acct()
        partners._ACCOUNTS["acme"]["periods"].append(
            partners._period("ch-alpha", "direct", "", "", None, self.t0 + DAY))
        self.assertTrue(partners.check_invariants())

    def test_attach_twice_rejected(self):
        self.mk()
        self.acct()
        with self.assertRaises(ValueError):
            self.acct()

    def test_reassign_same_attribution_rejected(self):
        self.mk()
        self.acct()
        with self.assertRaises(ValueError):
            partners.reassign("acme", "partner_referral", partner_id="ch-alpha",
                              now=self.t0 + DAY)

    def test_reassign_backwards_in_time_rejected(self):
        self.mk()
        self.mk("ch-beta", "B")
        self.acct()
        with self.assertRaises(ValueError):
            partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                              now=self.t0 - DAY)

    def test_detach_closes_but_keeps_history(self):
        self.mk()
        self.acct()
        t1 = self.t0 + 5 * DAY
        a = partners.detach("acme", now=t1)
        self.assertEqual(a["attribution"], "ended")
        self.assertFalse(a["active"])
        self.assertEqual(partners.attribution_at("acme", self.t0 + DAY)["partner_id"],
                         "ch-alpha")
        self.assertIsNone(partners.attribution_at("acme", t1 + DAY))

    def test_detach_twice_rejected(self):
        self.mk()
        self.acct()
        partners.detach("acme", now=self.t0 + DAY)
        with self.assertRaises(ValueError):
            partners.detach("acme", now=self.t0 + 2 * DAY)

    def test_reassign_after_detach_rejected(self):
        self.mk()
        self.mk("ch-beta", "B")
        self.acct()
        partners.detach("acme", now=self.t0 + DAY)
        with self.assertRaises(ValueError):
            partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                              now=self.t0 + 2 * DAY)

    def test_unknown_tenant_operations(self):
        for fn in (lambda: partners.detach("ghost", now=self.t0),
                   lambda: partners.reassign("ghost", "direct", now=self.t0)):
            with self.assertRaises(KeyError):
                fn()

    def test_attribution_at_unknown_tenant_is_none(self):
        self.assertIsNone(partners.attribution_at("ghost"))

    def test_contracted_at_recorded(self):
        self.mk()
        a = self.acct(contracted_at=self.t0 - 30 * DAY)
        self.assertTrue(a["contracted_at"] < partners._iso(self.t0))
        self.assertEqual(a["periods"][0]["from"], partners._iso(self.t0 - 30 * DAY))

    def test_future_contract_date_rejected(self):
        self.mk()
        with self.assertRaises(ValueError):
            self.acct(contracted_at=self.t0 + 10 * DAY)

    def test_change_limit(self):
        self.mk()
        self.mk("ch-beta", "B")
        self.acct()
        orig = partners.PERIODS_MAX
        partners.PERIODS_MAX = 2
        try:
            partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                              now=self.t0 + DAY)
            with self.assertRaises(ValueError):
                partners.reassign("acme", "direct", now=self.t0 + 2 * DAY)
        finally:
            partners.PERIODS_MAX = orig

    def test_account_limit(self):
        orig = partners.MAX_ACCOUNTS
        partners.MAX_ACCOUNTS = 1
        try:
            partners.attach("a1", "direct", now=self.t0)
            with self.assertRaises(ValueError):
                partners.attach("a2", "direct", now=self.t0)
        finally:
            partners.MAX_ACCOUNTS = orig

    def test_list_accounts_filters(self):
        self.mk()
        self.acct()
        partners.attach("selfco", "direct", now=self.t0)
        self.assertEqual([a["tenant_id"] for a in partners.list_accounts("ch-alpha")],
                         ["acme"])
        self.assertEqual([a["tenant_id"] for a in partners.list_accounts("-")],
                         ["selfco"])
        self.assertEqual(
            [a["tenant_id"] for a in partners.list_accounts(channel="partner_referral")],
            ["acme"])

    def test_history_records_actions(self):
        self.mk()
        self.acct(actor="tester")
        acts = [h["action"] for h in partners.history()]
        self.assertEqual(acts, ["create", "attach"])
        self.assertEqual(partners.history()[-1]["actor"], "tester")

    def test_history_capped(self):
        orig = partners.HISTORY_MAX
        partners.HISTORY_MAX = 3
        try:
            for i in range(6):
                self.mk("p%d" % i, "N%d" % i)
            self.assertEqual(len(partners._HISTORY), 3)
        finally:
            partners.HISTORY_MAX = orig

    def test_history_returns_copies(self):
        self.mk()
        h = partners.history()
        h[0]["action"] = "TAMPERED"
        self.assertEqual(partners.history()[0]["action"], "create")

    def test_account_view_returns_copies(self):
        self.mk()
        self.acct()
        v = partners.list_accounts()[0]
        v["periods"][0]["partner_id"] = "TAMPERED"
        self.assertEqual(partners.list_accounts()[0]["periods"][0]["partner_id"],
                         "ch-alpha")


# ==========================================================================
# 3) 경로 정합성
# ==========================================================================
class TestChannelConsistency(Base):
    def test_direct_with_partner_rejected(self):
        self.mk()
        with self.assertRaises(ValueError):
            partners.attach("acme", "direct", partner_id="ch-alpha", now=self.t0)

    def test_partner_channel_without_partner_rejected(self):
        for ch in ("partner_referral", "partner_managed"):
            with self.assertRaises(ValueError, msg=ch):
                partners.attach("acme", ch, now=self.t0)

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError):
            partners.attach("acme", "carrier_pigeon", now=self.t0)

    def test_non_partner_channels_allow_null(self):
        for i, ch in enumerate(("inbound", "event", "expansion")):
            a = partners.attach("t%d" % i, ch, now=self.t0)
            self.assertIsNone(a["partner_id"])
            self.assertEqual(a["attribution"], "direct")

    def test_channel_labels_present(self):
        for k, v in partners.CHANNELS.items():
            self.assertTrue(v["label"], k)
            self.assertIn("needs_partner", v)

    def test_reassign_respects_channel_rule(self):
        self.mk()
        self.acct()
        with self.assertRaises(ValueError):
            partners.reassign("acme", "direct", partner_id="ch-alpha",
                              now=self.t0 + DAY)


# ==========================================================================
# 4) 개인정보
# ==========================================================================
class TestPii(Base):
    def test_owner_name_masked(self):
        self.assertEqual(partners.mask_name("홍길동"), "홍*동")
        self.assertEqual(partners.mask_name("김철"), "김*")
        self.assertEqual(partners.mask_name("남궁민수"), "남**수")
        self.assertEqual(partners.mask_name("김"), "김")
        self.assertEqual(partners.mask_name(""), "")
        self.assertEqual(partners.mask_name(None), "")

    def test_contact_rejected(self):
        for bad in ("홍길동 010-1234-5678", "a@b.com", "김철수 01012345678"):
            with self.assertRaises(ValueError, msg=bad):
                partners.create_partner("ch-x", "이름", owner=bad, now=self.t0)

    def test_owner_raw_never_stored(self):
        self.mk(owner="홍길동")
        self.acct(owner="이영희")
        blob = json.dumps(
            [partners.list_partners(), partners.list_accounts(), partners.history()],
            ensure_ascii=False)
        for leak in ("홍길동", "이영희"):
            self.assertNotIn(leak, blob)

    def test_attach_rejects_contact_in_owner(self):
        self.mk()
        with self.assertRaises(ValueError):
            self.acct(owner="이영희 010-9999-8888")

    def test_note_truncated(self):
        self.mk()
        a = self.acct(reason="가" * 500)
        self.assertLessEqual(len(a["periods"][0]["reason"]), 200)

    def test_http_response_has_no_raw_name(self):
        self.mk(owner="홍길동")
        r = call("GET", query="op=list")
        self.assertNotIn("홍길동", r.wfile.data.decode("utf-8"))


# ==========================================================================
# 5) 역할 권한
# ==========================================================================
class TestAuthorize(Base):
    def setUp(self):
        Base.setUp(self)
        self.mk()
        self.mk("ch-beta", "다른 파트너")
        self.acct()
        partners.attach("selfco", "direct", now=self.t0)

    def test_all_scope_roles_see_everything(self):
        for role in ("owner", "ops", "viewer"):
            self.assertTrue(partners.authorize(role, None, "acme"), role)
            self.assertTrue(partners.authorize(role, None, "selfco"), role)

    def test_partner_admin_sees_own_only(self):
        self.assertTrue(partners.authorize("partner_admin", "ch-alpha", "acme"))
        self.assertFalse(partners.authorize("partner_admin", "ch-beta", "acme"))

    def test_partner_admin_cannot_see_direct_accounts(self):
        self.assertFalse(partners.authorize("partner_admin", "ch-alpha", "selfco"))

    def test_partner_admin_without_partner_id_denied(self):
        self.assertFalse(partners.authorize("partner_admin", None, "acme"))
        self.assertFalse(partners.authorize("partner_admin", "", "acme"))

    def test_unknown_role_denied(self):
        for role in ("admin", "", None, "PARTNER_ADMIN"):
            self.assertFalse(partners.authorize(role, "ch-alpha", "acme"), role)

    def test_unknown_tenant_denied_for_partner_admin(self):
        self.assertFalse(partners.authorize("partner_admin", "ch-alpha", "ghost"))

    def test_authorize_is_time_aware(self):
        t1 = self.t0 + 10 * DAY
        partners.reassign("acme", "partner_managed", partner_id="ch-beta", now=t1)
        self.assertTrue(partners.authorize("partner_admin", "ch-alpha", "acme", self.t0))
        self.assertFalse(partners.authorize("partner_admin", "ch-alpha", "acme", t1 + 1))
        self.assertTrue(partners.authorize("partner_admin", "ch-beta", "acme", t1 + 1))

    def test_detached_account_invisible_to_partner(self):
        partners.detach("acme", now=self.t0 + DAY)
        self.assertFalse(partners.authorize("partner_admin", "ch-alpha", "acme",
                                            self.t0 + 2 * DAY))

    def test_visible_tenants_scoping(self):
        self.assertEqual(partners.visible_tenants("owner"), ["acme", "selfco"])
        self.assertEqual(partners.visible_tenants("partner_admin", "ch-alpha"), ["acme"])
        self.assertEqual(partners.visible_tenants("partner_admin", "ch-beta"), [])

    def test_authorize_has_no_side_effects(self):
        before = json.dumps(partners.list_accounts(), ensure_ascii=False)
        partners.authorize("partner_admin", "ch-alpha", "acme")
        partners.visible_tenants("owner")
        self.assertEqual(json.dumps(partners.list_accounts(), ensure_ascii=False), before)
        self.assertEqual(len(partners.history()), 4)

    def test_partner_admin_not_activated_in_table(self):
        self.assertFalse(partners.ROLES["partner_admin"]["activated"])


# ==========================================================================
# 6) 활성화 게이트
# ==========================================================================
class TestRbacGate(Base):
    def setUp(self):
        Base.setUp(self)
        self.mk()
        self.acct()

    def test_enforce_blocked_until_approved(self):
        with self.assertRaises(RuntimeError) as cm:
            partners.enforce("owner", None, "acme")
        self.assertIn("[승인 필요]", str(cm.exception))

    def test_enforce_works_when_gate_on(self):
        os.environ["PARTNER_RBAC_LIVE"] = "1"
        self.assertTrue(partners.enforce("partner_admin", "ch-alpha", "acme"))
        with self.assertRaises(PermissionError):
            partners.enforce("partner_admin", "ch-nope", "acme")

    def test_gate_exact_match_only(self):
        for v in ("0", "true", "yes", " 1x", ""):
            os.environ["PARTNER_RBAC_LIVE"] = v
            self.assertFalse(partners.rbac_live(), v)
        os.environ["PARTNER_RBAC_LIVE"] = " 1 "
        self.assertTrue(partners.rbac_live())

    def test_gate_read_at_call_time(self):
        self.assertFalse(partners.rbac_live())
        os.environ["PARTNER_RBAC_LIVE"] = "1"
        self.assertTrue(partners.rbac_live())

    def test_read_operations_do_not_open_gate(self):
        call("GET")
        call("GET", query="op=policy")
        call("POST", body={"op": "attach", "tenant_id": "x", "channel": "direct"})
        self.assertFalse(partners.rbac_live())
        self.assertIsNone(os.environ.get("PARTNER_RBAC_LIVE"))

    def test_summary_declares_gate_off(self):
        s = partners.summary()
        self.assertFalse(s["rbac"]["active"])
        self.assertIn("[승인 필요]", s["rbac"]["note"])


# ==========================================================================
# 7) HTTP 계약
# ==========================================================================
class TestHttp(Base):
    def test_foreign_origin_denied(self):
        r = call("GET", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.body()["ok"], False)
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"),
                            "https://evil.example")

    def test_summary_ok(self):
        r = call("GET")
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertTrue(b["ok"])
        self.assertEqual(b["partners"]["total"], 0)
        self.assertEqual(b["accounts"]["total"], 0)
        self.assertEqual(b["integrity"], [])
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertTrue(r.header("X-Request-Id"))

    def test_policy_lists_channels_and_roles(self):
        b = call("GET", query="op=policy").body()
        self.assertEqual({c["value"] for c in b["channels"]}, set(partners.CHANNELS))
        self.assertEqual({r["value"] for r in b["roles"]}, set(partners.ROLES))
        self.assertIn("수수료", b["settlement"])

    def test_bad_op_400(self):
        r = call("GET", query="op=drop")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "op")

    def test_bad_query_values_400(self):
        for q, field in (("status=zzz", "status"), ("partner=BAD!", "partner"),
                         ("channel=zzz", "channel")):
            r = call("GET", query="op=list&" + q)
            self.assertEqual(r.status, 400, q)
            self.assertEqual(r.body()["details"][0]["field"], field)

    def test_attribution_requires_tenant(self):
        r = call("GET", query="op=attribution")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "tenant")

    def test_attribution_unknown_tenant_404(self):
        r = call("GET", query="op=attribution&tenant=ghost")
        self.assertEqual(r.status, 404)
        self.assertFalse(r.body()["ok"])

    def test_attribution_returns_periods(self):
        self.mk()
        self.acct()
        b = call("GET", query="op=attribution&tenant=acme").body()
        self.assertEqual(b["account"]["partner_id"], "ch-alpha")
        self.assertEqual(len(b["account"]["periods"]), 1)

    def test_post_create_and_attach(self):
        r = call("POST", body={"op": "create", "partner_id": "ch-alpha",
                               "name": "테스트 파트너", "owner": "홍길동"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["partner"]["owner_masked"], "홍*동")
        r2 = call("POST", body={"op": "attach", "tenant_id": "acme",
                                "channel": "partner_referral", "partner_id": "ch-alpha"})
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.body()["account"]["attribution"], "partner")
        self.assertIn("rbac", r2.body())

    def test_post_missing_op_400(self):
        r = call("POST", body={})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "op")

    def test_post_unknown_op_400(self):
        r = call("POST", body={"op": "delete_everything"})
        self.assertEqual(r.status, 400)

    def test_post_channel_mismatch_points_at_channel(self):
        call("POST", body={"op": "create", "partner_id": "ch-alpha", "name": "P"})
        r = call("POST", body={"op": "attach", "tenant_id": "acme",
                               "channel": "direct", "partner_id": "ch-alpha"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "channel")

    def test_post_contact_in_owner_points_at_owner(self):
        r = call("POST", body={"op": "create", "partner_id": "ch-a", "name": "P",
                               "owner": "홍길동 010-1234-5678"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "owner")

    def test_post_unknown_partner_400(self):
        r = call("POST", body={"op": "attach", "tenant_id": "acme",
                               "channel": "partner_referral", "partner_id": "ghost"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "partner_id")

    def test_post_unknown_tenant_400(self):
        r = call("POST", body={"op": "detach", "tenant_id": "ghost"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "tenant_id")

    def test_post_suspend_resume(self):
        call("POST", body={"op": "create", "partner_id": "ch-a", "name": "P"})
        r = call("POST", body={"op": "suspend", "partner_id": "ch-a"})
        self.assertEqual(r.body()["partner"]["status"], "suspended")
        r = call("POST", body={"op": "resume", "partner_id": "ch-a"})
        self.assertEqual(r.body()["partner"]["status"], "active")

    def test_post_reassign_and_detach(self):
        call("POST", body={"op": "create", "partner_id": "ch-a", "name": "A"})
        call("POST", body={"op": "create", "partner_id": "ch-b", "name": "B"})
        call("POST", body={"op": "attach", "tenant_id": "acme",
                           "channel": "partner_referral", "partner_id": "ch-a"})
        r = call("POST", body={"op": "reassign", "tenant_id": "acme",
                               "channel": "partner_managed", "partner_id": "ch-b"})
        self.assertEqual(r.body()["account"]["partner_id"], "ch-b")
        r = call("POST", body={"op": "detach", "tenant_id": "acme"})
        self.assertEqual(r.body()["account"]["attribution"], "ended")

    def test_oversized_body_413(self):
        r = call("POST", body={"op": "create", "partner_id": "ch-a",
                               "name": "P", "note": "가" * 40000})
        self.assertIn(r.status, (400, 413))
        self.assertFalse(r.body()["ok"])

    def test_options_preflight(self):
        r = Resp()
        inst = partners.handler.__new__(partners.handler)
        inst.headers = FakeHeaders(dict(ORIGIN))
        inst.wfile = r.wfile
        inst.path = "/api/partners"
        inst.send_response = lambda c: setattr(r, "status", c)
        inst.send_header = lambda k, v: r.sent.append((k, str(v)))
        inst.end_headers = lambda: None
        inst.do_OPTIONS()
        self.assertEqual(r.status, 204)

    def test_no_secret_leak_in_responses(self):
        os.environ["CALLBOT_API_KEY"] = "super-secret-key"
        self.mk()
        for q in ("", "op=list", "op=policy", "op=history"):
            txt = call("GET", query=q).wfile.data.decode("utf-8")
            self.assertNotIn("super-secret-key", txt)

    def test_no_network_touched(self):
        """여기까지 예외 없이 왔다는 것 자체가 urlopen 미호출의 증거."""
        self.mk()
        self.acct()
        call("GET")
        call("GET", query="op=accounts")
        call("POST", body={"op": "detach", "tenant_id": "acme"})


# ==========================================================================
# 8) 수치 날조 금지
# ==========================================================================
class TestNoFabricatedNumbers(Base):
    def test_empty_ledger_reports_zero_not_samples(self):
        s = partners.summary()
        self.assertEqual(s["accounts"],
                         {"total": 0, "via_partner": 0, "direct": 0, "ended": 0})
        self.assertEqual(s["by_partner"], {})
        self.assertEqual(partners.list_partners(), [])
        self.assertEqual(partners.list_accounts(), [])

    def test_no_seeded_demo_partners(self):
        """모듈 import 만으로 파트너·고객사가 생기면 안 된다(허위 도입사례 금지)."""
        self.assertEqual(len(partners._PARTNERS), 0)
        self.assertEqual(len(partners._ACCOUNTS), 0)

    def test_no_commission_amounts_computed(self):
        """수수료·청구액은 이 모듈이 만들지 않는다(계약 확정 후 별도)."""
        blob = json.dumps([partners.summary(), partners.policy()], ensure_ascii=False)
        for word in ("commission_rate", "amount", "revenue", "fee_krw"):
            self.assertNotIn(word, blob)

    def test_counts_match_ledger(self):
        self.mk()
        self.acct()
        partners.attach("selfco", "direct", now=self.t0)
        partners.attach("gone", "direct", now=self.t0)
        partners.detach("gone", now=self.t0 + DAY)
        s = partners.summary(self.t0 + 2 * DAY)
        self.assertEqual(s["accounts"]["total"], 3)
        self.assertEqual(s["accounts"]["via_partner"], 1)
        self.assertEqual(s["accounts"]["direct"], 1)
        self.assertEqual(s["accounts"]["ended"], 1)
        self.assertEqual(s["by_partner"], {"ch-alpha": 1})

    def test_persistence_limit_declared(self):
        self.assertIn("휘발", partners.summary()["persistence"])


if __name__ == "__main__":
    unittest.main()
