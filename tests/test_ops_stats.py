# -*- coding: utf-8 -*-
"""api/ops_stats.py 운영 지표 집계 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — ops_stats 순서)
  1) 집계 산식 — 기간별 합계·자동처리율·연결감축, baseline 주입
  2) 스키마 고정 — 소스가 죽어도 키·타입이 변하지 않는다(콘솔 화면이 깨지지 않는다)
  3) 게이트 플래그 — 읽기 전용. 여기서 활성화하지 않는다
  4) 읽기 전용 · 부작용 없음 — 호출해도 큐·저장소·환경이 변하지 않는다
  5) HTTP 계약 — 200/400/403, 표준 봉투, 캐시 금지, CORS·요청ID 헤더
  6) 감사 — 관리 경로 접근이 allow/deny **와 실제 결과**로 기록된다
  7) 안전 — 응답·감사에 비밀값·원문 IP가 새지 않는다. 데모 수치는 데모로 표시된다

실행: python3 -m pytest tests/test_ops_stats.py -q
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _audit        # noqa: E402
import _guard        # noqa: E402
import _ratelimit    # noqa: E402
import ops_stats     # noqa: E402


# --------------------------------------------------------------------------
# 최소 핸들러 대역 (소켓 없이 do_GET 을 돌린다)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class Resp(object):
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


def call(method="GET", path="/api/ops_stats", headers=None):
    r = Resp()
    hdrs = FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
    inst = ops_stats.handler.__new__(ops_stats.handler)
    inst.headers = hdrs
    inst.path = path
    inst.wfile = r.wfile
    inst.rfile = io.BytesIO(b"")
    inst.send_response = lambda c: setattr(r, "status", c)
    inst.send_header = lambda k, v: r.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    getattr(inst, "do_" + method)()
    return r


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.7"}

ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "RECORDING_LIVE", "CPAAS_LIVE", "SPEECH_LIVE",
       "CALLBOT_AUDIT", "CALLBOT_AUDIT_SALT")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()
        _audit.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        _audit.reset()


# ==========================================================================
# 1) 집계 산식
# ==========================================================================
class TestSummary(Base):
    def test_today_defaults(self):
        s = ops_stats.get_ops_summary()
        self.assertTrue(s["ok"])
        self.assertEqual(s["mode"], "sim")
        self.assertEqual(s["period"], "today")
        self.assertEqual(s["calls"]["total"], s["calls"]["today"])
        self.assertEqual(s["calls"]["auto_done"], round(214 * 0.72))

    def test_week_and_month_totals(self):
        w = ops_stats.get_ops_summary(period="week")
        self.assertEqual((w["period"], w["calls"]["total"]), ("week", 1486))
        self.assertEqual(w["calls"]["auto_done"], round(1486 * 0.71))
        m = ops_stats.get_ops_summary(period="month")
        self.assertEqual((m["period"], m["calls"]["total"]), ("month", 6120))

    def test_today_key_stays_daily_across_periods(self):
        """하위호환: 기간을 바꿔도 calls.today 는 일 단위 값이다."""
        for p in ops_stats.PERIODS:
            self.assertEqual(ops_stats.get_ops_summary(period=p)["calls"]["today"], 214)

    def test_agent_connect_saved_equals_auto_done(self):
        """연결감축은 봇 완결 콜 수 그대로 — 별도 추정치를 지어내지 않는다."""
        for p in ops_stats.PERIODS:
            s = ops_stats.get_ops_summary(period=p)
            self.assertEqual(s["calls"]["agent_connect_saved"], s["calls"]["auto_done"])

    def test_baseline_override(self):
        s = ops_stats.get_ops_summary({"calls_today": 100, "auto_rate": 0.5})
        self.assertEqual(s["calls"]["total"], 100)
        self.assertEqual(s["calls"]["auto_done"], 50)

    def test_baseline_override_does_not_mutate_module_constant(self):
        before = dict(ops_stats.DEMO_BASELINE)
        ops_stats.get_ops_summary({"calls_today": 1, "auto_rate": 0.1})
        self.assertEqual(ops_stats.DEMO_BASELINE, before)

    def test_unknown_period_falls_back_to_today(self):
        for bad in ("yyy", "", None, 3, ["week"], {"a": 1}):
            self.assertEqual(ops_stats.get_ops_summary(period=bad)["period"], "today")

    def test_rates_are_rounded_and_bounded(self):
        for p in ops_stats.PERIODS:
            s = ops_stats.get_ops_summary(period=p)
            for v in (s["calls"]["auto_rate"], s["sla"]["attain_rate"]):
                self.assertTrue(0.0 <= v <= 1.0, v)
                self.assertEqual(v, round(v, 3))

    def test_counts_are_non_negative_ints(self):
        s = ops_stats.get_ops_summary()
        for k in ("today", "total", "auto_done", "agent_connect_saved"):
            self.assertIsInstance(s["calls"][k], int)
            self.assertGreaterEqual(s["calls"][k], 0)

    def test_auto_done_never_exceeds_total(self):
        for p in ops_stats.PERIODS:
            s = ops_stats.get_ops_summary(period=p)
            self.assertLessEqual(s["calls"]["auto_done"], s["calls"]["total"])

    def test_ts_is_epoch_seconds(self):
        s = ops_stats.get_ops_summary()
        self.assertIsInstance(s["ts"], int)
        self.assertGreater(s["ts"], 1700000000)

    def test_json_serializable(self):
        json.dumps(ops_stats.get_ops_summary(period="week"), ensure_ascii=False)


# ==========================================================================
# 2) 스키마 고정 (소스 장애에도 화면이 깨지지 않는다)
# ==========================================================================
class TestNormalize(Base):
    def test_missing_source_is_zeroed_and_marked(self):
        n = ops_stats._norm_stats(None, ops_stats.ESCALATION_KEYS)
        self.assertEqual(n["source"], "unavailable")
        self.assertTrue(all(n[k] == 0 for k in ops_stats.ESCALATION_KEYS))

    def test_broken_values_default_to_zero(self):
        n = ops_stats._norm_stats(
            {"queued": "3", "assigned": None, "total": "x", "extra": 9},
            ops_stats.ESCALATION_KEYS)
        self.assertEqual(n["queued"], 3)
        self.assertEqual(n["assigned"], 0)
        self.assertEqual(n["total"], 0)
        self.assertEqual(n["resolved"], 0)
        self.assertEqual(n["source"], "sim")

    def test_unknown_keys_are_dropped(self):
        """소스가 키를 늘려도 응답 스키마는 고정 — 콘솔이 예상 못한 값을 그리지 않는다."""
        n = ops_stats._norm_stats({"queued": 1, "surprise": 5}, ops_stats.ESCALATION_KEYS)
        self.assertEqual(set(n), set(ops_stats.ESCALATION_KEYS) | {"source"})

    def test_non_dict_source_is_unavailable(self):
        for raw in ("oops", 5, [], None):
            self.assertEqual(
                ops_stats._norm_stats(raw, ops_stats.RECORDING_KEYS)["source"],
                "unavailable")

    def test_safe_stats_swallows_missing_module(self):
        self.assertIsNone(ops_stats._safe_stats("__no_such_module__", "QUEUE"))
        self.assertIsNone(ops_stats._safe_stats("escalation", "__NOPE__"))

    def test_summary_keeps_schema_when_source_raises(self):
        """stats() 가 터져도 200 스키마 그대로 — 대시보드가 빈 화면이 되지 않는다."""
        import escalation
        orig = escalation.QUEUE.stats
        escalation.QUEUE.stats = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            s = ops_stats.get_ops_summary()
        finally:
            escalation.QUEUE.stats = orig
        self.assertEqual(s["escalation"]["source"], "unavailable")
        for k in ops_stats.ESCALATION_KEYS:
            self.assertEqual(s["escalation"][k], 0)
        self.assertTrue(s["ok"])

    def test_response_key_set_is_stable(self):
        """콘솔 계약: 최상위 키가 조용히 바뀌면 화면이 드리프트한다."""
        self.assertEqual(
            set(ops_stats.get_ops_summary()),
            {"ok", "ts", "mode", "period", "calls", "wait", "sla",
             "escalation", "recording", "gates", "data_source"})


# ==========================================================================
# 3) 게이트 플래그 — 읽기 전용
# ==========================================================================
class TestGates(Base):
    def test_default_off(self):
        g = ops_stats.gate_flags()
        self.assertEqual(set(g), {"recording_live", "cpaas_live", "speech_live"})
        self.assertTrue(all(v is False for v in g.values()))

    def test_truthy_and_falsy_spellings(self):
        for raw, want in (("1", True), ("true", True), ("ON", True), ("Yes", True),
                          ("0", False), ("off", False), ("", False), ("maybe", False)):
            os.environ["SPEECH_LIVE"] = raw
            self.assertIs(ops_stats.gate_flags()["speech_live"], want, raw)

    def test_reading_gates_does_not_set_them(self):
        """읽기만 한다 — 조회가 실발신 스위치를 켜면 안 된다."""
        before = {k: os.environ.get(k) for k in ops_stats.GATE_FLAGS}
        ops_stats.gate_flags()
        ops_stats.get_ops_summary()
        call("GET", headers=SAME_ORIGIN)
        self.assertEqual({k: os.environ.get(k) for k in ops_stats.GATE_FLAGS}, before)

    def test_live_gate_is_reported_not_acted_on(self):
        os.environ["CPAAS_LIVE"] = "1"
        r = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertIs(r.body()["gates"]["cpaas_live"], True)
        self.assertEqual(r.body()["mode"], "sim")   # 조회는 여전히 sim


# ==========================================================================
# 4) 읽기 전용 · 부작용 없음
# ==========================================================================
class TestNoSideEffects(Base):
    def test_repeated_calls_do_not_change_queue(self):
        import escalation
        before = escalation.QUEUE.stats()
        for _ in range(3):
            call("GET", headers=SAME_ORIGIN)
        self.assertEqual(escalation.QUEUE.stats(), before)

    def test_no_network(self):
        """urlopen 감시 — 지표 조회가 외부를 호출하면(과금) 실패한다."""
        import urllib.request
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: self.fail("네트워크 호출 발생")
        try:
            call("GET", headers=SAME_ORIGIN)
            call("GET", path="/api/ops_stats?period=month", headers=SAME_ORIGIN)
        finally:
            urllib.request.urlopen = orig

    def test_two_reads_agree_except_timestamp(self):
        a = ops_stats.get_ops_summary()
        b = ops_stats.get_ops_summary()
        a.pop("ts"), b.pop("ts")
        self.assertEqual(a, b)


# ==========================================================================
# 5) HTTP 계약
# ==========================================================================
class TestHttp(Base):
    def test_get_ok(self):
        r = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body()["ok"])
        self.assertEqual(r.header("Content-Type"),
                         "application/json; charset=utf-8")

    def test_period_query_is_honoured(self):
        r = call("GET", path="/api/ops_stats?period=week", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["period"], "week")

    def test_period_is_case_insensitive(self):
        r = call("GET", path="/api/ops_stats?period=WEEK", headers=SAME_ORIGIN)
        self.assertEqual(r.body()["period"], "week")

    def test_blank_period_defaults_to_today(self):
        r = call("GET", path="/api/ops_stats?period=", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["period"], "today")

    def test_bad_period_is_400_with_field_detail(self):
        """잘못된 입력에 어느 항목이 왜 틀렸는지 알려준다(QUALITY_BAR §1)."""
        r = call("GET", path="/api/ops_stats?period=year", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 400)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["details"][0]["field"], "period")
        self.assertIn("week", b["details"][0]["reason"])

    def test_cross_origin_denied(self):
        r = call("GET", headers={"origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertFalse(r.body()["ok"])

    def test_no_browser_signal_denied(self):
        r = call("GET", headers={})
        self.assertEqual(r.status, 403)

    def test_api_key_allows_non_browser(self):
        os.environ["CALLBOT_API_KEY"] = "k-secret"
        r = call("GET", headers={"x-api-key": "k-secret"})
        self.assertEqual(r.status, 200)

    def test_cache_control_no_store(self):
        r = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(r.header("Cache-Control"), "no-store")

    def test_request_id_header_present(self):
        r = call("GET", headers=SAME_ORIGIN)
        self.assertTrue(r.header("X-Request-Id"))

    def test_inbound_request_id_is_inherited(self):
        h = dict(SAME_ORIGIN, **{"x-request-id": "rq-abc-123"})
        r = call("GET", headers=h)
        self.assertEqual(r.header("X-Request-Id"), "rq-abc-123")

    def test_cors_echoes_only_allowed_origin(self):
        r = call("GET", headers={"origin": "https://evil.example"})
        self.assertEqual(r.header("Access-Control-Allow-Origin"), _guard.ALLOWED[0])

    def test_content_length_matches_body(self):
        r = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(int(r.header("Content-Length")), len(r.wfile.data))

    def test_options_preflight(self):
        r = call("OPTIONS", headers=SAME_ORIGIN)
        self.assertEqual(r.status, 204)
        self.assertTrue(r.header("Access-Control-Allow-Origin"))

    def test_internal_error_is_standard_envelope_not_leak(self):
        """오류를 삼키지 않는다 · 내부 문구를 응답에 싣지 않는다."""
        orig = ops_stats.get_ops_summary
        ops_stats.get_ops_summary = lambda **k: (_ for _ in ()).throw(
            RuntimeError("db://user:pw@host 접속 실패"))
        try:
            r = call("GET", headers=SAME_ORIGIN)
        finally:
            ops_stats.get_ops_summary = orig
        self.assertEqual(r.status, 500)
        raw = r.wfile.data.decode("utf-8")
        self.assertNotIn("user:pw", raw)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertTrue(b.get("code"))


# ==========================================================================
# 6) 감사 기록
# ==========================================================================
class TestAudit(Base):
    def test_allow_is_recorded(self):
        call("GET", headers=SAME_ORIGIN)
        rec = _audit.recent()
        self.assertTrue(rec)
        self.assertEqual(rec[-1]["action"], "ops.stats.read")
        self.assertEqual(rec[-1]["result"], "allow")
        self.assertEqual(rec[-1]["status"], 200)

    def test_deny_is_recorded(self):
        """침해 조사에서는 실패한 시도가 더 중요하다."""
        call("GET", headers={"origin": "https://evil.example"})
        rec = _audit.recent()
        self.assertTrue(rec)
        self.assertEqual(rec[-1]["result"], "deny")
        self.assertEqual(rec[-1]["status"], 403)

    def test_error_is_not_recorded_as_allow_200(self):
        """감사 기록이 실패한 요청을 성공으로 적으면 조사 근거가 무너진다."""
        orig = ops_stats.get_ops_summary
        ops_stats.get_ops_summary = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = call("GET", headers=SAME_ORIGIN)
        finally:
            ops_stats.get_ops_summary = orig
        self.assertEqual(r.status, 500)
        last = _audit.recent()[-1]
        self.assertEqual(last["result"], "error")
        self.assertEqual(last["status"], 500)

    def test_bad_input_is_recorded_with_400(self):
        call("GET", path="/api/ops_stats?period=year", headers=SAME_ORIGIN)
        last = _audit.recent()[-1]
        self.assertEqual(last["status"], 400)
        self.assertIn(last["result"], ("error", "deny"))

    def test_audit_never_logs_raw_ip_or_key(self):
        os.environ["CALLBOT_API_KEY"] = "k-supersecret"
        call("GET", headers={"x-api-key": "k-supersecret",
                             "x-forwarded-for": "203.0.113.9"})
        raw = json.dumps(_audit.recent(), ensure_ascii=False)
        self.assertNotIn("203.0.113.9", raw)
        self.assertNotIn("k-supersecret", raw)

    def test_audit_failure_does_not_break_request(self):
        """감사가 죽어도 서비스는 산다(가용성 우선)."""
        orig = _audit.record_request
        _audit.record_request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down"))
        try:
            r = call("GET", headers=SAME_ORIGIN)
        finally:
            _audit.record_request = orig
        self.assertEqual(r.status, 200)


# ==========================================================================
# 7) 안전 · 정직성
# ==========================================================================
class TestSafety(Base):
    def test_no_secret_in_response(self):
        os.environ["CALLBOT_API_KEY"] = "k-supersecret"
        r = call("GET", headers={"x-api-key": "k-supersecret"})
        self.assertNotIn("supersecret", r.wfile.data.decode("utf-8"))

    def test_response_carries_no_identifiers(self):
        """집계 숫자만 — 통화·성명·번호 같은 원문이 실리지 않는다."""
        raw = json.dumps(ops_stats.get_ops_summary(), ensure_ascii=False)
        for bad in ("010", "@", "name", "phone", "msisdn", "caller"):
            self.assertNotIn(bad, raw)

    def test_demo_numbers_are_labelled_as_demo(self):
        """근거 없는 수치를 실측처럼 내보내지 않는다(QUALITY_BAR §3)."""
        s = ops_stats.get_ops_summary()
        self.assertEqual(s["mode"], "sim")
        self.assertEqual(s["data_source"], "demo")

    def test_periods_whitelist_matches_demo_definition(self):
        """드리프트 방지: 입력 화이트리스트와 데모 정의가 갈라지면 400 이 엉뚱해진다."""
        self.assertEqual(set(ops_stats.PERIODS), set(ops_stats.DEMO_PERIODS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
