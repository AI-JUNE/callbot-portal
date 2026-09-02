# -*- coding: utf-8 -*-
"""api/_ratelimit.py 요청 제한 불변식 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS 'rate limit 공개 API 적용')
  1) 등급 분류 — 과금 경로(llm·speech)와 웹훅·조회를 구분한다
  2) IP 한도 — 초과시 거부, 윈도가 지나면 회복
  3) 전역 한도 — IP 를 바꿔도 총량은 막힌다(과금 상한). 전역 초과시 IP 카운터를
     추가로 소모하지 않는다(정상 사용자 이중 벌점 방지)
  4) 응답 규약 — 429 봉투 + Retry-After·X-RateLimit-* 헤더
  5) 메모리 — 키 사전이 무한히 자라지 않는다
  6) 가용성 — 제한 로직 장애는 요청을 막지 않는다(허용 쪽으로 기운다)
  7) 설정 — 환경변수 오버라이드, OFF 스위치, 기존 CALLBOT_RATE_LIMIT 하위호환
  8) PII — snapshot 에 IP 원문이 없다

실행: python3 tests/test_ratelimit.py
"""
import io
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _ratelimit  # noqa: E402
import _guard      # noqa: E402
import _errors     # noqa: E402


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


def hdr(ip="1.1.1.1"):
    return FakeHeaders({"x-forwarded-for": ip})


ENV_KEYS = [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]


class Base(unittest.TestCase):
    def setUp(self):
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()

    tearDown = setUp


# --------------------------------------------------------------------------
class TestRouteClass(Base):
    def test_cost_paths_are_separated(self):
        self.assertEqual(_ratelimit.route_class("/api/chat"), "llm")
        self.assertEqual(_ratelimit.route_class("/api/assist?x=1"), "llm")
        self.assertEqual(_ratelimit.route_class("/api/stt"), "speech")
        self.assertEqual(_ratelimit.route_class("/api/tts/"), "speech")
        self.assertEqual(_ratelimit.route_class("/api/voice?t=abc"), "webhook")
        self.assertEqual(_ratelimit.route_class("/api/ops_stats"), "default")
        self.assertEqual(_ratelimit.route_class(""), "default")
        self.assertEqual(_ratelimit.route_class(None), "default")

    def test_py_suffix_and_case(self):
        self.assertEqual(_ratelimit.route_class("/api/chat.py"), "llm")
        self.assertEqual(_ratelimit.route_class("/API/STT"), "speech")

    def test_llm_is_stricter_than_default(self):
        self.assertLess(_ratelimit.limits("llm")[0], _ratelimit.limits("default")[0])
        self.assertLess(_ratelimit.limits("speech")[0], _ratelimit.limits("llm")[0])
        self.assertGreater(_ratelimit.limits("webhook")[0], _ratelimit.limits("default")[0])


class TestPerIpLimit(Base):
    def test_allows_up_to_limit_then_denies(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "3"
        for i in range(3):
            d = _ratelimit.check(hdr(), "/api/chat")
            self.assertTrue(d.allowed, "call %d should pass" % i)
            self.assertEqual(d.remaining, 3 - i - 1)
        d = _ratelimit.check(hdr(), "/api/chat")
        self.assertFalse(d.allowed)
        self.assertEqual(d.scope, "ip")
        self.assertGreaterEqual(d.retry_after, 1)

    def test_other_ip_unaffected(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        self.assertTrue(_ratelimit.check(hdr("1.1.1.1"), "/api/chat").allowed)
        self.assertFalse(_ratelimit.check(hdr("1.1.1.1"), "/api/chat").allowed)
        self.assertTrue(_ratelimit.check(hdr("2.2.2.2"), "/api/chat").allowed)

    def test_classes_have_separate_budgets(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        self.assertTrue(_ratelimit.check(hdr(), "/api/chat").allowed)
        self.assertFalse(_ratelimit.check(hdr(), "/api/assist").allowed)  # 같은 등급 공유
        self.assertTrue(_ratelimit.check(hdr(), "/api/ops_stats").allowed)  # 다른 등급

    def test_window_recovery(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        self.assertTrue(_ratelimit.check(hdr(), "/api/chat").allowed)
        self.assertFalse(_ratelimit.check(hdr(), "/api/chat").allowed)
        # 윈도를 지난 것처럼 타임스탬프를 과거로 민다
        for k in list(_ratelimit._HITS):
            _ratelimit._HITS[k] = [t - (_ratelimit.WINDOW + 1) for t in _ratelimit._HITS[k]]
        self.assertTrue(_ratelimit.check(hdr(), "/api/chat").allowed)

    def test_zero_limit_means_unlimited(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "0"
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_LLM"] = "0"
        for _ in range(50):
            self.assertTrue(_ratelimit.check(hdr(), "/api/chat").allowed)


class TestGlobalCap(Base):
    def test_rotating_ips_cannot_bypass(self):
        """XFF 는 위조 가능하다. 전역 상한이 없으면 IP 한도는 우회된다."""
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_LLM"] = "3"
        ok = sum(1 for i in range(20)
                 if _ratelimit.check(hdr("10.0.0.%d" % i), "/api/chat").allowed)
        self.assertEqual(ok, 3)

    def test_global_denial_is_labelled(self):
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_LLM"] = "1"
        _ratelimit.check(hdr("1.1.1.1"), "/api/chat")
        d = _ratelimit.check(hdr("2.2.2.2"), "/api/chat")
        self.assertFalse(d.allowed)
        self.assertEqual(d.scope, "global")

    def test_global_block_does_not_burn_ip_budget(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "5"
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_LLM"] = "1"
        _ratelimit.check(hdr("9.9.9.9"), "/api/chat")   # 전역 1 소진
        for _ in range(10):
            _ratelimit.check(hdr("1.1.1.1"), "/api/chat")  # 전부 전역에서 차단
        self.assertEqual(len(_ratelimit._HITS.get("llm:1.1.1.1", [])), 0)

    def test_global_budget_per_class(self):
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_LLM"] = "1"
        _ratelimit.check(hdr(), "/api/chat")
        self.assertFalse(_ratelimit.check(hdr("3.3.3.3"), "/api/chat").allowed)
        self.assertTrue(_ratelimit.check(hdr("3.3.3.3"), "/api/tts").allowed)


class TestResponseContract(Base):
    def test_guard_returns_429_envelope_with_headers(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        h = FakeHeaders({"x-forwarded-for": "5.5.5.5", "sec-fetch-site": "same-origin"})
        ok, code, msg = _guard.check(h, "/api/chat")
        self.assertTrue(ok)
        ok, code, msg = _guard.check(h, "/api/chat")
        self.assertFalse(ok)
        self.assertEqual(code, 429)

        fh = FakeHandler(h)
        body = _guard.deny(fh, code, msg)
        self.assertEqual(fh.status, 429)
        self.assertEqual(body["code"], "RATE_LIMITED")
        self.assertFalse(body["ok"])
        self.assertIsInstance(body["error"], str)
        self.assertIsNotNone(fh.header("Retry-After"))
        self.assertEqual(fh.header("X-RateLimit-Limit"), "1")
        # 내부 사유 문구는 기본적으로 노출하지 않는다
        self.assertNotIn("rate limit exceeded", json.dumps(body, ensure_ascii=False))

    def test_retry_after_is_positive_integer(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        _ratelimit.check(hdr(), "/api/chat")
        d = _ratelimit.check(hdr(), "/api/chat")
        v = dict(d.headers()).get("Retry-After")
        self.assertTrue(v and v.isdigit() and 1 <= int(v) <= int(_ratelimit.WINDOW) + 2)

    def test_no_rate_headers_on_non_429(self):
        fh = FakeHandler(hdr())
        _errors.send(fh, status=403, extra_headers=None)
        self.assertIsNone(fh.header("Retry-After"))

    def test_extra_headers_reject_injection(self):
        fh = FakeHandler(hdr())
        _errors.send(fh, status=429, extra_headers=[("Retry-After", "5\r\nX-Evil: 1")])
        self.assertNotIn("\r", fh.header("Retry-After") or "")
        self.assertIsNone(fh.header("X-Evil"))

    def test_ok_response_carries_remaining(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "5"
        d = _ratelimit.check(hdr(), "/api/chat")
        self.assertEqual(dict(d.headers()).get("X-RateLimit-Remaining"), "4")


class TestMemory(Base):
    def test_keys_are_bounded(self):
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_DEFAULT"] = "0"
        for i in range(_ratelimit.MAX_KEYS + 500):
            _ratelimit.check(hdr("10.%d.%d.%d" % (i // 65536, (i // 256) % 256, i % 256)),
                             "/api/ops_stats")
        self.assertLessEqual(len(_ratelimit._HITS), _ratelimit.MAX_KEYS + 1)

    def test_expired_keys_are_dropped(self):
        _ratelimit.check(hdr("7.7.7.7"), "/api/ops_stats")
        for k in list(_ratelimit._HITS):
            _ratelimit._HITS[k] = [t - (_ratelimit.WINDOW + 5) for t in _ratelimit._HITS[k]]
        _ratelimit.check(hdr("8.8.8.8"), "/api/ops_stats")
        self.assertNotIn("default:7.7.7.7", _ratelimit._HITS)


class TestAvailability(Base):
    def test_guard_allows_when_limiter_raises(self):
        """제한 로직 장애가 전면 장애가 되면 안 된다."""
        class Boom(object):
            def check(self, *a, **kw):
                raise RuntimeError("boom")

        orig = _guard._ratelimit
        try:
            _guard._ratelimit = Boom()
            self.assertTrue(_guard.rate_ok(FakeHeaders({}), "/api/chat"))
        finally:
            _guard._ratelimit = orig

    def test_guard_works_without_module(self):
        orig = _guard._ratelimit
        try:
            _guard._ratelimit = None
            self.assertTrue(_guard.rate_ok(FakeHeaders({}), "/api/chat"))
            self.assertEqual(_guard.rate_headers(), [])
            self.assertIsInstance(_guard._limit("/api/chat"), int)
        finally:
            _guard._ratelimit = orig

    def test_missing_headers_do_not_crash(self):
        self.assertTrue(_ratelimit.check(FakeHeaders({}), "/api/chat").allowed)
        self.assertEqual(_ratelimit.client_ip(FakeHeaders({})), "unknown")


class TestConfig(Base):
    def test_legacy_env_still_controls_default_class(self):
        os.environ["CALLBOT_RATE_LIMIT"] = "2"
        self.assertEqual(_ratelimit.limits("default")[0], 2)
        self.assertEqual(_guard._limit("/api/ops_stats"), 2)

    def test_off_switch(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        os.environ["CALLBOT_RATE_LIMIT_OFF"] = "1"
        for _ in range(30):
            self.assertTrue(_ratelimit.check(hdr(), "/api/chat").allowed)

    def test_invalid_env_falls_back_to_default(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "not-a-number"
        self.assertEqual(_ratelimit.limits("llm")[0], _ratelimit.DEFAULTS["llm"][0])
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "-5"
        self.assertEqual(_ratelimit.limits("llm")[0], _ratelimit.DEFAULTS["llm"][0])

    def test_global_factor(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "10"
        os.environ["CALLBOT_RATE_LIMIT_GLOBAL_FACTOR"] = "3"
        self.assertEqual(_ratelimit.limits("llm")[1], 30)

    def test_xff_trust_can_be_disabled(self):
        os.environ["CALLBOT_RATE_LIMIT_TRUST_XFF"] = "0"
        self.assertEqual(_ratelimit.client_ip(hdr("1.2.3.4")), "unknown")


class TestPrivacy(Base):
    def test_snapshot_has_no_client_ip(self):
        _ratelimit.check(hdr("203.0.113.9"), "/api/chat")
        s = json.dumps(_ratelimit.snapshot(), ensure_ascii=False)
        self.assertNotIn("203.0.113.9", s)
        self.assertIn("tracked_keys", s)

    def test_ip_key_is_length_capped(self):
        self.assertLessEqual(len(_ratelimit.client_ip(hdr("x" * 500))), 64)


class TestWiring(unittest.TestCase):
    """전 라우트가 경로를 넘겨 등급 제한을 받는지 — 소스 수준 확인."""

    def test_handlers_pass_path_to_guard(self):
        import glob
        checked = 0
        for f in glob.glob(os.path.join(ROOT, "api", "*.py")):
            if os.path.basename(f).startswith("_"):
                continue
            with io.open(f, encoding="utf-8") as fh:
                src = fh.read()
            for line in src.splitlines():
                if line.lstrip().startswith("#"):
                    continue  # 주석 언급은 호출이 아니다
                if "_guard.check(" in line:
                    checked += 1
                    self.assertIn("self.path", line,
                                  "%s: check() 에 경로를 넘겨야 등급 한도가 적용된다" % f)
        self.assertGreaterEqual(checked, 5, "배선된 라우트가 너무 적다")

    def test_guard_check_uses_path_for_rate_limit(self):
        with io.open(os.path.join(ROOT, "api", "_guard.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("rate_ok(headers, path)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
