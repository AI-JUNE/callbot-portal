# -*- coding: utf-8 -*-
"""운영 대시보드 집계(`api/ops_stats.py`)의 **방어 분기** 회귀 (2026-09-30).

기존 회귀(tests/test_ops_stats.py 51건)는 집계 산식·스키마·HTTP 계약을 덮었다.
남아 있던 것은 장애 때만 도는 코드 — 환경 조회 실패, 감사 모듈 부재·장애,
에러 봉투가 예상과 다른 모양일 때의 상태코드 폴백이다. 방어 코드는 평시에
돌지 않으므로 회귀가 없으면 '있는 줄만 알았지 실제로는 터지는' 상태가 된다.

핵심 원칙 두 가지를 고정한다.
  - **감사 장애가 요청을 죽이지 않는다**(가용성 우선). 단, 결과는 사실대로 적는다.
  - **조회가 게이트를 켜지 않는다**. 게이트 판독이 실패하면 OFF 로 답한다.

실행: python -m pytest tests/test_ops_stats_edge.py -q
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)
os.environ.setdefault("CALLBOT_API_KEY", "test-key")

import _ratelimit       # noqa: E402
import ops_stats        # noqa: E402


class Req(ops_stats.handler):
    def __init__(self, path="/api/ops_stats", auth=True):
        self.path = path
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        h = {}
        if auth:
            h["x-api-key"] = os.environ["CALLBOT_API_KEY"]
        self.headers = h
        self.status = None
        self.sent = {}

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent[str(k)] = str(v)

    def end_headers(self):
        pass

    def raw(self):
        return self.wfile.getvalue().decode("utf-8", "replace")

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def run(path="/api/ops_stats", auth=True):
    _ratelimit._HITS.clear()
    r = Req(path, auth=auth)
    r.do_GET()
    return r


class Recorder(object):
    """감사 모듈 대역 — 무엇이 어떻게 적혔는지 확인한다."""

    def __init__(self, boom=False):
        self.rows = []
        self.boom = boom

    def record_request(self, headers, path, method, result, status, request_id=None, **kw):
        self.rows.append({"method": method, "result": result, "status": status, "extra": kw})
        if self.boom:
            raise RuntimeError("audit sink down")


class Gates(unittest.TestCase):
    def test_flag_read_failure_is_off_not_crash(self):
        """환경 조회가 터져도 게이트는 OFF 로 답한다 — 장애가 스위치를 켜지 않는다."""
        class Boom(object):
            def get(self, *a, **kw):
                raise RuntimeError("environ gone")

        class OsShim(object):
            environ = Boom()
        saved = ops_stats.os
        ops_stats.os = OsShim()
        try:
            self.assertFalse(ops_stats._flag("CPAAS_LIVE"))
            g = ops_stats.gate_flags()
        finally:
            ops_stats.os = saved
        self.assertEqual(set(g), {"recording_live", "cpaas_live", "speech_live"})
        self.assertTrue(all(v is False for v in g.values()))


class AuditIsolation(unittest.TestCase):
    def setUp(self):
        self.saved = ops_stats._audit

    def tearDown(self):
        ops_stats._audit = self.saved

    def test_deny_is_recorded(self):
        rec = Recorder()
        ops_stats._audit = rec
        r = run(auth=False)
        self.assertEqual(r.status, 403)
        self.assertEqual([(x["result"], x["status"]) for x in rec.rows], [("deny", 403)])

    def test_audit_failure_does_not_kill_denied_request(self):
        """거부 기록이 실패해도 403 은 그대로 나간다(감사 장애 ≠ 서비스 장애)."""
        ops_stats._audit = Recorder(boom=True)
        r = run(auth=False)
        self.assertEqual(r.status, 403)
        self.assertEqual(r.body()["code"], "FORBIDDEN")

    def test_audit_failure_does_not_kill_success(self):
        ops_stats._audit = Recorder(boom=True)
        r = run()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])

    def test_missing_audit_module_still_serves(self):
        """감사 모듈이 없는 환경에서도 대시보드는 뜬다."""
        ops_stats._audit = None
        r = run()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["data_source"], "demo")

    def test_success_is_recorded_with_period(self):
        rec = Recorder()
        ops_stats._audit = rec
        run("/api/ops_stats?period=week")
        self.assertEqual([(x["result"], x["status"]) for x in rec.rows], [("allow", 200)])
        self.assertEqual(rec.rows[0]["extra"].get("period"), "week")


class ErrorStatusFallback(unittest.TestCase):
    """에러 봉투의 status 를 믿지 못할 때도 감사에는 사실이 적혀야 한다."""

    def setUp(self):
        self.saved_err, self.saved_audit = ops_stats._errors, ops_stats._audit
        self.saved_sum = ops_stats.get_ops_summary
        ops_stats.get_ops_summary = lambda **kw: (_ for _ in ()).throw(RuntimeError("agg down"))

    def tearDown(self):
        ops_stats._errors, ops_stats._audit = self.saved_err, self.saved_audit
        ops_stats.get_ops_summary = self.saved_sum

    def _shim(self, status_value):
        real = self.saved_err

        class Shim(object):
            def query_choice(self, *a, **kw):
                return real.query_choice(*a, **kw)

            def handle(self, h, exc, route="", method="", rq=None):
                real.send(h, status=500)
                return {"status": status_value}
        ops_stats._errors = Shim()

    def test_non_numeric_status_falls_back_to_500(self):
        rec = Recorder()
        ops_stats._audit = rec
        self._shim("알수없음")
        r = run()
        self.assertEqual(r.status, 500)
        self.assertEqual([(x["result"], x["status"]) for x in rec.rows], [("error", 500)])

    def test_missing_status_falls_back_to_500(self):
        rec = Recorder()
        ops_stats._audit = rec
        self._shim(None)
        run()
        self.assertEqual(rec.rows[0]["result"], "error")

    def test_client_error_status_is_recorded_as_deny(self):
        """4xx 는 우리 장애가 아니다 — error 로 부풀리지 않는다."""
        rec = Recorder()
        ops_stats._audit = rec
        self._shim(400)
        run()
        self.assertEqual([(x["result"], x["status"]) for x in rec.rows], [("deny", 400)])

    def test_internal_text_not_leaked(self):
        ops_stats._audit = None
        self._shim(500)
        r = run()
        self.assertNotIn("agg down", r.raw())


class Selftest(unittest.TestCase):
    def test_module_selftest_runs(self):
        """`python api/ops_stats.py` 셀프테스트가 실제로 통과하는지 확인한다."""
        import subprocess
        p = subprocess.run([sys.executable, os.path.join(API, "ops_stats.py")],
                           capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-400:])
        self.assertIn(b"selftest OK", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
