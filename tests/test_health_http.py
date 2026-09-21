# -*- coding: utf-8 -*-
"""api/health.py 잔여 분기 회귀 테스트 — HTTP 계약·의존성 분기·장애 격리.

`tests/test_health.py` 가 페이로드 산출(_payload)을 덮는다면, 이 파일은 그 바깥
껍질과 예외 경로를 덮는다. /health 는 **무인증·공개** 엔드포인트라 다른 라우트와
위험의 성질이 다르다: 여기서 새는 한 줄은 누구나 읽을 수 있다.

검증 대상
  1) HTTP 계약 — GET 200 · JSON · no-store · CORS, OPTIONS 204, HEAD 200
  2) 500 폴백 — 헬스가 터져도 status 키 의미를 지키고 내부 문구를 노출하지 않는다
  3) 감사 — deep 점검만 기록하고 shallow 대량 호출은 기록하지 않는다
  4) 의존성 분기 — order(http/deep)·speech·cpaas(live)·ratelimit·audit 미커버 분기
  5) 장애 격리 — 하위 모듈 import/호출 실패가 헬스를 죽이지 않고, **예외 문구를
     응답에 싣지 않는다**(타입명만)
  6) 부작용 없음 — 조회가 게이트를 켜지 않고, shallow 는 소켓을 열지 않는다

실행: python3 -m pytest tests/test_health_http.py -q
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import health  # noqa: E402


# --------------------------------------------------------------------------
# 최소 핸들러 대역 (소켓 없이 do_GET/do_HEAD/do_OPTIONS 를 돌린다)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class Resp(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = io.BytesIO()

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    def raw(self):
        return self.wfile.getvalue().decode("utf-8")


def call(method="GET", path="/api/health", headers=None):
    r = Resp()
    inst = health.handler.__new__(health.handler)
    inst.headers = FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
    inst.path = path
    inst.wfile = r.wfile
    inst.rfile = io.BytesIO(b"")
    inst.send_response = lambda c: setattr(r, "status", c)
    inst.send_header = lambda k, v: r.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    getattr(inst, "do_" + method)()
    return r


ENV = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ORDER_BACKEND", "ORDER_API_BASE",
       "ORDER_API_ALLOW_WRITE", "SPEECH_LIVE", "CALLBOT_STT_PROVIDER",
       "CALLBOT_TTS_PROVIDER", "CPAAS_LIVE", "CPAAS_PROVIDER",
       "CPAAS_WEBHOOK_TOKEN", "SENTRY_DSN", "HEALTH_DEEP", "CALLBOT_API_KEY",
       "CALLBOT_STRICT", "CALLBOT_RATE_LIMIT_OFF", "CALLBOT_AUDIT",
       "VERCEL_GIT_COMMIT_SHA", "VERCEL_GIT_COMMIT_REF", "VERCEL_ENV",
       "VERCEL_REGION", "CALLBOT_COMMIT")


class Base(unittest.TestCase):
    """환경변수를 매 테스트마다 비운다 — 다른 테스트 파일의 잔재에 의존하지 않는다."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        self._restore = []
        self._modules = []
        # 어떤 테스트도 실제 소켓을 열지 못하게 막는다(과금·외부 접속 0).
        self._sock = health.socket.create_connection
        health.socket.create_connection = self._no_socket

    def tearDown(self):
        health.socket.create_connection = self._sock
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        for modname, old in reversed(self._modules):
            if old is None:
                sys.modules.pop(modname, None)
            else:
                sys.modules[modname] = old
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _no_socket(*a, **k):
        raise AssertionError("이 테스트는 네트워크를 사용하면 안 된다: %r" % (a,))

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def allow_socket(self, fn):
        health.socket.create_connection = fn


# ==========================================================================
# 1) HTTP 계약
# ==========================================================================
class TestHttpContract(Base):
    def test_get_returns_200_json_payload(self):
        r = call("GET")
        self.assertEqual(r.status, 200)
        body = r.body()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "callbot-portal")
        self.assertIn(body["status"], ("healthy", "degraded", "unhealthy"))

    def test_get_sets_no_store_and_content_type(self):
        r = call("GET")
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertIn("application/json", r.header("Content-Type"))
        self.assertIn("charset=utf-8", r.header("Content-Type"))

    def test_content_length_matches_body_bytes(self):
        """모니터가 Content-Length 로 본문을 자르지 않도록 바이트 길이가 맞아야 한다."""
        r = call("GET")
        self.assertEqual(int(r.header("Content-Length")),
                         len(r.wfile.getvalue()))

    def test_cors_is_open_for_uptime_monitors(self):
        """무인증 헬스는 어디서든 읽을 수 있어야 한다 — 되비침이 아니라 고정 '*'."""
        r = call("GET", headers={"origin": "https://evil.example.com"})
        self.assertEqual(r.header("Access-Control-Allow-Origin"), "*")

    def test_query_parsing_without_question_mark(self):
        r = call("GET", path="/api/health")
        self.assertEqual(r.body()["checks"]["mode"], "shallow")

    def test_unknown_query_is_ignored_not_error(self):
        r = call("GET", path="/api/health?foo=bar&deep=0")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["checks"]["mode"], "shallow")

    def test_options_preflight_204(self):
        r = call("OPTIONS")
        self.assertEqual(r.status, 204)
        self.assertEqual(r.header("Access-Control-Allow-Origin"), "*")

    def test_head_returns_200_without_body(self):
        r = call("HEAD")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertEqual(r.wfile.getvalue(), b"")

    def test_get_is_read_only_for_gates(self):
        """조회가 실발신·실합성 스위치를 켜지 않는다."""
        call("GET")
        self.assertIsNone(os.environ.get("CPAAS_LIVE"))
        self.assertIsNone(os.environ.get("SPEECH_LIVE"))
        self.assertIsNone(os.environ.get("HEALTH_DEEP"))

    def test_shallow_get_opens_no_socket(self):
        os.environ["GOOGLE_API_KEY"] = "k"
        os.environ["ORDER_BACKEND"] = "http"
        os.environ["ORDER_API_BASE"] = "https://orders.example.com"
        r = call("GET")   # setUp 의 _no_socket 이 열리면 AssertionError 로 터진다
        self.assertEqual(r.status, 200)


# ==========================================================================
# 2) 500 폴백 — 헬스가 터져도 계약을 지킨다
# ==========================================================================
class TestFailureEnvelope(Base):
    def test_payload_exception_returns_500_envelope(self):
        self.patch(health, "_payload",
                   lambda q="": (_ for _ in ()).throw(RuntimeError("DSN=https://k@sentry.io/1")))
        r = call("GET")
        self.assertEqual(r.status, 500)
        body = r.body()
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "error")     # HTTP 코드가 아니라 헬스 등급
        self.assertEqual(body["code"], "INTERNAL_ERROR")
        self.assertTrue(body["error"])

    def test_failure_body_hides_internal_message(self):
        self.patch(health, "_payload",
                   lambda q="": (_ for _ in ()).throw(RuntimeError("DSN=https://k@sentry.io/1")))
        raw = call("GET").raw()
        for leak in ("sentry.io", "DSN", "RuntimeError", "Traceback"):
            self.assertNotIn(leak, raw)

    def test_failure_still_marks_no_store(self):
        self.patch(health, "_payload", lambda q="": (_ for _ in ()).throw(RuntimeError("x")))
        r = call("GET")
        self.assertEqual(r.header("Cache-Control"), "no-store")


# ==========================================================================
# 3) 감사 — deep 만 기록한다
# ==========================================================================
class TestAuditRecording(Base):
    def _spy(self):
        import _audit
        seen = []
        self.patch(_audit, "record",
                   lambda h, a, res, **kw: seen.append((a, res, kw.get("status"))))
        return seen

    def test_shallow_get_is_not_audited(self):
        seen = self._spy()
        call("GET")
        self.assertEqual(seen, [])

    def test_deep_get_is_audited(self):
        seen = self._spy()
        os.environ["HEALTH_DEEP"] = "1"
        self.allow_socket(lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable")))
        r = call("GET", path="/api/health?deep=1")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["checks"]["mode"], "deep")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "ops.health.deep")
        self.assertEqual(seen[0][1], "allow")
        self.assertEqual(seen[0][2], 200)

    def test_audit_failure_does_not_break_health(self):
        """감사 기록 실패가 헬스 응답을 죽이면 안 된다(가용성 우선)."""
        import _audit
        self.patch(_audit, "record",
                   lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("audit down")))
        os.environ["HEALTH_DEEP"] = "1"
        self.allow_socket(lambda *a, **k: (_ for _ in ()).throw(OSError()))
        r = call("GET", path="/api/health?deep=1")
        self.assertEqual(r.status, 200)


# ==========================================================================
# 4) 의존성 분기
# ==========================================================================
class TestDependencyBranches(Base):
    def test_order_http_deep_probes_backend_host_only(self):
        seen = []

        class _S:
            def close(self):
                pass

        self.allow_socket(lambda addr, timeout=None: seen.append(addr) or _S())
        os.environ.update(HEALTH_DEEP="1", GOOGLE_API_KEY="k", ORDER_BACKEND="http",
                          ORDER_API_BASE="https://user:pw@orders.example.com/v1/orders?token=t")
        p = health._payload("deep=1")
        d = [x for x in p["dependencies"] if x["name"] == "order_backend"][0]
        self.assertTrue(d["checked"])
        self.assertIn("latency_ms", d)
        self.assertEqual(d["status"], health.OK)
        self.assertIn(("orders.example.com", 443), seen)
        # 자격증명·경로·토큰이 도달성 점검에도 응답에도 실리지 않는다
        self.assertNotIn(("user:pw@orders.example.com", 443), seen)
        for leak in ("pw", "token=t", "/v1/orders"):
            self.assertNotIn(leak, json.dumps(p, ensure_ascii=False))

    def test_order_http_deep_unreachable_is_error(self):
        self.allow_socket(lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
        os.environ.update(HEALTH_DEEP="1", GOOGLE_API_KEY="k", ORDER_BACKEND="http",
                          ORDER_API_BASE="https://orders.example.com")
        p = health._payload("deep=1")
        d = [x for x in p["dependencies"] if x["name"] == "order_backend"][0]
        self.assertEqual(d["status"], health.ERROR)
        self.assertEqual(p["status"], "unhealthy")     # required 의존성 error

    def test_order_http_deep_unparseable_base_skips_probe(self):
        """호스트를 못 뽑으면 도달성 점검을 건너뛰되 상태를 꾸미지 않는다."""
        os.environ.update(HEALTH_DEEP="1", GOOGLE_API_KEY="k", ORDER_BACKEND="http",
                          ORDER_API_BASE="https:///v1")    # 호스트 없음
        p = health._payload("deep=1")                      # 소켓 열면 setUp 이 터뜨린다
        d = [x for x in p["dependencies"] if x["name"] == "order_backend"][0]
        self.assertFalse(d["checked"])
        self.assertEqual(d["status"], health.OK)

    def test_order_write_gate_is_reported_as_dry_run_by_default(self):
        os.environ.update(ORDER_BACKEND="http", ORDER_API_BASE="https://o.example.com")
        d = health._dep_order(False)
        self.assertIn("dry-run", d["detail"])
        os.environ["ORDER_API_ALLOW_WRITE"] = "1"
        self.assertIn("허용", health._dep_order(False)["detail"])
        # "1" 정확 일치만 ON
        os.environ["ORDER_API_ALLOW_WRITE"] = "true"
        self.assertIn("dry-run", health._dep_order(False)["detail"])

    def test_speech_live_reports_active_providers(self):
        d = health._dep_speech({"speech_live": True,
                                "stt": {"requested": "gemini", "active": "sim"},
                                "tts": {"requested": "edge"}})
        self.assertEqual(d["status"], health.OK)
        self.assertIn("stt=sim", d["detail"])
        self.assertIn("tts=edge", d["detail"])
        self.assertFalse(d["required"])   # 음성은 필수 의존성이 아니다

    def test_speech_malformed_input_is_error_not_crash(self):
        d = health._dep_speech({"speech_live": True, "stt": "broken", "tts": None})
        self.assertEqual(d["status"], health.ERROR)
        self.assertIn("점검 실패", d["detail"])

    def test_cpaas_live_reports_webhook_token_presence_not_value(self):
        os.environ.update(CPAAS_LIVE="1", CPAAS_PROVIDER="acme",
                          CPAAS_WEBHOOK_TOKEN="super-secret-token")
        d = health._dep_cpaas()
        self.assertEqual(d["status"], health.OK)
        self.assertIn("acme", d["detail"])
        self.assertIn("설정됨", d["detail"])
        self.assertNotIn("super-secret-token", d["detail"])

    def test_cpaas_live_without_token_says_missing(self):
        os.environ.update(CPAAS_LIVE="1")
        self.assertIn("미설정", health._dep_cpaas()["detail"])

    def test_ratelimit_disabled_is_not_configured(self):
        d = health._dep_ratelimit({"enabled": False})
        self.assertEqual(d["status"], health.NOT_CONFIGURED)
        self.assertFalse(d["required"])   # 꺼져도 서비스는 살아 있다

    def test_ratelimit_enabled_reports_tracked_key_count(self):
        d = health._dep_ratelimit({"enabled": True, "tracked_keys": 3})
        self.assertEqual(d["status"], health.OK)
        self.assertIn("3", d["detail"])

    def test_audit_disabled_is_not_configured(self):
        d = health._dep_audit({"enabled": False})
        self.assertEqual(d["status"], health.NOT_CONFIGURED)

    def test_audit_enabled_admits_volatile_buffer(self):
        d = health._dep_audit({"enabled": True, "buffered": 2, "capacity": 500})
        self.assertEqual(d["status"], health.OK)
        self.assertIn("[승인 필요]", d["detail"])     # 영속 저장소 미배선을 숨기지 않는다

    def test_audit_missing_counts_do_not_crash(self):
        d = health._dep_audit({"enabled": True})
        self.assertEqual(d["status"], health.OK)
        self.assertIn("0/0", d["detail"])


# ==========================================================================
# 5) 하위 모듈 장애 격리 — 헬스는 죽지 않고, 예외 문구를 싣지 않는다
# ==========================================================================
class TestSubsystemFailureIsolation(Base):
    def _break_import(self, modname):
        """해당 하위 모듈을 고장낸 상태로 바꾼다.

        실제 모듈을 sys.modules 에서 **빼지 않는다** — 빼면 다른 테스트가 재임포트한
        사본을 잡아 isinstance 가 깨진다. 대신 자리만 대역으로 바꿔 두고 tearDown 에서
        원본을 되돌린다. 예외 문구에 내부 경로·비밀값을 심어 누출을 검사한다.
        """
        boom = "/var/task/api/%s.py 내부경로 secret-token" % modname

        class _Broken(object):
            def __getattr__(self, name):
                raise RuntimeError(boom)

        old = sys.modules.get(modname)
        self._modules.append((modname, old))
        sys.modules[modname] = _Broken()
        return boom

    def test_monitoring_unavailable_does_not_leak_exception_text(self):
        boom = self._break_import("monitoring")
        info = health._monitoring()
        self.assertFalse(info["enabled"])
        self.assertNotIn(boom, json.dumps(info, ensure_ascii=False))
        self.assertNotIn("secret-token", json.dumps(info, ensure_ascii=False))

    def test_ratelimit_unavailable_does_not_leak_exception_text(self):
        boom = self._break_import("_ratelimit")
        info = health._ratelimit_status()
        self.assertFalse(info["enabled"])
        self.assertNotIn(boom, json.dumps(info, ensure_ascii=False))

    def test_audit_unavailable_does_not_leak_exception_text(self):
        boom = self._break_import("_audit")
        info = health._audit_status()
        self.assertFalse(info["enabled"])
        self.assertNotIn(boom, json.dumps(info, ensure_ascii=False))

    def test_speech_providers_unavailable_does_not_leak_exception_text(self):
        os.environ["CALLBOT_STT_PROVIDER"] = "clova"
        boom = self._break_import("speech_providers")
        info = health._speech()
        self.assertIn("note", info)
        self.assertNotIn(boom, json.dumps(info, ensure_ascii=False))
        self.assertNotIn("secret-token", json.dumps(info, ensure_ascii=False))

    def test_subsystem_failures_keep_health_answering_200(self):
        for mod in ("monitoring", "_ratelimit", "_audit"):
            self._break_import(mod)
        r = call("GET")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])


# ==========================================================================
# 6) speech 위임 요약
# ==========================================================================
class TestSpeechSummary(Base):
    def test_default_providers_are_not_delegated(self):
        info = health._speech()
        self.assertEqual(info["stt"]["requested"], "gemini")
        self.assertEqual(info["tts"]["requested"], "edge")
        self.assertFalse(info["stt"]["delegated"])
        self.assertFalse(info["tts"]["delegated"])

    def test_non_default_provider_is_delegated_and_forced_sim_when_live_off(self):
        os.environ.update(CALLBOT_STT_PROVIDER="clova", CALLBOT_TTS_PROVIDER="google")
        info = health._speech()
        self.assertTrue(info["stt"]["delegated"])
        self.assertTrue(info["tts"]["delegated"])
        # SPEECH_LIVE 미설정 → 실호출 차단(sim 강제)
        self.assertFalse(info["speech_live"])
        self.assertEqual(info["stt"]["active"], "sim")
        self.assertEqual(info["tts"]["active"], "sim")
        self.assertTrue(info["stt"]["forced_sim"])
        self.assertTrue(info["tts"]["forced_sim"])

    def test_provider_name_is_case_insensitive(self):
        os.environ["CALLBOT_STT_PROVIDER"] = "  CLOVA  "
        info = health._speech()
        self.assertEqual(info["stt"]["requested"], "clova")
        self.assertTrue(info["stt"]["delegated"])

    def test_speech_summary_carries_no_api_keys(self):
        os.environ.update(CALLBOT_STT_PROVIDER="clova",
                          CLOVA_SECRET="clova-secret-value", GOOGLE_API_KEY="gkey")
        try:
            raw = json.dumps(health._speech(), ensure_ascii=False)
        finally:
            os.environ.pop("CLOVA_SECRET", None)
        self.assertNotIn("clova-secret-value", raw)
        self.assertNotIn("gkey", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
