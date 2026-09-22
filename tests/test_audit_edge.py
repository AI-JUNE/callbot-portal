# -*- coding: utf-8 -*-
"""api/_audit.py · api/call_metrics.py 잔여 분기 회귀 — 의존성 0, 네트워크 미사용.

앞선 회차(tests/test_audit.py, tests/test_call_metrics.py)가 정상 경로와 주요
거부 경로를 덮었다. 이 파일은 **아직 한 번도 실행되지 않은 방어 분기**를 덮는다.
방어 코드는 장애가 났을 때에만 실행되므로, 회귀가 없으면 "있는 줄만 알았지
실제로는 터지는" 상태가 된다(감사 로그·지표는 조용히 죽으면 눈치채기 어렵다).

검증 대상
  1) 로깅 모듈 부재 폴백 — `_log` 를 못 불러와도 감사 기록이 동작한다
  2) 솔트 — CALLBOT_AUDIT_SALT 고정 시 해시가 재현되고, 미설정이면 인스턴스 임의값
  3) 해시 실패 — 해시 불가 입력에도 예외 대신 "-" 를 돌려준다
  4) UA 분류 — 알려진 계열에 없으면 "other"(알 수 없음을 삼키지 않는다)
  5) 보조 필드 정리 — 스칼라가 아닌 값도 문자열로 잘라 담고, 길이 상한을 지킨다
  6) 저장 실패 격리 — 버퍼 저장이 터져도 record() 는 예외를 내지 않는다
  7) 조회 실패 격리 — recent()/snapshot() 이 터져도 빈 결과·비활성 표시로 끝난다
  8) call_metrics 라벨·안전 래퍼 — 공백 라벨 거부, 카운터까지 죽어도 통화는 산다

실행: python3 -m pytest tests/test_audit_edge.py
"""
import os
import sys
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _audit          # noqa: E402
import call_metrics    # noqa: E402


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


def H(**kw):
    return FakeHeaders({k.replace("_", "-"): v for k, v in kw.items()})


class EdgeBase(unittest.TestCase):
    ENV_KEYS = ("CALLBOT_AUDIT", "CALLBOT_AUDIT_BUFFER", "CALLBOT_AUDIT_SALT",
                "CALLBOT_API_KEY", "CALLBOT_LOG")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        os.environ["CALLBOT_LOG"] = "off"
        _audit.reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _audit._SALT = None
        _audit.reset()


# --------------------------------------------------------------------------
# 1) 로깅 모듈 부재 폴백
# --------------------------------------------------------------------------
class TestLogFallback(EdgeBase):
    """_log 를 못 불러오는 환경(부분 배포·모듈 누락)에서도 감사 기록은 살아야 한다.

    _audit 를 `_log` import 가 실패하도록 강제한 상태에서 새로 적재해,
    폴백으로 정의된 _emit/_safe_path 가 실제로 동작하는지 본다.
    """

    def _load_without_log(self):
        import importlib
        import io
        import types
        saved_log = sys.modules.get("_log")
        saved_audit = sys.modules.get("_audit")
        broken = types.ModuleType("_log")   # emit/safe_path 가 없는 모듈 → ImportError

        buf = io.StringIO()
        saved_stdout = sys.stdout
        try:
            sys.modules["_log"] = broken
            sys.modules.pop("_audit", None)
            sys.stdout = buf
            mod = importlib.import_module("_audit")
            return mod, buf
        finally:
            sys.stdout = saved_stdout
            if saved_log is not None:
                sys.modules["_log"] = saved_log
            else:
                sys.modules.pop("_log", None)
            if saved_audit is not None:
                sys.modules["_audit"] = saved_audit

    def test_record_works_and_writes_json_line_without_log_module(self):
        mod, buf = self._load_without_log()
        os.environ.pop("CALLBOT_LOG", None)      # 폴백 emit 은 CALLBOT_LOG 를 모른다
        mod.reset()
        import io as _io
        cap = _io.StringIO()
        saved = sys.stdout
        try:
            sys.stdout = cap
            rec = mod.record(H(origin="https://callbot-portal.vercel.app"),
                             "ops.stats.read", "allow", status=200)
        finally:
            sys.stdout = saved
            os.environ["CALLBOT_LOG"] = "off"
        self.assertIsNotNone(rec)
        self.assertEqual(rec["action"], "ops.stats.read")
        line = cap.getvalue().strip().splitlines()[-1]
        self.assertEqual(json.loads(line)["kind"], "audit")   # 폴백도 JSON 한 줄

    def test_fallback_safe_path_strips_query_and_fragment(self):
        mod, _ = self._load_without_log()
        self.assertEqual(mod.action_for("/api/ops_stats?period=week&t=secret"),
                         "ops.stats.read")
        self.assertEqual(mod.action_for("/api/ops_stats#frag"), "ops.stats.read")
        self.assertIsNone(mod.action_for("/api/chat?x=1"))

    def test_fallback_safe_path_caps_length(self):
        mod, _ = self._load_without_log()
        rec = mod.record(H(), "ops.stats.read", "allow", status=200,
                         path="/api/ops_stats/" + ("a" * 500))
        self.assertLessEqual(len(rec["path"]), 200)


# --------------------------------------------------------------------------
# 2) 솔트
# --------------------------------------------------------------------------
class TestSalt(EdgeBase):
    def test_fixed_salt_makes_client_hash_reproducible(self):
        os.environ["CALLBOT_AUDIT_SALT"] = "fixed-salt-for-test"
        _audit._SALT = None
        h1 = _audit.client_hash(H(x_forwarded_for="203.0.113.9"))
        h2 = _audit.client_hash(H(x_forwarded_for="203.0.113.9"))
        self.assertEqual(h1, h2)
        self.assertNotIn("203.0.113.9", json.dumps(h1))

    def test_fixed_salt_changes_the_hash(self):
        os.environ["CALLBOT_AUDIT_SALT"] = "salt-a"
        _audit._SALT = None
        a = _audit.client_hash(H(x_forwarded_for="203.0.113.9"))
        os.environ["CALLBOT_AUDIT_SALT"] = "salt-b"
        _audit._SALT = None
        b = _audit.client_hash(H(x_forwarded_for="203.0.113.9"))
        self.assertNotEqual(a, b)

    def test_blank_salt_env_falls_back_to_process_salt(self):
        os.environ["CALLBOT_AUDIT_SALT"] = "   "
        _audit._SALT = None
        s1 = _audit._salt()
        self.assertTrue(s1)
        self.assertEqual(s1, _audit._salt())          # 프로세스 내에서는 고정
        self.assertFalse(_audit.snapshot()["salt_fixed"])

    def test_snapshot_reports_fixed_salt(self):
        os.environ["CALLBOT_AUDIT_SALT"] = "fixed"
        self.assertTrue(_audit.snapshot()["salt_fixed"])

    def test_hash_helper_never_raises(self):
        class Boom(object):
            def __str__(self):
                raise RuntimeError("no str")
        self.assertEqual(_audit._h(Boom()), "-")


# --------------------------------------------------------------------------
# 3) UA 분류
# --------------------------------------------------------------------------
class TestUaFamily(EdgeBase):
    def test_unknown_agent_is_other_not_browser(self):
        self.assertEqual(_audit.ua_family(H(user_agent="ACME-Monitor/2.1")), "other")

    def test_missing_and_broken_headers(self):
        self.assertEqual(_audit.ua_family(H()), "none")
        self.assertEqual(_audit.ua_family(H(user_agent="")), "none")

        class Bad(object):
            def get(self, *a, **kw):
                raise RuntimeError("header store down")
        self.assertEqual(_audit.ua_family(Bad()), "unknown")

    def test_known_families_still_classified(self):
        for ua, want in (("Mozilla/5.0 Chrome/120", "browser"),
                         ("curl/8.0", "cli"),
                         ("python-requests/2.31", "cli"),
                         ("Googlebot/2.1", "bot")):
            self.assertEqual(_audit.ua_family(H(user_agent=ua)), want, ua)


# --------------------------------------------------------------------------
# 4) 보조 필드 정리
# --------------------------------------------------------------------------
class TestSanitize(EdgeBase):
    def test_non_scalar_extra_is_stringified_and_capped(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200,
                            rows=[1, 2, 3], meta={"k": "v"})
        self.assertEqual(rec["extra"]["rows"], "[1, 2, 3]")
        self.assertEqual(rec["extra"]["meta"], "{'k': 'v'}")

    def test_long_non_scalar_is_capped(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200,
                            rows=list(range(500)))
        self.assertLessEqual(len(rec["extra"]["rows"]), _audit.MAX_FIELD)

    def test_scalar_types_are_preserved(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200,
                            ok=True, n=3, ratio=0.5, s="x")
        self.assertIs(rec["extra"]["ok"], True)
        self.assertEqual(rec["extra"]["n"], 3)
        self.assertEqual(rec["extra"]["ratio"], 0.5)
        self.assertEqual(rec["extra"]["s"], "x")

    def test_target_uses_the_same_sanitiser(self):
        rec = _audit.record(H(), "ops.stats.read", "allow", status=200,
                            target=["a"] * 200)
        self.assertIsInstance(rec["target"], str)
        self.assertLessEqual(len(rec["target"]), _audit.MAX_FIELD)


# --------------------------------------------------------------------------
# 5) 저장·조회 실패 격리 (가용성 우선)
# --------------------------------------------------------------------------
class TestFailureIsolation(EdgeBase):
    def test_store_failure_does_not_break_record(self):
        saved = _audit.buffer_size
        _audit.buffer_size = lambda: (_ for _ in ()).throw(RuntimeError("cap down"))
        try:
            rec = _audit.record(H(), "ops.stats.read", "allow", status=200)
        finally:
            _audit.buffer_size = saved
        self.assertIsNotNone(rec)            # 기록 자체는 성공(로그는 이미 나갔다)
        self.assertEqual(_audit.recent(10), [])   # 버퍼에는 안 들어갔음을 드러낸다

    def test_recent_returns_empty_on_failure_instead_of_raising(self):
        class Bad(object):
            def __int__(self):
                raise RuntimeError("bad limit")
        self.assertEqual(_audit.recent(Bad()), [])

    def test_recent_clamps_limit(self):
        for i in range(5):
            _audit.record(H(), "ops.stats.read", "allow", status=200, request_id="r%d" % i)
        self.assertEqual(len(_audit.recent(-5)), 1)      # 음수 → 최소 1건
        self.assertEqual(len(_audit.recent(0)), 5)       # 0·None → 기본 50건
        self.assertEqual(len(_audit.recent(None)), 5)
        self.assertEqual(len(_audit.recent(10 ** 9)), 5)  # 상한 밖 요청도 사고 없이

    def test_recent_returns_copies_not_live_records(self):
        _audit.record(H(), "ops.stats.read", "allow", status=200)
        got = _audit.recent(1)[0]
        got["result"] = "tampered"
        self.assertEqual(_audit.recent(1)[0]["result"], "allow")   # append-only

    def test_snapshot_degrades_without_raising(self):
        saved = _audit.counters
        _audit.counters = lambda: (_ for _ in ()).throw(RuntimeError("counters down"))
        try:
            s = _audit.snapshot()
        finally:
            _audit.counters = saved
        self.assertFalse(s["enabled"])
        self.assertIn("RuntimeError", s["note"])      # 타입명만, 내부 문구 미노출

    def test_emit_failure_does_not_break_record(self):
        saved = _audit._emit
        _audit._emit = lambda rec: (_ for _ in ()).throw(RuntimeError("sink down"))
        try:
            rec = _audit.record(H(), "ops.stats.read", "allow", status=200)
        finally:
            _audit._emit = saved
        self.assertIsNone(rec)                         # 예외는 삼키되 None 으로 드러냄

    def test_unknown_result_is_recorded_as_error_not_dropped(self):
        rec = _audit.record(H(), "ops.stats.read", "weird", status=500)
        self.assertEqual(rec["result"], "error")
        self.assertEqual(_audit.counters()["error"], 1)


# --------------------------------------------------------------------------
# 6) call_metrics 잔여 분기
# --------------------------------------------------------------------------
class TestCallMetricsEdge(unittest.TestCase):
    def setUp(self):
        call_metrics.reset()

    def tearDown(self):
        call_metrics.reset()

    def test_blank_label_is_rejected(self):
        self.assertIsNone(call_metrics._label("   "))
        self.assertIsNone(call_metrics._label(""))
        self.assertIsNone(call_metrics._label(None))
        self.assertIsNone(call_metrics._label(123))

    def test_number_like_label_is_rejected(self):
        self.assertIsNone(call_metrics._label("010-1234-5678"))
        self.assertEqual(call_metrics._label("  안부  "), "안부")

    def test_label_is_capped(self):
        self.assertEqual(len(call_metrics._label("가" * 100)), 32)

    def test_safe_wrapper_survives_even_when_the_counter_dies(self):
        """수집 실패를 세는 카운터마저 죽어도 통화 경로는 예외를 보지 않는다."""
        class DeadSink(object):
            @property
            def errors(self):
                raise RuntimeError("sink down")

            @errors.setter
            def errors(self, v):
                raise RuntimeError("sink down")

        saved = call_metrics.SINK
        call_metrics.SINK = DeadSink()
        try:
            out = call_metrics._safe(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        finally:
            call_metrics.SINK = saved
        self.assertIsNone(out)

    def test_safe_wrapper_counts_swallowed_errors(self):
        before = call_metrics.SINK.errors
        call_metrics._safe(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(call_metrics.SINK.errors, before + 1)   # 조용히 삼키지 않는다


if __name__ == "__main__":
    unittest.main(verbosity=2)
