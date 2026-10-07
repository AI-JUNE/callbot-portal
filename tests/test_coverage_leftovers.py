# -*- coding: utf-8 -*-
"""COMMERCIAL_READINESS 21행 「다음 순서 제안」의 잔여 분기 — 값싼 것만, 동작 변경 없음.

  _guard     : 리퍼러 URL 파싱 실패(IPv6 괄호 깨짐) → 거부 · 웹훅 `?t=` 경로 파싱 실패 → 401
  _ratelimit : x-real-ip 폴백 · `_LAST`(thread-local) 쓰기 실패를 삼킨다(_remember·reset)
  _errors    : 추가 헤더·X-Request-Id 전송 실패를 삼키고 나머지 헤더는 그대로 나간다
  health     : `_hostport_of` 비문자열 → (None, None) · 함수 안 sys.path 가드(api 가 경로에 없을 때) ·
               `_close` 의 rq 없음/finish 장애 흡수
네트워크 미사용(소켓·urlopen 모두 안 쓴다). 실행: python3 -m pytest tests/test_coverage_leftovers.py -q
"""
import io
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)

import _guard       # noqa: E402
import _ratelimit   # noqa: E402
import _errors      # noqa: E402
import health       # noqa: E402


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class Env(object):
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Guard(unittest.TestCase):
    def test_unparsable_referer_is_denied(self):
        """urlparse 가 ValueError 를 내는 리퍼러(IPv6 괄호 깨짐)는 허용이 아니라 거부다."""
        self.assertFalse(_guard._origin_ok(FakeHeaders({"referer": "https://[::1"})))
        self.assertFalse(_guard._origin_ok(FakeHeaders({"referer": "https://[bad/x"})))

    def test_webhook_token_path_parse_failure_is_401(self):
        with Env(CPAAS_WEBHOOK_TOKEN="tok-123456", CALLBOT_API_KEY=None, CALLBOT_STRICT=None):
            ok, code, _ = _guard.check(FakeHeaders({}), "//[::1/api/voice?t=tok-123456", allow_webhook=True)
            self.assertEqual((ok, code), (False, 401))
            ok, code, _ = _guard.check(FakeHeaders({}), "/api/voice?t=tok-123456", allow_webhook=True)
            self.assertEqual((ok, code), (True, 200), "정상 경로는 그대로 통과")


class RateLimit(unittest.TestCase):
    def test_x_real_ip_fallback(self):
        with Env(CALLBOT_RATE_LIMIT_TRUST_XFF=None):   # 기본값 = XFF 신뢰
            self.assertTrue(_ratelimit._trust_xff())
            self.assertEqual(_ratelimit.client_ip(FakeHeaders({"x-real-ip": " 10.0.0.9 "})), "10.0.0.9")
            self.assertEqual(_ratelimit.client_ip(FakeHeaders({"x-forwarded-for": " , "})), "unknown")

    def test_broken_header_store_is_unknown(self):
        class Boom(object):
            def get(self, *a):
                raise RuntimeError("headers gone")
        self.assertEqual(_ratelimit.client_ip(Boom()), "unknown")

    def test_last_store_failure_is_swallowed(self):
        class Broken(object):
            def __setattr__(self, k, v):
                raise RuntimeError("no thread-local")
        saved = _ratelimit._LAST
        _ratelimit._LAST = Broken()
        try:
            self.assertEqual(_ratelimit._remember({"a": 1}), {"a": 1}, "판정 결과는 그대로 돌려준다")
            _ratelimit.reset()          # 예외가 올라오면 테스트 실패
        finally:
            _ratelimit._LAST = saved
            _ratelimit.reset()


class Errors(unittest.TestCase):
    class H(object):
        def __init__(self, fail_on):
            self.fail_on = fail_on
            self.headers = FakeHeaders({})
            self.sent = []
            self.status = None
            self.wfile = io.BytesIO()

        def send_response(self, c):
            self.status = c

        def send_header(self, k, v):
            if k in self.fail_on:
                raise RuntimeError("header sink broken: " + k)
            self.sent.append((k, v))

        def end_headers(self):
            pass

    def test_extra_header_failure_does_not_kill_response(self):
        h = self.H(fail_on=("Retry-After",))
        _errors.send(h, status=429, extra_headers=[("Retry-After", "7"), ("X-RateLimit-Limit", "12")])
        self.assertEqual(h.status, 429)
        names = [k for k, _ in h.sent]
        self.assertIn("X-RateLimit-Limit", names, "뒤따르는 헤더는 계속 나간다")
        self.assertIn("Access-Control-Allow-Origin", names)
        self.assertEqual(json.loads(h.wfile.getvalue().decode())["status"], 429)

    def test_request_id_header_failure_is_swallowed(self):
        h = self.H(fail_on=("X-Request-Id",))
        _errors.send(h, status=400, request_id="rid-1")
        self.assertEqual(h.status, 400)
        self.assertIn("Access-Control-Expose-Headers", [k for k, _ in h.sent])
        self.assertEqual(json.loads(h.wfile.getvalue().decode())["request_id"], "rid-1", "본문에는 남는다")


class Health(unittest.TestCase):
    def test_hostport_of_non_string_is_none(self):
        self.assertEqual(health._hostport_of(12345), (None, None))
        # 자격증명·경로·쿼리는 떨어지고 포트 표기는 살아남는다(예전엔 버리고 443 고정)
        self.assertEqual(health._hostport_of("https://u:p@h.example:8443/x?y"),
                         ("h.example", 8443))

    def test_sub_module_lookups_restore_sys_path(self):
        """함수 안의 sys.path 가드 — api 폴더가 경로에서 빠져도 스스로 넣고 답한다."""
        def strip_api():
            gone = [p for p in sys.path if os.path.abspath(p) == os.path.abspath(API)]
            for p in gone:
                sys.path.remove(p)
            return gone
        saved = list(sys.path)
        try:
            with Env(SPEECH_LIVE=None):
                for fn in (health._monitoring, health._ratelimit_status, health._audit_status):
                    strip_api()      # 앞 함수가 다시 넣은 경로를 매번 뺀다 — 각 함수의 가드가 실제로 돈다
                    self.assertNotIn(os.path.abspath(API), [os.path.abspath(p) for p in sys.path])
                    self.assertIsInstance(fn(), dict, fn.__name__)
                    self.assertIn(os.path.abspath(API), [os.path.abspath(p) for p in sys.path], fn.__name__)
        finally:
            sys.path[:] = saved

    def test_close_without_rq_and_with_broken_rq(self):
        health._close(None, 200)                       # rq 없음 → 조용히 반환

        class Broken(object):
            def finish(self, *a, **k):
                raise RuntimeError("log sink down")
        health._close(Broken(), 500)                   # 실패 기록 경로 → 예외 흡수
        with Env(HEALTH_REQUEST_LOG="1"):
            health._close(Broken(), 200)               # 전량 기록 경로도 흡수


if __name__ == "__main__":
    unittest.main(verbosity=2)
