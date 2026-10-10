# -*- coding: utf-8 -*-
"""로그와 응답의 에러 코드 상관관계 — 네트워크 미사용, 과금 0.

왜 이 파일이 필요한가
  한 요청에는 성질이 다른 '코드'가 둘 있다.
    · 응답 봉투(`_errors`)의 `code` — **사용자가 신고하는 값**("INVALID_REQUEST 가
      떴어요"). 상태코드에서 뽑지만 라우트가 따로 지정할 수도 있다
      (`wellbeing` 의 `VALIDATION_ERROR`·`NOT_FOUND`) — 상태코드로 역산할 수 없다.
    · 구조화 로그의 `error_code` — 예외 **타입명** 유래. 원인별 집계용이다
      (5xx 분류는 전부 INTERNAL_ERROR 라 타입명을 지우면 무엇이 터졌는지 모른다).
  24차까지 로그에는 뒤쪽만 있었고 그 값이 봉투와 **달랐다**. 신고받은 코드로
  로그를 찾으면 아무것도 안 나오고, 더 나쁜 쪽은 거부(401/403/429)·404·413·405 —
  예외 없이 `_errors.send` 로만 끝나는 경로라 **로그에 코드가 한 칸도 없었다**.
  거부 사유별 집계가 아예 불가능했고, 호출부가 `rq.finish(_c, denied=True)` 로
  먼저 닫아 버렸기 때문에 뒤따르는 `send` 가 코드를 실을 자리도 없었다.

검증 대상
  1) 봉투 `code` == 로그 `code` — 라우트 12개 전부를 **실제로 태워** 확인한다
     (목록이 아니라 요청으로 본다 — 새 라우트가 빠지면 드리프트 회귀가 잡는다)
  2) 거부·404·413·405 에도 코드가 남고, 거부는 `extra.denied` 로 구분된다
  3) 5xx 는 두 칸이 **함께** 남는다 — `code=INTERNAL_ERROR`(신고용) +
     `error_code=<예외 타입명>`(원인). 한 칸으로 겹쳐 쓰면 한쪽 집계가 깨진다
  4) 상태코드로 역산하지 않는다 — `wellbeing` 의 400 은 `VALIDATION_ERROR` 다
  5) 성공(2xx)에는 코드 칸이 없다(오류 집계가 성공으로 오염되지 않는다)
  6) 요청 1건 = 로그 1줄 유지 · PII·비밀값 미기록 · 길이 상한
  7) `_errors` 가 없는 최후 폴백에서도 거부가 한 줄은 남는다

실행: python3 -m pytest tests/test_error_code_correlation.py -q
"""
import contextlib
import glob
import io
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)

import _errors        # noqa: E402
import _guard         # noqa: E402
import _log           # noqa: E402
import _ratelimit     # noqa: E402


# --------------------------------------------------------------------------
# 대역 — 소켓 없이 do_* 를 돌리고 stdout 의 JSON 줄을 모은다
# --------------------------------------------------------------------------
@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def req_lines(buf):
    """요청 로그만. 감사 스트림(`kind=audit`)은 다른 스트림이라 분리한다."""
    out = []
    for ln in buf.getvalue().strip().splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("kind") != "audit":
            out.append(rec)
    return out


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class FakeRFile(object):
    def __init__(self, data=b""):
        self._d = data

    def read(self, n=None):
        d = self._d if n is None else self._d[:n]
        self._d = b"" if n is None else self._d[n:]
        return d


class Res(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()
        self.log_raw = ""

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


SAME = {"sec-fetch-site": "same-origin", "host": "callbot-portal.vercel.app",
        "x-forwarded-for": "10.9.9.9"}
CROSS = {"sec-fetch-site": "cross-site", "x-forwarded-for": "203.0.113.9"}


def drive(mod, method, path, headers=None, raw=b"", ctype="application/json",
          clen=None):
    hdrs = dict(SAME)
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    if raw or clen is not None:
        hdrs.setdefault("content-length", str(clen if clen is not None else len(raw)))
        hdrs.setdefault("content-type", ctype)
    res = Res()
    inst = mod.handler.__new__(mod.handler)
    inst.headers = FakeHeaders(hdrs)
    inst.rfile = FakeRFile(raw)
    inst.wfile = res.wfile
    inst.path = path
    inst.send_response = lambda c, *a: setattr(res, "status", c)
    inst.send_header = lambda k, v: res.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    with captured() as buf:
        getattr(inst, "do_" + method)()
    res.log_raw = buf.getvalue()
    return res, req_lines(buf)


# 라우트 모듈 — `_` 로 시작하지 않는 api 파일 중 handler 를 가진 것.
# 목록을 손으로 적지 않는다(새 라우트가 추가되면 자동으로 포함된다).
def route_modules():
    out = []
    for path in sorted(glob.glob(os.path.join(API, "*.py"))):
        name = os.path.basename(path)[:-3]
        if name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            if "\nclass handler" not in f.read():
                continue
        out.append((name, __import__(name)))
    return out


ENV = ("CALLBOT_LOG", "CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CALLBOT_RATE_LIMIT_LLM", "CALLBOT_AUDIT", "CPAAS_WEBHOOK_TOKEN",
       "CPAAS_LIVE", "GOOGLE_API_KEY", "GEMINI_API_KEY", "SENTRY_DSN")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["CALLBOT_AUDIT"] = "off"   # 감사 스트림은 이 파일의 관심사가 아니다
        _ratelimit.reset()
        self._restore = []

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    # 공통 단정 -----------------------------------------------------------
    def one(self, recs):
        self.assertEqual(len(recs), 1, "요청 1건 = 로그 1줄: %r" % (recs,))
        return recs[0]

    def assert_correlated(self, res, rec):
        """신고받은 코드로 이 줄을 찾을 수 있는가."""
        body = res.body()
        self.assertEqual(rec["status"], res.status)
        self.assertEqual(rec.get("code"), body.get("code"),
                         "응답 봉투 code 와 로그 code 가 다르다 — 신고받은 "
                         "코드로 로그를 찾을 수 없다: %r vs %r" % (body, rec))
        self.assertEqual(rec["request_id"], body.get("request_id"))


# ==========================================================================
# 1) 라우트 전수 — 거부 한 건을 모든 라우트에 태워 코드가 맞물리는지 본다
# ==========================================================================
class TestEveryRouteCorrelatesOnDeny(Base):

    def test_denied_request_carries_the_envelope_code_in_its_log_line(self):
        os.environ["CALLBOT_STRICT"] = "1"      # 오리진 신호를 믿지 않는 모드
        checked = []
        for name, mod in route_modules():
            for method in ("GET", "POST"):
                if not hasattr(mod.handler, "do_" + method):
                    continue
                with self.subTest(route=name, method=method):
                    _ratelimit.reset()
                    res, recs = drive(mod, method, "/api/%s" % name, headers=CROSS)
                    if res.status is None or res.status < 400:
                        continue                # health 는 무인증 공개 경로다
                    rec = self.one(recs)
                    self.assert_correlated(res, rec)
                    self.assertIn(rec["code"], ("UNAUTHORIZED", "FORBIDDEN"))
                    self.assertEqual(rec["level"], "warn")
                    self.assertIs(rec.get("extra", {}).get("denied"), True)
                    checked.append("%s.%s" % (name, method))
        # 라우트가 통째로 빠져 '0건 통과'가 되는 것을 막는다
        self.assertGreaterEqual(len(checked), 18, "검사된 라우트: %r" % checked)

    def test_every_route_module_is_reachable(self):
        names = [n for n, _ in route_modules()]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 12, names)


# ==========================================================================
# 2~5) 상태별 — 거부·입력오류·413·405·404·5xx·성공
# ==========================================================================
class TestCodesByOutcome(Base):

    def setUp(self):
        super().setUp()
        global chat, wellbeing, settlement
        import chat
        import wellbeing
        import settlement

    def test_cross_origin_deny_is_forbidden_in_both_places(self):
        res, recs = drive(chat, "GET", "/api/chat",
                          headers={"sec-fetch-site": "cross-site", "host": "x"})
        rec = self.one(recs)
        self.assertEqual(res.status, 403)
        self.assertEqual(rec["code"], "FORBIDDEN")
        self.assert_correlated(res, rec)

    def test_strict_mode_deny_is_unauthorized_in_both_places(self):
        os.environ["CALLBOT_STRICT"] = "1"
        res, recs = drive(chat, "GET", "/api/chat", headers=CROSS)
        rec = self.one(recs)
        self.assertEqual(res.status, 401)
        self.assertEqual(rec["code"], "UNAUTHORIZED")
        self.assert_correlated(res, rec)

    def test_rate_limited_deny_is_rate_limited_in_both_places(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        drive(chat, "GET", "/api/chat")                   # 1회 소모
        res, recs = drive(chat, "GET", "/api/chat")
        rec = self.one(recs)
        self.assertEqual(res.status, 429)
        self.assertEqual(rec["code"], "RATE_LIMITED")
        self.assert_correlated(res, rec)
        # 429 는 Retry-After 가 응답에, 코드가 로그에 — 둘 다 있어야 쓸모가 있다
        self.assertIsNotNone(res.header("Retry-After"))

    def test_validation_failure_keeps_both_codes(self):
        """입력검증 400: 신고용 code 와 원인 error_code 가 **다른 값**이다."""
        res, recs = drive(chat, "POST", "/api/chat", raw=b"", clen=0)
        rec = self.one(recs)
        self.assertEqual(res.status, 400)
        self.assertEqual(rec["code"], "INVALID_REQUEST")
        self.assertEqual(rec["error_code"], "VALIDATION_ERROR")
        self.assertNotEqual(rec["code"], rec["error_code"])  # 한 칸으로 못 합친다
        self.assert_correlated(res, rec)

    def test_payload_too_large_is_logged_with_its_code(self):
        res, recs = drive(chat, "POST", "/api/chat", raw=b"{}",
                          clen=_errors.MAX_BODY + 1)
        rec = self.one(recs)
        self.assertEqual(res.status, 413)
        self.assertEqual(rec["code"], "PAYLOAD_TOO_LARGE")
        self.assert_correlated(res, rec)

    def test_method_not_allowed_is_logged_with_its_code(self):
        res, recs = drive(settlement, "POST", "/api/settlement", raw=b"{}")
        rec = self.one(recs)
        self.assertEqual(res.status, 405)
        self.assertEqual(rec["code"], "METHOD_NOT_ALLOWED")
        self.assertIs(rec.get("extra", {}).get("denied"), True)
        self.assert_correlated(res, rec)

    def test_route_specific_code_is_not_derived_from_status(self):
        """`wellbeing` 의 400 은 `VALIDATION_ERROR` 다 — 상태코드 표(400 ->
        INVALID_REQUEST)로 역산하면 틀린다. 로그는 라우트가 **실제로 보낸** 값을
        적어야 한다."""
        res, recs = drive(wellbeing, "GET", "/api/wellbeing?op=result")
        rec = self.one(recs)
        self.assertEqual(res.status, 400)
        self.assertEqual(rec["code"], "VALIDATION_ERROR")
        self.assertNotEqual(rec["code"], _errors.CODE_BY_STATUS[400])
        self.assert_correlated(res, rec)

    def test_not_found_code_is_logged(self):
        res, recs = drive(wellbeing, "GET", "/api/wellbeing?op=result&ref=없는참조")
        rec = self.one(recs)
        self.assertEqual(res.status, 404)
        self.assertEqual(rec["code"], "NOT_FOUND")
        self.assert_correlated(res, rec)
        self.assertNotIn("없는참조", res.log_raw)   # ?ref= 값은 로그에 남지 않는다

    def test_server_error_keeps_the_exception_type_next_to_the_envelope_code(self):
        """5xx 분류는 전부 INTERNAL_ERROR 다 — 타입명이 사라지면 원인을 잃는다."""
        self.patch(chat, "run_turn",
                   lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("내부 /var/task 경로")))
        res, recs = drive(chat, "POST", "/api/chat",
                          raw=json.dumps({"messages": [{"role": "user", "content": "안녕"}]
                                          }).encode("utf-8"))
        rec = self.one(recs)
        self.assertEqual(res.status, 500)
        self.assertEqual(rec["code"], "INTERNAL_ERROR")
        self.assertEqual(rec["error_code"], "RUNTIME_ERROR")
        self.assertEqual(rec["level"], "error")
        self.assert_correlated(res, rec)
        self.assertNotIn("/var/task", res.log_raw)   # 예외 문구는 기록하지 않는다

    def test_upstream_failure_is_classified_in_both_places(self):
        self.patch(chat, "run_turn",
                   lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("업스트림")))
        res, recs = drive(chat, "POST", "/api/chat",
                          raw=json.dumps({"messages": [{"role": "user", "content": "안녕"}]
                                          }).encode("utf-8"))
        rec = self.one(recs)
        self.assertEqual(res.status, 504)
        self.assertEqual(rec["code"], "UPSTREAM_TIMEOUT")
        self.assertEqual(rec["error_code"], "TIMEOUT_ERROR")
        self.assert_correlated(res, rec)

    def test_success_has_no_code_field(self):
        """성공에 코드를 붙이면 `code` 로 거는 오류 집계가 성공으로 오염된다."""
        res, recs = drive(chat, "GET", "/api/chat")
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertNotIn("code", rec)
        self.assertNotIn("error_code", rec)
        self.assertEqual(rec["level"], "info")


# ==========================================================================
# 6) _log 계약 — 두 칸은 겹쳐 쓰지 않는다 · 길이 상한 · 흡수
# ==========================================================================
class TestLogContract(Base):

    def setUp(self):
        super().setUp()
        os.environ["CALLBOT_LOG"] = "on"

    def rec(self, fn):
        with captured() as buf:
            fn()
        return req_lines(buf)[0]

    def test_finish_records_only_the_envelope_code(self):
        rec = self.rec(lambda: _log.begin(None, "/api/x", "GET").finish(403, code="FORBIDDEN"))
        self.assertEqual(rec["code"], "FORBIDDEN")
        self.assertNotIn("error_code", rec)

    def test_fail_records_both_and_does_not_overwrite_either(self):
        rec = self.rec(lambda: _log.begin(None, "/api/x", "POST")
                       .fail(ValueError("x"), 400, code="INVALID_REQUEST"))
        self.assertEqual((rec["error_code"], rec["code"]),
                         ("VALUE_ERROR", "INVALID_REQUEST"))

    def test_fail_without_an_envelope_keeps_the_old_shape(self):
        """봉투를 거치지 않는 직접 호출(`health` 의 폴백 등)은 종전 그대로."""
        rec = self.rec(lambda: _log.begin(None, "/api/x", "GET").fail(KeyError("k"), 500))
        self.assertEqual(rec["error_code"], "KEY_ERROR")
        self.assertNotIn("code", rec)

    def test_code_is_length_capped(self):
        rec = self.rec(lambda: _log.begin(None, "/api/x", "GET").finish(400, code="C" * 500))
        self.assertEqual(len(rec["code"]), 64)

    def test_non_string_code_does_not_break_the_line(self):
        rec = self.rec(lambda: _log.begin(None, "/api/x", "GET").finish(403, code=403))
        self.assertEqual(rec["code"], "403")

    def test_first_close_wins_so_one_request_is_one_line(self):
        with captured() as buf:
            rq = _log.begin(None, "/api/x", "GET")
            rq.finish(403, code="FORBIDDEN")
            rq.finish(200)                 # 뒤늦은 호출은 무시된다
            rq.fail(ValueError("x"), 500, code="INTERNAL_ERROR")
        recs = req_lines(buf)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["code"], "FORBIDDEN")


# ==========================================================================
# 7) 폴백 — `_errors` 가 없어도 거부는 한 줄 남는다
# ==========================================================================
class TestDenyFallbackStillLogs(Base):

    def setUp(self):
        super().setUp()
        os.environ["CALLBOT_LOG"] = "on"

    def test_deny_without_errors_module_logs_the_status(self):
        res = Res()
        h = FakeHeaders(dict(SAME))

        class H(object):
            headers = h
            wfile = res.wfile
            send_response = staticmethod(lambda c, *a: setattr(res, "status", c))
            send_header = staticmethod(lambda k, v: res.sent.append((k, str(v))))
            end_headers = staticmethod(lambda: None)

        # import 가 터지는 상황을 만든다(모듈 부재·순환 참조 등)
        saved = sys.modules.get("_errors")
        sys.modules["_errors"] = None        # import _errors -> ImportError
        try:
            with captured() as buf:
                rq = _log.begin(None, "/api/chat", "GET")
                _guard.deny(H(), 403, "forbidden: cross-origin", rq)
            recs = req_lines(buf)
        finally:
            if saved is None:
                sys.modules.pop("_errors", None)
            else:
                sys.modules["_errors"] = saved
        rec = self.one(recs)
        self.assertEqual((rec["status"], rec["level"]), (403, "warn"))
        self.assertIs(rec["extra"]["denied"], True)
        # 봉투 코드 표를 못 쓰는 경로라 code 칸은 없지만, **줄은 남는다**
        self.assertNotIn("forbidden: cross-origin", json.dumps(rec, ensure_ascii=False))

    def test_broken_request_object_does_not_turn_a_deny_into_a_500(self):
        """로그 객체가 고장나도 거부 응답은 그대로 나간다 — 막는 장치가 터져서
        요청이 500 이 되면, 가드가 피해를 만드는 쪽이 된다(가용성 우선)."""
        res = Res()

        class H(object):
            headers = FakeHeaders(dict(SAME))
            wfile = res.wfile
            send_response = staticmethod(lambda c, *a: setattr(res, "status", c))
            send_header = staticmethod(lambda k, v: res.sent.append((k, str(v))))
            end_headers = staticmethod(lambda: None)

        class Hostile(object):
            request_id = "rid-1"

            def set(self, **kw):
                raise RuntimeError("로그 저장소 고장")

            def finish(self, *a, **kw):
                raise RuntimeError("로그 저장소 고장")

        with captured():
            _guard.deny(H(), 403, "forbidden: cross-origin", Hostile())
        self.assertEqual(res.status, 403)
        self.assertEqual(res.body()["code"], "FORBIDDEN")

    def test_fallback_path_absorbs_a_broken_finish(self):
        """`_errors` 도 없고 로그도 고장난 이중 장애 — 응답만은 나간다."""
        res = Res()

        class H(object):
            headers = FakeHeaders(dict(SAME))
            wfile = res.wfile
            send_response = staticmethod(lambda c, *a: setattr(res, "status", c))
            send_header = staticmethod(lambda k, v: res.sent.append((k, str(v))))
            end_headers = staticmethod(lambda: None)

        class Hostile(object):
            request_id = "rid-2"

            def set(self, **kw):
                return self

            def finish(self, *a, **kw):
                raise RuntimeError("로그 저장소 고장")

        saved = sys.modules.get("_errors")
        sys.modules["_errors"] = None
        try:
            with captured():
                _guard.deny(H(), 401, "unauthorized: API key required", Hostile())
        finally:
            if saved is None:
                sys.modules.pop("_errors", None)
            else:
                sys.modules["_errors"] = saved
        self.assertEqual(res.status, 401)
        self.assertNotIn("API key", res.wfile.data.decode("utf-8"))  # 힌트 미노출

    def test_deny_resolves_the_request_from_the_handler(self):
        """`_stt`·`_tts` 처럼 rq 를 넘기지 않는 호출부도 표식·로그를 얻는다."""
        res = Res()

        class H(object):
            headers = FakeHeaders(dict(SAME))
            wfile = res.wfile
            send_response = staticmethod(lambda c, *a: setattr(res, "status", c))
            send_header = staticmethod(lambda k, v: res.sent.append((k, str(v))))
            end_headers = staticmethod(lambda: None)

        inst = H()
        inst._rq = _log.begin(None, "/api/stt", "GET")
        with captured() as buf:
            _guard.deny(inst, 403, "forbidden: cross-origin")
        rec = self.one(req_lines(buf))
        self.assertEqual(rec["code"], "FORBIDDEN")
        self.assertIs(rec["extra"]["denied"], True)


# ==========================================================================
# 8) 드리프트 — 거부 로그를 봉투보다 먼저 닫지 말 것
# ==========================================================================
class TestNoPrematureClose(unittest.TestCase):

    RX = re.compile(r"\.finish\([^)]*denied\s*=\s*True")

    def test_no_route_closes_its_log_before_the_envelope_code_is_known(self):
        hits = []
        for path in sorted(glob.glob(os.path.join(API, "*.py"))):
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if self.RX.search(line):
                        hits.append("%s:%d" % (os.path.basename(path), i))
        self.assertEqual(hits, [],
                         "거부 로그를 `_errors.send` 보다 먼저 닫으면 그 줄에 봉투 "
                         "code 가 실리지 않는다(표식은 `rq.set(denied=True)` 로): %s" % hits)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
