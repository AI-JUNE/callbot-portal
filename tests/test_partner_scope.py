# -*- coding: utf-8 -*-
"""파트너 조회 스코프 단일 진입점 회귀 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS '2계층 확장 여지 확보')
  1) 단일 지점 — 조회 경로(고지문구·발신번호)는 스스로 판정하지 않고
     `partners.scope_tenants()` 만 부른다. 리셀러 필터는 그 한 곳에서만 끼어든다.
  2) 승인 전 무변화 — PARTNER_RBAC_LIVE 가 꺼져 있으면 **아무것도 걸러지지 않는다**.
     반쯤 배선된 채 배포돼 화면에서 데이터가 조용히 사라지는 일을 막는다.
  3) 켜기 전 영향 확인 — 그래도 "켜면 무엇이 가려질지"(`would_hide`)는 계산해 준다.
  4) 미등록 고객사를 말없이 버리지 않는다 — 장부에 없는 고객사는 `unknown` 으로
     드러난다(등록 누락은 사고지 권한 통제가 아니다).
  5) 시점 인자 — 담당이 넘어가기 전 기간은 여전히 보이고, 넘어간 뒤에는 안 보인다.
  6) 부작용 없음 — 조회가 장부를 바꾸지 않고 게이트를 켜지도 않는다.
  7) 가용성 — partners 모듈이 터져도 조회 경로는 죽지 않는다.

실행: python3 -m pytest tests/test_partner_scope.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import partners       # noqa: E402
import disclosure     # noqa: E402
import caller_id      # noqa: E402

GATE = "PARTNER_RBAC_LIVE"


class ScopeBase(unittest.TestCase):
    def setUp(self):
        self._gate = os.environ.get(GATE)
        os.environ.pop(GATE, None)
        partners._clear_for_tests()
        partners.create_partner("ch-alpha", "알파채널")
        partners.create_partner("ch-beta", "베타채널")
        partners.attach("acme", "partner_managed", partner_id="ch-alpha")
        partners.attach("selfco", "direct")

    def tearDown(self):
        if self._gate is None:
            os.environ.pop(GATE, None)
        else:
            os.environ[GATE] = self._gate
        partners._clear_for_tests()

    def live(self):
        os.environ[GATE] = "1"


# --------------------------------------------------------------------------
# 1~3) 승인 전에는 거르지 않되, 영향은 보여준다
# --------------------------------------------------------------------------
class TestGateOff(ScopeBase):
    def test_nothing_is_filtered_before_approval(self):
        s = partners.scope_tenants(["acme", "selfco"], "partner_admin", "ch-alpha")
        self.assertEqual(s["tenant_ids"], ["acme", "selfco"])
        self.assertFalse(s["applied"])
        self.assertEqual(s["hidden"], 0)
        self.assertIn("[승인 필요]", s["note"])

    def test_would_hide_shows_the_impact_of_turning_it_on(self):
        s = partners.scope_tenants(["acme", "selfco"], "partner_admin", "ch-alpha")
        self.assertEqual(s["would_hide"], ["selfco"])      # 직접 계약은 파트너에게 안 보인다

    def test_owner_scope_hides_nothing_either_way(self):
        s = partners.scope_tenants(["acme", "selfco"], "owner")
        self.assertEqual(s["would_hide"], [])
        self.assertEqual(s["scope"], "all")

    def test_reading_does_not_turn_the_gate_on(self):
        partners.scope_tenants(["acme"], "partner_admin", "ch-alpha")
        self.assertFalse(partners.rbac_live())
        self.assertIsNone(os.environ.get(GATE))


# --------------------------------------------------------------------------
# 4~5) 켠 뒤의 동작
# --------------------------------------------------------------------------
class TestGateOn(ScopeBase):
    def test_partner_sees_only_its_own_accounts(self):
        self.live()
        s = partners.scope_tenants(["acme", "selfco"], "partner_admin", "ch-alpha")
        self.assertEqual(s["tenant_ids"], ["acme"])
        self.assertTrue(s["applied"])
        self.assertEqual(s["hidden"], 1)

    def test_other_partner_sees_nothing(self):
        self.live()
        s = partners.scope_tenants(["acme", "selfco"], "partner_admin", "ch-beta")
        self.assertEqual(s["tenant_ids"], [])

    def test_owner_still_sees_everything(self):
        self.live()
        s = partners.scope_tenants(["acme", "selfco"], "owner")
        self.assertEqual(s["tenant_ids"], ["acme", "selfco"])
        self.assertEqual(s["hidden"], 0)

    def test_unknown_tenant_is_reported_not_silently_dropped(self):
        self.live()
        s = partners.scope_tenants(["acme", "ghostco"], "partner_admin", "ch-alpha")
        self.assertEqual(s["tenant_ids"], ["acme"])
        self.assertIn("ghostco", s["unknown"])          # 장부 등록 누락이 드러난다

    def test_unknown_tenant_is_reported_for_owner_too(self):
        s = partners.scope_tenants(["acme", "ghostco"], "owner")
        self.assertIn("ghostco", s["unknown"])
        self.assertEqual(s["tenant_ids"], ["acme", "ghostco"])   # owner 에게는 보인다

    def test_attribution_handover_respects_the_point_in_time(self):
        self.live()
        before = partners._now()
        partners.reassign("acme", "partner_managed", partner_id="ch-beta",
                          reason="담당 이관")
        after = partners._now() + 1
        self.assertEqual(
            partners.scope_tenants(["acme"], "partner_admin", "ch-alpha", at=before)["tenant_ids"],
            ["acme"])                                   # 이관 전 기간은 여전히 보인다
        self.assertEqual(
            partners.scope_tenants(["acme"], "partner_admin", "ch-alpha", at=after)["tenant_ids"],
            [])                                          # 이관 후에는 안 보인다
        self.assertEqual(
            partners.scope_tenants(["acme"], "partner_admin", "ch-beta", at=after)["tenant_ids"],
            ["acme"])


# --------------------------------------------------------------------------
# 입력·안전
# --------------------------------------------------------------------------
class TestScopeInput(ScopeBase):
    def test_unknown_role_is_not_silently_allowed_or_denied(self):
        s = partners.scope_tenants(["acme"], "superuser", "ch-alpha")
        self.assertTrue(s["unknown_role"])
        self.assertFalse(s["applied"])
        self.assertIsNone(s["scope"])

    def test_partner_admin_without_partner_id_sees_nothing_when_live(self):
        self.live()
        s = partners.scope_tenants(["acme", "selfco"], "partner_admin", None)
        self.assertEqual(s["tenant_ids"], [])

    def test_blank_and_non_string_entries_are_dropped(self):
        s = partners.scope_tenants(["acme", "", "   ", None, 7], "owner")
        self.assertEqual(s["tenant_ids"], ["acme"])

    def test_duplicates_are_collapsed_preserving_order(self):
        s = partners.scope_tenants(["selfco", "acme", "selfco"], "owner")
        self.assertEqual(s["tenant_ids"], ["selfco", "acme"])

    def test_empty_input_is_fine(self):
        s = partners.scope_tenants([], "owner")
        self.assertEqual(s["tenant_ids"], [])
        self.assertEqual(s["unknown"], [])

    def test_non_list_input_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            partners.scope_tenants(object(), "owner")

    def test_report_lists_are_capped(self):
        self.live()
        many = ["t%03d" % i for i in range(200)]
        s = partners.scope_tenants(many, "partner_admin", "ch-alpha")
        self.assertLessEqual(len(s["would_hide"]), partners.SCOPE_MAX_REPORT)
        self.assertLessEqual(len(s["unknown"]), partners.SCOPE_MAX_REPORT)
        self.assertEqual(s["hidden"], 200)          # 개수는 잘리지 않는다

    def test_scope_has_no_side_effects_on_the_ledger(self):
        before = partners.attribution_periods("acme")
        hist = len(partners.history(limit=100))
        partners.scope_tenants(["acme", "selfco"], "partner_admin", "ch-alpha")
        self.assertEqual(partners.attribution_periods("acme"), before)
        self.assertEqual(len(partners.history(limit=100)), hist)


# --------------------------------------------------------------------------
# 1) 조회 경로 배선
# --------------------------------------------------------------------------
class TestReadPathWiring(ScopeBase):
    def setUp(self):
        super(TestReadPathWiring, self).setUp()
        disclosure._clear_for_tests()
        good = ("안녕하세요, {brand} AI 상담원입니다. 상담사 연결도 가능합니다. "
                "무엇을 도와드릴까요?")
        disclosure.set_text("acme", good)
        disclosure.set_text("selfco", good)
        caller_id._clear_for_tests()
        caller_id.register("acme", "02-1234-5678", label="대표")
        caller_id.register("selfco", "02-9876-5432", label="대표")

    def tearDown(self):
        disclosure._clear_for_tests()
        caller_id._clear_for_tests()
        super(TestReadPathWiring, self).tearDown()

    def _tids(self, rows):
        return sorted({r["tenant_id"] for r in rows})

    def test_disclosure_default_is_unchanged(self):
        self.assertEqual(self._tids(disclosure.list_tenants()), ["acme", "selfco"])

    def test_disclosure_scope_is_a_noop_before_approval(self):
        rows = disclosure.list_tenants(role="partner_admin", actor_partner_id="ch-alpha")
        self.assertEqual(self._tids(rows), ["acme", "selfco"])

    def test_disclosure_scope_applies_when_live(self):
        self.live()
        rows = disclosure.list_tenants(role="partner_admin", actor_partner_id="ch-alpha")
        self.assertEqual(self._tids(rows), ["acme"])

    def test_caller_id_default_is_unchanged(self):
        self.assertEqual(self._tids(caller_id.list_numbers()), ["acme", "selfco"])

    def test_caller_id_scope_applies_when_live(self):
        self.live()
        rows = caller_id.list_numbers(role="partner_admin", actor_partner_id="ch-alpha")
        self.assertEqual(self._tids(rows), ["acme"])

    def test_caller_id_scope_composes_with_existing_filters(self):
        self.live()
        rows = caller_id.list_numbers(tenant="selfco", role="partner_admin",
                                      actor_partner_id="ch-alpha")
        self.assertEqual(rows, [])                 # 기존 필터와 스코프가 함께 적용

    def test_read_paths_do_not_reimplement_the_decision(self):
        """판정이 조회 모듈로 복사되지 않았는지 — 소스에 파트너 판정 흔적이 없어야 한다."""
        import io
        for path in ("api/disclosure.py", "api/caller_id.py"):
            src = io.open(os.path.join(ROOT, path), encoding="utf-8").read()
            self.assertNotIn("attribution_at", src, path)
            self.assertNotIn("PARTNER_RBAC_LIVE", src, path)
            self.assertIn("scope_tenants", src, path)

    def test_read_paths_survive_a_broken_partners_module(self):
        """스코프 모듈이 터져도 조회가 죽지 않는다(가용성 우선, 필터는 미적용)."""
        saved = partners.scope_tenants
        partners.scope_tenants = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self.live()
            d = disclosure.list_tenants(role="partner_admin", actor_partner_id="ch-alpha")
            c = caller_id.list_numbers(role="partner_admin", actor_partner_id="ch-alpha")
        finally:
            partners.scope_tenants = saved
        self.assertEqual(self._tids(d), ["acme", "selfco"])
        self.assertEqual(self._tids(c), ["acme", "selfco"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
