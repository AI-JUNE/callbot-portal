# -*- coding: utf-8 -*-
"""api/sip_adapter.py 회선 어댑터 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen·socket.create_connection 감시로 강제).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — sip_adapter 0% → 보강)
  1) 이벤트 모델 — ring→answered→transcript*→hangup 순서·필수 키·미지 타입 거부
  2) sim 인바운드 — call_id 형식, 발화 주입, 빈 발화, 잘못된 발화 타입 거부, 종료 후 정리
  3) dry-run 발신 — 실발신 없음(billed 0)·기록 보관 상한·입력검증(빈값/타입/자릿수)
  4) 실발신 게이트 — CPAAS_LIVE=1 이어도 sim 은 PermissionError, 실회선 골격은 항상 거부
  5) 팩토리 — 게이트 OFF 면 무조건 sim, ON 이면 요청 어댑터, 미지명은 sim 폴백, 대소문자·공백
  6) 리스너 격리 — 리스너 예외가 통화를 끊지 않고(누수 없음) 건수로 드러남, 이벤트 사본 전달
  7) 개인정보 — 이벤트·dry-run 기록에 원문 번호 없음(마스킹 규칙 voice._mask_phone 과 동일)
  8) health — 비밀값·원문 번호 없음, 게이트 상태·활성 통화 수, 실회선 골격은 ok=False

실행: python3 -m pytest tests/test_sip_adapter.py -q
"""
import os
import sys
import socket
import unittest
import importlib
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import sip_adapter  # noqa: E402
import voice        # noqa: E402


class NetworkTouched(AssertionError):
    pass


def _no_net(*a, **k):
    raise NetworkTouched("network call attempted")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("CPAAS_LIVE", None)
        os.environ.pop("CALLBOT_SIP_ADAPTER", None)
        self._urlopen = urllib.request.urlopen
        self._conn = socket.create_connection
        urllib.request.urlopen = _no_net
        socket.create_connection = _no_net

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        socket.create_connection = self._conn
        os.environ.clear()
        os.environ.update(self._env)


# --------------------------------------------------------------------------
# 1) 이벤트 모델
# --------------------------------------------------------------------------
class TestEventModel(Base):
    def test_event_has_required_keys(self):
        ev = sip_adapter._event("c1", "ring", "inbound", caller="010****0000")
        for k in ("call_id", "type", "direction", "ts", "sim"):
            self.assertIn(k, ev)
        self.assertTrue(ev["sim"])
        self.assertEqual(ev["caller"], "010****0000")

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            sip_adapter._event("c1", "explode", "inbound")

    def test_unknown_direction_rejected(self):
        with self.assertRaises(ValueError):
            sip_adapter._event("c1", "ring", "sideways")

    def test_event_types_frozen(self):
        self.assertEqual(sip_adapter.EVENT_TYPES,
                         ("ring", "answered", "transcript", "dtmf", "hangup"))


# --------------------------------------------------------------------------
# 2) sim 인바운드
# --------------------------------------------------------------------------
class TestSimInbound(Base):
    def setUp(self):
        super().setUp()
        self.a = sip_adapter.SimSIPAdapter()
        self.got = []
        self.a.on_event(self.got.append)

    def test_sequence(self):
        self.a.simulate_inbound("01012345678", ["여보세요", "환불이요"])
        self.assertEqual([e["type"] for e in self.got],
                         ["ring", "answered", "transcript", "transcript", "hangup"])
        self.assertEqual([e["text"] for e in self.got if e["type"] == "transcript"],
                         ["여보세요", "환불이요"])

    def test_default_utterances(self):
        self.a.simulate_inbound("01012345678")
        self.assertEqual(sum(1 for e in self.got if e["type"] == "transcript"), 2)

    def test_empty_utterances(self):
        self.a.simulate_inbound("01012345678", [])
        self.assertEqual([e["type"] for e in self.got], ["ring", "answered", "hangup"])

    def test_call_id_format_and_unique(self):
        c1 = self.a.simulate_inbound("01012345678")
        c2 = self.a.simulate_inbound("01012345678")
        self.assertTrue(c1.startswith("sim-") and len(c1) == 16)
        self.assertNotEqual(c1, c2)
        self.assertTrue(all(e["call_id"] in (c1, c2) for e in self.got))

    def test_all_events_inbound_and_sim(self):
        self.a.simulate_inbound("01012345678")
        self.assertTrue(all(e["direction"] == "inbound" and e["sim"] is True for e in self.got))

    def test_utterances_string_rejected(self):
        with self.assertRaises(ValueError):
            self.a.simulate_inbound("01012345678", "여보세요")
        self.assertEqual(self.got, [])          # 검증 실패 전에 이벤트가 새지 않는다

    def test_utterances_non_string_item_rejected(self):
        with self.assertRaises(ValueError):
            self.a.simulate_inbound("01012345678", ["안녕", 3])

    def test_active_cleared_after_call(self):
        self.a.simulate_inbound("01012345678")
        self.assertEqual(self.a._active, {})
        self.assertEqual(self.a.health()["active_calls"], 0)

    def test_hangup_unknown_false(self):
        self.assertFalse(self.a.hangup("sim-nope"))
        self.assertEqual(self.got, [])

    def test_hangup_twice(self):
        cid = self.a.simulate_inbound("01012345678")
        self.assertFalse(self.a.hangup(cid))
        self.assertEqual(sum(1 for e in self.got if e["type"] == "hangup"), 1)


# --------------------------------------------------------------------------
# 3) dry-run 발신 · 4) 실발신 게이트
# --------------------------------------------------------------------------
class TestDial(Base):
    def setUp(self):
        super().setUp()
        self.a = sip_adapter.SimSIPAdapter()

    def test_dry_run_record(self):
        rec = self.a.dial("010-1111-2222", {"campaign": "care"})
        self.assertTrue(rec["dry_run"])
        self.assertEqual(rec["billed"], 0)
        self.assertEqual(rec["meta"], {"campaign": "care"})
        self.assertEqual(len(self.a.dry_run_log), 1)

    def test_returned_record_is_copy(self):
        rec = self.a.dial("01011112222")
        rec["billed"] = 999
        self.assertEqual(self.a.dry_run_log[0]["billed"], 0)

    def test_meta_default_and_copy(self):
        meta = {"k": "v"}
        rec = self.a.dial("01011112222", meta)
        meta["k"] = "changed"
        self.assertEqual(rec["meta"], {"k": "v"})
        self.assertEqual(self.a.dial("01011112222")["meta"], {})

    def test_meta_type_rejected(self):
        with self.assertRaises(ValueError):
            self.a.dial("01011112222", meta="care")
        self.assertEqual(self.a.dry_run_log, [])

    def test_number_validation(self):
        for bad in ("", "abc", "12", None, 1011112222, ["01011112222"], "1" * 21):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.a.dial(bad)
        self.assertEqual(self.a.dry_run_log, [])   # 잘못된 시도는 기록되지 않는다

    def test_log_capped(self):
        for _ in range(sip_adapter.SimSIPAdapter.MAX_DRY_RUN_LOG + 25):
            self.a.dial("01011112222")
        self.assertEqual(len(self.a.dry_run_log), sip_adapter.SimSIPAdapter.MAX_DRY_RUN_LOG)

    def test_sim_refuses_when_gate_on(self):
        os.environ["CPAAS_LIVE"] = "1"
        with self.assertRaises(PermissionError) as cm:
            self.a.dial("01011112222")
        self.assertIn("[승인 필요]", str(cm.exception))
        self.assertEqual(self.a.dry_run_log, [])

    def test_gate_read_at_call_time(self):
        self.a.dial("01011112222")
        os.environ["CPAAS_LIVE"] = "1"
        with self.assertRaises(PermissionError):
            self.a.dial("01011112222")
        os.environ["CPAAS_LIVE"] = "0"
        self.a.dial("01011112222")
        self.assertEqual(len(self.a.dry_run_log), 2)

    def test_live_skeletons_always_deny(self):
        for cls in (sip_adapter.TwilioAdapter, sip_adapter.KtAdapter, sip_adapter.LgAdapter):
            for gate in ("0", "1"):
                os.environ["CPAAS_LIVE"] = gate
                ad = cls()
                with self.assertRaises(PermissionError) as cm:
                    ad.dial("01011112222")
                self.assertIn("[승인 필요]", str(cm.exception))
                self.assertIn(cls.name, str(cm.exception))
                with self.assertRaises(PermissionError):
                    ad.hangup("x")

    def test_base_interface_abstract(self):
        b = sip_adapter.SIPAdapter()
        with self.assertRaises(NotImplementedError):
            b.dial("01011112222")
        with self.assertRaises(NotImplementedError):
            b.hangup("x")

    def test_no_network_during_dial_and_call(self):
        self.a.dial("01011112222")
        self.a.simulate_inbound("01011112222")


# --------------------------------------------------------------------------
# 5) 팩토리
# --------------------------------------------------------------------------
class TestFactory(Base):
    def test_default_sim(self):
        self.assertIsInstance(sip_adapter.get_adapter(), sip_adapter.SimSIPAdapter)

    def test_gate_off_forces_sim(self):
        for want in ("twilio", "kt", "lg", "TWILIO", " kt "):
            os.environ["CALLBOT_SIP_ADAPTER"] = want
            self.assertEqual(sip_adapter.get_adapter().name, "sim", want)

    def test_gate_on_returns_requested(self):
        os.environ["CPAAS_LIVE"] = "1"
        for want, cls in (("twilio", sip_adapter.TwilioAdapter), ("KT ", sip_adapter.KtAdapter),
                          ("lg", sip_adapter.LgAdapter), ("sim", sip_adapter.SimSIPAdapter)):
            os.environ["CALLBOT_SIP_ADAPTER"] = want
            self.assertIsInstance(sip_adapter.get_adapter(), cls, want)

    def test_gate_on_unknown_falls_back_to_sim(self):
        os.environ["CPAAS_LIVE"] = "1"
        os.environ["CALLBOT_SIP_ADAPTER"] = "asterisk"
        self.assertEqual(sip_adapter.get_adapter().name, "sim")

    def test_gate_only_exact_one(self):
        for v in ("true", "yes", "on", "01", ""):
            os.environ["CPAAS_LIVE"] = v
            self.assertFalse(sip_adapter.is_live(), v)
        os.environ["CPAAS_LIVE"] = "1"
        self.assertTrue(sip_adapter.is_live())

    def test_factory_returns_fresh_instances(self):
        a, b = sip_adapter.get_adapter(), sip_adapter.get_adapter()
        self.assertIsNot(a, b)
        a.dial("01011112222")
        self.assertEqual(b.dry_run_log, [])

    def test_module_reload_default_gate_off(self):
        importlib.reload(sip_adapter)
        self.assertFalse(sip_adapter.CPAAS_LIVE)


# --------------------------------------------------------------------------
# 6) 리스너 격리
# --------------------------------------------------------------------------
class TestListeners(Base):
    def setUp(self):
        super().setUp()
        self.a = sip_adapter.SimSIPAdapter()

    def test_bad_listener_does_not_break_call(self):
        got = []

        def boom(ev):
            raise RuntimeError("listener crashed")
        self.a.on_event(boom)
        self.a.on_event(got.append)
        cid = self.a.simulate_inbound("01011112222", ["안녕"])
        self.assertEqual([e["type"] for e in got], ["ring", "answered", "transcript", "hangup"])
        self.assertEqual(self.a._active, {}, "리스너 예외로 통화가 누수되면 안 된다")
        self.assertEqual(self.a.listener_errors, 4)
        self.assertEqual(self.a.health()["listener_errors"], 4)
        self.assertFalse(self.a.hangup(cid))

    def test_listener_gets_copy(self):
        seen = []

        def tamper(ev):
            ev["type"] = "hangup"
            ev["injected"] = True
        self.a.on_event(tamper)
        self.a.on_event(seen.append)
        self.a.simulate_inbound("01011112222", [])
        self.assertEqual([e["type"] for e in seen], ["ring", "answered", "hangup"])
        self.assertTrue(all("injected" not in e for e in seen))

    def test_non_callable_rejected(self):
        with self.assertRaises(TypeError):
            self.a.on_event("not a function")

    def test_no_listeners_ok(self):
        cid = self.a.simulate_inbound("01011112222")
        self.assertTrue(cid.startswith("sim-"))


# --------------------------------------------------------------------------
# 7) 개인정보 · 8) health
# --------------------------------------------------------------------------
class TestPrivacyHealth(Base):
    RAW = "01012345678"

    def test_mask_rule_matches_voice(self):
        for v in ("01012345678", "010-1234-5678", "+82 10 1234 5678", "1234", "", None, "sip:a@b"):
            self.assertEqual(sip_adapter.mask_phone(v), voice._mask_phone(v), repr(v))
        self.assertEqual(sip_adapter.mask_phone(self.RAW), "010****5678")

    def test_events_have_no_raw_number(self):
        a = sip_adapter.SimSIPAdapter()
        got = []
        a.on_event(got.append)
        a.simulate_inbound(self.RAW, ["내 번호는 " + self.RAW])   # 발화 원문은 STT 대역이라 그대로
        self.assertEqual(got[0]["caller"], "010****5678")
        self.assertNotIn(self.RAW, repr({k: v for e in got for k, v in e.items() if k != "text"}))

    def test_dry_run_has_no_raw_number(self):
        a = sip_adapter.SimSIPAdapter()
        rec = a.dial("010-1234-5678", {"campaign": "care"})
        self.assertEqual(rec["would_dial"], "010****5678")
        self.assertNotIn(self.RAW, repr(a.dry_run_log))
        self.assertNotIn("1234-5678", repr(a.dry_run_log))

    def test_health_shape(self):
        a = sip_adapter.SimSIPAdapter()
        a.dial("01012345678")
        h = a.health()
        self.assertEqual(h["adapter"], "sim")
        self.assertFalse(h["live"])
        self.assertTrue(h["ok"])
        self.assertEqual(h["gate"], "off")
        self.assertEqual(h["active_calls"], 0)
        self.assertEqual(h["dry_runs"], 1)
        self.assertNotIn("01012345678", repr(h))

    def test_health_gate_on_still_not_live(self):
        os.environ["CPAAS_LIVE"] = "1"
        h = sip_adapter.SimSIPAdapter().health()
        self.assertEqual(h["gate"], "on")
        self.assertFalse(h["live"])

    def test_live_skeleton_health_not_ok(self):
        for cls in (sip_adapter.TwilioAdapter, sip_adapter.KtAdapter, sip_adapter.LgAdapter):
            h = cls().health()
            self.assertFalse(h["ok"])
            self.assertFalse(h["live"])
            self.assertIn("[승인 필요]", h["detail"])
            for secret in ("TOKEN", "SID", "sk_", "Bearer"):
                self.assertNotIn(secret, repr(h))


if __name__ == "__main__":
    unittest.main()
