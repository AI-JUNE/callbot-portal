# -*- coding: utf-8 -*-
"""api/_guard.py 접근 가드 + api/stt.py 전사 엔드포인트 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — _guard·stt 순서)
  1) 오리진 판정 — 동일출처 신호·허용 오리진·리퍼러 폴백, 그 외는 거부
  2) 자격 통과 경로 — API 키 / STRICT 잠금 / 웹훅 토큰(헤더·?t=)
  3) 거부 응답 규약 — 표준 봉투, 429 헤더, **비밀값 미노출**
  4) 가용성 — 제한·모듈 장애가 요청을 죽이지 않는다(허용 쪽 폴백)
  5) STT 입력검증 — mime 화이트리스트·본문 상한(413)·필수값(400)
  6) STT 엔드포인트 — GET 진단(키 원문 미노출), POST sim 프로바이더 경로
  7) 과금 안전 — 검증 실패·sim 경로에서 업스트림(Gemini) 호출이 없다

실행: python3 -m pytest tests/test_guard_stt.py -q
"""
import os
import sys
import json
import base64
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _guard       # noqa: E402
import _errors      # noqa: E402
import _ratelimit   # noqa: E402
import stt          # noqa: E402


# --------------------------------------------------------------------------
# 테스트용 최소 핸들러 (BaseHTTPRequestHandler 대역)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class FakeRFile(object):
    def __init__(self, data=b""):
        self._d = data

    def read(self, n=None):
        d = self._d if n is None else self._d[:n]
        self._d = b"" if n is None else self._d[n:]
        return d


class FakeHandler(object):
    def __init__(self, headers=None, body=None):
        self.headers = FakeHeaders(headers or {})
        self.wfile = FakeWFile()
        self.rfile = FakeRFile(body or b"")
        self.status = None
        self.sent = []

    def send_response(self, c):
        self.status = c

    def send_header(self, k, v):
        self.sent.append((k, str(v)))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


def sttcall(method, headers=None, payload=None, path="/api/stt"):
    """stt.handler 를 소켓 없이 호출한다."""
    raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = dict(headers or {})
    if payload is not None:
        hdrs.setdefault("content-length", str(len(raw)))
    h = FakeHandler(hdrs, raw)
    h.path = path
    inst = stt.handler.__new__(stt.handler)
    inst.headers = h.headers
    inst.wfile = h.wfile
    inst.rfile = h.rfile
    inst.path = path
    inst.send_response = h.send_response
    inst.send_header = h.send_header
    inst.end_headers = h.end_headers
    getattr(inst, "do_" + method)()
    return h


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.1"}

GUARD_ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CPAAS_WEBHOOK_TOKEN",
             "CALLBOT_DEBUG_ERRORS", "CALLBOT_STT_PROVIDER", "SPEECH_LIVE",
             "GOOGLE_API_KEY", "GEMINI_API_KEY")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in GUARD_ENV}
        for k in GUARD_ENV:
            os.environ.pop(k, None)
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()


# ==========================================================================
# 1) 오리진 판정
# ==========================================================================
class TestOrigin(Base):
    def test_same_origin_signal_passes(self):
        self.assertTrue(_guard._origin_ok(FakeHeaders({"sec-fetch-site": "same-origin"})))

    def test_cross_site_signal_falls_back_to_origin_list(self):
        h = FakeHeaders({"sec-fetch-site": "cross-site",
                         "origin": "https://evil.example"})
        self.assertFalse(_guard._origin_ok(h))

    def test_allowed_origin_passes_with_trailing_slash(self):
        h = FakeHeaders({"origin": _guard.ALLOWED[0] + "/"})
        self.assertTrue(_guard._origin_ok(h))

    def test_referer_fallback(self):
        ok = FakeHeaders({"referer": _guard.ALLOWED[0] + "/admin?x=1"})
        self.assertTrue(_guard._origin_ok(ok))
        bad = FakeHeaders({"referer": "https://evil.example/admin"})
        self.assertFalse(_guard._origin_ok(bad))

    def test_malformed_referer_is_denied_not_crashed(self):
        self.assertFalse(_guard._origin_ok(FakeHeaders({"referer": "::::"})))

    def test_no_browser_signal_is_denied(self):
        """오리진·리퍼러 없는 호출(curl)은 브라우저가 아니다 — 과금 경로 방어."""
        self.assertFalse(_guard._origin_ok(FakeHeaders({})))

    def test_env_list_override_and_default(self):
        self.assertEqual(_guard._env_list("__NOPE__", ["a"]), ["a"])
        os.environ["__GUARD_TEST_LIST__"] = " https://a.example/, https://b.example ,, "
        try:
            self.assertEqual(_guard._env_list("__GUARD_TEST_LIST__", []),
                             ["https://a.example", "https://b.example"])
        finally:
            os.environ.pop("__GUARD_TEST_LIST__", None)

    def test_allow_origin_header_echoes_only_allowed(self):
        self.assertEqual(_guard.allow_origin_header(
            FakeHeaders({"origin": _guard.ALLOWED[0]})), _guard.ALLOWED[0])
        # 미허용 오리진을 그대로 되비추면 CORS 우회가 된다 — 기본값으로 고정
        self.assertEqual(_guard.allow_origin_header(
            FakeHeaders({"origin": "https://evil.example"})), _guard.ALLOWED[0])


# ==========================================================================
# 2) 자격 통과 경로
# ==========================================================================
class TestCheck(Base):
    def test_same_origin_allowed_by_default(self):
        ok, code, _ = _guard.check(FakeHeaders(SAME_ORIGIN), "/api/stt")
        self.assertTrue(ok)
        self.assertEqual(code, 200)

    def test_cross_origin_forbidden(self):
        ok, code, _ = _guard.check(FakeHeaders({"origin": "https://evil.example"}), "/api/stt")
        self.assertFalse(ok)
        self.assertEqual(code, 403)

    def test_api_key_passes(self):
        os.environ["CALLBOT_API_KEY"] = "k-secret"
        ok, code, _ = _guard.check(FakeHeaders({"x-api-key": "k-secret"}), "/api/stt")
        self.assertTrue(ok)
        ok2, code2, _ = _guard.check(FakeHeaders({"x-api-key": "wrong"}), "/api/stt")
        self.assertFalse(ok2)
        self.assertEqual(code2, 403)

    def test_strict_requires_key_even_from_portal(self):
        os.environ["CALLBOT_STRICT"] = "1"
        os.environ["CALLBOT_API_KEY"] = "k-secret"
        ok, code, _ = _guard.check(FakeHeaders(SAME_ORIGIN), "/api/stt")
        self.assertFalse(ok)
        self.assertEqual(code, 401)
        ok2, _, _ = _guard.check(FakeHeaders(dict(SAME_ORIGIN, **{"x-api-key": "k-secret"})),
                                 "/api/stt")
        self.assertTrue(ok2)

    def test_webhook_token_header_and_query(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "t-1"
        ok, _, _ = _guard.check(FakeHeaders({"x-webhook-token": "t-1"}),
                                "/api/voice", allow_webhook=True)
        self.assertTrue(ok)
        ok2, _, _ = _guard.check(FakeHeaders({}), "/api/voice?t=t-1", allow_webhook=True)
        self.assertTrue(ok2)
        bad, code, _ = _guard.check(FakeHeaders({}), "/api/voice?t=nope", allow_webhook=True)
        self.assertFalse(bad)
        self.assertEqual(code, 401)

    def test_webhook_without_token_configured_is_401(self):
        ok, code, msg = _guard.check(FakeHeaders({}), "/api/voice", allow_webhook=True)
        self.assertFalse(ok)
        self.assertEqual(code, 401)
        self.assertIn("webhook", msg.lower())

    def test_webhook_path_still_honors_portal_origin(self):
        ok, _, _ = _guard.check(FakeHeaders(SAME_ORIGIN), "/api/voice", allow_webhook=True)
        self.assertTrue(ok)

    def test_rate_limit_exceeded_returns_429(self):
        os.environ["CALLBOT_RATE_LIMIT_SPEECH"] = "2"
        _ratelimit.reset()
        h = FakeHeaders(dict(SAME_ORIGIN))
        codes = [_guard.check(h, "/api/stt")[1] for _ in range(4)]
        self.assertIn(429, codes)


# ==========================================================================
# 3) 거부 응답 규약 · 비밀값 미노출
# ==========================================================================
class TestDeny(Base):
    def test_envelope_shape(self):
        h = FakeHandler({"origin": "https://evil.example"})
        _guard.deny(h, 403, "forbidden: cross-origin")
        self.assertEqual(h.status, 403)
        b = h.body()
        self.assertIs(b["ok"], False)
        self.assertEqual(b["code"], "FORBIDDEN")
        self.assertEqual(b["status"], 403)
        self.assertIsInstance(b["error"], str)

    def test_internal_hint_not_leaked_by_default(self):
        h = FakeHandler({})
        _guard.deny(h, 401, "webhook auth required (set CPAAS_WEBHOOK_TOKEN and pass ?t=)")
        raw = h.wfile.data.decode("utf-8")
        self.assertNotIn("CPAAS_WEBHOOK_TOKEN", raw)
        self.assertNotIn("debug", h.body())

    def test_debug_hint_only_when_opted_in(self):
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        h = FakeHandler({})
        _guard.deny(h, 403, "forbidden: cross-origin")
        self.assertIn("debug", h.body())

    def test_429_carries_retry_headers(self):
        os.environ["CALLBOT_RATE_LIMIT_SPEECH"] = "1"
        _ratelimit.reset()
        hd = dict(SAME_ORIGIN)
        _guard.check(FakeHeaders(hd), "/api/stt")
        ok, code, msg = _guard.check(FakeHeaders(hd), "/api/stt")
        self.assertFalse(ok)
        h = FakeHandler(hd)
        _guard.deny(h, code, msg)
        self.assertEqual(h.status, 429)
        self.assertIsNotNone(h.header("Retry-After"))

    def test_deny_never_echoes_api_key(self):
        os.environ["CALLBOT_API_KEY"] = "k-supersecret"
        h = FakeHandler({"x-api-key": "k-supersecret-wrong"})
        ok, code, msg = _guard.check(h.headers, "/api/stt")
        self.assertFalse(ok)
        _guard.deny(h, code, msg)
        self.assertNotIn("supersecret", h.wfile.data.decode("utf-8"))


# ==========================================================================
# 4) 가용성 — 장애는 허용 쪽으로 기운다
# ==========================================================================
class TestAvailability(Base):
    def test_rate_ok_true_when_ratelimit_module_absent(self):
        saved = _guard._ratelimit
        _guard._ratelimit = None
        try:
            self.assertTrue(_guard.rate_ok(FakeHeaders({}), "/api/stt"))
            self.assertEqual(_guard.rate_headers(), [])
            self.assertEqual(_guard._limit("/api/stt"), 40)
            self.assertEqual(_guard._client_ip(FakeHeaders({"x-forwarded-for": "9.9.9.9, 1.1.1.1"})),
                             "9.9.9.9")
            self.assertEqual(_guard._client_ip(FakeHeaders({})), "unknown")
        finally:
            _guard._ratelimit = saved

    def test_rate_ok_true_when_check_raises(self):
        saved = _ratelimit.check
        _ratelimit.check = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertTrue(_guard.rate_ok(FakeHeaders({}), "/api/stt"))
        finally:
            _ratelimit.check = saved

    def test_limit_reads_legacy_env_when_module_absent(self):
        saved = _guard._ratelimit
        _guard._ratelimit = None
        os.environ["CALLBOT_RATE_LIMIT"] = "7"
        try:
            self.assertEqual(_guard._limit("/api/stt"), 7)
            os.environ["CALLBOT_RATE_LIMIT"] = "not-a-number"
            self.assertEqual(_guard._limit("/api/stt"), 40)
        finally:
            os.environ.pop("CALLBOT_RATE_LIMIT", None)
            _guard._ratelimit = saved


# ==========================================================================
# 5) STT 입력검증
# ==========================================================================
class TestMime(Base):
    def test_allowed_containers(self):
        for m in stt.MIME_BASES:
            self.assertEqual(stt._check_mime(m), m)

    def test_codecs_parameter_and_case_are_tolerated(self):
        self.assertEqual(stt._check_mime("audio/webm;codecs=opus"), "audio/webm;codecs=opus")
        self.assertEqual(stt._check_mime("AUDIO/MP4"), "AUDIO/MP4")

    def test_empty_defaults_to_webm(self):
        self.assertEqual(stt._check_mime(""), "audio/webm")
        self.assertEqual(stt._check_mime(None), "audio/webm")

    def test_unknown_mime_rejected(self):
        for bad in ("application/json", "audio/../etc", "text/html", "audio"):
            with self.assertRaises(_errors.ValidationError):
                stt._check_mime(bad)

    def test_key_env_precedence(self):
        os.environ["GOOGLE_API_KEY"] = " g-key "
        self.assertEqual(stt._key(), "g-key")
        os.environ.pop("GOOGLE_API_KEY")
        os.environ["GEMINI_API_KEY"] = "m-key"
        self.assertEqual(stt._key(), "m-key")
        os.environ.pop("GEMINI_API_KEY")
        self.assertEqual(stt._key(), "")


class TestProviderSelection(Base):
    def test_default_and_gemini_use_builtin_path(self):
        self.assertIsNone(stt._alt_provider())
        os.environ["CALLBOT_STT_PROVIDER"] = "gemini"
        self.assertIsNone(stt._alt_provider())

    def test_sim_provider_is_delegated(self):
        os.environ["CALLBOT_STT_PROVIDER"] = "sim"
        p = stt._alt_provider()
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "sim")

    def test_provider_health_is_readonly_dict(self):
        self.assertIsInstance(stt._provider_health(), dict)


# ==========================================================================
# 6~7) 엔드포인트 · 과금 안전
# ==========================================================================
class NetworkTouched(AssertionError):
    pass


class TestEndpoint(Base):
    def setUp(self):
        Base.setUp(self)
        # 어떤 경로에서도 업스트림(Gemini)을 건드리면 즉시 실패한다
        self._urlopen = stt.urllib.request.urlopen
        stt.urllib.request.urlopen = self._boom

    def tearDown(self):
        stt.urllib.request.urlopen = self._urlopen
        Base.tearDown(self)

    def _boom(self, *a, **k):
        raise NetworkTouched("업스트림 호출 발생 — 테스트는 네트워크를 쓰지 않는다")

    def test_get_denied_without_origin(self):
        h = sttcall("GET", {})
        self.assertEqual(h.status, 403)

    def test_get_reports_status_without_leaking_key(self):
        os.environ["GOOGLE_API_KEY"] = "g-supersecret"
        h = sttcall("GET", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        b = h.body()
        self.assertTrue(b["ok"])
        self.assertIs(b["key_present"], True)
        self.assertEqual(b["provider"], "gemini")
        self.assertIn("health", b)
        self.assertNotIn("supersecret", h.wfile.data.decode("utf-8"))

    def test_post_denied_without_origin(self):
        h = sttcall("POST", {}, {"audio": "AAA"})
        self.assertEqual(h.status, 403)

    def test_post_requires_audio(self):
        h = sttcall("POST", SAME_ORIGIN, {"mime": "audio/webm"})
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["code"], "INVALID_REQUEST")

    def test_post_rejects_empty_body(self):
        h = FakeHandler(dict(SAME_ORIGIN, **{"content-length": "0"}))
        inst = stt.handler.__new__(stt.handler)
        inst.headers = h.headers
        inst.wfile = h.wfile
        inst.rfile = h.rfile
        inst.path = "/api/stt"
        inst.send_response = h.send_response
        inst.send_header = h.send_header
        inst.end_headers = h.end_headers
        inst.do_POST()
        self.assertEqual(h.status, 400)

    def test_post_rejects_oversized_body_with_413(self):
        h = FakeHandler(dict(SAME_ORIGIN, **{
            "content-length": str(_errors.MAX_BODY_AUDIO + 1)}))
        inst = stt.handler.__new__(stt.handler)
        inst.headers = h.headers
        inst.wfile = h.wfile
        inst.rfile = h.rfile
        inst.path = "/api/stt"
        inst.send_response = h.send_response
        inst.send_header = h.send_header
        inst.end_headers = h.end_headers
        inst.do_POST()
        self.assertEqual(h.status, 413)
        self.assertEqual(h.body()["code"], "PAYLOAD_TOO_LARGE")

    def test_post_rejects_unknown_mime(self):
        h = sttcall("POST", SAME_ORIGIN, {"audio": "AAA", "mime": "application/json"})
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["details"][0]["field"], "mime")

    def test_post_sim_provider_returns_text_without_network(self):
        os.environ["CALLBOT_STT_PROVIDER"] = "sim"
        audio = base64.b64encode(b"\x00" * 64).decode("ascii")
        h = sttcall("POST", SAME_ORIGIN, {"audio": audio, "mime": "audio/webm;codecs=opus"})
        self.assertEqual(h.status, 200)
        b = h.body()
        self.assertEqual(b["provider"], "sim")
        self.assertIs(b["sim"], True)
        self.assertIn("SIM", b["text"])

    def test_upstream_failure_becomes_standard_envelope(self):
        """키가 있어도 업스트림 장애는 502/500 봉투로 나간다(내부 문구 미노출)."""
        os.environ["GOOGLE_API_KEY"] = "g-key"
        h = sttcall("POST", SAME_ORIGIN, {"audio": "AAA"})
        self.assertGreaterEqual(h.status, 500)
        b = h.body()
        self.assertIs(b["ok"], False)
        self.assertNotIn("g-key", h.wfile.data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
