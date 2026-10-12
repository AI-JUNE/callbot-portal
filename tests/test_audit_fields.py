# -*- coding: utf-8 -*-
"""감사 스트림 등록부 회귀 — 27차. 의존성 0 · 네트워크 미사용.

왜 이 파일이 필요한가
  `caller_id`·`partners`·`settlement`·`wellbeing` 은 감사 기록을
  allow/deny/error 세 자리에 모두 배선해 두었는데 `_audit.ADMIN_ACTIONS` 에
  경로가 없어서 **아무것도 남지 않았다**(`action_for` 가 None 이면
  `record_request` 는 조용히 None 으로 끝난다). 기존 회귀는 전부 **가짜 감사
  모듈**(`_CountingAudit`)을 끼워 "호출됐는가"만 확인했기 때문에, 호출은 되는데
  남지 않는 상태에서도 전부 초록불이었다 — 그래서 여기서는 **실제 `_audit`
  모듈**을 태운다.

여기서 고정하는 것
  1) 감사를 배선한 라우트는 실제로 기록을 남긴다(허용·거부·오류 모두).
  2) 등록부는 라우트 단위이고 하위경로(`/api/wellbeing/call`)도 같은 액션이다.
     단, 세그먼트 경계만 자른다 — `/api/wellbeingX` 는 관리 경로가 아니다.
  3) 보조 필드(extra)는 `_audit.FIELDS` 등록부 안의 칸만 남고, 밖의 칸은
     기록되지 않고 `extra_dropped` 로 **센다**(PII 유입 경로를 닫되 조용하지 않게).
  4) 두 스트림(요청 로그 `_log.FIELDS` ↔ 감사 `_audit.FIELDS`)의 **같은 이름은
     같은 뜻**이다 — 다르면 "그 요청의 감사 기록은 어디냐"가 어긋난다.

실행: python3 -m pytest tests/test_audit_fields.py -q
"""
import io
import os
import re
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _audit  # noqa: E402
import _log    # noqa: E402

API_DIR = os.path.join(ROOT, "api")


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


def H(**kw):
    return FakeHeaders({k.replace("_", "-"): v for k, v in kw.items()})


def api_sources():
    return {f: io.open(os.path.join(API_DIR, f), encoding="utf-8").read()
            for f in sorted(os.listdir(API_DIR)) if f.endswith(".py")}


class Base(unittest.TestCase):
    ENV = ("CALLBOT_AUDIT", "CALLBOT_AUDIT_BUFFER", "CALLBOT_LOG")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        os.environ["CALLBOT_LOG"] = "off"      # stdout 소음 제거
        _audit.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _audit.reset()


# ==========================================================================
# 1) 배선한 라우트가 실제로 남는가 (실제 _audit 모듈)
# ==========================================================================
class TestAdminRoutes(Base):

    # 감사를 배선한 라우트 — 넷 다 "누가 바꿨나"가 분쟁 근거인 장부다.
    LEDGERS = ("/api/caller_id", "/api/partners", "/api/settlement",
               "/api/wellbeing", "/api/disclosure")

    def test_ledger_routes_are_classified(self):
        for p in self.LEDGERS:
            self.assertIsNotNone(_audit.action_for(p), p)

    def test_ledger_routes_actually_record(self):
        """등록부에 없으면 이 호출은 조용히 None 으로 끝났다(27차 결함)."""
        for p in self.LEDGERS:
            _audit.reset()
            rec = _audit.record_request(H(), p, "POST", "allow", 200,
                                        request_id="rq-1")
            self.assertIsNotNone(rec, p)
            self.assertEqual(rec["path"], p)
            self.assertEqual(_audit.counters()["allow"], 1, p)

    def test_denied_and_failed_ledger_access_is_recorded(self):
        """침해 조사에서는 실패한 시도가 더 중요하다."""
        for p in self.LEDGERS:
            for result, status in (("deny", 403), ("deny", 429), ("error", 500)):
                _audit.reset()
                rec = _audit.record_request(H(), p, "POST", result, status)
                self.assertIsNotNone(rec, "%s %s" % (p, result))
                self.assertEqual(rec["result"], result)
                self.assertEqual(rec["status"], status)

    def test_subpath_resolves_to_the_route_action(self):
        """`/api/wellbeing/call` — Vercel 은 `?op=` 로 rewrite 하지만 로컬·직접
        호출에서는 하위경로가 그대로 온다."""
        self.assertEqual(_audit.action_for("/api/wellbeing/call"),
                         _audit.action_for("/api/wellbeing"))
        rec = _audit.record_request(H(), "/api/wellbeing/call?x=1", "POST", "allow", 200)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["path"], "/api/wellbeing/call")   # 쿼리는 잘린다

    def test_prefix_lookalike_is_not_an_admin_route(self):
        """세그먼트 경계만 자른다 — 접두어가 같은 남의 경로를 끌어오지 않는다."""
        for p in ("/api/wellbeingX", "/api/partnersale", "/api/caller_idx"):
            self.assertIsNone(_audit.action_for(p), p)

    def test_normal_traffic_is_still_not_audited(self):
        """감사 스트림이 일반 트래픽에 묻히면 조사 가치가 떨어진다."""
        for p in ("/api/chat", "/api/stt", "/api/tts", "/api/assist",
                  "/api/sim_call", "/api/chat/anything", "/"):
            self.assertIsNone(_audit.action_for(p), p)
            self.assertIsNone(_audit.record_request(H(), p, "POST", "allow", 200), p)

    def test_action_names_are_stable_tokens(self):
        rx = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
        for path, act in _audit.ADMIN_ACTIONS.items():
            self.assertRegex(act, rx, path)

    def test_registry_matches_the_wiring_in_both_directions(self):
        """등록부와 코드가 어긋나면 어느 한쪽이 거짓말을 한다."""
        src = api_sources()
        wired = set()
        for name, text in src.items():
            if name.startswith("_") or "class handler" not in text:
                continue
            code = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith("#"))
            if "_audit.record(" in code or "_audit.record_request(" in code:
                wired.add("/api/" + name[:-3])
        self.assertEqual(sorted(wired - set(_audit.ADMIN_ACTIONS)), [])
        self.assertEqual(sorted(set(_audit.ADMIN_ACTIONS) - wired), [])


# ==========================================================================
# 2) 보조 필드 등록부
# ==========================================================================
class TestFieldRegistry(Base):

    def test_registered_field_is_recorded(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200, period="week")
        self.assertEqual(rec["extra"]["period"], "week")
        self.assertEqual(_audit.counters()["extra_dropped"], 0)

    def test_unregistered_field_is_dropped_and_counted(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200,
                            senior_id="SR-1", note="x")
        self.assertNotIn("extra", rec)
        self.assertEqual(_audit.counters()["extra_dropped"], 2)

    def test_dropping_does_not_lose_the_record_itself(self):
        """칸 하나 때문에 감사 기록 한 건을 잃지 않는다(`_log.emit` 과 같은 판단)."""
        rec = _audit.record(H(), "ops.stats.read", "deny", status=403, whatever=1)
        self.assertEqual(rec["result"], "deny")
        self.assertEqual(_audit.counters()["deny"], 1)

    def test_unregistered_field_cannot_smuggle_pii_into_the_stream(self):
        rec = _audit.record(H(), "call.webhook", "allow", status=200,
                            caller="010-1234-5678")
        self.assertNotIn("010-1234-5678", json.dumps(rec, ensure_ascii=False))

    def test_none_values_are_not_counted_as_drops(self):
        """'값 없음'은 규약 위반이 아니다 — 카운터가 거짓 경보를 내면 안 본다."""
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200, period=None)
        self.assertNotIn("extra", rec)
        self.assertEqual(_audit.counters()["extra_dropped"], 0)

    def test_drop_counter_is_visible_in_health_summary(self):
        _audit.record(H(), "ops.stats.read", "allow", status=200, nope=1)
        self.assertEqual(_audit.snapshot()["counts"]["extra_dropped"], 1)

    def test_registry_has_no_stale_entries(self):
        """아무도 안 쓰는 칸이 남아 있으면 등록부가 '현실'이 아니다."""
        src = api_sources()
        for field in _audit.FIELDS:
            used = any(("%s=" % field) in text
                       for name, text in src.items() if not name.startswith("_"))
            self.assertTrue(used, "등록부에만 있는 칸: %s" % field)

    def test_names_follow_the_log_convention(self):
        rx = re.compile(r"^[a-z][a-z0-9_]*$")
        for k, why in _audit.FIELDS.items():
            self.assertRegex(k, rx, k)
            self.assertLessEqual(len(k), _log.MAX_KEY, k)
            self.assertFalse(k.startswith("is_"), k)
            self.assertTrue(isinstance(why, str) and len(why) >= 4, k)

    def test_shared_names_mean_the_same_thing_in_both_streams(self):
        """두 스트림이 같은 이름에 다른 뜻을 담으면 상관 분석이 어긋난다."""
        shared = set(_audit.FIELDS) & set(_log.FIELDS)
        self.assertTrue(shared)            # 겹치는 칸이 하나도 없으면 규약이 아니다
        for k in sorted(shared):
            self.assertEqual(_audit.FIELDS[k], _log.FIELDS[k], k)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
