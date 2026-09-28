# -*- coding: utf-8 -*-
"""api/monitoring.py 잔여 방어 분기 회귀 — 네트워크 미사용(로컬 응답만 흉내).

89% 가 이미 덮여 있다. 남은 것은 **입력이 예상을 벗어났을 때만 도는 방어 분기**와
전송 성공 경로다. 모니터링 자체가 죽으면 정작 필요한 순간(다른 API 가 500 을 낼 때)
조용히 아무것도 남기지 않는 상태가 된다 — 방어 코드가 실제로 도는지 고정한다.

검증 대상
  1) DSN 파싱 실패 격리 — parse_dsn 이 어떤 입력에도 예외 대신 None
  2) scrub 격리        — str() 자체가 실패해도 예외 대신 "<unprintable>"
  3) 스택프레임 격리   — 트레이스백 추출 실패 시 빈 목록(예외 전파 금지)
  4) 컨텍스트 필드     — None 값은 전송하지 않는다(빈 필드로 채우지 않음)
  5) 전송 성공 경로    — _post() 가 실제로 성공해도 예외 없이 끝난다
  6) 최상위 격리       — capture_error 내부 어디서 터져도 예외 대신 None

실행: python3 -m pytest tests/test_monitoring_edge.py -q
"""
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _monitoring as monitoring  # noqa: E402

GOOD_DSN = "https://abc123@o0.ingest.sentry.io/4507"


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SENTRY_DSN")
        self._timeout = monitoring.TIMEOUT

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SENTRY_DSN", None)
        else:
            os.environ["SENTRY_DSN"] = self._saved
        monitoring.TIMEOUT = self._timeout


# ==========================================================================
# 1) DSN 파싱 실패 격리
# ==========================================================================
class TestParseDsnEdge(unittest.TestCase):
    def test_none_input_does_not_raise(self):
        self.assertIsNone(monitoring.parse_dsn(None))

    def test_non_string_input_does_not_raise(self):
        self.assertIsNone(monitoring.parse_dsn(12345))
        self.assertIsNone(monitoring.parse_dsn(["https://k@h/1"]))


# ==========================================================================
# 2) scrub 격리
# ==========================================================================
class _Unprintable(object):
    def __str__(self):
        raise RuntimeError("문자열화 실패")


class TestScrubEdge(unittest.TestCase):
    def test_object_whose_str_raises_is_not_propagated(self):
        self.assertEqual(monitoring.scrub(_Unprintable()), "<unprintable>")


# ==========================================================================
# 3) 스택프레임 격리
# ==========================================================================
class _NotAnException(object):
    """__traceback__ 속성이 없는 객체 — extract_tb 호출이 AttributeError 로 터진다."""
    pass


class TestFramesEdge(unittest.TestCase):
    def test_object_without_traceback_returns_empty_list(self):
        self.assertEqual(monitoring._frames(_NotAnException()), [])

    def test_real_exception_yields_filename_lineno_function(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            frames = monitoring._frames(e)
        self.assertTrue(frames, "실제 예외는 프레임을 최소 1건 남겨야 한다")
        f0 = frames[0]
        self.assertEqual(set(f0.keys()), {"filename", "lineno", "function"})
        self.assertTrue(f0["filename"].endswith(".py"))
        self.assertIsInstance(f0["lineno"], int)


# ==========================================================================
# 4) 컨텍스트 필드 — None 값 스킵
# ==========================================================================
class TestEnvelopeContextEdge(unittest.TestCase):
    def test_none_context_values_are_omitted(self):
        exc = ValueError("boom")
        body = monitoring._envelope(exc, {"route": "/api/x", "request_id": None}, "e" * 32)
        text = body.decode("utf-8")
        self.assertIn("route", text)
        self.assertNotIn("request_id", text)


# ==========================================================================
# 5) 전송 성공 경로
# ==========================================================================
class _FakeCtx(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPostSuccessPath(unittest.TestCase):
    def test_post_success_does_not_raise(self):
        saved = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: _FakeCtx()
        try:
            # 예외를 던지지 않으면 통과 — 반환값도 없다(성공 시에도 side-effect 없음)
            self.assertIsNone(monitoring._post("https://example.test/api/1/envelope/", "key", b"data"))
        finally:
            urllib.request.urlopen = saved


# ==========================================================================
# 6) 최상위 격리 — capture_error 내부 어디서든 예외는 None 으로 흡수
# ==========================================================================
class TestCaptureErrorTopLevelIsolation(EnvGuard):
    def test_envelope_building_failure_returns_none_not_raises(self):
        os.environ["SENTRY_DSN"] = GOOD_DSN
        saved = monitoring._envelope
        monitoring._envelope = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("envelope 조립 실패"))
        try:
            self.assertIsNone(monitoring.capture_error(ValueError("boom")))
        finally:
            monitoring._envelope = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
