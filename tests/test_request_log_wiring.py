# -*- coding: utf-8 -*-
"""요청 1건 = 구조화 로그 1줄 — voice·wellbeing·health 배선 회귀. 네트워크 미사용.

왜 이 파일이 필요한가
  17차까지 이 세 핸들러는 **기본 접근로그만 침묵**시킨 상태였다. 즉 통화 웹훅과
  이음 안부 시연, 공개 헬스체크는 요청이 들어와도 로그에 한 줄도 남지 않았다 —
  사용자/심사관이 "그때 실패했다"고 말해도 맞출 기록이 없고, 거부(401/403/429)와
  오류는 아무 흔적 없이 사라졌다. `tests/test_logging.py` 의 배선 목록 회귀는
  "`_log.begin(` 이 소스에 있는가"만 보므로, **실제로 한 줄이 나가는지·거기에
  PII 가 없는지**는 요청을 태워봐야 안다.

검증 대상
  1) 요청 1건당 정확히 1줄 — 성공·거부·입력오류·실발신 차단·서버오류 모두
  2) 레벨 규약 — 2xx=info · 4xx=warn · 5xx=error (`_errors.handle` 경유 포함).
     4xx 가 error 로 적히면 level=error 알림이 사용자 오타에도 울려 5xx 가 묻힌다.
  3) PII·비밀값 미기록 — `?t=<웹훅 토큰>`·발화 원문·발신번호·대상자 ID·`?ref=`
  4) 응답 X-Request-Id 가 로그 request_id 와 같고, 교차출처에서 읽을 수 있다
  5) /health 는 shallow 성공을 남기지 않는다(업타임 모니터 폴링) — 단 실패와
     deep 점검은 항상 남고, 전량 기록은 HEALTH_REQUEST_LOG=1 로 켠다
  6) 보조필드는 아는 값만 — 외부가 보낸 임의 `op`·`type` 은 "other" 로 접힌다

실행: python3 -m pytest tests/test_request_log_wiring.py -q
"""
import io
import os
import sys
import json
import contextlib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _log            # noqa: E402
import _ratelimit      # noqa: E402
import voice           # noqa: E402
import wellbeing as W  # noqa: E402
import health          # noqa: E402


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


def lines(buf, kind=None):
    """stdout 의 JSON 줄. 감사 스트림(`kind=audit`)은 별도 스트림이라 분리한다."""
    out = [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]
    if kind == "request":
        return [r for r in out if r.get("kind") != "audit"]
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

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


SAME = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.7.7.7"}


def drive(mod, method, path, headers=None, raw=b"", ctype="application/json"):
    """핸들러를 소켓 없이 실행하고 (응답, 로그줄 목록) 을 돌려준다."""
    hdrs = dict(SAME)
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    if raw:
        hdrs.setdefault("content-length", str(len(raw)))
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
    # 유출 검사는 감사 스트림까지 포함한 stdout 전체를 본다.
    res.log_raw = buf.getvalue()
    return res, lines(buf, kind="request")


ENV = ("CALLBOT_LOG", "CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CPAAS_WEBHOOK_TOKEN", "CPAAS_LIVE", "CALLBACK_SECRET",
       "WELLBEING_CALLBACK_HOSTS", "WELLBEING_ALLOW_INSECURE",
       "HEALTH_DEEP", "HEALTH_REQUEST_LOG", "ORDER_BACKEND", "ORDER_API_BASE",
       "GOOGLE_API_KEY", "GEMINI_API_KEY", "SPEECH_LIVE", "SENTRY_DSN")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        _ratelimit.reset()
        del voice.RECENT[:]
        del W.RECENT[:]
        del W.FAILED[:]
        W.RESULTS.clear()
        del W._RESULT_ORDER[:]
        # 어떤 테스트도 바깥으로 나가지 않는다 — 호출되면 즉시 실패(과금 0).
        self._sock = health.socket.create_connection
        health.socket.create_connection = self._no_net
        self._urlopen = W.urllib.request.urlopen
        W.urllib.request.urlopen = self._no_net

    def tearDown(self):
        health.socket.create_connection = self._sock
        W.urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def _no_net(self, *a, **kw):
        raise AssertionError("테스트가 외부로 나가려 했다")

    # 공통 단정 -----------------------------------------------------------
    def one(self, recs):
        self.assertEqual(len(recs), 1, "요청 1건 = 로그 1줄: %r" % (recs,))
        return recs[0]

    def assert_traceable(self, res, rec):
        """응답 헤더와 로그가 같은 request_id 를 가리키는지."""
        self.assertEqual(res.header("X-Request-Id"), rec["request_id"])

    def assert_no_leak(self, res, *needles):
        """요청로그 + 감사로그 전체(stdout)에 민감값이 없는지."""
        raw = getattr(res, "log_raw", "")
        for n in needles:
            self.assertNotIn(n, raw, "로그에 유출: %s" % n)


# ==========================================================================
# 1) /api/voice — CPaaS 웹훅
# ==========================================================================
class TestVoice(Base):
    def test_get_logs_one_line(self):
        res, recs = drive(voice, "GET", "/api/voice")
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual((rec["route"], rec["method"], rec["status"], rec["level"]),
                         ("/api/voice", "GET", 200, "info"))
        self.assertEqual(rec["path"], "/api/voice")
        self.assert_traceable(res, rec)

    def test_webhook_token_never_reaches_log(self):
        """웹훅 인증은 `?t=<CPAAS_WEBHOOK_TOKEN>` 으로 온다 — 비밀값이다."""
        os.environ["CPAAS_WEBHOOK_TOKEN"] = "tok-super-secret"
        os.environ["CALLBOT_STRICT"] = "1"   # 오리진 신호를 믿지 않는 모드
        res, recs = drive(voice, "GET", "/api/voice?op=log&t=tok-super-secret",
                          headers={"sec-fetch-site": "cross-site"})
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual(rec["path"], "/api/voice")      # 쿼리 전체가 잘렸다
        self.assert_no_leak(res, "tok-super-secret", "?t=", "t=tok")

    def test_denied_request_is_logged_as_warn(self):
        """거부도 기록한다 — 침해 조사에서 실패 시도가 더 중요하다."""
        os.environ["CALLBOT_STRICT"] = "1"
        res, recs = drive(voice, "GET", "/api/voice",
                          headers={"sec-fetch-site": "cross-site"})
        rec = self.one(recs)
        self.assertIn(res.status, (401, 403))
        self.assertEqual(rec["status"], res.status)
        self.assertEqual(rec["level"], "warn")
        self.assert_traceable(res, rec)

    def test_event_type_recorded_without_pii(self):
        body = json.dumps({"type": "answered", "call_id": "c-1",
                           "from": "01044445555"}).encode("utf-8")
        res, recs = drive(voice, "POST", "/api/voice", raw=body)
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual(rec["extra"]["ev"], "answered")
        self.assert_no_leak(res, "01044445555", "c-1")

    def test_speech_text_never_reaches_log(self):
        body = json.dumps({"type": "speech", "call_id": "c-2",
                           "from": "01011112222",
                           "text": "제 주민번호는 900101-1234567 입니다"}).encode("utf-8")
        res, _recs = drive(voice, "POST", "/api/voice", raw=body)
        self.assert_no_leak(res, "900101", "1234567", "주민번호", "01011112222")

    def test_unknown_event_type_is_folded(self):
        """외부가 보낸 임의 값으로 로그 카디널리티를 늘리지 못한다."""
        body = json.dumps({"type": "a" * 300, "call_id": "c-3"}).encode("utf-8")
        _res, recs = drive(voice, "POST", "/api/voice", raw=body)
        self.assertEqual(self.one(recs)["extra"]["ev"], "other")
        self.assertLessEqual(len(json.dumps(recs)), 2000)   # 긴 값이 통째로 안 실린다

    def test_input_error_is_warn_not_error(self):
        """입력검증 실패(413)는 서비스 장애가 아니다 — level=error 로 적으면
        알림이 사용자 오타에 울려 진짜 5xx 가 묻힌다."""
        res, recs = drive(voice, "POST", "/api/voice", raw=b"{}",
                          headers={"content-length": str(voice.MAX_WEBHOOK_BODY + 1)})
        rec = self.one(recs)
        self.assertEqual(res.status, 413)
        self.assertEqual(rec["level"], "warn")
        self.assertEqual(rec["status"], 413)
        self.assertIn("error_code", rec)      # 어느 검증에서 걸렸는지는 남는다

    def test_broken_content_length_is_warn(self):
        res, recs = drive(voice, "POST", "/api/voice", raw=b"{}",
                          headers={"content-length": "abc"})
        rec = self.one(recs)
        self.assertEqual(res.status, 400)
        self.assertEqual(rec["level"], "warn")

    def test_voiceml_response_logged_once(self):
        from urllib.parse import urlencode
        raw = urlencode({"CallSid": "CA1", "From": "01033334444"}).encode("utf-8")
        res, recs = drive(voice, "POST", "/api/voice", raw=raw,
                          ctype="application/x-www-form-urlencoded")
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual(rec["extra"]["kind"], "voiceml")
        self.assert_no_leak(res, "01033334444", "CA1")

    def test_request_id_inherited_and_exposed(self):
        res, recs = drive(voice, "GET", "/api/voice",
                          headers={"x-request-id": "trace-abc-1"})
        rec = self.one(recs)
        self.assertEqual(rec["request_id"], "trace-abc-1")
        self.assertEqual(res.header("X-Request-Id"), "trace-abc-1")
        # 교차출처에서 읽을 수 있어야 추적 키가 쓸모 있다(CORS 안전목록 밖)
        self.assertIn("X-Request-Id", res.header("Access-Control-Expose-Headers") or "")


# ==========================================================================
# 2) /api/wellbeing — 이음 안부 연동(심사 경로)
# ==========================================================================
class TestWellbeing(Base):
    def test_info_get_logs_one_line(self):
        res, recs = drive(W, "GET", "/api/wellbeing")
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual((rec["route"], rec["status"], rec["extra"]["op"]),
                         ("/api/wellbeing", 200, "info"))
        self.assert_traceable(res, rec)

    def test_call_records_outcome_not_identity(self):
        body = json.dumps({"senior_id": "SR-홍길동-01012345678",
                           "profile": "risk"}).encode("utf-8")
        res, recs = drive(W, "POST", "/api/wellbeing/call", raw=body)
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual(rec["extra"]["op"], "call")
        self.assertEqual(rec["extra"]["risk"], W.RISK_HIGH)
        self.assertIs(rec["extra"]["delivered"], False)   # dry-run
        self.assert_no_leak(res, "홍길동", "01012345678", "SR-")

    def test_live_block_is_logged(self):
        """실발신 차단(501)도 기록된다 — 승인 전 시도를 되짚을 수 있어야 한다."""
        body = json.dumps({"senior_id": "SR-1", "mode": "live"}).encode("utf-8")
        res, recs = drive(W, "POST", "/api/wellbeing/call", raw=body)
        rec = self.one(recs)
        self.assertEqual(res.status, 501)
        self.assertEqual(rec["status"], 501)
        self.assertEqual(rec["level"], "error")
        self.assertEqual(res.body()["request_id"], rec["request_id"])

    def test_missing_ref_is_standard_envelope(self):
        res, recs = drive(W, "GET", "/api/wellbeing?op=result")
        rec = self.one(recs)
        body = res.body()
        self.assertEqual(res.status, 400)
        self.assertEqual(body["code"], "VALIDATION_ERROR")   # 기존 소비자 계약 유지
        self.assertEqual(body["details"][0]["field"], "ref")
        self.assertEqual(body["request_id"], rec["request_id"])
        self.assertEqual(rec["level"], "warn")

    def test_unknown_ref_is_404_with_trace(self):
        res, recs = drive(W, "GET", "/api/wellbeing?op=result&ref=wb_ffffffffffff")
        rec = self.one(recs)
        self.assertEqual(res.status, 404)
        self.assertEqual(res.body()["code"], "NOT_FOUND")
        self.assertEqual(res.body()["request_id"], rec["request_id"])

    def test_ref_value_not_logged(self):
        """`?ref=` 는 대상자 실행 식별자다 — 경로에서 잘려 나가야 한다."""
        res, recs = drive(W, "GET", "/api/wellbeing?op=result&ref=wb_deadbeefcafe")
        rec = self.one(recs)
        self.assertEqual(rec["path"], "/api/wellbeing")
        self.assert_no_leak(res, "wb_deadbeefcafe")

    def test_unknown_op_is_folded(self):
        _res, recs = drive(W, "GET", "/api/wellbeing?op=" + "z" * 200)
        self.assertEqual(self.one(recs)["extra"]["op"], "other")

    def test_denied_request_is_logged(self):
        os.environ["CALLBOT_STRICT"] = "1"
        res, recs = drive(W, "GET", "/api/wellbeing",
                          headers={"sec-fetch-site": "cross-site"})
        rec = self.one(recs)
        self.assertIn(res.status, (401, 403))
        self.assertEqual(rec["level"], "warn")


# ==========================================================================
# 3) /api/health — 무인증 공개 경로(업타임 모니터가 폴링한다)
# ==========================================================================
class TestHealth(Base):
    def test_shallow_success_is_not_logged(self):
        """모니터가 수십 초마다 치는 경로다 — 그 한 줄이 실제 트래픽을 덮는다.
        (감사 스트림이 deep 만 기록하는 것과 같은 판단)"""
        res, recs = drive(health, "GET", "/api/health")
        self.assertEqual(res.status, 200)
        self.assertEqual(recs, [])
        # 그래도 추적 키는 응답에 돌려준다 — 신고를 로그와 맞추려면 필요하다.
        self.assertTrue(res.header("X-Request-Id"))
        self.assertIn("X-Request-Id", res.header("Access-Control-Expose-Headers") or "")

    def test_opt_in_logs_every_request(self):
        os.environ["HEALTH_REQUEST_LOG"] = "1"
        res, recs = drive(health, "GET", "/api/health")
        rec = self.one(recs)
        self.assertEqual((rec["route"], rec["status"], rec["extra"]["mode"]),
                         ("/api/health", 200, "shallow"))
        self.assertEqual(rec["extra"]["health"], res.body()["status"])
        self.assert_traceable(res, rec)

    def test_deep_check_is_always_logged(self):
        """deep 은 외부 도달성을 건드리는 '관리 기능'이라 전량 기록한다."""
        os.environ["HEALTH_DEEP"] = "1"
        res, recs = drive(health, "GET", "/api/health?deep=1")
        rec = self.one(recs)
        self.assertEqual(res.status, 200)
        self.assertEqual(rec["extra"]["mode"], "deep")

    def test_failure_is_always_logged_without_internal_text(self):
        orig = health._payload
        health._payload = lambda q="": (_ for _ in ()).throw(
            RuntimeError("DSN=https://key@sentry.io/1 에서 실패"))
        try:
            res, recs = drive(health, "GET", "/api/health")
        finally:
            health._payload = orig
        rec = self.one(recs)
        self.assertEqual(res.status, 500)
        self.assertEqual(rec["level"], "error")
        self.assertEqual(rec["error_code"], "RUNTIME_ERROR")
        self.assert_no_leak(res, "sentry.io", "DSN", "실패")

    def test_head_is_traceable_and_quiet(self):
        res, recs = drive(health, "HEAD", "/api/health")
        self.assertEqual(res.status, 200)
        self.assertEqual(recs, [])
        self.assertTrue(res.header("X-Request-Id"))

    def test_query_string_not_logged(self):
        os.environ["HEALTH_REQUEST_LOG"] = "1"
        res, recs = drive(health, "GET", "/api/health?who=010-1234-5678")
        rec = self.one(recs)
        self.assertEqual(rec["path"], "/api/health")
        self.assert_no_leak(res, "010-1234-5678")


# ==========================================================================
# 4) 레벨 규약 자체 (_log.level_for)
# ==========================================================================
class TestLevelRule(Base):
    def test_level_for_by_status(self):
        for status, level in ((200, "info"), (204, "info"), (302, "info"),
                              (400, "warn"), (404, "warn"), (429, "warn"),
                              (500, "error"), (502, "error"), (504, "error")):
            self.assertEqual(_log.level_for(status), level, status)

    def test_level_for_tolerates_garbage(self):
        self.assertEqual(_log.level_for("x"), "error")   # 모르면 안전한 쪽(드러냄)

    def test_fail_uses_status_not_always_error(self):
        with captured() as buf:
            _log.begin(None, "/x", "POST").fail(ValueError("bad input"), 400)
            _log.begin(None, "/x", "POST").fail(RuntimeError("boom"), 500)
        got = [(r["level"], r["status"], r["error_code"]) for r in lines(buf)]
        self.assertEqual(got, [("warn", 400, "VALUE_ERROR"),
                               ("error", 500, "RUNTIME_ERROR")])


# ==========================================================================
# 5) 드리프트 — 배선된 핸들러는 '응답을 쓰면 로그를 닫는다'
# ==========================================================================
class TestNoSilentResponse(Base):
    """응답 경로가 늘어났을 때 로그 없이 끝나는 길이 생기지 않는지 훑는다.

    목록이 아니라 실제 요청으로 확인한다 — 소스에 `_log.begin(` 이 있어도
    새 분기가 `_send` 를 거치지 않으면 그 요청은 조용히 사라진다.
    """
    CASES = (
        (voice, "GET", "/api/voice"),
        (voice, "GET", "/api/voice?op=log"),
        (W, "GET", "/api/wellbeing"),
        (W, "GET", "/api/wellbeing?op=recent"),
        (W, "GET", "/api/wellbeing?op=failures"),
    )

    def test_every_read_path_emits_exactly_one_line(self):
        for mod, method, path in self.CASES:
            res, recs = drive(mod, method, path)
            self.assertEqual(res.status, 200, path)
            rec = self.one(recs)
            self.assertEqual(res.header("X-Request-Id"), rec["request_id"], path)


if __name__ == "__main__":
    unittest.main(verbosity=1)
