# -*- coding: utf-8 -*-
"""감사 기록이 **응답과 같은 말**을 하는가 — 42차 회귀.

의존성 0 · 네트워크 미사용.

무엇을 고정하는가
  1) 상태→라벨 판정(`_audit.result_for`)이 한 곳에 있고 2xx/3xx=allow · 4xx=deny ·
     5xx=error · 해석불가=error 다. 예외→상태(`_audit.status_of`)도 같은 자리.
  2) `/api/wellbeing` — 실발신 차단(501)·콜백 전송 실패(502)·입력오류(400)·
     미보관 조회(404)가 감사에 **성공으로 남지 않는다**. `op` 가 함께 적힌다
     (같은 경로가 안부 실행·최근 이력·**판정 근거 열람**을 모두 맡는다).
  3) `/api/voice` — 입력오류(400)·과대본문(413)이 성공으로 남지 않고, 캠페인
     발신 시도는 실패해도 `op=campaign` 으로 식별된다.
  4) 관리 장부 넷(`caller_id`·`partners`·`settlement`·`disclosure`)의 입력오류는
     `error` 가 아니라 `deny` 다(18차 규약의 감사판 — 사용자 오타가 `error` 칸을
     올리면 진짜 5xx 가 묻힌다).
  5) 릴리스 게이트 21단계 `audit_outcome` 이 **고치기 전 모양**을 실제로 잡는다.

실행: python -m pytest tests/test_audit_outcome.py -q
"""
import io
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
for p in (API, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _audit        # noqa: E402
import _ratelimit    # noqa: E402
import wellbeing as W  # noqa: E402
import voice         # noqa: E402
import settlement    # noqa: E402
import disclosure    # noqa: E402
import caller_id     # noqa: E402
import partners      # noqa: E402
import health        # noqa: E402
import verify        # noqa: E402


def _no_net(*a, **k):
    raise AssertionError("network call attempted")


# --------------------------------------------------------------------------
# 핸들러 대역 (소켓 없이)
# --------------------------------------------------------------------------
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

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.9.9.9"}


def call(mod, method, path, payload=None, headers=None, raw=None):
    """실제 핸들러를 소켓 없이 호출한다(응답 규약까지 그대로 탄다)."""
    if raw is None:
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = dict(SAME_ORIGIN)
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    if raw:
        hdrs.setdefault("content-length", str(len(raw)))
        hdrs.setdefault("content-type", "application/json")
    res = Res()
    inst = mod.handler.__new__(mod.handler)
    inst.headers = FakeHeaders(hdrs)
    inst.rfile = FakeRFile(raw)
    inst.wfile = res.wfile
    inst.path = path
    inst.send_response = lambda c, *a: setattr(res, "status", c)
    inst.send_header = lambda k, v: res.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    getattr(inst, "do_" + method)()
    return res


ENV = ("CPAAS_LIVE", "CALLBACK_SECRET", "WELLBEING_CALLBACK_HOSTS",
       "WELLBEING_ALLOW_INSECURE", "CALLBOT_API_KEY", "CALLBOT_STRICT",
       "CALLBOT_DEBUG_ERRORS", "CALLBOT_LOG")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["CALLBOT_LOG"] = "off"      # 테스트 출력 소음 제거
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = _no_net
        _ratelimit.reset()
        _audit.reset()
        del W.RECENT[:]
        del W.FAILED[:]
        W.RESULTS.clear()
        del W._RESULT_ORDER[:]

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _audit.reset()
        _ratelimit.reset()

    # 보조 — 마지막 감사 기록
    def last(self):
        rows = _audit.recent(50)
        self.assertTrue(rows, "감사 기록이 없다")
        return rows[-1]

    def assertAudit(self, result, status, **extra):
        rec = self.last()
        self.assertEqual((rec["result"], rec["status"]), (result, status), rec)
        for k, v in extra.items():
            self.assertEqual((rec.get("extra") or {}).get(k), v, rec)


# ==========================================================================
# 1) 판정은 한 곳에
# ==========================================================================
class TestJudgement(Base):
    def test_status_maps_to_label(self):
        for status, want in verify.AUDIT_RESULT_CASES:
            self.assertEqual(_audit.result_for(status), want, status)

    def test_unknown_is_never_success(self):
        for bad in (None, "", "x", object(), -1, 99):
            self.assertEqual(_audit.result_for(bad), "error", bad)

    def test_status_of_reads_the_exception(self):
        import _errors

        self.assertEqual(_audit.status_of(_errors.ValidationError.field("x", "y")), 400)
        self.assertEqual(_audit.status_of(RuntimeError("boom")), 500)
        self.assertEqual(_audit.status_of(RuntimeError("boom"), default=502), 502)

        class Weird(Exception):
            status = "알수없음"

        self.assertEqual(_audit.status_of(Weird()), 500)   # 모르면 5xx(성공 아님)


# ==========================================================================
# 2) /api/wellbeing — 외부 발송·실발신 시도의 근거 장부
# ==========================================================================
class TestWellbeing(Base):
    def test_live_block_is_not_recorded_as_success(self):
        """실발신 차단(501)은 감사에 error/501 로 남는다 — 그 전에는 allow/200 이었다."""
        r = call(W, "POST", "/api/wellbeing/call",
                 {"senior_id": "SR-1", "mode": "live"})
        self.assertEqual(r.status, 501)
        self.assertAudit("error", 501, op="call", live=False)

    def test_gate_on_is_also_not_a_success(self):
        os.environ["CPAAS_LIVE"] = "1"
        r = call(W, "POST", "/api/wellbeing/call", {"senior_id": "SR-1"})
        self.assertEqual(r.status, 501)
        self.assertAudit("error", 501, op="call", live=True)

    def test_validation_error_is_deny(self):
        r = call(W, "POST", "/api/wellbeing/call", {"senior_id": ""})
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400, op="call")

    def test_unknown_op_is_deny(self):
        r = call(W, "POST", "/api/wellbeing", {"op": "nope"})
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400, op="other")

    def test_delivery_failure_is_error_not_allow(self):
        """콜백 전송 실패는 502 다 — 감사도 그렇게 말해야 한다."""
        os.environ["WELLBEING_CALLBACK_HOSTS"] = "eum.example.org"
        saved = W.deliver
        W.deliver = lambda u, pl, **kw: {"delivered": False, "status": 503,
                                         "error": "콜백이 503 로 응답했습니다"}
        try:
            r = call(W, "POST", "/api/wellbeing/call",
                     {"senior_id": "SR-2", "callback_url": "https://eum.example.org/h"})
        finally:
            W.deliver = saved
        self.assertEqual(r.status, 502)
        self.assertEqual(r.body()["code"], "CALLBACK_DELIVERY_FAILED")
        self.assertAudit("error", 502, op="call")

    def test_success_is_recorded_after_the_response(self):
        r = call(W, "POST", "/api/wellbeing/call", {"senior_id": "SR-3"})
        self.assertEqual(r.status, 200)
        self.assertAudit("allow", 200, op="call")

    def test_missing_history_is_deny_with_op(self):
        r = call(W, "GET", "/api/wellbeing?op=result&ref=wb_nope")
        self.assertEqual(r.status, 404)
        self.assertAudit("deny", 404, op="result")

    def test_missing_ref_is_deny(self):
        r = call(W, "GET", "/api/wellbeing?op=result")
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400, op="result")

    def test_reading_the_evidence_is_distinguishable_from_running_a_call(self):
        """누가 **판정 근거를 열람**했는지가 액션명만으로는 가려지지 않는다."""
        ran = call(W, "POST", "/api/wellbeing/call", {"senior_id": "SR-4"})
        ref = ran.body()["payload"]["raw_ref"]
        call(W, "GET", "/api/wellbeing?op=result&ref=" + ref)
        ops = [(x["result"], (x.get("extra") or {}).get("op"))
               for x in _audit.recent(10)]
        self.assertIn(("allow", "call"), ops)
        self.assertIn(("allow", "result"), ops)

    def test_op_label_is_a_whitelist_not_free_text(self):
        """외부가 보낸 임의 문자열이 감사 칸에 그대로 실리지 않는다."""
        call(W, "GET", "/api/wellbeing?op=" + ("SR-SECRET-0001" * 3))
        self.assertEqual((self.last().get("extra") or {}).get("op"), "other")

    def test_denied_request_still_recorded(self):
        r = call(W, "GET", "/api/wellbeing", headers={"origin": "https://evil.example",
                                                      "sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 401)
        self.assertEqual(self.last()["result"], "deny")


# ==========================================================================
# 3) /api/voice — 통화 웹훅·캠페인 발신 시도
# ==========================================================================
class TestVoice(Base):
    def test_bad_json_is_deny_not_allow(self):
        r = call(voice, "POST", "/api/voice", raw=b"{nope",
                 headers={"content-length": "5", "content-type": "application/json"})
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400)

    def test_oversized_body_is_deny(self):
        r = call(voice, "POST", "/api/voice", raw=b"{}",
                 headers={"content-length": str(voice.MAX_WEBHOOK_BODY + 1),
                          "content-type": "application/json"})
        self.assertEqual(r.status, 413)
        self.assertAudit("deny", 413)

    def test_event_success_is_allow(self):
        r = call(voice, "POST", "/api/voice", {"type": "answered", "CallSid": "c-1"})
        self.assertEqual(r.status, 200)
        self.assertAudit("allow", 200, live=False)

    def test_campaign_failure_is_identifiable(self):
        """실패한 **발신 시도**도 `op=campaign` 으로 남는다(추적 가치가 높다)."""
        r = call(voice, "POST", "/api/voice",
                 {"op": "campaign", "numbers": "01012345678"})
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400, op="campaign")

    def test_campaign_success_is_allow_with_op(self):
        r = call(voice, "POST", "/api/voice",
                 {"op": "campaign", "numbers": ["01011112222"]})
        self.assertEqual(r.status, 200)
        self.assertAudit("allow", 200, op="campaign")

    def test_get_status_is_allow(self):
        r = call(voice, "GET", "/api/voice")
        self.assertEqual(r.status, 200)
        self.assertAudit("allow", 200)


# ==========================================================================
# 4) 관리 장부 — 입력오류는 deny(error 는 5xx 전용)
# ==========================================================================
class TestLedgers(Base):
    def test_settlement_bad_month_is_deny(self):
        before = _audit.counters()["error"]
        r = call(settlement, "GET", "/api/settlement?op=report&month=20XX-99")
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400)
        self.assertEqual(_audit.counters()["error"], before)

    def test_disclosure_bad_tenant_is_deny(self):
        before = _audit.counters()["error"]
        r = call(disclosure, "POST", "/api/disclosure", {"tenant_id": "Bad Id!",
                                                         "text": "x"})
        self.assertEqual(r.status, 400)
        self.assertAudit("deny", 400)
        self.assertEqual(_audit.counters()["error"], before)

    def test_method_not_allowed_is_deny(self):
        r = call(settlement, "POST", "/api/settlement", {"x": 1})
        self.assertEqual(r.status, 405)
        self.assertAudit("deny", 405)


# ==========================================================================
# 4-2) 방어 분기 — 감사 장애가 요청을 죽이지 않고, 상태는 그대로 나간다
# ==========================================================================
class _BrokenAudit(object):
    """기록만 터지는 감사 모듈 대역 — 환산은 진짜 규칙을 쓴다."""

    status_of = staticmethod(_audit.status_of)
    result_for = staticmethod(_audit.result_for)

    def record_request(self, *a, **k):
        raise RuntimeError("감사 저장소 장애 /var/secret/audit.log")

    def actor(self, *a, **k):
        return {"type": "origin", "id": "portal"}


# (모듈, 400 을 내는 요청)
LEDGER_400 = (
    (caller_id, "GET", "/api/caller_id?op=nope", None),
    (partners, "GET", "/api/partners?op=nope", None),
    (settlement, "GET", "/api/settlement?op=report&month=20XX-99", None),
    (disclosure, "POST", "/api/disclosure", {"tenant_id": "Bad Id!", "text": "x"}),
)


class TestFailureBranches(Base):
    def test_no_audit_module_still_answers_with_the_right_status(self):
        for mod, method, path, payload in LEDGER_400:
            saved, mod._audit = mod._audit, None
            try:
                r = call(mod, method, path, payload)
            finally:
                mod._audit = saved
            self.assertEqual(r.status, 400, mod.__name__)

    def test_broken_audit_does_not_change_the_status_or_leak(self):
        for mod, method, path, payload in LEDGER_400:
            saved, mod._audit = mod._audit, _BrokenAudit()
            try:
                r = call(mod, method, path, payload)
            finally:
                mod._audit = saved
            self.assertEqual(r.status, 400, mod.__name__)
            self.assertNotIn("audit.log", json.dumps(r.body(), ensure_ascii=False))

    def test_wellbeing_get_failure_is_a_standard_envelope_and_audited(self):
        """조회 경로가 터져도 표준 봉투로 끝나고 감사에 error/500 이 남는다."""
        saved = W.build_payload
        W.build_payload = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = call(W, "GET", "/api/wellbeing")
        finally:
            W.build_payload = saved
        self.assertEqual(r.status, 500)
        self.assertEqual(r.body()["code"], "INTERNAL_ERROR")
        self.assertAudit("error", 500, op="info")

    def test_voice_get_failure_is_a_standard_envelope_and_audited(self):
        saved = voice._Session.stats
        voice._Session.stats = staticmethod(
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            r = call(voice, "GET", "/api/voice")
        finally:
            voice._Session.stats = saved
        self.assertEqual(r.status, 500)
        self.assertEqual(r.body()["code"], "INTERNAL_ERROR")
        self.assertAudit("error", 500)

    def test_health_deep_failure_is_recorded_as_error(self):
        """deep 점검이 터진 것도 감사에 남는다 — 성공만 남으면 조사가 안 된다."""
        os.environ["HEALTH_DEEP"] = "1"
        self.addCleanup(os.environ.pop, "HEALTH_DEEP", None)
        res = Res()
        state = {"n": 0}

        def send_response(code, *a):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("소켓이 끊겼다")      # 응답 전송이 터진다
            res.status = code

        inst = health.handler.__new__(health.handler)
        inst.headers = FakeHeaders(dict(SAME_ORIGIN))
        inst.rfile = FakeRFile(b"")
        inst.wfile = res.wfile
        inst.path = "/api/health?deep=1"
        inst.send_response = send_response
        inst.send_header = lambda k, v: res.sent.append((k, str(v)))
        inst.end_headers = lambda: None
        inst.do_GET()
        self.assertEqual(res.status, 500)
        rows = [(x["action"], x["result"], x["status"]) for x in _audit.recent(10)]
        self.assertIn(("ops.health.deep", "error", 500), rows)
        self.assertNotIn(("ops.health.deep", "allow", 200), rows)


# ==========================================================================
# 5) 게이트 21단계가 고치기 전 모양을 **실제로** 잡는가
# ==========================================================================
BEFORE_FIX = '''
class handler(object):
    def do_GET(self):
        ok, code = guard()
        if not ok:
            _audit.record_request(self.headers, self.path, "GET", "deny", code)
            return deny(self, code)
        _audit.record_request(self.headers, self.path, "GET", "allow", 200)
        self._send({"ok": True})
'''

AFTER_FIX = '''
class handler(object):
    def do_GET(self):
        ok, code = guard()
        if not ok:
            _audit.record_request(self.headers, self.path, "GET", "deny", code)
            return deny(self, code)
        status = 200
        self._send({"ok": True})
        _audit.record_request(self.headers, self.path, "GET",
                              _audit.result_for(status), status)
'''

BLANKET_ERROR = AFTER_FIX + '''
    def do_POST(self):
        try:
            self._send({"ok": True})
        except Exception as e:
            _audit.record_request(self.headers, self.path, "POST", "error",
                                  getattr(e, "status", 500))
'''


class TestGate(unittest.TestCase):
    def _run(self, extra_sources, mod=None):
        srcs = {"ops_stats.py": AFTER_FIX, "caller_id.py": AFTER_FIX,
                "partners.py": AFTER_FIX, "settlement.py": AFTER_FIX,
                "disclosure.py": AFTER_FIX, "voice.py": AFTER_FIX,
                "wellbeing.py": AFTER_FIX, "health.py": AFTER_FIX}
        srcs.update(extra_sources or {})
        return verify.check_audit_outcome(srcs, mod=mod or _audit)

    def test_clean_sources_pass(self):
        ok, msg = self._run({})
        self.assertTrue(ok, msg)

    def test_premature_allow_is_caught(self):
        ok, msg = self._run({"wellbeing.py": BEFORE_FIX})
        self.assertFalse(ok)
        self.assertIn("응답 **전에** allow", msg)

    def test_blanket_error_label_is_caught(self):
        ok, msg = self._run({"caller_id.py": BLANKET_ERROR})
        self.assertFalse(ok)
        self.assertIn("실패 라벨을 손으로 적었다", msg)

    def test_missing_wiring_is_caught(self):
        ok, msg = self._run({"voice.py": BEFORE_FIX.replace("allow", "deny")})
        self.assertFalse(ok)
        self.assertIn("상태→라벨 판정 배선이 없다", msg)

    def test_unregistered_user_is_caught(self):
        ok, msg = self._run({"chat.py": AFTER_FIX})
        self.assertFalse(ok)
        self.assertIn("등록부에 없는 사용처", msg)

    def test_broken_judgement_is_caught(self):
        class Wrong(object):
            @staticmethod
            def result_for(status):
                return "allow"          # 무엇이든 성공으로 적는 판정기

        ok, msg = self._run({}, mod=Wrong)
        self.assertFalse(ok)
        self.assertIn("result_for", msg)

    def test_missing_judgement_is_caught(self):
        class Gone(object):
            pass

        ok, msg = self._run({}, mod=Gone)
        self.assertFalse(ok)
        self.assertIn("result_for 가 없다", msg)

    def test_real_repository_passes(self):
        ok, msg = verify.check_audit_outcome(verify.api_sources())
        self.assertTrue(ok, msg)


# ==========================================================================
# 6) 로컬 E2E — **실제 소켓**으로 라우팅·가드·직렬화를 전부 거친 뒤의 기록
#
# 감사 기록의 결함은 '응답이 나간 뒤에 적히는가' 라는 **순서** 문제라, 라우팅과
# 가드를 거친 실제 요청으로 한 번 더 본다. 루프백(127.0.0.1)만 쓰고 콜백 URL 을
# 주지 않으므로 외부로 나가는 요청도 과금 경로도 없다(dry-run).
# ==========================================================================
class Server(object):
    def __init__(self, handler):
        import http.server
        import threading
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def call(self, path, body=None, method=None):
        import urllib.error
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.origin + path, data=data,
                                     method=method or ("POST" if data else "GET"))
        req.add_header("Origin", self.origin)          # 동일출처(Origin == Host)
        req.add_header("Sec-Fetch-Site", "same-origin")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._open(req) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200]}

    @staticmethod
    def _open(req):
        # Base.setUp 이 `urlopen` 을 감시 함수로 바꿔 두므로 오프너를 직접 쓴다
        # (루프백 요청만 하고 외부로는 나가지 않는다).
        import urllib.request as _u
        return _u.build_opener().open(req, timeout=8)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class TestOverSocket(Base):
    def setUp(self):
        Base.setUp(self)
        self.srv = Server(W.handler)

    def tearDown(self):
        self.srv.close()
        Base.tearDown(self)

    def test_the_whole_sequence_is_recorded_truthfully(self):
        _audit.reset()
        seq = []
        st, _b = self.srv.call("/api/wellbeing/call", {"senior_id": "SR-E2E"})
        seq.append(("call 성공", st, 200))
        st, _b = self.srv.call("/api/wellbeing/call", {"senior_id": "SR-E2E",
                                                       "mode": "live"})
        seq.append(("실발신 차단", st, 501))
        st, _b = self.srv.call("/api/wellbeing/call", {"senior_id": ""})
        seq.append(("입력오류", st, 400))
        st, _b = self.srv.call("/api/wellbeing?op=result&ref=wb_nope")
        seq.append(("미보관 조회", st, 404))
        for label, got, want in seq:
            self.assertEqual(got, want, label)
        rows = [(x["result"], x["status"]) for x in _audit.recent(20)]
        self.assertEqual(rows, [("allow", 200), ("error", 501),
                                ("deny", 400), ("deny", 404)], rows)
        # 감사 기록에 대상자 ID·조회 ref 가 남지 않는다(PII 1번 원칙).
        dump = json.dumps(_audit.recent(20), ensure_ascii=False)
        self.assertNotIn("SR-E2E", dump)
        self.assertNotIn("wb_nope", dump)


# ==========================================================================
# 7) 네트워크 미사용 확인(테스트가 과금되지 않는다)
# ==========================================================================
class TestNoNetwork(Base):
    def test_urlopen_is_never_called(self):
        call(W, "POST", "/api/wellbeing/call", {"senior_id": "SR-9"})
        call(voice, "GET", "/api/voice")
        # setUp 이 urlopen 을 감시 함수로 바꿔 두었고 여기까지 왔으므로 미호출이다.
        self.assertIs(urllib.request.urlopen, _no_net)


if __name__ == "__main__":
    unittest.main(verbosity=2)
