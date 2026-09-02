# -*- coding: utf-8 -*-
"""/api/health 확장 검증 — 의존성 상태·버전 노출·민감정보 차단.

실행: python3 tests/test_health.py   (의존성 없음, 표준 unittest)
불변식
  1) 헬스는 어떤 환경에서도 예외를 던지지 않는다.
  2) 키·토큰·DSN "값"은 응답 어디에도 나타나지 않는다.
  3) 기본(shallow) 모드는 네트워크를 사용하지 않는다.
  4) status 는 required 의존성 상태에서 결정론적으로 산출된다.
"""
import os, sys, json, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import health  # noqa: E402


class Env(object):
    """환경변수 임시 설정 컨텍스트."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


BASE = dict(GOOGLE_API_KEY=None, GEMINI_API_KEY=None, ORDER_BACKEND=None,
            ORDER_API_BASE=None, ORDER_API_ALLOW_WRITE=None, SPEECH_LIVE=None,
            CPAAS_LIVE=None, CPAAS_PROVIDER=None, CPAAS_WEBHOOK_TOKEN=None,
            SENTRY_DSN=None, HEALTH_DEEP=None, VERCEL_GIT_COMMIT_SHA=None,
            VERCEL_GIT_COMMIT_REF=None, VERCEL_ENV=None, VERCEL_REGION=None,
            CALLBOT_COMMIT=None, CALLBOT_API_KEY=None, CALLBOT_STRICT=None)


def dep(payload, name):
    for d in payload["dependencies"]:
        if d["name"] == name:
            return d
    raise AssertionError("dependency not found: %s" % name)


class TestShape(unittest.TestCase):
    def test_payload_is_json_serializable(self):
        with Env(**BASE):
            json.dumps(health._payload(), ensure_ascii=False)

    def test_core_keys_present(self):
        with Env(**BASE):
            p = health._payload()
        for k in ("ok", "service", "status", "ts", "version", "dependencies", "checks", "config"):
            self.assertIn(k, p)
        self.assertTrue(p["ok"])

    def test_backward_compatible_keys_kept(self):
        """기존 모니터가 읽던 키(build·config·speech·monitoring)를 깨지 않는다."""
        with Env(**BASE):
            p = health._payload()
        for k in ("build", "config", "speech", "monitoring", "runtime"):
            self.assertIn(k, p)

    def test_dependency_entries_have_contract_fields(self):
        with Env(**BASE):
            p = health._payload()
        self.assertTrue(p["dependencies"])
        for d in p["dependencies"]:
            for k in ("name", "kind", "required", "status", "detail", "checked"):
                self.assertIn(k, d)


class TestVersion(unittest.TestCase):
    def test_commit_from_vercel_env(self):
        with Env(**dict(BASE, VERCEL_GIT_COMMIT_SHA="abcdef1234567890",
                        VERCEL_GIT_COMMIT_REF="main", VERCEL_ENV="production",
                        VERCEL_REGION="icn1")):
            v = health._payload()["version"]
        self.assertEqual(v["commit"], "abcdef1234567890")
        self.assertEqual(v["commit_short"], "abcdef1")
        self.assertEqual(v["branch"], "main")
        self.assertEqual(v["env"], "production")
        self.assertEqual(v["region"], "icn1")

    def test_commit_fallback_and_local_env(self):
        with Env(**dict(BASE, CALLBOT_COMMIT="deadbee")):
            v = health._payload()["version"]
        self.assertEqual(v["commit_short"], "deadbee")
        self.assertEqual(v["env"], "local")

    def test_no_commit_is_none_not_crash(self):
        with Env(**BASE):
            v = health._payload()["version"]
        self.assertIsNone(v["commit"])
        self.assertIsNone(v["commit_short"])


class TestDependencies(unittest.TestCase):
    def test_llm_missing_key_degrades(self):
        with Env(**BASE):
            p = health._payload()
        self.assertEqual(dep(p, "llm")["status"], health.NOT_CONFIGURED)
        self.assertEqual(p["status"], "degraded")

    def test_llm_key_present_is_ok_and_healthy(self):
        with Env(**dict(BASE, GOOGLE_API_KEY="k-secret-value")):
            p = health._payload()
        self.assertEqual(dep(p, "llm")["status"], health.OK)
        self.assertEqual(p["status"], "healthy")

    def test_order_demo_is_simulated_not_failure(self):
        with Env(**dict(BASE, GOOGLE_API_KEY="k")):
            p = health._payload()
        self.assertEqual(dep(p, "order_backend")["status"], health.SIMULATED)
        self.assertEqual(p["status"], "healthy")

    def test_order_http_without_base_is_misconfigured(self):
        with Env(**dict(BASE, GOOGLE_API_KEY="k", ORDER_BACKEND="http")):
            p = health._payload()
        self.assertEqual(dep(p, "order_backend")["status"], health.MISCONFIGURED)
        self.assertEqual(p["status"], "degraded")

    def test_order_http_write_gate_reflected(self):
        with Env(**dict(BASE, GOOGLE_API_KEY="k", ORDER_BACKEND="http",
                        ORDER_API_BASE="https://api.example.com")):
            d = dep(health._payload(), "order_backend")
        self.assertEqual(d["status"], health.OK)
        self.assertIn("dry-run", d["detail"])

    def test_cpaas_off_is_simulated(self):
        with Env(**BASE):
            d = dep(health._payload(), "cpaas")
        self.assertEqual(d["status"], health.SIMULATED)
        self.assertFalse(d["required"])

    def test_speech_off_is_simulated(self):
        with Env(**BASE):
            d = dep(health._payload(), "speech")
        self.assertEqual(d["status"], health.SIMULATED)

    def test_storage_flagged_as_ephemeral(self):
        with Env(**BASE):
            d = dep(health._payload(), "storage")
        self.assertEqual(d["status"], health.SIMULATED)
        self.assertIn("[승인 필요]", d["detail"])

    def test_monitoring_absent_is_not_configured_but_not_required(self):
        with Env(**BASE):
            d = dep(health._payload(), "monitoring")
        self.assertEqual(d["status"], health.NOT_CONFIGURED)
        self.assertFalse(d["required"])


class TestOverall(unittest.TestCase):
    def test_error_in_required_is_unhealthy(self):
        deps = [health._dep("llm", "external_api", True, health.ERROR, "x")]
        self.assertEqual(health._overall(deps), "unhealthy")

    def test_error_in_optional_is_degraded(self):
        deps = [health._dep("m", "observability", False, health.ERROR, "x")]
        self.assertEqual(health._overall(deps), "degraded")

    def test_all_sim_optional_is_healthy(self):
        deps = [health._dep("llm", "external_api", True, health.OK, "x"),
                health._dep("cpaas", "external_api", False, health.SIMULATED, "x")]
        self.assertEqual(health._overall(deps), "healthy")


class TestSecrecy(unittest.TestCase):
    SECRETS = {
        "GOOGLE_API_KEY": "AIza-SECRET-abcdefg",
        "CALLBOT_API_KEY": "apikey-SECRET-1234",
        "CPAAS_WEBHOOK_TOKEN": "tok-SECRET-9999",
        "SENTRY_DSN": "https://pub-SECRET@o1.ingest.sentry.io/42",
        "ORDER_API_BASE": "https://user:pw-SECRET@orders.example.com/v1",
    }

    def test_no_secret_values_in_payload(self):
        with Env(**dict(BASE, ORDER_BACKEND="http", **self.SECRETS)):
            body = json.dumps(health._payload(), ensure_ascii=False)
        for v in self.SECRETS.values():
            self.assertNotIn(v, body)
        self.assertNotIn("SECRET", body)

    def test_host_extraction_drops_credentials(self):
        self.assertEqual(health._host_of("https://user:pw@orders.example.com:8443/v1"),
                         "orders.example.com")
        self.assertIsNone(health._host_of(""))


class TestNoSideEffects(unittest.TestCase):
    def test_shallow_mode_makes_no_socket_connection(self):
        calls = []
        orig = health.socket.create_connection
        health.socket.create_connection = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError())
        try:
            with Env(**dict(BASE, GOOGLE_API_KEY="k", ORDER_BACKEND="http",
                            ORDER_API_BASE="https://orders.example.com")):
                p = health._payload()
        finally:
            health.socket.create_connection = orig
        self.assertEqual(calls, [])
        self.assertEqual(p["checks"]["mode"], "shallow")
        self.assertFalse(any(d["checked"] for d in p["dependencies"]))

    def test_deep_requires_both_env_and_query(self):
        with Env(**dict(BASE, HEALTH_DEEP=None)):
            self.assertFalse(health._deep_allowed("deep=1"))
        with Env(**dict(BASE, HEALTH_DEEP="1")):
            self.assertFalse(health._deep_allowed(""))
            self.assertTrue(health._deep_allowed("deep=1"))

    def test_deep_mode_uses_tcp_only_and_records_latency(self):
        seen = []

        class _S:
            def close(self):
                pass

        orig = health.socket.create_connection
        health.socket.create_connection = lambda addr, timeout=None: seen.append(addr) or _S()
        try:
            with Env(**dict(BASE, HEALTH_DEEP="1", GOOGLE_API_KEY="k")):
                p = health._payload("deep=1")
        finally:
            health.socket.create_connection = orig
        self.assertEqual(p["checks"]["mode"], "deep")
        self.assertIn(("generativelanguage.googleapis.com", 443), seen)
        llm = dep(p, "llm")
        self.assertTrue(llm["checked"])
        self.assertIn("latency_ms", llm)

    def test_deep_unreachable_marks_error_not_crash(self):
        orig = health.socket.create_connection
        health.socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            with Env(**dict(BASE, HEALTH_DEEP="1", GOOGLE_API_KEY="k")):
                p = health._payload("deep=1")
        finally:
            health.socket.create_connection = orig
        self.assertEqual(dep(p, "llm")["status"], health.ERROR)
        self.assertEqual(p["status"], "unhealthy")

    def test_dependency_check_exception_is_isolated(self):
        orig = health._dep_cpaas
        health._dep_cpaas = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            with Env(**dict(BASE, GOOGLE_API_KEY="k")):
                p = health._payload()
        finally:
            health._dep_cpaas = orig
        self.assertEqual(p["status"], "degraded")   # 격리되어 500이 아니라 degraded
        self.assertTrue(any(d["status"] == health.ERROR for d in p["dependencies"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
