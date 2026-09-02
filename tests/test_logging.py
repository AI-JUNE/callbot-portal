# -*- coding: utf-8 -*-
"""api/_log.py 구조화 로깅 불변식 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS '구조화 로깅': 요청 ID·소요시간·에러코드, PII 미기록)
  1) 필수 필드(request_id·route·method·status·duration_ms) 존재, JSON 1줄
  2) PII 미기록 — 쿼리스트링 제거, 예외 메시지 미기록, set() 값 마스킹
  3) 요청 ID 전파(x-request-id / x-vercel-id) 및 위험문자 제거
  4) 에러코드 정규화 안정성
  5) 이중 종료 방지, 로깅 실패·비활성화가 서비스에 영향 없음
  6) 핸들러 배선(chat·assist·ops_stats)

실행: python3 tests/test_logging.py
"""
import io
import os
import sys
import json
import unittest
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _log  # noqa: E402


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def lines(buf):
    return [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]


class Headers(dict):
    """http.client.HTTPMessage 유사 — .get(name) 소문자 조회."""
    def get(self, k, d=None):
        return dict.get(self, k.lower(), d)


class TestFields(unittest.TestCase):
    def test_success_record_has_required_fields(self):
        with captured() as b:
            _log.begin(None, "/api/chat", "POST").finish(200)
        (r,) = lines(b)
        for f in ("ts", "level", "service", "env", "release",
                  "request_id", "route", "method", "path", "status", "duration_ms"):
            self.assertIn(f, r, f)
        self.assertEqual(r["level"], "info")
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["service"], "callbot-portal")
        self.assertIsInstance(r["duration_ms"], int)
        self.assertGreaterEqual(r["duration_ms"], 0)

    def test_one_json_line_per_request(self):
        with captured() as b:
            _log.begin(None, "/api/a", "GET").finish(200)
            _log.begin(None, "/api/b", "GET").finish(200)
        self.assertEqual(len(b.getvalue().strip().splitlines()), 2)

    def test_level_by_status(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").finish(200)
            _log.begin(None, "/x", "GET").finish(403)
            _log.begin(None, "/x", "GET").finish(500)
        self.assertEqual([r["level"] for r in lines(b)], ["info", "warn", "error"])


class TestNoPII(unittest.TestCase):
    def test_query_string_stripped(self):
        with captured() as b:
            _log.begin(None, "/api/chat", "POST",
                       path="/api/chat?phone=010-1234-5678&email=hong@example.com").finish(200)
        (r,) = lines(b)
        self.assertEqual(r["path"], "/api/chat")
        self.assertNotIn("010", json.dumps(r))
        self.assertNotIn("hong", json.dumps(r))

    def test_exception_message_not_logged(self):
        with captured() as b:
            _log.begin(None, "/api/chat", "POST").fail(
                ValueError("고객 010-1234-5678 / hong@example.com 처리 실패"))
        (r,) = lines(b)
        raw = json.dumps(r, ensure_ascii=False)
        self.assertEqual(r["error_code"], "VALUE_ERROR")
        self.assertEqual(r["status"], 500)
        for leak in ("010-1234-5678", "hong@example.com", "처리 실패"):
            self.assertNotIn(leak, raw, leak)

    def test_extra_values_are_scrubbed(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").set(note="연락처 010-1234-5678", n=3).finish(200)
        (r,) = lines(b)
        self.assertEqual(r["extra"]["n"], 3)
        self.assertNotIn("010-1234-5678", json.dumps(r, ensure_ascii=False))

    def test_no_body_or_header_values_in_record(self):
        h = Headers({"authorization": "Bearer secret-token-xyz", "cookie": "sid=abc"})
        with captured() as b:
            _log.begin(h, "/x", "GET").finish(200)
        raw = json.dumps(lines(b)[0])
        self.assertNotIn("secret-token-xyz", raw)
        self.assertNotIn("sid=abc", raw)


class TestRequestId(unittest.TestCase):
    def test_inherits_inbound_id(self):
        rq = _log.begin(Headers({"x-request-id": "abc-123"}), "/x", "GET")
        self.assertEqual(rq.request_id, "abc-123")

    def test_inherits_vercel_id(self):
        rq = _log.begin(Headers({"x-vercel-id": "icn1::abc123"}), "/x", "GET")
        self.assertEqual(rq.request_id, "icn1::abc123")

    def test_strips_unsafe_chars_and_limits_length(self):
        rq = _log.begin(Headers({"x-request-id": "a<script>b\n\"c" + "z" * 200}), "/x", "GET")
        for bad in ("<", ">", "\n", '"'):
            self.assertNotIn(bad, rq.request_id)
        self.assertLessEqual(len(rq.request_id), 64)

    def test_generates_when_absent(self):
        a = _log.begin(None, "/x", "GET").request_id
        b2 = _log.begin(Headers({}), "/x", "GET").request_id
        self.assertTrue(a and b2 and a != b2)

    def test_id_shared_between_log_and_response(self):
        rq = _log.begin(None, "/x", "GET")
        with captured() as b:
            rq.finish(200)
        self.assertEqual(lines(b)[0]["request_id"], rq.request_id)


class TestErrorCode(unittest.TestCase):
    def test_normalization(self):
        cases = [(ValueError(), "VALUE_ERROR"), (KeyError(), "KEY_ERROR"),
                 (TimeoutError(), "TIMEOUT_ERROR"), (OSError(), "OS_ERROR"),
                 (RuntimeError(), "RUNTIME_ERROR")]
        for exc, code in cases:
            self.assertEqual(_log.error_code(exc), code)

    def test_stable_across_messages(self):
        self.assertEqual(_log.error_code(ValueError("a")), _log.error_code(ValueError("b")))


class TestRobustness(unittest.TestCase):
    def test_double_finish_emits_once(self):
        with captured() as b:
            rq = _log.begin(None, "/x", "GET")
            rq.finish(200)
            rq.finish(500)
            rq.fail(ValueError("x"))
        self.assertEqual(len(lines(b)), 1)

    def test_context_manager_reraises_and_logs(self):
        with captured() as b:
            with self.assertRaises(KeyError):
                with _log.begin(None, "/x", "GET"):
                    raise KeyError("boom")
        self.assertEqual(lines(b)[0]["error_code"], "KEY_ERROR")

    def test_context_manager_success(self):
        with captured() as b:
            with _log.begin(None, "/x", "GET"):
                pass
        self.assertEqual(lines(b)[0]["status"], 200)

    def test_disabled_is_silent(self):
        saved = os.environ.get("CALLBOT_LOG")
        os.environ["CALLBOT_LOG"] = "off"
        try:
            with captured() as b:
                _log.begin(None, "/x", "GET").finish(200)
            self.assertEqual(b.getvalue().strip(), "")
        finally:
            if saved is None:
                os.environ.pop("CALLBOT_LOG", None)
            else:
                os.environ["CALLBOT_LOG"] = saved

    def test_unserializable_extra_does_not_raise(self):
        with captured():
            _log.begin(None, "/x", "GET").set(obj=object()).finish(200)  # 예외 없이 통과

    def test_attach_tolerates_bad_handler(self):
        _log.attach(object(), _log.begin(None, "/x", "GET"))  # 예외 없이 통과


class TestHandlersWired(unittest.TestCase):
    def test_handlers_use_log(self):
        for name in ("chat.py", "assist.py", "ops_stats.py"):
            with open(os.path.join(ROOT, "api", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("import _log", src, name)
            self.assertIn("_log.begin(", src, name)
            self.assertIn("_log.attach(", src, name)
            self.assertIn("X-Request-Id", src, name)
            # 기본 접근로그(쿼리스트링 PII 유출) 침묵 배선
            self.assertIn("log_message = _log.suppress_access_log", src, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
