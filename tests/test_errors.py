# -*- coding: utf-8 -*-
"""api/_errors.py 표준 에러 응답 + 입력검증 불변식 테스트 — 의존성 0, 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS '표준 에러 응답 전 API 통일 + 입력검증')
  1) 봉투 규약 — ok/error(문자열)/code/status, request_id·details·event_id 선택
  2) 내부 문구 미노출 — str(exc) 가 기본 응답에 절대 없다(디버그 스위치 OFF 기본)
  3) 예외 분류 안정성 — 업스트림/타임아웃/버그 구분
  4) 입력검증 — 타입·길이·화이트리스트·본문 상한(413), 값 몰래 고치지 않음
  5) 전송 헬퍼 — 상태코드·헤더·Content-Length 정확, 전송 실패가 서비스에 전파 안 됨
  6) 4xx 는 모니터링으로 보내지 않는다(노이즈 차단), 5xx 만 보낸다
  7) 전 API 배선 — 핸들러가 표준 경로를 쓰고 raw str(e) 를 반환하지 않는다

실행: python3 tests/test_errors.py
"""
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _errors  # noqa: E402


SECRET = "sk-live-DEADBEEF-내부경로/var/task/api/engine.py"


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class FakeHandler(object):
    """BaseHTTPRequestHandler 흉내 — 네트워크 없이 응답을 수집한다."""

    def __init__(self, headers=None, body=b""):
        self.headers = FakeHeaders(headers or {})
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()
        self.ended = False
        self.rfile = _Reader(body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        self.ended = True

    # 편의
    def header(self, k):
        for a, b in self.sent:
            if a.lower() == k.lower():
                return b
        return None

    def json(self):
        return json.loads(self.wfile.data.decode("utf-8"))


class _Reader(object):
    def __init__(self, body):
        self.body = body

    def read(self, n):
        return self.body[:n]


class BrokenHandler(FakeHandler):
    def send_response(self, code):
        raise IOError("client gone")


def _mk(body_obj, extra_headers=None):
    raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    h = {"content-length": str(len(raw))}
    h.update(extra_headers or {})
    return FakeHandler(h, raw)


# --------------------------------------------------------------------------
class TestEnvelope(unittest.TestCase):
    def test_shape(self):
        p = _errors.payload(400, request_id="rid-1")
        self.assertEqual(p["ok"], False)
        self.assertEqual(p["code"], "INVALID_REQUEST")
        self.assertEqual(p["status"], 400)
        self.assertEqual(p["request_id"], "rid-1")

    def test_error_stays_string_for_console_compat(self):
        """콘솔이 '오류: ' + d.error 로 쓰므로 error 는 항상 문자열이어야 한다."""
        for st in (400, 401, 403, 413, 429, 500, 502, 504):
            self.assertIsInstance(_errors.payload(st)["error"], str)
            self.assertTrue(_errors.payload(st)["error"])

    def test_every_status_has_message(self):
        for st, code in _errors.CODE_BY_STATUS.items():
            self.assertIn(code, _errors.MESSAGE_BY_CODE, st)

    def test_unknown_status_falls_back(self):
        p = _errors.payload(599)
        self.assertEqual(p["code"], "INTERNAL_ERROR")
        p2 = _errors.payload("나쁜값")
        self.assertEqual(p2["status"], 500)

    def test_details_and_event_id_optional(self):
        p = _errors.payload(500)
        self.assertNotIn("details", p)
        self.assertNotIn("event_id", p)
        self.assertNotIn("request_id", p)


class TestNoInternalLeak(unittest.TestCase):
    def setUp(self):
        os.environ.pop("CALLBOT_DEBUG_ERRORS", None)

    tearDown = setUp

    def test_debug_omitted_by_default(self):
        p = _errors.payload(500, debug=SECRET)
        self.assertNotIn("debug", p)
        self.assertNotIn("DEADBEEF", json.dumps(p, ensure_ascii=False))

    def test_handle_does_not_leak_exception_text(self):
        h = FakeHandler()
        _errors.handle(h, RuntimeError(SECRET), route="/api/chat", method="POST")
        blob = h.wfile.data.decode("utf-8")
        self.assertNotIn("DEADBEEF", blob)
        self.assertNotIn("/var/task", blob)
        self.assertEqual(h.status, 500)
        self.assertEqual(h.json()["code"], "INTERNAL_ERROR")

    def test_debug_opt_in_is_scrubbed(self):
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        p = _errors.payload(500, debug="token=sk-live-ABCDEFGHIJKLMNOP 문의 01012345678")
        self.assertIn("debug", p)
        # monitoring.scrub 경유 — 원문 그대로는 남지 않는다
        self.assertNotIn("01012345678", p["debug"])


class TestClassify(unittest.TestCase):
    def test_validation_is_400(self):
        st, code = _errors.classify(_errors.ValidationError.field("a", "b"))
        self.assertEqual((st, code), (400, "INVALID_REQUEST"))

    def test_validation_custom_status_preserved(self):
        e = _errors.ValidationError(code="PAYLOAD_TOO_LARGE", status=413)
        self.assertEqual(_errors.classify(e), (413, "PAYLOAD_TOO_LARGE"))

    def test_upstream_network_is_502(self):
        import urllib.error
        st, code = _errors.classify(urllib.error.URLError("boom"))
        self.assertEqual((st, code), (502, "UPSTREAM_ERROR"))

    def test_timeout_is_504(self):
        st, code = _errors.classify(TimeoutError("slow"))
        self.assertEqual((st, code), (504, "UPSTREAM_TIMEOUT"))

    def test_plain_bug_is_500(self):
        self.assertEqual(_errors.classify(KeyError("k")), (500, "INTERNAL_ERROR"))


class TestSend(unittest.TestCase):
    def test_headers_and_length(self):
        h = FakeHandler({"origin": "https://callbot-portal.vercel.app"})
        _errors.send(h, 429, request_id="rid-9")
        self.assertEqual(h.status, 429)
        self.assertTrue(h.ended)
        self.assertEqual(h.header("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(h.header("Cache-Control"), "no-store")
        self.assertEqual(h.header("X-Request-Id"), "rid-9")
        self.assertEqual(int(h.header("Content-Length")), len(h.wfile.data))
        self.assertEqual(h.json()["code"], "RATE_LIMITED")

    def test_cors_echoes_allowed_origin(self):
        h = FakeHandler({"origin": "https://callbot-portal.vercel.app"})
        _errors.send(h, 403)
        self.assertEqual(h.header("Access-Control-Allow-Origin"),
                         "https://callbot-portal.vercel.app")

    def test_broken_socket_does_not_raise(self):
        _errors.send(BrokenHandler(), 500)  # 예외가 새어나오면 테스트 실패


class TestMonitoringNoise(unittest.TestCase):
    """4xx(사용자 입력 오류)는 모니터링에 보내지 않는다 — 알림 피로 방지."""

    def setUp(self):
        import monitoring
        self.monitoring = monitoring
        self.calls = []
        self.orig = monitoring.capture_error
        monitoring.capture_error = lambda e, **kw: self.calls.append(kw) or "ev-1"

    def tearDown(self):
        self.monitoring.capture_error = self.orig

    def test_4xx_not_captured(self):
        _errors.handle(FakeHandler(), _errors.ValidationError.field("x", "y"),
                       route="/api/chat", method="POST")
        self.assertEqual(self.calls, [])

    def test_5xx_captured_with_route(self):
        h = FakeHandler()
        _errors.handle(h, RuntimeError("x"), route="/api/chat", method="POST")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["route"], "/api/chat")
        self.assertEqual(h.json().get("event_id"), "ev-1")


class TestReadJson(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(_errors.read_json(_mk({"a": 1})), {"a": 1})

    def test_empty_required(self):
        with self.assertRaises(_errors.ValidationError):
            _errors.read_json(FakeHandler({"content-length": "0"}))

    def test_empty_optional(self):
        self.assertEqual(
            _errors.read_json(FakeHandler({"content-length": "0"}), required=False), {})

    def test_bad_json(self):
        h = FakeHandler({"content-length": "3"}, b"{{{")
        with self.assertRaises(_errors.ValidationError):
            _errors.read_json(h)

    def test_non_object_rejected(self):
        h = _mk([1, 2, 3])
        with self.assertRaises(_errors.ValidationError):
            _errors.read_json(h)

    def test_oversize_is_413_without_reading_body(self):
        h = FakeHandler({"content-length": str(_errors.MAX_BODY + 1)}, b"")
        try:
            _errors.read_json(h)
            self.fail("413 이 발생해야 한다")
        except _errors.ValidationError as e:
            self.assertEqual((e.status, e.code), (413, "PAYLOAD_TOO_LARGE"))

    def test_bad_content_length(self):
        with self.assertRaises(_errors.ValidationError):
            _errors.read_json(FakeHandler({"content-length": "abc"}, b""))


class TestFieldValidators(unittest.TestCase):
    def test_as_str_type_and_length(self):
        self.assertEqual(_errors.as_str({"a": " hi "}, "a"), "hi")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({"a": 1}, "a")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({"a": "xxxx"}, "a", max_len=3)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({}, "a", required=True)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({"a": "  "}, "a", allow_empty=False)

    def test_as_str_does_not_silently_truncate(self):
        """값을 몰래 잘라 통과시키지 않는다 — 거부한다."""
        with self.assertRaises(_errors.ValidationError):
            _errors.as_str({"a": "abcdef"}, "a", max_len=3)

    def test_as_choice(self):
        self.assertEqual(_errors.as_choice({"t": "qa"}, "t", ("qa", "ta")), "qa")
        self.assertEqual(_errors.as_choice({}, "t", ("qa",), default="qa"), "qa")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_choice({"t": "drop_table"}, "t", ("qa", "ta"))

    def test_as_int_bounds(self):
        self.assertEqual(_errors.as_int({"n": "5"}, "n"), 5)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_int({"n": "x"}, "n")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_int({"n": 0}, "n", minimum=1)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_int({"n": 99}, "n", maximum=10)

    def test_as_list(self):
        self.assertEqual(_errors.as_list({"m": [{}]}, "m", item_type=dict), [{}])
        with self.assertRaises(_errors.ValidationError):
            _errors.as_list({"m": "nope"}, "m")
        with self.assertRaises(_errors.ValidationError):
            _errors.as_list({"m": [1, 2, 3]}, "m", max_items=2)
        with self.assertRaises(_errors.ValidationError):
            _errors.as_list({"m": ["a"]}, "m", item_type=dict)

    def test_query_helpers(self):
        self.assertEqual(_errors.query_choice({"period": ["WEEK"]}, "period",
                                              ("today", "week")), "week")
        self.assertEqual(_errors.query_choice({}, "period", ("today",), default="today"),
                         "today")
        with self.assertRaises(_errors.ValidationError):
            _errors.query_choice({"period": ["bad"]}, "period", ("today",))
        self.assertEqual(_errors.query_str({"text": [" hi "]}, "text"), "hi")
        with self.assertRaises(_errors.ValidationError):
            _errors.query_str({}, "text", required=True)

    def test_details_carry_field_name(self):
        try:
            _errors.as_str({"a": 1}, "a")
        except _errors.ValidationError as e:
            self.assertEqual(e.details[0]["field"], "a")


class TestSttMime(unittest.TestCase):
    """MediaRecorder 가 붙이는 codecs 파라미터 때문에 실사용이 깨지지 않아야 한다."""

    def setUp(self):
        import stt
        self.stt = stt

    def test_browser_variants_accepted(self):
        for m in ("audio/webm", "audio/webm;codecs=opus", "audio/ogg; codecs=opus",
                  "audio/mp4", "AUDIO/WEBM", ""):
            self.assertTrue(self.stt._check_mime(m))

    def test_original_value_not_rewritten(self):
        self.assertEqual(self.stt._check_mime("audio/webm;codecs=opus"),
                         "audio/webm;codecs=opus")

    def test_foreign_type_rejected(self):
        for m in ("text/html", "application/json", "video/mp4"):
            with self.assertRaises(_errors.ValidationError):
                self.stt._check_mime(m)


class TestHandlersWired(unittest.TestCase):
    ROUTES = ("chat.py", "assist.py", "ops_stats.py", "stt.py", "tts.py", "voice.py")

    def _src(self, name):
        with open(os.path.join(ROOT, "api", name), encoding="utf-8") as f:
            return f.read()

    def test_all_routes_use_standard_errors(self):
        for name in self.ROUTES:
            src = self._src(name)
            self.assertIn("import _errors", src, name)
            self.assertIn("_errors.handle(", src, name)

    def test_no_raw_exception_in_responses(self):
        """어떤 라우트도 str(e) 를 응답 본문에 담지 않는다."""
        for name in self.ROUTES + ("health.py", "_guard.py"):
            src = self._src(name)
            self.assertNotIn('"error": str(e)', src, name)
            self.assertNotIn('"error":str(e)', src, name)

    def test_guard_deny_uses_envelope(self):
        src = self._src("_guard.py")
        self.assertIn("_errors.send(", src)

    def test_health_error_branch_keeps_grade_semantics(self):
        """/health 의 status 는 헬스 등급이다 — HTTP 코드로 덮어쓰지 않는다."""
        src = self._src("health.py")
        self.assertIn('"status": "error"', src)
        self.assertIn('"code": "INTERNAL_ERROR"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
