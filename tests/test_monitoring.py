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
import _monitoring as monitoring  # noqa: E402

GOOD_DSN = "https://abc123@o0.ingest.sentry.io/4507"


ENV_KEYS = ("SENTRY_DSN", "SENTRY_ALLOW_INSECURE", "SENTRY_HOSTS")


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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


class TestScrubKoreanBoundary(unittest.TestCase):
    """회귀(30차): 한국어 문장에서 마스킹이 **한 건도 걸리지 않던** 결함.

    규칙 경계가 `\\b` 였고 파이썬 3 의 `\\w` 는 유니코드라 한글도 낱말 문자다.
    그래서 조사·단위가 숫자에 붙는 실제 대화문(「…1234567이에요」·「…2222번으로」)
    에서는 끝쪽 경계가 성립하지 않아 원문이 그대로 통과했다. 기존 회귀는 모두
    숫자 뒤에 **공백**을 둔 문장이라 침묵했다.
    """

    SUFFIXED = (
        ("주민번호는 900101-1234567이에요", "900101-1234567"),
        ("번호는 010-1111-2222번으로", "010-1111-2222"),
        ("카드 1234-5678-9012-3456으로 결제", "1234-5678-9012-3456"),
        ("계좌 110-1234-567890로 보냈어요", "110-1234-567890"),
        ("(010-1111-2222)", "010-1111-2222"),
        ("연락처:01011112222、", "01011112222"),
    )

    def test_josa_attached_numbers_are_masked(self):
        for text, secret in self.SUFFIXED:
            self.assertNotIn(secret, monitoring.scrub(text), text)

    def test_prefixed_korean_is_masked_too(self):
        self.assertNotIn("01011112222", monitoring.scrub("고객번호01011112222"))

    def test_longer_digit_runs_are_still_not_partially_matched(self):
        """`\\b` 를 둔 원래 이유(긴 숫자열 안쪽 부분 일치 방지)는 지켜진다."""
        for safe in ("12345678901234567890", "주문 12345678901234567 건"):
            self.assertEqual(monitoring.scrub(safe), safe, safe)

    def test_ordinary_numbers_are_not_over_masked(self):
        """과차단되면 로그가 못 읽게 된다 — 날짜·버전·금액은 건드리지 않는다."""
        for safe in ("2026-10-16 접수", "버전 1.2.3", "금액 37,000원",
                     "소요 1.5초", "건수 12건", "주문 A-12-3456"):
            self.assertEqual(monitoring.scrub(safe), safe, safe)

    def test_limit_is_stated_not_hidden(self):
        """숫자가 아닌 표기(말로 읽은 번호)는 이 규칙으로 가려지지 않는다.

        마스킹은 '표기 패턴' 방어다 — 「공일공 일일일일」 처럼 읽은 발화는
        걸리지 않는다. 그 한계를 회귀로 적어 둔다(해결됐다고 착각하지 않도록).
        """
        spoken = "제 번호는 공일공 일일일일 이이이이예요"
        self.assertEqual(monitoring.scrub(spoken), spoken)


class TestNoHardcodedDsn(unittest.TestCase):
    def test_source_has_no_dsn_literal(self):
        with open(os.path.join(ROOT, "api", "_monitoring.py"), encoding="utf-8") as f:
            src = f.read()
        # 실제 DSN 리터럴만 탐지(<key>@<host> 같은 문서용 자리표시자는 제외)
        self.assertIsNone(re.search(r"https?://[A-Za-z0-9]{8,}@[A-Za-z0-9.-]+", src),
                          "monitoring.py 에 DSN 리터럴이 있으면 안 된다")
        self.assertIn('os.environ.get("SENTRY_DSN")', src)


class TestFailureIsolation(EnvGuard):
    def test_unreachable_endpoint_does_not_raise(self):
        # 라우팅 불가 주소 — 전송은 실패하지만 예외가 전파되면 안 된다.
        # 평문 http·루프백은 기본으로 아웃바운드 가드가 막으므로(10-09) 개발
        # 플래그를 켜서 **실제로 전송을 시도하는** 경로를 지나게 한다.
        os.environ["SENTRY_DSN"] = "http://k@127.0.0.1:1/9"
        os.environ["SENTRY_ALLOW_INSECURE"] = "1"
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
    def test_handlers_report_errors(self):
        """핸들러가 예외를 모니터링으로 흘려보내는지 — 직접 호출 또는 _errors.handle 경유."""
        for name in ("chat.py", "assist.py", "ops_stats.py"):
            with open(os.path.join(ROOT, "api", name), encoding="utf-8") as f:
                src = f.read()
            self.assertTrue(
                "monitoring.capture_error" in src or "_errors.handle(" in src,
                name)

    def test_errors_handle_captures(self):
        """표준 에러 처리기(_errors.handle)가 5xx 를 monitoring 으로 보낸다."""
        with open(os.path.join(ROOT, "api", "_errors.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("import _monitoring as monitoring", src)   # 라이브러리는 _ 접두사(서버리스 함수로 배포되지 않게)
        self.assertIn("monitoring.capture_error", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
