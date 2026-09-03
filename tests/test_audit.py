# -*- coding: utf-8 -*-
"""api/_audit.py 접근·감사 로그 불변식 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS '접근·감사 로그 — 관리 기능 접근 이력')
  1) 기록 대상 — 관리 경로만 감사 스트림에 남고, 일반 경로는 남지 않는다
  2) 거부 기록 — 실패한 접근 시도(403/401/429)도 반드시 남는다
  3) PII·비밀값 — 원문 IP·API 키·웹훅 토큰·쿼리스트링이 기록에 남지 않는다
  4) 호출자 식별 — 자격 종류(api_key/origin/webhook/anonymous)를 구분한다
  5) append-only — 수정·삭제 API 가 없고, 버퍼 유실은 evicted 로 드러난다
  6) 가용성 — 감사 실패가 예외로 새어나가 요청을 죽이지 않는다
  7) 배선 — ops_stats·voice 라우트가 허용/거부 양쪽을 기록한다
  8) 노출 — /health 가 개수 요약만 노출하고 개별 이벤트를 노출하지 않는다

실행: python3 tests/test_audit.py
"""
import io
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _audit  # noqa: E402


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


def H(**kw):
    return FakeHeaders({k.replace("_", "-"): v for k, v in kw.items()})


class AuditBase(unittest.TestCase):
    ENV_KEYS = ("CALLBOT_AUDIT", "CALLBOT_AUDIT_BUFFER", "CALLBOT_AUDIT_SALT",
                "CALLBOT_API_KEY", "CALLBOT_LOG")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ["CALLBOT_LOG"] = "off"    # 테스트 중 stdout 소음 제거
        _audit.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _audit.reset()


class TestScope(AuditBase):
    """1) 무엇을 남기는가 — 관리 기능만"""

    def test_admin_paths_are_classified(self):
        self.assertEqual(_audit.action_for("/api/ops_stats"), "ops.stats.read")
        self.assertEqual(_audit.action_for("/api/voice"), "call.webhook")
        self.assertEqual(_audit.action_for("/api/health"), "ops.health.deep")

    def test_query_string_does_not_break_classification(self):
        self.assertEqual(_audit.action_for("/api/ops_stats?period=week"), "ops.stats.read")

    def test_non_admin_paths_are_not_audited(self):
        for p in ("/api/chat", "/api/stt", "/api/tts", "/api/assist", "/"):
            self.assertIsNone(_audit.action_for(p), p)
            self.assertIsNone(_audit.record_request(H(), p, "POST", "allow", 200), p)

    def test_admin_request_is_recorded(self):
        r = _audit.record_request(H(origin="https://callbot-portal.vercel.app"),
                                  "/api/ops_stats", "GET", "allow", 200)
        self.assertEqual(r["action"], "ops.stats.read")
        self.assertEqual(r["method"], "GET")
        self.assertEqual(_audit.counters()["allow"], 1)


class TestDenials(AuditBase):
    """2) 실패한 접근 시도도 남는다"""

    def test_deny_is_recorded_with_status(self):
        for code in (401, 403, 429):
            _audit.reset()
            r = _audit.record_request(H(), "/api/ops_stats", "GET", "deny", code)
            self.assertEqual(r["result"], "deny")
            self.assertEqual(r["status"], code)
            self.assertEqual(_audit.counters()["deny"], 1)

    def test_unknown_result_is_normalized_to_error(self):
        r = _audit.record(H(), "ops.stats.read", "whatever", status=500)
        self.assertEqual(r["result"], "error")


class TestPrivacy(AuditBase):
    """3) PII·비밀값이 남지 않는다 — 감사 로그 자체가 유출원이 되면 안 된다"""

    def _dump(self, rec):
        return json.dumps(rec, ensure_ascii=False)

    def test_raw_ip_is_never_stored(self):
        r = _audit.record(H(x_forwarded_for="203.0.113.9, 10.0.0.1"), "ops.stats.read")
        self.assertNotIn("203.0.113.9", self._dump(r))
        self.assertNotIn("10.0.0.1", self._dump(r))
        self.assertTrue(r["client"] and r["client"] != "-")

    def test_same_ip_hashes_consistently_within_process(self):
        a = _audit.record(H(x_forwarded_for="203.0.113.9"), "ops.stats.read")
        b = _audit.record(H(x_forwarded_for="203.0.113.9"), "ops.stats.read")
        c = _audit.record(H(x_forwarded_for="198.51.100.1"), "ops.stats.read")
        self.assertEqual(a["client"], b["client"])
        self.assertNotEqual(a["client"], c["client"])

    def test_api_key_value_is_not_stored(self):
        os.environ["CALLBOT_API_KEY"] = "supersecret-value"
        r = _audit.record(H(x_api_key="supersecret-value"), "ops.stats.read")
        self.assertNotIn("supersecret-value", self._dump(r))
        self.assertEqual(r["actor"]["type"], "api_key")
        self.assertEqual(len(r["actor"]["id"]), 8)

    def test_webhook_token_value_is_not_stored(self):
        r = _audit.record(H(x_webhook_token="tok-abc-123"), "call.webhook")
        self.assertNotIn("tok-abc-123", self._dump(r))
        self.assertEqual(r["actor"]["type"], "webhook")

    def test_query_string_is_stripped_from_path(self):
        r = _audit.record_request(H(), "/api/voice?t=secret-token&phone=010-1234-5678",
                                  "POST", "allow", 200)
        self.assertNotIn("secret-token", self._dump(r))
        self.assertNotIn("010-1234-5678", self._dump(r))
        self.assertEqual(r["path"], "/api/voice")

    def test_user_agent_is_reduced_to_family(self):
        r = _audit.record(H(user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0.0.0"),
                          "ops.stats.read")
        self.assertEqual(r["ua"], "browser")
        self.assertNotIn("Windows NT", self._dump(r))
        self.assertEqual(_audit.record(H(user_agent="curl/8.4"), "x")["ua"], "cli")
        self.assertEqual(_audit.record(H(), "x")["ua"], "none")

    def test_extra_fields_are_length_capped(self):
        r = _audit.record(H(), "ops.stats.read", target="T" * 5000, note="N" * 5000)
        self.assertLessEqual(len(r["target"]), _audit.MAX_FIELD)
        self.assertLessEqual(len(r["extra"]["note"]), _audit.MAX_FIELD)


class TestActor(AuditBase):
    """4) 누가 접근했는지 — 자격의 '종류'를 구분한다"""

    def test_actor_types(self):
        os.environ["CALLBOT_API_KEY"] = "k1"
        self.assertEqual(_audit.actor(H(x_api_key="k1"))["type"], "api_key")
        self.assertEqual(_audit.actor(H(x_api_key="wrong"))["type"], "api_key_invalid")
        self.assertEqual(_audit.actor(H(x_webhook_token="t"))["type"], "webhook")
        self.assertEqual(_audit.actor(H(origin="https://x.example"))["type"], "origin")
        self.assertEqual(_audit.actor(H(sec_fetch_site="same-origin"))["type"], "origin")
        self.assertEqual(_audit.actor(H())["type"], "anonymous")

    def test_invalid_key_attempt_is_distinguishable(self):
        """틀린 키로 두드린 흔적은 침해 조사에서 중요하다 — 지문으로 반복 시도를 묶는다."""
        os.environ["CALLBOT_API_KEY"] = "k1"
        a = _audit.record(H(x_api_key="guess1"), "ops.stats.read", "deny", status=403)
        b = _audit.record(H(x_api_key="guess1"), "ops.stats.read", "deny", status=403)
        self.assertEqual(a["actor"]["id"], b["actor"]["id"])
        self.assertNotIn("guess1", json.dumps([a, b], ensure_ascii=False))


class TestAppendOnly(AuditBase):
    """5) append-only — 임의 삭제 경로가 없고 유실은 드러난다"""

    def test_no_mutation_api(self):
        with io.open(os.path.join(ROOT, "api", "_audit.py"), encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("def delete", "def remove", "def update", "def edit"):
            self.assertNotIn(banned, src, banned)

    def test_recent_returns_copies(self):
        _audit.record(H(), "ops.stats.read")
        got = _audit.recent(10)
        got[0]["action"] = "TAMPERED"
        self.assertEqual(_audit.recent(10)[0]["action"], "ops.stats.read")

    def test_buffer_is_bounded_and_eviction_is_counted(self):
        os.environ["CALLBOT_AUDIT_BUFFER"] = "3"
        _audit.reset()
        for i in range(10):
            _audit.record(H(), "ops.stats.read", request_id="r%d" % i)
        self.assertEqual(len(_audit.recent(100)), 3)
        self.assertEqual(_audit.counters()["evicted"], 7)
        self.assertEqual(_audit.counters()["total"], 10)

    def test_buffer_hard_max(self):
        os.environ["CALLBOT_AUDIT_BUFFER"] = "999999"
        self.assertEqual(_audit.buffer_size(), _audit.BUFFER_HARD_MAX)
        os.environ["CALLBOT_AUDIT_BUFFER"] = "not-a-number"
        self.assertEqual(_audit.buffer_size(), _audit.DEFAULT_BUFFER)

    def test_order_is_chronological(self):
        for i in range(3):
            _audit.record(H(), "ops.stats.read", request_id="r%d" % i)
        self.assertEqual([r["request_id"] for r in _audit.recent(3)], ["r0", "r1", "r2"])


class TestAvailability(AuditBase):
    """6) 감사 실패가 서비스 장애가 되지 않는다"""

    def test_broken_headers_do_not_raise(self):
        class Boom(object):
            def get(self, *a, **k):
                raise RuntimeError("boom")
        r = _audit.record(Boom(), "ops.stats.read")
        self.assertIsNotNone(r)                     # 기록은 남되
        self.assertEqual(r["actor"]["type"], "unknown")   # 식별만 포기한다

    def test_emit_failure_is_swallowed(self):
        orig = _audit._emit

        def boom(rec):
            raise RuntimeError("sink down")
        _audit._emit = boom
        try:
            self.assertIsNone(_audit.record(H(), "ops.stats.read"))
        finally:
            _audit._emit = orig

    def test_off_switch(self):
        os.environ["CALLBOT_AUDIT"] = "off"
        self.assertFalse(_audit.enabled())
        self.assertIsNone(_audit.record(H(), "ops.stats.read"))
        self.assertFalse(_audit.snapshot()["enabled"])


class TestWiring(AuditBase):
    """7) 라우트 배선 — 코드에 실제로 연결돼 있는가"""

    def _src(self, name):
        with io.open(os.path.join(ROOT, "api", name), encoding="utf-8") as fh:
            return fh.read()

    def test_ops_stats_records_allow_and_deny(self):
        s = self._src("ops_stats.py")
        self.assertIn("import _audit", s)
        self.assertIn('"deny"', s)
        self.assertIn('"allow"', s)
        self.assertGreaterEqual(s.count("_audit.record_request("), 2)

    def test_voice_records_allow_and_deny(self):
        s = self._src("voice.py")
        self.assertIn("_audit_ev(", s)
        self.assertGreaterEqual(s.count("_audit_ev(self.headers"), 4)

    def test_health_records_only_deep_checks(self):
        s = self._src("health.py")
        self.assertIn("ops.health.deep", s)
        self.assertIn('== "deep"', s)


class TestHealthExposure(AuditBase):
    """8) /health 노출 — 요약만, 개별 이벤트·식별자는 금지"""

    def test_snapshot_has_no_individual_events(self):
        _audit.record(H(x_forwarded_for="203.0.113.9"), "ops.stats.read",
                      request_id="rq-secret")
        s = _audit.snapshot()
        dump = json.dumps(s, ensure_ascii=False)
        self.assertNotIn("rq-secret", dump)
        self.assertNotIn("203.0.113.9", dump)
        self.assertEqual(s["buffered"], 1)
        self.assertEqual(s["counts"]["allow"], 1)

    def test_health_payload_includes_audit_dependency(self):
        import health
        p = health._payload("")
        self.assertIn("audit", p)
        self.assertTrue(p["audit"]["enabled"])
        names = [d["name"] for d in p["dependencies"]]
        self.assertIn("audit", names)
        dep = [d for d in p["dependencies"] if d["name"] == "audit"][0]
        self.assertFalse(dep["required"])          # 감사 부재로 서비스를 죽이지 않는다
        json.dumps(p, ensure_ascii=False)          # 직렬화 가능

    def test_snapshot_is_json_serializable(self):
        json.dumps(_audit.snapshot(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
