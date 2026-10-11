# -*- coding: utf-8 -*-
"""구조화 로그 **보조 필드(extra) 규약** 회귀 — 26차.

25차가 남긴 문제: `rq.set()` 으로 붙는 칸 이름이 라우트마다 제각각이었다
(`msg_count`·`lines`·`turns` 가 모두 '건수'). 횡단 집계("어떤 라우트가 어떤
입력에서 걸리는가")를 하려면 같은 뜻에 같은 이름이어야 하고, 더 위험한 쪽은
**임의 칸이 한 줄로 늘어나는 것**이다 — 집계 카디널리티가 터지고 PII 유입
경로가 된다(18차가 `op`·`ev` 의 *값*을 화이트리스트로 접은 것과 같은 이유).

여기서 고정하는 것
  1) 등록부(`_log.FIELDS`)와 실제 사용이 **양방향으로** 일치한다
     — 등록부 밖의 칸을 쓰면 릴리스 게이트(`scripts/verify.py` log_fields)가
       실패하고, 아무도 안 쓰는 칸이 썩어 남지도 않는다.
  2) 이름 규약(snake_case·길이·수량 접미 `_count`·불리언 `is_` 금지).
  3 ) 값 규약: 스칼라만 남고 문자열은 마스킹되며, **보조 필드 하나 때문에
      요청 로그 한 줄을 잃지 않는다**(직렬화 실패 시 extra 만 떼고 남긴다).

의존성 0 · 네트워크 미사용. 실행: python3 tests/test_log_fields.py
"""
import io
import os
import re
import sys
import json
import unittest
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _log  # noqa: E402
import verify  # noqa: E402

API_DIR = os.path.join(ROOT, "api")


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


def api_sources():
    return {f: io.open(os.path.join(API_DIR, f), encoding="utf-8").read()
            for f in sorted(os.listdir(API_DIR)) if f.endswith(".py")}


# ==========================================================================
# 1) 등록부 ↔ 실제 사용
# ==========================================================================
class TestRegistry(unittest.TestCase):

    def setUp(self):
        self.src = api_sources()
        self.used = set()
        for name, text in self.src.items():
            if name == "_log.py":
                continue
            for field, _line in verify._log_kwargs(text):
                self.used.add(field)

    def test_every_used_field_is_registered(self):
        self.assertEqual(sorted(self.used - set(_log.FIELDS)), [])

    def test_registry_has_no_stale_entries(self):
        """쓰지 않는 칸이 남아 있으면 등록부가 '현실'이 아니다(outbound 게이트와 같은 판단)."""
        self.assertEqual(sorted(set(_log.FIELDS) - self.used), [])

    def test_gate_reads_the_registry_from_source(self):
        """게이트는 api 코드를 **실행하지 않고** 등록부를 읽는다."""
        self.assertEqual(verify._log_fields_registry(), set(_log.FIELDS))

    def test_gate_passes_on_current_tree(self):
        ok, msg = verify.check_log_fields(self.src)
        self.assertTrue(ok, msg)

    def test_gate_fails_on_unregistered_field(self):
        bad = dict(self.src)
        bad["zz_fake_route.py"] = "def f(rq):\n    rq.set(customer_phone='010')\n"
        ok, msg = verify.check_log_fields(bad)
        self.assertFalse(ok)
        self.assertIn("customer_phone", msg)

    def test_gate_catches_wrapper_calls(self):
        """`_close(rq, code, ...)` 같은 래퍼 경유도 센다(voice·health 가 그 모양이다)."""
        bad = dict(self.src)
        bad["zz_fake_route.py"] = "def f(rq):\n    _close(rq, 200, senior_id='SR-1')\n"
        ok, msg = verify.check_log_fields(bad)
        self.assertFalse(ok)
        self.assertIn("senior_id", msg)

    def test_gate_ignores_control_kwargs(self):
        src = {"zz.py": "def f(rq):\n    rq.finish(200, code='X')\n    _close(rq, 1, deep=True)\n"}
        ok, msg = verify.check_log_fields(src)
        self.assertTrue(ok, msg)

    def test_gate_reports_missing_registry(self):
        saved = verify.API_DIR
        verify.API_DIR = os.path.join(ROOT, "scripts")   # _log.py 가 없는 폴더
        try:
            ok, msg = verify.check_log_fields(self.src)
        finally:
            verify.API_DIR = saved
        self.assertFalse(ok)
        self.assertIn("FIELDS", msg)

    def test_verify_main_includes_the_gate(self):
        import subprocess
        p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "verify.py"),
                            "--json"], stdout=subprocess.PIPE)
        data = json.loads(p.stdout.decode("utf-8"))
        steps = {s["step"]: s["ok"] for s in data["steps"]}
        self.assertIn("log_fields", steps)
        self.assertTrue(data["ok"], p.stdout.decode("utf-8"))


# ==========================================================================
# 2) 이름 규약
# ==========================================================================
class TestNaming(unittest.TestCase):

    def test_names_are_snake_case_and_short(self):
        rx = re.compile(r"^[a-z][a-z0-9_]*$")
        for k in _log.FIELDS:
            self.assertRegex(k, rx, k)
            self.assertLessEqual(len(k), _log.MAX_KEY, k)

    def test_every_field_documents_why(self):
        for k, why in _log.FIELDS.items():
            self.assertTrue(isinstance(why, str) and len(why) >= 4, k)

    def test_boolean_fields_do_not_use_is_prefix(self):
        for k in _log.FIELDS:
            self.assertFalse(k.startswith("is_"), k)

    def test_counts_use_count_suffix(self):
        """같은 뜻에 같은 이름 — `lines`·`turns` 처럼 제각각이던 것을 통일했다."""
        for k in ("msg_count", "line_count", "turn_count"):
            self.assertIn(k, _log.FIELDS, k)
        self.assertNotIn("lines", _log.FIELDS)
        self.assertNotIn("turns", _log.FIELDS)
        src = api_sources()
        for name, text in src.items():
            for field, line in verify._log_kwargs(text):
                self.assertNotIn(field, ("lines", "turns"), "%s:%d" % (name, line))


# ==========================================================================
# 3) 값 규약
# ==========================================================================
class TestValues(unittest.TestCase):

    def test_scalars_keep_their_type(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").set(msg_count=3, denied=True, rate=0.5).finish(200)
        (r,) = lines(b)
        self.assertEqual(r["extra"]["msg_count"], 3)
        self.assertIs(r["extra"]["denied"], True)
        self.assertEqual(r["extra"]["rate"], 0.5)

    def test_non_scalar_is_folded_to_string(self):
        """비스칼라를 그대로 담으면 직렬화 실패로 **그 줄이 통째로 사라졌다**."""
        with captured() as b:
            _log.begin(None, "/x", "GET").set(rows=[1, 2], meta={"k": "v"}).finish(200)
        (r,) = lines(b)
        self.assertEqual(r["extra"]["rows"], "[1, 2]")
        self.assertEqual(r["extra"]["meta"], "{'k': 'v'}")

    def test_unserializable_value_still_leaves_one_line(self):
        class Boom(object):
            def __str__(self):
                raise RuntimeError("nope")

        with captured() as b:
            _log.begin(None, "/x", "GET").set(obj=Boom()).finish(500)
        (r,) = lines(b)
        self.assertEqual(r["status"], 500)
        self.assertEqual(r["extra"]["obj"], "<unprintable>")

    def test_long_values_are_capped(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").set(op="x" * 500).finish(200)
        (r,) = lines(b)
        self.assertLessEqual(len(r["extra"]["op"]), _log.MAX_FIELD)

    def test_string_values_are_masked(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").set(op="call 010-1234-5678").finish(200)
        (r,) = lines(b)
        self.assertNotIn("010-1234-5678", json.dumps(r, ensure_ascii=False))

    def test_key_names_are_capped(self):
        with captured() as b:
            _log.begin(None, "/x", "GET").set(**{"k" * 100: 1}).finish(200)
        (r,) = lines(b)
        self.assertEqual([len(k) for k in r["extra"]], [_log.MAX_KEY])

    def test_emit_drops_only_extra_when_serialization_fails(self):
        """요청 1건=1줄은 장애 조사의 바닥이다 — extra 한 칸에 양보하지 않는다."""
        with captured() as b:
            _log.emit({"status": 200, "request_id": "abc", "extra": {"bad": {1, 2}}})
        (r,) = lines(b)
        self.assertEqual(r["request_id"], "abc")
        self.assertIs(r["extra_error"], True)
        self.assertNotIn("extra", r)

    def test_emit_survives_broken_enable_check(self):
        """게이트 판독이 터져도 예외를 올리지 않는다(로깅이 서비스를 죽이지 않는다)."""
        saved = _log.enabled
        _log.enabled = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with captured() as b:
                _log.emit({"status": 200})
            self.assertEqual(b.getvalue(), "")
        finally:
            _log.enabled = saved

    def test_emit_stays_silent_when_disabled(self):
        os.environ["CALLBOT_LOG"] = "off"
        try:
            with captured() as b:
                _log.begin(None, "/x", "GET").set(op="info").finish(200)
            self.assertEqual(b.getvalue(), "")
        finally:
            os.environ.pop("CALLBOT_LOG", None)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
