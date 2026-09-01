# -*- coding: utf-8 -*-
"""api/monitoring.py 불변식 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (MONITORING_GUIDE.md 3항)
  1) DSN 미설정 시 완전한 no-op
  2) PII 마스킹(주민등록번호·카드·휴대전화·이메일·계좌)
  3) DSN 하드코딩 없음(소스 스캔)
  4) 전송 실패가 서비스에 영향 없음(예외 미전파)
  5) guard() 는 오류를 리포트하되 그대로 전파

실행: python3 tests/test_monitoring.py
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import monitoring  # noqa: E402

GOOD_DSN = "https://abc123@o0.ingest.sentry.io/4507"


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SENTRY_DSN")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SENTRY_DSN", None)
        else:
            os.environ["SENTRY_DSN"] = self._saved


class TestNoop(EnvGuard):
    def test_no_dsn_is_noop(self):
        os.environ.pop("SENTRY_DSN", None)
        self.assertFalse(monitoring.enabled())
        self.assertIsNone(monitoring.capture_error(ValueError("boom")))

    def test_blank_dsn_is_noop(self):
        os.environ["SENTRY_DSN"] = "   "
        self.assertFalse(monitoring.enabled())
        self.assertIsNone(monitoring.capture_error(ValueError("boom")))

    def test_malformed_dsn_is_noop(self):
        for bad in ("not-a-url", "https://o0.ingest.sentry.io/4507", "https://key@host", "ftp://k@h/1"):
            os.environ["SENTRY_DSN"] = bad
            self.assertFalse(monitoring.enabled(), bad)
            self.assertIsNone(monitoring.capture_error(ValueError("boom")), bad)

    def test_status_never_leaks_dsn(self):
        os.environ["SENTRY_DSN"] = GOOD_DSN
        st = monitoring.status()
        self.assertTrue(st["enabled"])
        self.assertNotIn("abc123", repr(st))
        self.assertNotIn("sentry.io", repr(st))


class TestScrub(unittest.TestCase):
    def test_rrn(self):
        self.assertEqual(monitoring.scrub("고객 900101-1234567 조회"), "고객 900101-******* 조회")

    def test_card(self):
        self.assertNotIn("4111", monitoring.scrub("카드 4111-1111-1111-1111"))
        self.assertNotIn("4111", monitoring.scrub("카드 4111 1111 1111 1111"))

    def test_phone(self):
        self.assertEqual(monitoring.scrub("연락처 010-1234-5678"), "연락처 01*-****-****")
        self.assertEqual(monitoring.scrub("연락처 01012345678"), "연락처 01*-****-****")

    def test_email(self):
        self.assertEqual(monitoring.scrub("메일 hong@example.co.kr"), "메일 ***@***")

    def test_account(self):
        self.assertEqual(monitoring.scrub("계좌 110-1234-567890"), "계좌 ***-****-****")

    def test_non_string_and_safe_text(self):
        self.assertEqual(monitoring.scrub(12345), "12345")
        self.assertEqual(monitoring.scrub("정상 메시지"), "정상 메시지")

    def test_envelope_masks_pii(self):
        exc = ValueError("실패: 010-1234-5678 / hong@example.com")
        body = monitoring._envelope(exc, {"route": "/api/x", "phone": "010-9999-8888"}, "e" * 32).decode("utf-8")
        for leak in ("010-1234-5678", "hong@example.com", "010-9999-8888"):
            self.assertNotIn(leak, body, leak)


class TestNoHardcodedDsn(unittest.TestCase):
    def test_source_has_no_dsn_literal(self):
        with open(os.path.join(ROOT, "api", "monitoring.py"), encoding="utf-8") as f:
            src = f.read()
        # 실제 DSN 리터럴만 탐지(<key>@<host> 같은 문서용 자리표시자는 제외)
        self.assertIsNone(re.search(r"https?://[A-Za-z0-9]{8,}@[A-Za-z0-9.-]+", src),
                          "monitoring.py 에 DSN 리터럴이 있으면 안 된다")
        self.assertIn('os.environ.get("SENTRY_DSN")', src)


class TestFailureIsolation(EnvGuard):
    def test_unreachable_endpoint_does_not_raise(self):
        # 라우팅 불가 주소 — 전송은 실패하지만 예외가 전파되면 안 된다
        os.environ["SENTRY_DSN"] = "http://k@127.0.0.1:1/9"
        monitoring.TIMEOUT = 0.3
        self.assertIsNotNone(monitoring.capture_error(RuntimeError("boom")))

    def test_guard_reports_and_reraises(self):
        os.environ.pop("SENTRY_DSN", None)
        with self.assertRaises(KeyError):
            with monitoring.guard("/api/chat", "POST"):
                raise KeyError("missing")

    def test_guard_passthrough_on_success(self):
        with monitoring.guard("/api/chat", "POST"):
            pass


class TestHandlersWired(unittest.TestCase):
    def test_handlers_call_capture_error(self):
        for name in ("chat.py", "assist.py", "ops_stats.py"):
            with open(os.path.join(ROOT, "api", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("import monitoring", src, name)
            self.assertIn("monitoring.capture_error", src, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
