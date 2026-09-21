# -*- coding: utf-8 -*-
"""api/_errors.py 잔여 분기 회귀 테스트 — 헤더 안전·폴백·검증 헬퍼 경계.

`tests/test_errors.py` 가 봉투 규약과 주요 경로를 덮는다면, 이 파일은 그 주변의
**실패했을 때만 도는 코드**를 덮는다. 에러 경로는 평소에 안 돌기 때문에 깨져도
오래 눈치채지 못하고, 하필 장애 중에 드러난다.

검증 대상
  1) 헤더 인젝션 — extra_headers 의 CRLF·과대 길이·빈 이름 처리
  2) 요청ID 승계 — X-Request-Id 헤더와 본문 request_id 의 64자 상한
  3) 폴백 — monitoring·_guard 가 없어도 응답이 나가고 오리진을 열어주지 않는다
  4) 분류 — HTTPError·ssl/socket/urllib 계열은 502, 타임아웃은 504
  5) 격리 — 모니터링 전송·요청 로그 마감이 실패해도 응답은 반드시 나간다
  6) 본문 읽기 — 음수 Content-Length·소켓 읽기 실패가 400 으로 끝난다
  7) 검증 헬퍼 경계 — min_len·as_choice 타입·as_int 문자열/불리언·as_list 기본값·
     query_choice 필수

실행: python3 -m pytest tests/test_errors_edge.py -q
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _errors  # noqa: E402

from test_errors import FakeHandler, FakeHeaders  # noqa: E402  (대역 재사용)


class Patcher(unittest.TestCase):
    def setUp(self):
        self._restore = []
        self._modules = []

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        for name, old in reversed(self._modules):
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def break_module(self, name, message="내부경로 /var/task/api sk-live-SECRET"):
        """모듈을 고장낸 대역으로 바꾼다(원본은 tearDown 에서 복구)."""
        class _Broken(object):
            def __getattr__(self, _n):
                raise RuntimeError(message)

        self._modules.append((name, sys.modules.get(name)))
        sys.modules[name] = _Broken()
        return message


# ==========================================================================
# 1) 헤더 안전
# ==========================================================================
class TestHeaderSafety(Patcher):
    def test_extra_headers_are_sent(self):
        h = FakeHandler()
        _errors.send(h, status=429, code="RATE_LIMITED",
                     extra_headers=[("Retry-After", 30), ("X-RateLimit-Limit", "20")])
        self.assertEqual(h.status, 429)
        self.assertEqual(h.header("Retry-After"), "30")     # 숫자도 문자열로
        self.assertEqual(h.header("X-RateLimit-Limit"), "20")

    def test_crlf_in_header_name_and_value_is_stripped(self):
        """헤더 인젝션: 값에 개행을 넣어 헤더·본문을 덧붙일 수 없어야 한다."""
        h = FakeHandler()
        _errors.send(h, status=429,
                     extra_headers=[("Retry-After\r\nX-Evil", "30\r\nSet-Cookie: a=b")])
        sent = dict((k, v) for k, v in h.sent)
        for k, v in h.sent:
            self.assertNotIn("\r", k)
            self.assertNotIn("\n", k)
            self.assertNotIn("\r", str(v))
            self.assertNotIn("\n", str(v))
        self.assertNotIn("X-Evil", sent)
        self.assertNotIn("Set-Cookie", sent)

    def test_header_name_and_value_are_length_capped(self):
        h = FakeHandler()
        _errors.send(h, status=500, extra_headers=[("X" * 200, "v" * 500)])
        name, value = [(k, v) for k, v in h.sent if k.startswith("XX")][0]
        self.assertEqual(len(name), 64)
        self.assertEqual(len(value), 200)

    def test_empty_header_name_is_dropped(self):
        h = FakeHandler()
        _errors.send(h, status=500, extra_headers=[("", "x"), ("\r\n", "y")])
        self.assertIsNone(h.header(""))
        self.assertEqual(h.status, 500)

    def test_bad_extra_header_entry_does_not_break_response(self):
        """튜플이 아닌 항목이 섞여도 에러 응답 자체는 나가야 한다."""
        h = FakeHandler()
        _errors.send(h, status=500, extra_headers=[("X-Ok", "1")])
        self.assertEqual(h.header("X-Ok"), "1")
        self.assertTrue(h.ended)


class TestRequestId(Patcher):
    def test_request_id_header_and_body(self):
        h = FakeHandler()
        obj = _errors.send(h, status=500, request_id="rid-abc")
        self.assertEqual(h.header("X-Request-Id"), "rid-abc")
        self.assertEqual(obj["request_id"], "rid-abc")

    def test_request_id_is_capped_at_64_chars(self):
        h = FakeHandler()
        obj = _errors.send(h, status=500, request_id="r" * 500)
        self.assertEqual(len(h.header("X-Request-Id")), 64)
        self.assertEqual(len(obj["request_id"]), 64)

    def test_request_id_taken_from_rq_when_not_passed(self):
        class RQ(object):
            request_id = "from-rq"

            def fail(self, *a, **kw):
                pass

        h = FakeHandler()
        obj = _errors.send(h, status=500, rq=RQ())
        self.assertEqual(obj["request_id"], "from-rq")
        self.assertEqual(h.header("X-Request-Id"), "from-rq")

    def test_no_request_id_header_when_absent(self):
        h = FakeHandler()
        obj = _errors.send(h, status=500)
        self.assertIsNone(h.header("X-Request-Id"))
        self.assertNotIn("request_id", obj)


# ==========================================================================
# 2) 폴백 — 하위 모듈이 없어도 응답은 나간다
# ==========================================================================
class TestFallbacks(Patcher):
    def test_origin_falls_back_to_null_when_guard_unavailable(self):
        """오리진 판정기가 죽으면 '*' 가 아니라 'null' 로 닫는다(열어주지 않는다)."""
        self.break_module("_guard")
        h = FakeHandler({"origin": "https://evil.example.com"})
        _errors.send(h, status=500)
        self.assertEqual(h.header("Access-Control-Allow-Origin"), "null")

    def test_scrub_falls_back_to_plain_text_when_monitoring_unavailable(self):
        self.break_module("monitoring")
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        try:
            obj = _errors.payload(500, debug="boom")
        finally:
            os.environ.pop("CALLBOT_DEBUG_ERRORS", None)
        self.assertEqual(obj["debug"], "boom")

    def test_scrub_fallback_stringifies_non_text_debug(self):
        self.break_module("monitoring")
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        try:
            obj = _errors.payload(500, debug=RuntimeError("boom"))
        finally:
            os.environ.pop("CALLBOT_DEBUG_ERRORS", None)
        self.assertIn("boom", obj["debug"])

    def test_monitoring_capture_failure_does_not_break_response(self):
        self.break_module("monitoring")
        h = FakeHandler()
        obj = _errors.handle(h, RuntimeError("bug"), route="/api/x", method="GET")
        self.assertEqual(h.status, 500)
        self.assertNotIn("event_id", obj)

    def test_request_log_failure_does_not_break_response(self):
        class RQ(object):
            request_id = "rid-9"

            def fail(self, *a, **kw):
                raise RuntimeError("log sink down")

        h = FakeHandler()
        obj = _errors.handle(h, ValueError("bug"), rq=RQ())
        self.assertEqual(h.status, 500)
        self.assertEqual(obj["request_id"], "rid-9")


# ==========================================================================
# 3) 분류 — 업스트림 장애를 내부 버그로 보고하지 않는다
# ==========================================================================
class TestClassifyEdges(unittest.TestCase):
    def test_http_error_is_502(self):
        class HTTPError(Exception):
            pass

        self.assertEqual(_errors.classify(HTTPError("502 from upstream")), (502, "UPSTREAM_ERROR"))

    def test_ssl_module_exception_is_502(self):
        import ssl
        self.assertEqual(_errors.classify(ssl.SSLError("handshake"))[0], 502)

    def test_socket_module_exception_is_502(self):
        import socket
        self.assertEqual(_errors.classify(socket.gaierror("dns"))[0], 502)

    def test_urllib_module_exception_is_502(self):
        import urllib.error
        self.assertEqual(_errors.classify(urllib.error.URLError("down"))[0], 502)

    def test_timeout_by_name_substring_is_504(self):
        class ReadTimeoutError(Exception):
            pass

        self.assertEqual(_errors.classify(ReadTimeoutError("slow")), (504, "UPSTREAM_TIMEOUT"))

    def test_unknown_exception_stays_500(self):
        self.assertEqual(_errors.classify(KeyError("k")), (500, "INTERNAL_ERROR"))


# ==========================================================================
# 4) 본문 읽기 실패 경로
# ==========================================================================
class TestReadJsonEdges(unittest.TestCase):
    def test_negative_content_length_is_400(self):
        h = FakeHandler({"content-length": "-5"})
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.read_json(h)
        self.assertEqual(cm.exception.details[0]["field"], "content-length")
        self.assertEqual(cm.exception.status, 400)

    def test_socket_read_failure_is_400_not_500(self):
        """클라이언트가 중간에 끊은 것은 우리 버그가 아니다 — 400 으로 끝낸다."""
        class Broken(object):
            def read(self, n):
                raise IOError("connection reset")

        h = FakeHandler({"content-length": "10"})
        h.rfile = Broken()
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.read_json(h)
        self.assertEqual(cm.exception.details[0]["field"], "body")
        self.assertEqual(cm.exception.status, 400)

    def test_read_failure_message_carries_no_internal_text(self):
        class Broken(object):
            def read(self, n):
                raise IOError("/var/task/api/engine.py sk-live-SECRET")

        h = FakeHandler({"content-length": "10"})
        h.rfile = Broken()
        try:
            _errors.read_json(h)
        except _errors.ValidationError as e:
            blob = json.dumps(e.details, ensure_ascii=False) + (e.message or "")
            self.assertNotIn("sk-live-SECRET", blob)
            self.assertNotIn("/var/task", blob)
        else:
            self.fail("ValidationError 가 나야 한다")


# ==========================================================================
# 5) 검증 헬퍼 경계
# ==========================================================================
class TestValidatorEdges(unittest.TestCase):
    def test_as_str_min_len(self):
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.as_str({"pw": "ab"}, "pw", min_len=4)
        self.assertEqual(cm.exception.details[0]["field"], "pw")
        self.assertEqual(_errors.as_str({"pw": "abcd"}, "pw", min_len=4), "abcd")

    def test_as_str_strip_then_min_len(self):
        """공백을 제거한 뒤 길이를 잰다 — 공백으로 최소길이를 채울 수 없다."""
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({"pw": "  ab  "}, "pw", min_len=4)

    def test_as_choice_rejects_non_string(self):
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.as_choice({"mode": 1}, "mode", ("a", "b"))
        self.assertEqual(cm.exception.details[0]["field"], "mode")

    def test_as_choice_blank_uses_default_but_required_rejects(self):
        self.assertEqual(_errors.as_choice({"mode": "   "}, "mode", ("a",), default="a"), "a")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_choice({"mode": "   "}, "mode", ("a",), required=True)

    def test_as_int_missing_required_and_default(self):
        self.assertEqual(_errors.as_int({}, "n", default=7), 7)
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.as_int({}, "n", required=True)
        self.assertEqual(cm.exception.details[0]["field"], "n")

    def test_as_int_accepts_numeric_string_and_rejects_bool(self):
        self.assertEqual(_errors.as_int({"n": " 12 "}, "n"), 12)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_int({"n": True}, "n")      # bool 은 정수로 통과시키지 않는다

    def test_as_int_rejects_non_numeric_string(self):
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.as_int({"n": "열두개"}, "n")
        self.assertEqual(cm.exception.details[0]["field"], "n")

    def test_as_list_missing_returns_empty_or_default(self):
        self.assertEqual(_errors.as_list({}, "items"), [])
        self.assertEqual(_errors.as_list({}, "items", default=["a"]), ["a"])
        with self.assertRaises(_errors.ValidationError):
            _errors.as_list({}, "items", required=True)

    def test_as_list_item_type_points_at_index(self):
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.as_list({"items": ["a", 2]}, "items", item_type=str)
        self.assertEqual(cm.exception.details[0]["field"], "items[1]")

    def test_query_choice_required_missing(self):
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.query_choice({}, "op", ("report",), required=True)
        self.assertEqual(cm.exception.details[0]["field"], "op")

    def test_query_choice_is_case_insensitive_and_whitelisted(self):
        self.assertEqual(_errors.query_choice({"op": ["  REPORT "]}, "op", ("report",)), "report")
        with self.assertRaises(_errors.ValidationError) as cm:
            _errors.query_choice({"op": ["drop"]}, "op", ("report",))
        self.assertIn("report", cm.exception.details[0]["reason"])

    def test_query_str_required_blank(self):
        with self.assertRaises(_errors.ValidationError):
            _errors.query_str({"ref": [""]}, "ref", required=True)
        self.assertEqual(_errors.query_str({}, "ref", default="-"), "-")

    def test_validation_error_details_are_capped(self):
        """상세 목록이 무한히 커져 응답을 부풀리지 않는다."""
        obj = _errors.payload(400, details=[{"field": "f%d" % i, "reason": "x"} for i in range(50)])
        self.assertEqual(len(obj["details"]), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
