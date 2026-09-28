# -*- coding: utf-8 -*-
"""api/_guard.py 잔여 방어 분기 회귀 — 네트워크 미사용, 의존성 0.

81% 가 이미 덮여 있다(오리진 판정·자격 경로·거부 규약·요청 제한은
tests/test_guard_stt.py). 남은 것은 **하위 모듈 장애 때만 도는 방어 분기**다.
가드는 모든 API 앞단이라, 장애 시에도 요청이 죽지 않는지·설정 힌트가
새지 않는지를 고정한다.

검증 대상
  1) _client_ip — _ratelimit 존재 시 위임 경로
  2) rate_headers — _ratelimit.current() 장애 시 빈 목록(요청은 안 죽는다)
  3) check() — 429 판정 중 scope 조회 장애를 삼킨다(429는 그대로 나간다)
  4) check() — 웹훅 토큰의 ?t= 파싱 장애를 삼키고 401로 마무리한다
  5) deny() — _errors 자체가 없거나 터진 최후 폴백
     · 디버그 OFF: 설정 힌트(msg)를 그대로 노출하지 않는다(일반 문구만)
     · 디버그 ON: 기존과 동일하게 문구를 보여준다(운영자 진단용)
     · 그래도 code·CORS 헤더는 정상 봉투와 동일하게 나간다

실행: python3 -m pytest tests/test_guard_edge.py -q
"""
import os
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _guard       # noqa: E402
import _ratelimit   # noqa: E402


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


class FakeHandler(object):
    def __init__(self, headers=None):
        self.headers = FakeHeaders(headers or {})
        self.wfile = FakeWFile()
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


GUARD_ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CPAAS_WEBHOOK_TOKEN",
             "CALLBOT_DEBUG_ERRORS", "CALLBOT_RATE_LIMIT")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in GUARD_ENV}
        for k in GUARD_ENV:
            os.environ.pop(k, None)
        _ratelimit.reset()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def _boom(self, *a, **k):
        raise NetworkTouched("가드가 네트워크를 건드렸다")


# ==========================================================================
# 1) _client_ip — _ratelimit 위임 경로
# ==========================================================================
class TestClientIp(Base):
    def test_delegates_to_ratelimit_when_present(self):
        self.assertIsNotNone(_guard._ratelimit)
        h = FakeHeaders({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
        self.assertEqual(_guard._client_ip(h), _ratelimit.client_ip(h))


# ==========================================================================
# 2) rate_headers — 하위 모듈 장애를 삼킨다
# ==========================================================================
class TestRateHeadersFallback(Base):
    def test_current_failure_yields_empty_headers(self):
        real_current = _ratelimit.current
        _ratelimit.current = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(_guard.rate_headers(), [])
        finally:
            _ratelimit.current = real_current


# ==========================================================================
# 3) check() — 429 판정 중 scope 조회 장애를 삼킨다
# ==========================================================================
class TestCheckRateLimitScopeFallback(Base):
    def test_scope_lookup_failure_still_returns_429(self):
        real_check = _ratelimit.check
        real_current = _ratelimit.current

        class _Denied(object):
            allowed = False

        _ratelimit.check = lambda *a, **k: _Denied()
        _ratelimit.current = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            ok, code, msg = _guard.check(FakeHeaders({}), "/api/stt")
            self.assertFalse(ok)
            self.assertEqual(code, 429)
            self.assertIn("ip", msg)  # scope 조회가 실패하면 "ip" 로 대체
        finally:
            _ratelimit.check = real_check
            _ratelimit.current = real_current


# ==========================================================================
# 4) check() — 웹훅 ?t= 파싱 장애를 삼키고 401로 마무리한다
# ==========================================================================
class TestWebhookPathParseFallback(Base):
    def test_unparseable_path_falls_back_to_401(self):
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "t-secret"
        # path=None → urlparse(None) 이 예외를 던진다. 헤더 토큰도 없으니
        # 최종적으로 401 로 끝나야 한다(예외가 요청을 죽이면 안 된다).
        ok, code, msg = _guard.check(FakeHeaders({}), None, allow_webhook=True)
        self.assertFalse(ok)
        self.assertEqual(code, 401)
        self.assertIn("webhook", msg)


# ==========================================================================
# 5) deny() — _errors 자체가 없거나 터진 최후 폴백
# ==========================================================================
class TestDenyLastResortFallback(Base):
    def setUp(self):
        super().setUp()
        self._saved_errors = sys.modules.get("_errors")
        # import _errors 가 ImportError 를 내도록 캐시를 깬다.
        sys.modules["_errors"] = None

    def tearDown(self):
        if self._saved_errors is None:
            sys.modules.pop("_errors", None)
        else:
            sys.modules["_errors"] = self._saved_errors
        super().tearDown()

    def test_debug_off_hides_hint_message(self):
        h = FakeHandler({"origin": _guard.ALLOWED[0]})
        _guard.deny(h, 403, "forbidden: cross-origin(CALLBOT_STRICT 설정 힌트)")
        self.assertEqual(h.status, 403)
        body = h.body()
        self.assertEqual(body["code"], 403)
        self.assertNotIn("설정 힌트", body["error"])
        self.assertNotIn("CALLBOT_STRICT", body["error"])
        self.assertEqual(h.header("Access-Control-Allow-Origin"), _guard.ALLOWED[0])
        self.assertEqual(h.header("Content-Length"), str(len(h.wfile.data)))

    def test_debug_on_still_shows_message(self):
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        h = FakeHandler({})
        _guard.deny(h, 401, "unauthorized: API key required")
        self.assertEqual(h.status, 401)
        body = h.body()
        self.assertEqual(body["error"], "unauthorized: API key required")

    def test_response_is_valid_json_envelope(self):
        h = FakeHandler({})
        _guard.deny(h, 500, "internal")
        body = h.body()
        self.assertEqual(set(body.keys()), {"ok", "error", "code"})
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
