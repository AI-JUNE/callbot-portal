# -*- coding: utf-8 -*-
"""실활성화 게이트(`*_LIVE`) 회귀 — 37차. 네트워크 미사용.

왜 이 파일이 필요한가
  `CPAAS_LIVE`·`SPEECH_LIVE`·`RECORDING_LIVE` 는 이 저장소의 안전선이다
  (실발신·실과금·실녹취). 37차에 그 안전선에서 결함 두 건이 나왔고 둘 다
  "게이트를 **어떻게·언제** 읽는가"가 자리마다 달랐던 것이다.

  1) `api/voice.py` 만 게이트를 **import 시점에** 상수로 굳혀 두었다.
     웜 인스턴스에서는 `CPAAS_LIVE=0` 으로 내려도 캠페인이 계속
     `mode="live"` 로 답했다 — **비상정지가 듣지 않는다**. 같은 순간
     `wellbeing` 은 호출 시점에 읽어 501 로 막으므로, 한 인스턴스 안에서
     라우트마다 게이트가 엇갈렸다.
  2) `ops_stats.gate_flags()` 는 게이트 값을 **자기 규칙으로 다시 해석**했다
     (`"yes"` 까지 참). `CPAAS_LIVE=true` 에서 콘솔 모니터는 「ON」, 코드는
     전부 OFF 였고, 같은 화면의 발신번호 패널은 「OFF」라고 적었다.

  둘 다 평상시(기본 OFF)에는 답이 같아서 테스트가 적색이 되지 않는다.
  그래서 **다른 표기·다른 시점**을 직접 넣어 본다.

실행: python -m pytest tests/test_live_gates.py -q
"""
import ast
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import caller_id                     # noqa: E402
import ops_stats                     # noqa: E402
import verify                        # noqa: E402
import voice                         # noqa: E402
import wellbeing                     # noqa: E402
import _recording_audit              # noqa: E402
import _sip_adapter                  # noqa: E402
import _speech_providers             # noqa: E402

GATES = ("CPAAS_LIVE", "SPEECH_LIVE", "RECORDING_LIVE")
# 운영자가 실제로 적어 넣는 표기들. `true`·`on`·`yes` 는 사람이 자주 쓴다.
SPELLINGS = ("1", " 1 ", "0", "", "true", "TRUE", "on", "ON", "yes", "Yes",
             "off", "maybe", "01")


def _src(name):
    return io.open(os.path.join(API, name), encoding="utf-8").read()


class GateBase(unittest.TestCase):
    """환경변수는 **통째로** 복원한다.

    게이트만 되돌리면 이 파일이 켠 다른 변수(`CALLBOT_LOG`)가 뒤 테스트로
    새어 나간다 — 실제로 한 번 그렇게 다른 파일을 적색으로 만들었다.
    """

    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)


# ==========================================================================
# 1) 결함 ①: 게이트를 **호출 시점에** 읽는다 (비상정지가 듣는다)
# ==========================================================================
class TestReadAtCallTime(GateBase):
    def test_kill_switch_works_without_reimport(self):
        """켜고 → 내렸을 때, 이미 떠 있는 모듈이 그 자리에서 sim 으로 돌아간다."""
        os.environ["CPAAS_LIVE"] = "1"
        self.assertTrue(voice.live())
        self.assertEqual(voice.trigger_campaign(["01011112222"])["mode"], "live")
        os.environ["CPAAS_LIVE"] = "0"          # 비상정지 — 재배포 없이
        self.assertFalse(voice.live())
        out = voice.trigger_campaign(["01011112222"])
        self.assertEqual(out["queued"], 0)
        self.assertIn("dry-run", out["mode"])

    def test_turning_it_on_also_takes_effect_at_once(self):
        os.environ.pop("CPAAS_LIVE", None)
        self.assertFalse(voice.live())
        os.environ["CPAAS_LIVE"] = "1"
        self.assertTrue(voice.live())

    def test_rule_is_only_1_after_trimming(self):
        """`"1"` 정확 일치 — `true`·`on`·`yes` 는 OFF. 공백만 버린다."""
        for raw in SPELLINGS:
            os.environ["CPAAS_LIVE"] = raw
            self.assertIs(voice.live(), raw.strip() == "1", raw)

    def test_default_is_off_when_unset(self):
        os.environ.pop("CPAAS_LIVE", None)
        self.assertFalse(voice.live())

    def test_snapshot_constant_is_kept_for_compat(self):
        """`LIVE` 는 하위호환 스냅샷으로 남아 있다(판정에는 쓰지 않는다)."""
        self.assertIsInstance(voice.LIVE, bool)

    def test_module_level_no_longer_freezes_the_gate(self):
        """소스로 확인 — 모듈 전역 대입에 환경 조회가 다시 들어오면 적색."""
        tree = ast.parse(_src("voice.py"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for sub in ast.walk(node.value):
                if (isinstance(sub, ast.Constant)
                        and sub.value in GATES):
                    self.fail("voice.py 모듈 전역이 다시 게이트를 굳혔다: %d줄"
                              % node.lineno)

    def test_all_cpaas_routes_agree_at_the_same_moment(self):
        """라우트마다 게이트가 엇갈리면 운영자가 상태를 알 수 없다."""
        for raw in SPELLINGS:
            os.environ["CPAAS_LIVE"] = raw
            answers = {
                "voice": voice.live(),
                "wellbeing": wellbeing.live_mode(),
                "caller_id": caller_id.cpaas_live(),
                "_sip_adapter": _sip_adapter.is_live(),
            }
            self.assertEqual(len(set(answers.values())), 1,
                             "%r 에서 엇갈림: %r" % (raw, answers))

    def test_get_endpoint_reports_the_current_value(self):
        from test_voice import get, SAME_ORIGIN       # 같은 FakeHandler 재사용
        os.environ["CALLBOT_LOG"] = "off"
        os.environ["CPAAS_LIVE"] = "1"
        self.assertIs(get("/api/voice", headers=SAME_ORIGIN).json()["live"], True)
        os.environ["CPAAS_LIVE"] = "0"
        self.assertIs(get("/api/voice", headers=SAME_ORIGIN).json()["live"], False)

    def test_log_view_reports_the_current_value(self):
        from test_voice import get, SAME_ORIGIN
        os.environ["CALLBOT_LOG"] = "off"
        os.environ["CPAAS_LIVE"] = "1"
        r = get("/api/voice?op=log", headers=SAME_ORIGIN)
        self.assertIs(r.json()["live"], True)


# ==========================================================================
# 2) 결함 ②: 게이트 현황은 **소유 술어**가 판정한다 (복제 금지)
# ==========================================================================
class TestGateViewFollowsOwner(GateBase):
    OWNERS = {
        "cpaas_live": lambda: _sip_adapter.is_live(),
        "speech_live": lambda: _speech_providers.is_live(),
        "recording_live": lambda: _recording_audit.recording_live(),
    }

    def test_every_spelling_matches_the_owner(self):
        for gate in GATES:
            key = gate.lower()
            for raw in SPELLINGS:
                os.environ[gate] = raw
                self.assertIs(ops_stats.gate_flags()[key], self.OWNERS[key](),
                              "%s=%r" % (gate, raw))

    def test_true_is_not_on_for_the_dialing_gate(self):
        """결함 복귀 감시 — 예전에는 여기서 True 였다(화면만 켜졌다)."""
        for raw in ("true", "on", "yes"):
            os.environ["CPAAS_LIVE"] = raw
            self.assertFalse(ops_stats.gate_flags()["cpaas_live"], raw)
            self.assertFalse(voice.live(), raw)

    def test_recording_gate_keeps_the_wider_rule(self):
        """녹취는 장부·고지가 `("1","true","on")` 을 쓴다 — 좁히지도 않는다."""
        for raw in ("1", "true", "ON"):
            os.environ["RECORDING_LIVE"] = raw
            self.assertTrue(ops_stats.gate_flags()["recording_live"], raw)
        for raw in ("yes", "0", "", "maybe"):
            os.environ["RECORDING_LIVE"] = raw
            self.assertFalse(ops_stats.gate_flags()["recording_live"], raw)

    def test_registry_points_at_predicates_that_exist(self):
        for gate, (mod, attr, words) in ops_stats._GATE_OWNERS.items():
            self.assertIn(gate, GATES, gate)
            m = __import__(mod)
            self.assertTrue(callable(getattr(m, attr, None)),
                            "%s.%s 가 없다" % (mod, attr))
            self.assertIsInstance(words, tuple)
            self.assertTrue(all(isinstance(w, str) for w in words))

    def test_fallback_uses_the_same_rule_when_owner_is_unreachable(self):
        """소유 모듈을 못 불러와도 **규칙은 같다**(넓히면 화면이 거짓이 된다)."""
        saved = ops_stats._owner_flag
        ops_stats._owner_flag = lambda name: None
        try:
            for raw, want in (("1", True), ("true", False), ("yes", False),
                              ("0", False), ("", False)):
                os.environ["CPAAS_LIVE"] = raw
                self.assertIs(ops_stats.gate_flags()["cpaas_live"], want, raw)
            for raw, want in (("1", True), ("true", True), ("on", True),
                              ("yes", False), ("0", False)):
                os.environ["RECORDING_LIVE"] = raw
                self.assertIs(ops_stats.gate_flags()["recording_live"], want, raw)
        finally:
            ops_stats._owner_flag = saved

    def test_owner_exception_does_not_crash_the_dashboard(self):
        saved = dict(ops_stats._GATE_OWNERS)
        ops_stats._GATE_OWNERS["CPAAS_LIVE"] = ("_sip_adapter", "nope", ("1",))
        try:
            os.environ["CPAAS_LIVE"] = "1"
            g = ops_stats.gate_flags()
            self.assertIs(g["cpaas_live"], True)        # 폴백이 같은 규칙으로 답한다
        finally:
            ops_stats._GATE_OWNERS.clear()
            ops_stats._GATE_OWNERS.update(saved)

    def test_environ_failure_answers_off_not_on(self):
        """장애가 스위치를 켜지 않는다."""
        class Boom(object):
            def get(self, *a, **kw):
                raise RuntimeError("environ gone")

        class OsShim(object):
            environ = Boom()
        saved_os, saved_owner = ops_stats.os, ops_stats._owner_flag
        ops_stats.os = OsShim()
        ops_stats._owner_flag = lambda name: None
        try:
            self.assertFalse(ops_stats._flag("CPAAS_LIVE"))
            self.assertTrue(all(v is False for v in ops_stats.gate_flags().values()))
        finally:
            ops_stats.os, ops_stats._owner_flag = saved_os, saved_owner

    def test_reading_the_dashboard_does_not_set_the_gates(self):
        before = {k: os.environ.get(k) for k in GATES}
        ops_stats.gate_flags()
        ops_stats.get_ops_summary()
        self.assertEqual({k: os.environ.get(k) for k in GATES}, before)

    def test_health_does_not_echo_a_value_it_did_not_read(self):
        """`/health` 가 「CPAAS_LIVE=0」이라고 적으면 읽지 않은 값을 단언하는 것이다."""
        import health
        os.environ["CPAAS_LIVE"] = "true"          # OFF 로 읽히지만 0 은 아니다
        detail = [d for d in health._dependencies({}, {}, False)
                  if d["name"] == "cpaas"][0]["detail"]
        self.assertNotIn("CPAAS_LIVE=0", detail)
        self.assertIn("실발신 차단", detail)

    def test_schema_is_unchanged(self):
        g = ops_stats.gate_flags()
        self.assertEqual(set(g), {"recording_live", "cpaas_live", "speech_live"})
        self.assertTrue(all(isinstance(v, bool) for v in g.values()))


# ==========================================================================
# 3) 릴리스 게이트 18단계 `live_gates`
# ==========================================================================
class TestReleaseGate(unittest.TestCase):
    def setUp(self):
        self.srcs = verify.api_sources()

    def test_repo_passes(self):
        ok, msg = verify.check_live_gates(self.srcs)
        self.assertTrue(ok, msg)
        self.assertIn("게이트 3개", msg)

    def _mutate(self, name, old, new):
        srcs = dict(self.srcs)
        self.assertIn(old, srcs[name], "변이 대상이 없다: %s" % old)
        srcs[name] = srcs[name].replace(old, new, 1)
        return verify.check_live_gates(srcs)

    def test_catches_a_frozen_module_level_gate(self):
        """결함 ① 복귀 — 게이트 단독으로 잡아야 한다."""
        srcs = dict(self.srcs)
        srcs["voice.py"] = srcs["voice.py"].replace(
            "LIVE = live()", 'LIVE = os.environ.get("CPAAS_LIVE", "0") == "1"', 1)
        ok, msg = verify.check_live_gates(srcs)
        self.assertFalse(ok)
        self.assertIn("굳혔다", msg)

    def test_catches_an_unregistered_snapshot(self):
        srcs = dict(self.srcs)
        srcs["voice.py"] = srcs["voice.py"].replace("LIVE = live()",
                                                    "LIVE2 = live()", 1)
        ok, msg = verify.check_live_gates(srcs)
        self.assertFalse(ok)
        self.assertIn("LIVE_GATE_SNAPSHOTS", msg)

    def test_catches_an_unregistered_reader(self):
        srcs = dict(self.srcs)
        srcs["chat.py"] = srcs["chat.py"] + (
            '\n\ndef _x():\n    return os.environ.get("CPAAS_LIVE") == "1"\n')
        ok, msg = verify.check_live_gates(srcs)
        self.assertFalse(ok)
        self.assertIn("LIVE_GATE_READERS", msg)

    def test_catches_a_widened_reader_rule(self):
        ok, msg = self._mutate(
            "wellbeing.py",
            'return (os.environ.get("CPAAS_LIVE") or "").strip() == "1"',
            'return (os.environ.get("CPAAS_LIVE") or "").strip() in ("1", "yes")')
        self.assertFalse(ok)
        self.assertIn("판정 규칙", msg)

    def test_catches_a_narrowed_reader_rule(self):
        ok, msg = self._mutate(
            "disclosure.py", 'in ("1", "true", "on")', '== "1"')
        self.assertFalse(ok)
        self.assertIn("판정 규칙", msg)

    def test_catches_a_widened_owner_rule(self):
        ok, msg = self._mutate(
            "_sip_adapter.py",
            'return (os.environ.get("CPAAS_LIVE") or "0").strip() == "1"',
            'return (os.environ.get("CPAAS_LIVE") or "0").strip() in ("1", "true")')
        self.assertFalse(ok)
        self.assertIn("등록 규칙", msg)

    def test_catches_a_widened_owner_that_reads_through_a_variable(self):
        """`os.environ.get(GATE)` 처럼 변수를 거치는 자리는 **소유 술어 검사**만 본다.

        읽는 자리 검사(b)는 게이트 이름 문자열을 찾으므로 여기를 못 본다 —
        그래서 이 변이는 소유 술어 규칙 검사가 유일한 그물이다.
        """
        ok, msg = self._mutate(
            "_speech_providers.py", 'return (os.environ.get(GATE) or "").strip() == "1"',
            'return (os.environ.get(GATE) or "").strip() in ("1", "true")')
        self.assertFalse(ok)
        self.assertIn("등록 규칙", msg)

    def test_catches_a_missing_owner_predicate(self):
        ok, msg = self._mutate("_recording_audit.py", "def recording_live(",
                                "def recording_live_x(")
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_catches_a_view_that_copies_the_judgment(self):
        """결함 ② 복귀 — 현황 자리가 자기 규칙을 쓰면 잡아야 한다."""
        ok, msg = self._mutate(
            "ops_stats.py", '"CPAAS_LIVE": ("_sip_adapter", "is_live", ("1",))',
            '"CPAAS_LIVE": ("_sip_adapter", "is_live", ("1", "true", "on", "yes"))')
        self.assertFalse(ok)
        self.assertIn("폴백 규칙", msg)

    def test_catches_a_view_pointing_at_the_wrong_owner(self):
        ok, msg = self._mutate(
            "ops_stats.py", '"CPAAS_LIVE": ("_sip_adapter", "is_live", ("1",))',
            '"CPAAS_LIVE": ("ops_stats", "_flag", ("1",))')
        self.assertFalse(ok)
        self.assertIn("소유자", msg)

    def test_catches_a_view_that_drops_a_gate(self):
        ok, msg = self._mutate(
            "ops_stats.py", '"SPEECH_LIVE": ("_speech_providers", "is_live", ("1",)),',
            "")
        self.assertFalse(ok)
        self.assertIn("SPEECH_LIVE", msg)

    def test_catches_a_registered_reader_that_stopped_reading(self):
        """등록부 잔재 — 적어 둔 자리가 더 이상 읽지 않으면 등록부가 거짓말을 한다."""
        ok, msg = self._mutate("wellbeing.py", 'os.environ.get("CPAAS_LIVE")',
                                'os.environ.get("CPAAS_LIVE_GONE")')
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_catches_a_deleted_view_registry(self):
        ok, msg = self._mutate("ops_stats.py", "_GATE_OWNERS = {", "_GONE = {")
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    # -- 과차단 경계: 게이트가 울지 않아야 하는 자리들 --------------------
    def test_selftest_block_is_not_production_code(self):
        srcs = dict(self.srcs)
        srcs["chat.py"] = srcs["chat.py"] + (
            '\n\nif __name__ == "__main__":\n'
            '    os.environ.pop("CPAAS_LIVE", None)\n')
        ok, msg = verify.check_live_gates(srcs)
        self.assertTrue(ok, msg)

    def test_doc_and_message_mentions_are_not_reads(self):
        srcs = dict(self.srcs)
        srcs["chat.py"] = srcs["chat.py"] + (
            '\n\ndef _note():\n'
            '    """CPAAS_LIVE=1 이면 실발신이다."""\n'
            '    return "게이트 CPAAS_LIVE 를 확인하세요"\n')
        ok, msg = verify.check_live_gates(srcs)
        self.assertTrue(ok, msg)

    def test_a_gate_name_constant_is_not_a_read(self):
        """`GATE = "SPEECH_LIVE"` 는 이름이고 판정이 아니다(소유 술어가 쓴다)."""
        srcs = dict(self.srcs)
        srcs["chat.py"] = srcs["chat.py"] + '\n\nMY_GATE = "SPEECH_LIVE"\n'
        ok, msg = verify.check_live_gates(srcs)
        self.assertTrue(ok, msg)

    def test_other_env_vars_are_out_of_scope(self):
        srcs = dict(self.srcs)
        srcs["chat.py"] = srcs["chat.py"] + (
            '\n\nTHING = os.environ.get("CALLBOT_THING", "0") == "1"\n')
        ok, msg = verify.check_live_gates(srcs)
        self.assertTrue(ok, msg)

    def test_registry_is_not_stale(self):
        """등록한 읽는 자리가 실제로 있고, 등록한 스냅샷이 실제로 있다."""
        for (f, g) in verify.LIVE_GATE_READERS:
            self.assertIn(f, self.srcs, f)
            self.assertIn(g, verify.LIVE_GATES, g)
        for (f, n) in verify.LIVE_GATE_SNAPSHOTS:
            self.assertIn(f, self.srcs, f)
        ok, msg = verify.check_live_gates(self.srcs)
        self.assertTrue(ok, msg)

    def test_json_mode_lists_the_new_step(self):
        import subprocess
        import json as _json
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "verify.py"),
                              "--json"], capture_output=True, text=True,
                             encoding="utf-8", cwd=ROOT)
        data = _json.loads(out.stdout)
        steps = {s["step"]: s["ok"] for s in data["steps"]}
        self.assertIn("live_gates", steps)
        self.assertTrue(steps["live_gates"], data)
        self.assertTrue(data["ok"], data)


if __name__ == "__main__":
    unittest.main()
