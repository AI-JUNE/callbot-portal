# -*- coding: utf-8 -*-
"""아웃바운드 캠페인 계획 — 42차 회귀.

의존성 0 · 네트워크 미사용.

무엇을 고정하는가
  1) `numbers` 검증 — 문자열이 오면 **글자마다 한 통**을 만들던 것(조용한 오동작),
     사전·숫자·개수 상한 없음, 전화번호 꼴이 아닌 값.
  2) `meta`·`scenario` 가 객체·긴 문자열이어도 **400** 이다(500·알림 노이즈 금지).
  3) **하지 않은 일을 보고하지 않는다** — 실회선 결선이 없으므로 게이트가 켜져
     있어도 `queued` 는 0 이고 계획 건수는 `would_call` 이다.
  4) 번호 규칙이 `_sip_adapter._clean_number` 와 **같다**(실발신이 열리면 이
     계획이 그대로 그 함수로 들어간다 — 두 규칙이 어긋나면 계획과 발신이 다르다).
  5) 계획 단계에서 네트워크·발신이 일어나지 않는다.

실행: python -m pytest tests/test_campaign_plan.py -q
"""
import json
import os
import socket
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)

import voice            # noqa: E402
import _sip_adapter     # noqa: E402
import _ratelimit       # noqa: E402


def _no_net(*a, **k):
    raise AssertionError("network call attempted")


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


def post(payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    res = Res()
    inst = voice.handler.__new__(voice.handler)
    inst.headers = FakeHeaders({"sec-fetch-site": "same-origin",
                                "x-forwarded-for": "10.8.8.8",
                                "content-type": "application/json",
                                "content-length": str(len(raw))})
    inst.rfile = FakeRFile(raw)
    inst.wfile = res.wfile
    inst.path = "/api/voice"
    inst.send_response = lambda c, *a: setattr(res, "status", c)
    inst.send_header = lambda k, v: res.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    inst.do_POST()
    return res


class Base(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k)
                     for k in ("CPAAS_LIVE", "CALLBOT_CALLER_ID", "CALLBOT_LOG")}
        for k in self._env:
            os.environ.pop(k, None)
        os.environ["CALLBOT_LOG"] = "off"
        self._urlopen, self._conn = urllib.request.urlopen, socket.create_connection
        urllib.request.urlopen = _no_net
        socket.create_connection = _no_net
        _ratelimit.reset()

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        socket.create_connection = self._conn
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()


# ==========================================================================
# 1) numbers 검증
# ==========================================================================
class TestNumbers(Base):
    def test_a_string_is_not_a_list_of_numbers(self):
        """「01012345678」 한 건이 글자 11건으로 늘어나던 결함."""
        with self.assertRaises(ValueError):
            voice.trigger_campaign("01012345678")
        r = post({"op": "campaign", "numbers": "01012345678"})
        self.assertEqual(r.status, 400)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["details"][0]["field"], "campaign")
        self.assertNotIn("calls", b)

    def test_bytes_and_dict_and_int_are_rejected(self):
        for bad in (b"01012345678", {"01012345678": 1}, 1012345678, 3.14, True):
            with self.assertRaises(ValueError):
                voice.trigger_campaign(bad)

    def test_missing_numbers_is_an_input_error(self):
        with self.assertRaises(ValueError):
            voice.trigger_campaign(None)
        self.assertEqual(post({"op": "campaign"}).status, 400)

    def test_count_cap(self):
        ok = ["01011112222"] * voice.CAMPAIGN_MAX
        self.assertEqual(voice.trigger_campaign(ok)["would_call"], voice.CAMPAIGN_MAX)
        with self.assertRaises(ValueError):
            voice.trigger_campaign(ok + ["01011112222"])

    def test_non_phone_entries_are_rejected_with_the_index(self):
        for bad in ([""], ["abc"], ["123"], [None], [{}], ["0" * 21]):
            with self.assertRaises(ValueError) as cm:
                voice.trigger_campaign(bad)
            self.assertIn("numbers[0]", str(cm.exception))

    def test_separators_are_normalised(self):
        out = voice.trigger_campaign(["010-1111-2222", "010 3333 4444"])
        self.assertEqual([c["to"] for c in out["calls"]],
                         ["01011112222", "01033334444"])

    def test_one_bad_entry_rejects_the_whole_plan(self):
        """절반만 계획을 만들어 돌려주면 받는 쪽이 전부라고 읽는다."""
        with self.assertRaises(ValueError):
            voice.trigger_campaign(["01011112222", "nope"])
        r = post({"op": "campaign", "numbers": ["01011112222", "nope"]})
        self.assertEqual(r.status, 400)

    def test_digit_rule_matches_the_dialer(self):
        """번호 규칙은 `_sip_adapter._clean_number` 와 같다(계획 ≠ 발신 금지)."""
        cases = ["01011112222", "010-1111-2222", "0212345678", "07012345678",
                 "1234", "0" * 20, "", "abc", "123", "0" * 21]
        for raw in cases:
            try:
                mine = voice.campaign_targets([raw])[0]
                ok_mine = True
            except ValueError:
                mine, ok_mine = None, False
            try:
                theirs = _sip_adapter._clean_number(raw)
                ok_theirs = True
            except ValueError:
                theirs, ok_theirs = None, False
            self.assertEqual((ok_mine, mine), (ok_theirs, theirs), raw)


# ==========================================================================
# 2) meta·scenario — 입력 오류는 400 이다(500 이 아니다)
# ==========================================================================
class TestMetaAndScenario(Base):
    def test_non_object_meta_is_400_not_500(self):
        """`{**meta}` 가 TypeError 로 터져 사용자 오타가 서비스 장애로 보고됐다."""
        for bad in (["a"], "a", 1):
            with self.assertRaises(ValueError):
                voice.trigger_campaign(["01011112222"], meta=bad)
        r = post({"op": "campaign", "numbers": ["01011112222"], "meta": ["a"]})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "campaign")

    def test_meta_is_copied_not_shared(self):
        meta = {"tenant": "acme"}
        out = voice.trigger_campaign(["01011112222"], meta=meta)
        out["calls"][0]["metadata"]["tenant"] = "변조"
        self.assertEqual(meta, {"tenant": "acme"})

    def test_scenario_must_be_a_short_string(self):
        for bad in (None, 1, {}, "", "   ", "x" * (voice.CAMPAIGN_SCENARIO_MAX + 1)):
            with self.assertRaises(ValueError):
                voice.trigger_campaign(["01011112222"], scenario=bad)
        self.assertEqual(post({"op": "campaign", "numbers": ["01011112222"],
                               "scenario": {"a": 1}}).status, 400)

    def test_scenario_lands_in_metadata(self):
        out = voice.trigger_campaign(["01011112222"], scenario="care")
        self.assertEqual(out["calls"][0]["metadata"]["scenario"], "care")


# ==========================================================================
# 3) 하지 않은 일을 보고하지 않는다
# ==========================================================================
class TestHonestReport(Base):
    def test_dry_run_says_zero_queued(self):
        out = voice.trigger_campaign(["01011112222", "01033334444"])
        self.assertEqual((out["queued"], out["would_call"]), (0, 2))
        self.assertIn("dry-run", out["mode"])

    def test_live_gate_does_not_invent_a_queue(self):
        """게이트가 켜져도 발신 결선이 없다 — `queued` 는 0 이고 사유를 적는다."""
        os.environ["CPAAS_LIVE"] = "1"
        out = voice.trigger_campaign(["01011112222", "01033334444"])
        self.assertEqual(out["mode"], "live")
        self.assertEqual((out["queued"], out["would_call"]), (0, 2))
        self.assertIn("[승인 필요]", out["note"])

    def test_console_fields_survive(self):
        """콘솔은 `would_call ?? queued` 와 `mode`·`calls` 를 읽는다."""
        out = voice.trigger_campaign(["01011112222"])
        for k in ("mode", "queued", "would_call", "calls"):
            self.assertIn(k, out)
        self.assertEqual(out["calls"][0]["webhook"], "/api/voice")
        self.assertTrue(out["calls"][0]["consent_required"])

    def test_http_plan_is_dry_run_by_default(self):
        r = post({"op": "campaign", "numbers": ["01011112222"], "scenario": "care"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["queued"], 0)


# ==========================================================================
# 4) 계획은 발신하지 않는다
# ==========================================================================
class TestNoDialing(Base):
    def test_no_network_in_either_branch(self):
        voice.trigger_campaign(["01011112222"])
        os.environ["CPAAS_LIVE"] = "1"
        voice.trigger_campaign(["01011112222"])
        self.assertIs(urllib.request.urlopen, _no_net)
        self.assertIs(socket.create_connection, _no_net)

    def test_plan_does_not_touch_the_sim_dialer_log(self):
        before = len(_sip_adapter.recent_calls()) if hasattr(_sip_adapter, "recent_calls") else None
        voice.trigger_campaign(["01011112222"])
        if before is not None:
            self.assertEqual(len(_sip_adapter.recent_calls()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
