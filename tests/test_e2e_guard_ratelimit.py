# -*- coding: utf-8 -*-
"""로컬 E2E — **실제 소켓**으로 접근 가드와 요청 제한을 확인한다.

왜 단위 테스트로 부족한가
  가드 판정은 `self.headers`·`self.path` 를 거쳐 들어온다. 대역(FakeHandler)으로
  부르면 실제 HTTP 가 채워 주는 헤더(Host)가 빠진 채로 검사돼, "진짜 브라우저
  요청"과 "위조한 요청"이 같은 모양으로 보인다. 21차에서 고친 구멍이 바로 그
  틈에 있었다 — `Sec-Fetch-Site: same-origin` 한 줄로 허용 오리진 목록을
  건너뛸 수 있었다. 그래서 소켓을 실제로 열고 확인한다.

네트워크
  **루프백(127.0.0.1) + 임의 포트만** 사용한다. 외부로 나가는 요청은 없고
  과금되는 경로(LLM·STT·TTS)도 타지 않는다(ops_stats·wellbeing GET 는 sim).

실행: python3 -m pytest tests/test_e2e_guard_ratelimit.py -q
"""
import os
import sys
import json
import threading
import unittest
import http.server
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import ops_stats    # noqa: E402
import wellbeing    # noqa: E402
import _ratelimit   # noqa: E402

GUARD_ENV = ("CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_ALLOWED_ORIGINS")


class Server(object):
    """핸들러 하나를 루프백에 띄운다. 포트는 OS 가 고른다(충돌 없음)."""

    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def get(self, path, headers=None, timeout=5):
        req = urllib.request.Request(self.origin + path, method="GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class Base(unittest.TestCase):
    handler = None

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in GUARD_ENV}
        for k in GUARD_ENV:
            os.environ.pop(k, None)
        cls.srv = Server(cls.handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()

    tearDown = setUp

    def same_origin(self, **extra):
        h = {"Sec-Fetch-Site": "same-origin", "Origin": self.srv.origin}
        h.update(extra)
        return h


class TestGuardOverSocket(Base):
    handler = ops_stats.handler

    def test_real_same_origin_request_succeeds(self):
        st, _b = self.srv.get("/api/ops_stats", self.same_origin())
        self.assertEqual(st, 200)

    def test_forged_same_origin_header_is_rejected(self):
        """헤더 한 줄로 허용목록을 건너뛰지 못한다."""
        st, body = self.srv.get("/api/ops_stats", {"Sec-Fetch-Site": "same-origin",
                                                   "Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        self.assertFalse(json.loads(body)["ok"])

    def test_forged_same_origin_with_foreign_referer_is_rejected(self):
        st, _b = self.srv.get("/api/ops_stats", {"Sec-Fetch-Site": "same-origin",
                                                 "Referer": "https://evil.example/x"})
        self.assertEqual(st, 403)

    def test_plain_curl_is_rejected(self):
        st, _b = self.srv.get("/api/ops_stats")
        self.assertEqual(st, 403)

    def test_same_origin_without_origin_header_still_works(self):
        """동일출처 GET 은 Origin 없이 오기도 한다 — 끊지 않는다."""
        st, _b = self.srv.get("/api/ops_stats", {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(st, 200)

    def test_allowlisted_cross_origin_still_works(self):
        import _guard
        st, _b = self.srv.get("/api/ops_stats", {"Origin": _guard.ALLOWED[0],
                                                 "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(st, 200)


class TestRateLimitOverSocket(Base):
    handler = ops_stats.handler

    def test_one_flooding_client_cannot_lock_everyone_out(self):
        """21차 회귀 — IP 한도에서 거부된 요청이 전역 과금 상한을 먹지 않는다."""
        os.environ["CALLBOT_RATE_LIMIT"] = "2"
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_DEFAULT"] = "6"
        flood = self.same_origin(**{"X-Forwarded-For": "1.1.1.1"})
        codes = [self.srv.get("/api/ops_stats", flood)[0] for _ in range(12)]
        self.assertEqual(codes.count(200), 2)
        self.assertEqual(codes.count(429), 10)

        others = [self.srv.get("/api/ops_stats",
                               self.same_origin(**{"X-Forwarded-For": "2.2.2.%d" % i}))[0]
                  for i in range(4)]
        self.assertEqual(others, [200, 200, 200, 200], "전역 예산이 폭주로 소진됐다")

        st, body = self.srv.get("/api/ops_stats",
                                self.same_origin(**{"X-Forwarded-For": "3.3.3.3"}))
        self.assertEqual(st, 429)                      # 전역 상한은 여전히 선다
        self.assertEqual(json.loads(body)["code"], "RATE_LIMITED")

    def test_429_carries_retry_after(self):
        os.environ["CALLBOT_RATE_LIMIT"] = "1"
        h = self.same_origin(**{"X-Forwarded-For": "4.4.4.4"})
        self.assertEqual(self.srv.get("/api/ops_stats", h)[0], 200)
        req = urllib.request.Request(self.srv.origin + "/api/ops_stats", method="GET")
        for k, v in h.items():
            req.add_header(k, v)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 429)
        self.assertTrue((cm.exception.headers.get("Retry-After") or "").isdigit())


class TestWellbeingOverSocket(Base):
    """이음 2R 시연 경로가 가드 변경에도 그대로 열려 있는가(전화망 미경유)."""

    handler = wellbeing.handler

    def test_question_sheet_is_served(self):
        st, body = self.srv.get("/api/wellbeing", self.same_origin())
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d.get("result_schema"), "wellbeing.result.v1")
        self.assertEqual(d.get("mode"), "simulation")   # 실발신은 [승인 필요]
        self.assertTrue(d.get("questions"))

    def test_request_id_header_present(self):
        req = urllib.request.Request(self.srv.origin + "/api/wellbeing", method="GET")
        for k, v in self.same_origin().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertTrue(r.headers.get("X-Request-Id"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
