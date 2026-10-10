# -*- coding: utf-8 -*-
"""음성 실호출 게이트(SPEECH_LIVE) 회귀 — 의존성 0, 네트워크 미사용.

배경 — 게이트를 **import 시점 상수**로 읽으면 배포 후 환경변수를 바꿔도 이미 뜬
인스턴스에 반영되지 않는다. 켜는 쪽은 불편할 뿐이지만 **끄는 쪽은 비상정지가 듣지
않는다**(과금·실호출을 멈추려고 OFF 로 내려도 그 인스턴스는 계속 라이브 프로바이더를
내준다). 게다가 `health.py` 는 같은 변수를 호출 시점에 따로 읽으므로, 헬스가
"게이트 OFF" 라고 답하는 동안 팩토리는 라이브 프로바이더를 내주는 엇갈림이 생긴다.

검증 대상
  1) 게이트는 호출 시점에 읽힌다 — 켜고 끄는 것이 즉시 반영된다(비상정지).
  2) `"1"` 정확 일치 — true·yes·10·0 은 OFF(실수로 켜지지 않는다).
  3) 팩토리·health 리포트·`health.py` 가 **같은 순간 같은 값**을 본다(드리프트 금지).
  4) 게이트를 켜도 실연동은 열리지 않는다 — 골격은 여전히 PermissionError「[승인 필요]」.
  5) 조회·팩토리는 게이트를 켜지 않고 네트워크를 쓰지 않는다.
  6) (2026-10-10 보강) **등록된 골격 전부**가 거부한다. 지금까지는 clova 한 곳만
     호출해 봤는데 골격은 6개다 — 나머지 다섯 중 하나에 실호출 코드가 들어가도
     회귀가 침묵했다. 실발신·실과금과 같은 등급의 가드라 '있는 줄만 알았지 실제로는
     안 도는' 상태를 남기지 않는다. 기반 클래스의 인터페이스 계약도 함께 고정한다
     (구현을 빼먹으면 조용히 None 을 돌려주는 대신 NotImplementedError 로 드러난다).

실행: python3 -m pytest tests/test_speech_gate.py
"""
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import _speech_providers as sp     # noqa: E402
import health                      # noqa: E402

GATE = "SPEECH_LIVE"
ENV = (GATE, "CALLBOT_STT_PROVIDER", "CALLBOT_TTS_PROVIDER")


class GateBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _boom(self, *a, **k):
        raise AssertionError("게이트 점검은 네트워크를 쓰지 않는다")

    def live(self, value="1"):
        os.environ[GATE] = value

    def want(self, provider):
        os.environ["CALLBOT_STT_PROVIDER"] = provider
        os.environ["CALLBOT_TTS_PROVIDER"] = provider


# --------------------------------------------------------------------------
# 1~2) 호출 시점 판독 · "1" 정확 일치
# --------------------------------------------------------------------------
class TestGateReading(GateBase):
    def test_off_by_default(self):
        self.assertFalse(sp.is_live())

    def test_turning_it_on_takes_effect_without_reimport(self):
        self.live()
        self.assertTrue(sp.is_live())

    def test_kill_switch_takes_effect_immediately(self):
        """켜진 상태에서 내리면 그 즉시 sim 으로 돌아온다 — 비상정지가 들어야 한다."""
        self.live()
        self.want("clova")
        self.assertEqual(sp.get_tts().name, "clova")
        os.environ.pop(GATE, None)                  # 운영자가 게이트를 내린다
        self.assertEqual(sp.get_tts().name, "sim")
        self.assertEqual(sp.get_stt().name, "sim")

    def test_only_exact_one_turns_it_on(self):
        for v in ("0", "", "true", "yes", "on", "10", "1x", "01"):
            self.live(v)
            self.assertFalse(sp.is_live(), v)

    def test_surrounding_whitespace_is_tolerated(self):
        self.live(" 1 ")
        self.assertTrue(sp.is_live())


# --------------------------------------------------------------------------
# 3) 같은 순간 같은 값 — 헬스와 팩토리가 엇갈리지 않는다
# --------------------------------------------------------------------------
class TestNoDrift(GateBase):
    def test_report_matches_the_factory_when_off(self):
        self.want("clova")
        rep = sp.health_report()
        self.assertFalse(rep["speech_live"])
        self.assertTrue(rep["sim"])
        self.assertTrue(rep["stt"]["forced_sim"])
        self.assertEqual(rep["stt"]["effective"], "sim")
        self.assertEqual(sp.get_stt().name, "sim")

    def test_report_matches_the_factory_when_on(self):
        self.live()
        self.want("clova")
        rep = sp.health_report()
        self.assertTrue(rep["speech_live"])
        self.assertFalse(rep["stt"]["forced_sim"])
        self.assertEqual(rep["stt"]["effective"], "clova")
        self.assertEqual(sp.get_stt().name, "clova")

    def test_health_module_and_provider_report_agree(self):
        """`/api/health` 가 '게이트 OFF' 라고 답하며 라이브 프로바이더를 내주면 안 된다."""
        for value in (None, "1", "true"):
            if value is None:
                os.environ.pop(GATE, None)
            else:
                os.environ[GATE] = value
            self.want("clova")
            hinfo = health._speech()
            rep = sp.health_report()
            self.assertEqual(hinfo["speech_live"], rep["speech_live"], value)
            self.assertEqual(bool(hinfo["stt"].get("forced_sim")),
                             bool(rep["stt"]["forced_sim"]), value)
            self.assertEqual(hinfo["stt"].get("active"), sp.get_stt().name, value)

    def test_unknown_provider_falls_back_to_sim_even_when_live(self):
        self.live()
        self.want("acme-tts")
        rep = sp.health_report()
        self.assertFalse(rep["tts"]["known"])
        self.assertTrue(rep["tts"]["forced_sim"])
        self.assertEqual(sp.get_tts().name, "sim")


# --------------------------------------------------------------------------
# 4~5) 게이트를 켜도 실연동은 열리지 않는다 · 부작용 없음
# --------------------------------------------------------------------------
class TestStillPendingApproval(GateBase):
    def test_live_gate_does_not_open_the_real_integration(self):
        self.live()
        self.want("clova")
        for call in (lambda: sp.get_stt().transcribe("QUJD", "audio/webm"),
                     lambda: sp.get_tts().synthesize("안내 문구")):
            with self.assertRaises(PermissionError) as cm:
                call()
            self.assertIn("[승인 필요]", str(cm.exception))

    def test_every_non_sim_provider_is_still_pending_in_the_report(self):
        self.live()
        rep = sp.health_report()
        for kind in ("stt", "tts"):
            for p in rep[kind]["providers"]:
                if p["provider"] == "sim":
                    self.assertEqual(p["status"], "ready")
                    self.assertTrue(p["ok"])
                else:
                    self.assertEqual(p["status"], "pending_approval")
                    self.assertFalse(p["ok"])
                    self.assertIn("[승인 필요]", p["note"])

    def test_sim_provider_never_calls_out(self):
        self.assertEqual(sp.get_stt().transcribe("QUJD")["provider"], "sim")
        audio, meta = sp.get_tts().synthesize("안내 문구")
        self.assertEqual(audio, b"")                 # 오디오 미생성 · 과금 0
        self.assertTrue(meta["sim"])

    def test_reading_does_not_turn_the_gate_on(self):
        sp.health_report()
        sp.get_stt()
        sp.get_tts()
        self.assertIsNone(os.environ.get(GATE))

    def test_report_carries_no_secret(self):
        self.live()
        self.want("clova")
        blob = repr(sp.health_report())
        for needle in ("SECRET", "KEY", "TOKEN", "CREDENTIAL"):
            self.assertNotIn(needle, blob.upper())


# --------------------------------------------------------------------------
# 6) 등록된 골격 전부가 거부한다 · 기반 클래스 인터페이스 계약
# --------------------------------------------------------------------------
class TestEveryPendingSkeletonDenies(GateBase):
    """승인 전 거부를 **등록부의 모든 비-sim 프로바이더**에서 확인한다."""

    def test_every_registered_stt_skeleton_denies(self):
        self.live()                      # 게이트를 켜도 열리지 않아야 한다
        for name, cls in sorted(sp._STT.items()):
            if name == "sim":
                continue
            with self.subTest(provider=name):
                with self.assertRaises(PermissionError) as cm:
                    cls().transcribe("QUJD", "audio/webm")
                self.assertIn("[승인 필요]", str(cm.exception))
                self.assertIn(name, str(cm.exception))   # 어느 골격인지 드러난다

    def test_every_registered_tts_skeleton_denies(self):
        self.live()
        for name, cls in sorted(sp._TTS.items()):
            if name == "sim":
                continue
            with self.subTest(provider=name):
                with self.assertRaises(PermissionError) as cm:
                    cls().synthesize("안내 문구")
                self.assertIn("[승인 필요]", str(cm.exception))
                self.assertIn(name, str(cm.exception))

    def test_every_pending_class_in_the_module_is_registered(self):
        """등록부를 거치지 않는 골격이 조용히 늘어나면 위 두 회귀를 비켜간다."""
        registered = set(sp._STT.values()) | set(sp._TTS.values())
        orphans = []
        for attr in dir(sp):
            obj = getattr(sp, attr)
            if not isinstance(obj, type) or obj is sp._PendingApproval:
                continue
            if issubclass(obj, sp._PendingApproval) and obj not in registered:
                orphans.append(attr)
        self.assertEqual(orphans, [],
                         "팩토리 등록부(_STT·_TTS)에 없는 승인대기 골격 — "
                         "거부 회귀가 비켜간다: %s" % orphans)

    def test_denied_call_leaves_no_partial_result(self):
        """거부는 '빈 결과'가 아니라 예외다 — 빈 값을 돌려주면 호출부가
        '합성했는데 무음'으로 오해하고 통화가 조용히 망가진다."""
        self.live()
        self.want("clova")
        tts = sp.get_tts()
        self.assertRaises(PermissionError, tts.synthesize, "안내 문구")


class TestInterfaceContract(GateBase):
    """기반 클래스는 인터페이스다 — 구현을 빼먹으면 드러나야 한다."""

    def test_base_classes_refuse_instead_of_returning_nothing(self):
        self.assertRaises(NotImplementedError, sp.STTProvider().transcribe, "QUJD")
        self.assertRaises(NotImplementedError, sp.TTSProvider().synthesize, "문구")

    def test_base_health_reports_not_live_and_names_itself(self):
        for cls in (sp.STTProvider, sp.TTSProvider):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls().health(),
                                 {"provider": "base", "live": False, "ok": True})

    def test_sim_inherits_the_health_shape(self):
        """health() 는 라우트가 쓰지 않는 인터페이스 affordance 라, 이 회귀가
        유일한 강제 장치다(모양이 틀어지면 붙이는 쪽에서 알 수 없다)."""
        for p in (sp.SimSTT(), sp.SimTTS()):
            h = p.health()
            self.assertEqual(h["provider"], "sim")
            self.assertFalse(h["live"])
            self.assertTrue(h["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
