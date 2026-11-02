# -*- coding: utf-8 -*-
"""대본(시나리오) 이름 등록부 — 44차 회귀.

의존성 0 · 네트워크 미사용.

무엇을 고정하는가
  1) 이름의 출처는 `_engine.SCENARIOS` **한 곳**이다. `chat`·`sim_call` 의
     목록이 그 안에 있고, 등록부에 잔재가 없다.
  2) 전용 프롬프트 대본(안부·복지·조사·연체)은 **주문·환불 툴을 열지 않는다**.
     모르는 이름이 조용히 기본 CS 프롬프트가 되면 그때 툴이 열린다.
  3) `voice` 는 외부(CPaaS 이벤트·캠페인 `metadata`)가 보낸 이름을 **등록된
     이름으로 되돌린 뒤에** 세션·지표·엔진으로 넘긴다. 되돌렸다는 사실도 남긴다.
  4) 캠페인 계획은 모르는 이름을 **400** 으로 거부한다(계획은 실발신 승인 뒤
     그대로 발신 요청이 된다 — `sim_call` 의 「조용히 다른 대본으로 바꾸지
     않는다」와 같은 규약).
  5) `meta` 안의 `scenario` 키가 **검증한 이름을 덮지 못한다**.
  6) 과차단 금지 — 등록된 이름·앞뒤 공백은 그대로 쓴다.
  7) 릴리스 게이트 22단계가 **고치기 전 모양을 실제로 잡는다**.

실행: python -m pytest tests/test_scenario_registry.py -q
"""
import json
import os
import socket
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
for p in (API, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _engine as engine      # noqa: E402
import chat                   # noqa: E402
import sim_call               # noqa: E402
import voice                  # noqa: E402
import _ratelimit             # noqa: E402
import verify                 # noqa: E402

PHONE = "01000000000"         # 더미 발신번호(실번호 아님)


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
                                "x-forwarded-for": "10.9.9.9",
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


def get(path="/api/voice"):
    res = Res()
    inst = voice.handler.__new__(voice.handler)
    inst.headers = FakeHeaders({"sec-fetch-site": "same-origin",
                                "x-forwarded-for": "10.9.9.9"})
    inst.wfile = res.wfile
    inst.path = path
    inst.send_response = lambda c, *a: setattr(res, "status", c)
    inst.send_header = lambda k, v: res.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    inst.do_GET()
    return res


class Base(unittest.TestCase):
    ENV = ("CALLBOT_DEFAULT_SCENARIO", "CPAAS_LIVE", "CPAAS_PROVIDER",
           "CALLBOT_LOG")

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
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
# 1) 출처는 한 곳이다
# ==========================================================================
class TestOneSource(Base):
    def test_engine_registry_has_the_default_and_the_prompt_names(self):
        self.assertIn(engine.DEFAULT_SCENARIO, engine.SCENARIOS)
        for n in engine.SCENARIO_PROMPTS:
            self.assertIn(n, engine.SCENARIOS, n)
        self.assertEqual(len(engine.SCENARIOS), len(set(engine.SCENARIOS)),
                         "등록부에 같은 이름이 두 번 있다")

    def test_chat_whitelist_is_derived_not_copied(self):
        """`/api/chat` 의 공개 목록은 등록부에서 나온다(베껴 적지 않는다)."""
        self.assertEqual(tuple(chat.SCENARIOS),
                         (engine.DEFAULT_SCENARIO,) + tuple(engine.SCENARIO_PROMPTS))
        for n in chat.SCENARIOS:
            self.assertIn(n, engine.SCENARIOS, n)

    def test_simulator_scripts_are_registered(self):
        for n in sim_call.SCRIPTS:
            self.assertIn(n, engine.SCENARIOS,
                          "대본 %r 가 등록부에 없다 — sim 이 받아 놓고 엔진이 "
                          "다른 프롬프트를 고른다" % n)

    def test_registry_has_no_leftovers(self):
        used = ({engine.DEFAULT_SCENARIO} | set(engine.SCENARIO_PROMPTS)
                | set(chat.SCENARIOS) | set(sim_call.SCRIPTS))
        self.assertEqual(set(engine.SCENARIOS) - used, set())


# ==========================================================================
# 2) 전용 프롬프트 대본은 툴을 열지 않는다
# ==========================================================================
class TestToolGate(Base):
    def test_dedicated_prompts_close_the_tools(self):
        for n, want in sorted(engine.SCENARIO_PROMPTS.items()):
            sysp, use_tools = engine.scenario_prompt(n, PHONE)
            self.assertIs(sysp, want, n)
            self.assertFalse(use_tools,
                             "「%s」 통화에 주문·환불 툴이 열렸다" % n)

    def test_alias_picks_the_same_prompt(self):
        self.assertIs(engine.scenario_prompt("안부", PHONE)[0],
                      engine.scenario_prompt("wellbeing", PHONE)[0])

    def test_cs_scenarios_keep_the_tools(self):
        for n in engine.TOOL_SCENARIOS:
            sysp, use_tools = engine.scenario_prompt(n, PHONE)
            self.assertTrue(use_tools, n)
            self.assertIn(PHONE, sysp, "CS 프롬프트는 발신번호를 담는다")

    def test_unknown_name_keeps_the_engine_contract(self):
        """엔진은 모르는 이름을 거부하지 않는다 — 판정은 경계의 몫이다.

        엔진이 통화 중에 예외를 내면 전화가 끊긴다. 그래서 계약은 그대로
        두고(기본 CS 경로) 화이트리스트를 라우트에 뒀다.
        """
        for bad in ("없는대본", "", None, 1, {}, ["refund"]):
            sysp, use_tools = engine.scenario_prompt(bad, PHONE)
            self.assertTrue(use_tools)
            self.assertIn(PHONE, sysp)

    def test_normalize_scenario(self):
        self.assertEqual(engine.normalize_scenario(" wellbeing "), "wellbeing")
        self.assertEqual(engine.normalize_scenario("안부"), "안부")
        for bad in ("Refund", "웰비잉", "refund_over_9000", "", "   ",
                    None, 1, {}, ["refund"]):
            self.assertIsNone(engine.normalize_scenario(bad), bad)
        self.assertEqual(engine.normalize_scenario("nope", default="refund"),
                         "refund")


# ==========================================================================
# 3) voice — 외부가 보낸 이름을 되돌린다 (44차 결함)
# ==========================================================================
class TestVoiceNormalises(Base):
    def test_unknown_event_scenario_falls_back_and_says_so(self):
        name, unknown = voice.event_scenario("웰비잉")
        self.assertEqual(name, engine.DEFAULT_SCENARIO)
        self.assertTrue(unknown)

    def test_missing_scenario_is_not_an_unknown_one(self):
        for blank in (None, "", "   "):
            name, unknown = voice.event_scenario(blank)
            self.assertEqual(name, engine.DEFAULT_SCENARIO)
            self.assertFalse(unknown, "보내지 않은 것은 오타가 아니다")

    def test_known_scenario_survives(self):
        for good in ("wellbeing", " 안부 ", "integrity", "care"):
            name, unknown = voice.event_scenario(good)
            self.assertEqual(name, good.strip())
            self.assertFalse(unknown)

    def test_non_string_scenario_does_not_crash(self):
        for bad in (1, {}, ["refund"], 3.5, True):
            name, unknown = voice.event_scenario(bad)
            self.assertIn(name, engine.SCENARIOS)
            self.assertTrue(unknown)

    def test_parsed_event_carries_a_registered_name(self):
        """결함: CPaaS 가 보낸 이름이 그대로 세션·엔진으로 갔다."""
        ev = voice._parse_event({"type": "answered", "scenario": "웰비잉"})
        self.assertIn(ev["scenario"], engine.SCENARIOS)
        self.assertTrue(ev["scenario_unknown"])

    def test_parsed_event_clawops_metadata(self):
        os.environ["CPAAS_PROVIDER"] = "clawops"
        try:
            prev, voice.CPAAS = voice.CPAAS, "clawops"
            ev = voice._parse_event({"event": "call.answered",
                                     "metadata": {"scenario": "환불해줘"}})
            self.assertIn(ev["scenario"], engine.SCENARIOS)
            self.assertTrue(ev["scenario_unknown"])
            ev = voice._parse_event({"event": "call.answered",
                                     "metadata": {"scenario": "wellbeing"}})
            self.assertEqual((ev["scenario"], ev["scenario_unknown"]),
                             ("wellbeing", False))
        finally:
            voice.CPAAS = prev

    def test_env_default_must_be_registered(self):
        os.environ["CALLBOT_DEFAULT_SCENARIO"] = "wellbeing"
        self.assertEqual(voice.default_scenario(), "wellbeing")
        os.environ["CALLBOT_DEFAULT_SCENARIO"] = "오타난대본"
        self.assertEqual(voice.default_scenario(), engine.DEFAULT_SCENARIO)
        os.environ.pop("CALLBOT_DEFAULT_SCENARIO")
        self.assertEqual(voice.default_scenario(), engine.DEFAULT_SCENARIO)

    def test_get_reports_the_effective_default(self):
        os.environ["CALLBOT_DEFAULT_SCENARIO"] = "오타난대본"
        r = get()
        self.assertEqual(r.status, 200)
        st = r.body()["scenario"]
        self.assertEqual(st["name"], engine.DEFAULT_SCENARIO)
        self.assertTrue(st["env_override"] and st["env_unknown"],
                        "되돌려 쓰고 있다는 사실이 드러나야 한다")
        self.assertEqual(st["known"], list(engine.SCENARIOS))

    def test_get_does_not_claim_an_override_that_is_not_there(self):
        st = get().body()["scenario"]
        self.assertFalse(st["env_override"] or st["env_unknown"])

    def test_without_the_engine_only_the_default_name_is_known(self):
        """등록부를 못 읽어도 임의 문자열을 통과시키지 않는다(fail-closed).

        engine 이 없으면 통화는 「점검 중」으로 끝나지만, 여기서 바깥 값을
        흘려 보내면 그 값이 세션·지표·전환 티켓 라벨에 남는다.
        """
        prev_norm, prev_names = voice.normalize_scenario, voice.SCENARIOS
        voice.normalize_scenario, voice.SCENARIOS = None, ("refund",)
        try:
            self.assertEqual(voice._known_scenario(" refund "), "refund")
            self.assertIsNone(voice._known_scenario("wellbeing"))
            self.assertIsNone(voice._known_scenario(7))
            self.assertEqual(voice.event_scenario("웰비잉"), ("refund", True))
        finally:
            voice.normalize_scenario, voice.SCENARIOS = prev_norm, prev_names

    def test_the_log_line_says_it_fell_back(self):
        """조용히 바꾸면 보내는 쪽의 오타가 영원히 고쳐지지 않는다."""
        import contextlib
        import io as _io
        os.environ["CALLBOT_LOG"] = "info"
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = post({"type": "answered", "call_id": "scn-log",
                      "scenario": "웰비잉", "from": "01011112222"})
        self.assertEqual(r.status, 200)
        voice._Session.drop("scn-log")
        rows = [json.loads(l) for l in buf.getvalue().strip().splitlines()
                if l.strip().startswith("{")]
        req = [r for r in rows if r.get("kind") != "audit"
               and r.get("route") == "/api/voice"]
        self.assertTrue(req, buf.getvalue())
        extra = req[-1].get("extra", {})
        self.assertTrue(extra.get("scenario_fallback"), extra)
        self.assertEqual(extra.get("scenario"), engine.DEFAULT_SCENARIO)
        self.assertNotIn("웰비잉", buf.getvalue(),
                         "모르는 이름 자체를 로그에 싣지 않는다(카디널리티·PII)")

    def test_handle_event_uses_the_registered_name(self):
        """세션에 들어가는 이름도 등록부를 거친 값이다."""
        ev = voice._parse_event({"type": "answered", "call_id": "scn-1",
                                 "scenario": "없는대본", "from": "01011112222"})
        voice.handle_event(ev)
        sess = voice._Session.get("scn-1")
        self.assertIn(sess["scenario"], engine.SCENARIOS)
        voice._Session.drop("scn-1")


# ==========================================================================
# 4) 캠페인 계획은 모르는 대본을 거부한다
# ==========================================================================
class TestCampaignRejects(Base):
    def test_unknown_scenario_is_400(self):
        for bad in ("웰비잉", "Refund", "wellbeing;drop", "refund_over_9000"):
            with self.assertRaises(ValueError, msg=bad):
                voice.trigger_campaign(["01011112222"], scenario=bad)
        r = post({"op": "campaign", "numbers": ["01011112222"],
                  "scenario": "웰비잉"})
        self.assertEqual(r.status, 400)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["details"][0]["field"], "campaign")
        self.assertNotIn("calls", b)

    def test_registered_scenarios_still_plan(self):
        for good in sorted(engine.SCENARIOS):
            out = voice.trigger_campaign(["01011112222"], scenario=good)
            self.assertEqual(out["calls"][0]["metadata"]["scenario"], good, good)
            self.assertEqual(out["queued"], 0)

    def test_surrounding_space_is_trimmed_not_rejected(self):
        out = voice.trigger_campaign(["01011112222"], scenario=" care ")
        self.assertEqual(out["calls"][0]["metadata"]["scenario"], "care")

    def test_meta_cannot_override_the_validated_name(self):
        """`{"scenario": name, **meta}` 였을 때 meta 가 검증을 비켜 갔다."""
        out = voice.trigger_campaign(["01011112222"], scenario="care",
                                     meta={"scenario": "아무거나", "tenant": "acme"})
        md = out["calls"][0]["metadata"]
        self.assertEqual(md["scenario"], "care")
        self.assertEqual(md["tenant"], "acme")

    def test_shape_errors_keep_their_own_message(self):
        for bad in (None, 1, {}, "", "   "):
            with self.assertRaises(ValueError):
                voice.trigger_campaign(["01011112222"], scenario=bad)
        with self.assertRaises(ValueError) as cm:
            voice.trigger_campaign(["01011112222"],
                                   scenario="x" * (voice.CAMPAIGN_SCENARIO_MAX + 1))
        self.assertIn("자 이하", str(cm.exception))

    def test_plan_does_not_dial(self):
        voice.trigger_campaign(["01011112222"], scenario="care")
        self.assertIs(urllib.request.urlopen, _no_net)
        self.assertIs(socket.create_connection, _no_net)


# ==========================================================================
# 5) 릴리스 게이트 22단계 — 고치기 전 모양을 실제로 잡는가
# ==========================================================================
class _Engine(object):
    """등록부만 가진 가짜 엔진."""

    def __init__(self, names, prompts=None, default="refund"):
        self.SCENARIOS = tuple(names)
        self.SCENARIO_PROMPTS = dict(prompts or {})
        self.DEFAULT_SCENARIO = default

    def normalize_scenario(self, value, default=None):
        return value if value in self.SCENARIOS else default

    def scenario_prompt(self, scenario, phone):
        p = self.SCENARIO_PROMPTS.get(scenario)
        return (p, False) if p else ("CS " + phone, True)


class _Route(object):
    """44차 **이전** 모양 — 받은 이름을 그대로 쓰고 계획도 그대로 만든다."""

    @staticmethod
    def event_scenario(value):
        v = value if isinstance(value, str) and value.strip() else "refund"
        return v, False

    @staticmethod
    def trigger_campaign(numbers, scenario="care", meta=None):
        return {"calls": [{"metadata": {"scenario": scenario}}]}


class TestGate(Base):
    def test_real_repository_passes(self):
        ok, msg = verify.check_scenario_names()
        self.assertTrue(ok, msg)

    def test_pre_fix_route_is_caught(self):
        ok, msg = verify.check_scenario_names(mods={"voice.py": _Route})
        self.assertFalse(ok)
        self.assertIn("세션·지표로", msg)

    def test_silent_fallback_is_caught(self):
        class Quiet(object):
            @staticmethod
            def event_scenario(value):
                return "refund", False          # 되돌렸다는 말을 하지 않는다

            @staticmethod
            def trigger_campaign(numbers, scenario="care", meta=None):
                raise ValueError("unknown")

        ok, msg = verify.check_scenario_names(mods={"voice.py": Quiet})
        self.assertFalse(ok)
        self.assertIn("되돌렸다는 사실", msg)

    def test_campaign_that_accepts_anything_is_caught(self):
        class Loose(object):
            @staticmethod
            def event_scenario(value):
                return voice.event_scenario(value)

            @staticmethod
            def trigger_campaign(numbers, scenario="care", meta=None):
                return {"calls": []}

        ok, msg = verify.check_scenario_names(mods={"voice.py": Loose})
        self.assertFalse(ok)
        self.assertIn("발신 요청이 된다", msg)

    def test_overblocking_is_caught(self):
        class Strict(object):
            @staticmethod
            def event_scenario(value):
                return "refund", True           # 정상 이름까지 되돌렸다고 적는다

            @staticmethod
            def trigger_campaign(numbers, scenario="care", meta=None):
                raise ValueError("no")

        ok, msg = verify.check_scenario_names(mods={"voice.py": Strict})
        self.assertFalse(ok)
        self.assertIn("과차단", msg)

    def test_drift_between_route_and_engine_is_caught(self):
        """`chat` 목록에 있고 등록부에 없는 이름 = 드리프트."""
        eng = _Engine(("refund",), {"refund": "P"})
        ok, msg = verify.check_scenario_names(engine=eng)
        self.assertFalse(ok)
        self.assertIn("_engine.SCENARIOS 에 없다", msg)

    def test_registry_leftover_is_caught(self):
        eng = _Engine(tuple(engine.SCENARIOS) + ("ghost",),
                      dict(engine.SCENARIO_PROMPTS))
        ok, msg = verify.check_scenario_names(engine=eng)
        self.assertFalse(ok)
        self.assertIn("등록부 잔재", msg)

    def test_open_tools_on_a_dedicated_prompt_is_caught(self):
        class Open(_Engine):
            def scenario_prompt(self, scenario, phone):
                return "P", True                # 전용 프롬프트인데 툴을 연다

        eng = Open(tuple(engine.SCENARIOS), dict(engine.SCENARIO_PROMPTS))
        ok, msg = verify.check_scenario_names(engine=eng)
        self.assertFalse(ok)
        self.assertIn("툴을 연다", msg)

    def test_missing_registry_is_caught(self):
        class Gone(object):
            SCENARIOS = ("refund",)

        ok, msg = verify.check_scenario_names(engine=Gone)
        self.assertFalse(ok)
        self.assertIn("없다", msg)

    def test_gate_step_is_wired(self):
        """단계를 지우면(또는 steps 에 안 넣으면) 여기서 드러난다."""
        import io as _io
        import contextlib
        buf = _io.StringIO()
        argv = sys.argv
        sys.argv = ["verify.py", "--json"]
        try:
            with contextlib.redirect_stdout(buf):
                rc = verify.main()
        finally:
            sys.argv = argv
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        report = json.loads(out)
        names = [s["step"] for s in report["steps"]]
        self.assertIn("scenario_names", names)
        self.assertTrue(report["ok"], out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
