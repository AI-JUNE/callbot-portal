# -*- coding: utf-8 -*-
"""api/_engine.py 잔여 분기 회귀 — 환불 가드 경계·바깥 값 타입·import 자립.

`tests/test_escalation.py` 가 엔진의 **전환 경로**(차단 → 상담사 전환)를 덮는다면,
이 파일은 그 옆에 남아 있던 칸들을 덮는다.

  1) `_amount` — 금액류 값 해석(정수·실수·문자열·콤마·불리언·쓰레기)
  2) `_guard` 허용 경로 — 환불 확정이 **실제로 통과하는** 조건 조합.
     지금까지 회귀는 차단만 확인했다. 되돌리기 어려운 동작을 여는 쪽이
     테스트되지 않으면 "가드가 꽉 막혀 있어도" 아무도 모른다.
  3) `_guard` 바깥 값 타입 — 견적·정책 한도는 주문 백엔드(ORDER_BACKEND=http 면
     고객사 REST API 의 임의 JSON)에서, 확정 금액은 LLM 에서 온다. 숫자가 아니어도
     **판정으로 끝나야** 한다(예외로 죽으면 감사기록조차 남지 않는다).
  4) `_mem`·`_to_contents` — 툴 결과가 JSON 이 아닐 때
  5) `_call` — 업스트림 호출 조립(키 출처·URL·타임아웃). 네트워크는 타지 않는다.
  6) import 자립 — `api/` 가 sys.path 에 없는 깨끗한 인터프리터에서도
     `_engine` 이 로드되고 에스컬레이션 큐가 살아 있어야 한다.

LLM·네트워크는 호출하지 않는다(`_call` 대역 + urlopen 감시).

실행: python3 -m pytest tests/test_engine_edge.py -q
"""
import json
import os
import subprocess
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)

import _engine as engine  # noqa: E402


class NoNetwork(unittest.TestCase):
    """urlopen 이 실제로 불리면 즉시 실패 — 테스트가 과금되지 않는다."""

    def setUp(self):
        self._restore = []
        self.opened = []

        def watchdog(*a, **kw):
            self.opened.append(a[:1])
            raise AssertionError("네트워크 호출 금지")

        self.patch(urllib.request, "urlopen", watchdog)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)


# ==========================================================================
# 1) 금액류 값 해석
# ==========================================================================
class TestAmount(NoNetwork):

    def test_integers_pass_through(self):
        self.assertEqual(engine._amount(0), 0)
        self.assertEqual(engine._amount(159000), 159000)
        self.assertEqual(engine._amount(-5), -5)

    def test_float_is_truncated_to_won(self):
        self.assertEqual(engine._amount(1590.9), 1590)

    def test_numeric_strings_are_read(self):
        # 고객사 API 가 금액을 문자열로 주는 일은 흔하다
        self.assertEqual(engine._amount("159000"), 159000)
        self.assertEqual(engine._amount(" 159,000 "), 159000)
        self.assertEqual(engine._amount("1590.0"), 1590)

    def test_booleans_are_not_amounts(self):
        # True == 1 로 새어 들어오면 '1원 환불'이 유효한 금액이 된다
        self.assertIsNone(engine._amount(True))
        self.assertIsNone(engine._amount(False))

    def test_unreadable_values_are_none(self):
        for v in (None, "", "오만원", "5만", "50000원", "1e5", {"krw": 1}, [1], object()):
            self.assertIsNone(engine._amount(v), v)

    def test_absurdly_long_digit_string_is_blocked_not_crashed(self):
        """숫자 모양이지만 float 범위를 넘는 값 — `int(float(s))` 가 터지는 자리.

        `_RX_AMOUNT` 는 자릿수를 세지 않으므로 400자리 숫자도 '금액 모양'으로
        통과한다. `float("1"*400)` 은 inf 가 되고 `int(inf)` 는 OverflowError 다.
        금액은 LLM(확정)·주문 백엔드(견적·한도)가 주는 바깥 값이라 이 모양이
        들어올 수 있고, 가드 **안에서** 예외가 터지면 판정도 감사기록도 남지
        않은 채 500 으로 끝난다(19차에 고친 결함과 같은 계열). 해석 불가는
        예외가 아니라 차단 판정이어야 한다.
        """
        huge = "9" * 400
        self.assertEqual(float(huge), float("inf"))     # 전제 확인
        self.assertIsNone(engine._amount(huge))
        self.assertIsNone(engine._amount("-" + huge))
        self.assertIsNone(engine._amount(huge + ".5"))
        # 확정 금액으로 들어와도 가드는 예외 대신 '차단'으로 답한다
        ok, reason, esc = engine._guard(
            "confirm_refund", dict(CONFIRM, refund_amount=huge),
            mem(awaiting=True, quoted_amount=159000, max_refund=159000))
        self.assertFalse(ok)
        self.assertFalse(esc)
        self.assertIn("형식", reason)
        # 견적·한도 쪽으로 들어오면 확인 자체가 불가능하므로 상담사 전환까지
        for key in ("quoted_amount", "max_refund"):
            with self.subTest(field=key):
                ok, _r, esc = engine._guard(
                    "confirm_refund", CONFIRM,
                    mem(awaiting=True, **{"quoted_amount": 159000, key: huge}))
                self.assertFalse(ok)
                self.assertTrue(esc)


# ==========================================================================
# 2) 환불 가드 — 허용 경로와 한도
# ==========================================================================
def mem(**kw):
    m = {"order_id": "SSG-1", "max_refund": None, "quoted_amount": None,
         "awaiting": False, "affirm": True, "transferred": False}
    m.update(kw)
    return m


CONFIRM = {"order_id": "SSG-1", "refund_amount": 159000, "user_confirmed": True}


class TestGuardAllow(NoNetwork):

    def test_all_conditions_met_allows_refund(self):
        ok, reason, esc = engine._guard("confirm_refund", CONFIRM,
                                        mem(awaiting=True, quoted_amount=159000,
                                            max_refund=159000))
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertFalse(esc)

    def test_allowed_at_exact_policy_limit(self):
        # 경계값: 한도와 같은 금액은 '초과'가 아니다
        ok, _, _ = engine._guard("confirm_refund", CONFIRM,
                                 mem(awaiting=True, quoted_amount=159000, max_refund=159000))
        self.assertTrue(ok)

    def test_over_policy_limit_blocks_and_escalates(self):
        ok, reason, esc = engine._guard(
            "confirm_refund", dict(CONFIRM, refund_amount=159001),
            mem(awaiting=True, quoted_amount=159001, max_refund=159000))
        self.assertFalse(ok)
        self.assertIn("한도 초과", reason)
        self.assertTrue(esc)

    def test_no_policy_lookup_does_not_block_on_limit(self):
        # max_refund 미조회(None)는 '한도 0'이 아니다 — 견적 일치만으로 통과
        ok, _, _ = engine._guard("confirm_refund", CONFIRM,
                                 mem(awaiting=True, quoted_amount=159000))
        self.assertTrue(ok)

    def test_unknown_quote_amount_blocks_and_escalates(self):
        """견적을 '모르는' 상태는 일치로 넘기지 않는다.

        quote_refund 가 금액 없는 응답을 주면(백엔드 장애 시
        `{"error":"backend_unavailable"}`) awaiting 만 True 가 되는데, 예전에는
        quoted_amount is None 이면 금액 재확인을 **건너뛰었다**. 한도까지 미조회면
        임의 금액이 그대로 승인되는 fail-open 경로다.
        """
        ok, reason, esc = engine._guard("confirm_refund", CONFIRM,
                                        mem(awaiting=True, quoted_amount=None))
        self.assertFalse(ok)
        self.assertIn("견적 금액 확인 불가", reason)
        self.assertTrue(esc)

    def test_backend_outage_during_quote_does_not_open_the_gate(self):
        m = engine._mem([
            {"role": "tool", "name": "quote_refund",
             "content": json.dumps({"error": "backend_unavailable", "detail": "timeout"})},
            {"role": "user", "content": "네 환불해 주세요"},
        ])
        self.assertTrue(m["awaiting"])
        ok, _, esc = engine._guard("confirm_refund", dict(CONFIRM, refund_amount=9999999), m)
        self.assertFalse(ok)
        self.assertTrue(esc)

    def test_redelivery_allowed_with_consent(self):
        ok, reason, esc = engine._guard("request_redelivery", {"order_id": "SSG-1"}, mem())
        self.assertTrue(ok)
        self.assertFalse(esc)

    def test_unknown_tool_is_not_gated_here(self):
        ok, _, _ = engine._guard("lookup_recent_order", {"phone": "01012345678"}, mem(affirm=False))
        self.assertTrue(ok)


# ==========================================================================
# 3) 바깥에서 온 값이 숫자가 아닐 때 — 예외가 아니라 판정
# ==========================================================================
class TestGuardForeignTypes(NoNetwork):

    def test_string_policy_limit_is_compared_not_crashed(self):
        """고객사 API 가 한도를 문자열로 주는 경우.

        예전에는 `int > str` TypeError 가 가드 안에서 터져 요청이 500 으로 끝났다.
        환불이 나가지는 않았지만 판정도 감사기록도 남지 않았다.
        """
        ok, reason, esc = engine._guard(
            "confirm_refund", dict(CONFIRM, refund_amount=500000),
            mem(awaiting=True, quoted_amount="500000", max_refund="159000"))
        self.assertFalse(ok)
        self.assertIn("한도 초과", reason)
        self.assertTrue(esc)

    def test_string_limit_within_range_allows(self):
        ok, reason, _ = engine._guard("confirm_refund", CONFIRM,
                                      mem(awaiting=True, quoted_amount="159000",
                                          max_refund="159,000"))
        self.assertTrue(ok, reason)

    def test_unreadable_quote_blocks_and_escalates(self):
        # 견적을 읽을 수 없으면 '일치'로 넘기지 않는다 — 확인 자체가 불가능하다
        ok, reason, esc = engine._guard("confirm_refund", CONFIRM,
                                        mem(awaiting=True, quoted_amount={"krw": 159000}))
        self.assertFalse(ok)
        self.assertIn("견적 금액 확인 불가", reason)
        self.assertNotIn("krw", reason)      # 바깥 값을 판정 문구로 되돌려 보내지 않는다
        self.assertTrue(esc)

    def test_unreadable_policy_limit_blocks_and_escalates(self):
        ok, reason, esc = engine._guard("confirm_refund", CONFIRM,
                                        mem(awaiting=True, quoted_amount=159000,
                                            max_refund="한도없음"))
        self.assertFalse(ok)
        self.assertIn("한도 확인 불가", reason)
        self.assertTrue(esc)

    def test_unreadable_confirm_amount_blocks(self):
        ok, reason, esc = engine._guard("confirm_refund",
                                        dict(CONFIRM, refund_amount="오만원"),
                                        mem(awaiting=True, quoted_amount=50000))
        self.assertFalse(ok)
        self.assertIn("형식 오류", reason)
        self.assertFalse(esc)       # 되물으면 되는 상황 — 전환까지 갈 일이 아니다

    def test_missing_confirm_amount_blocks(self):
        ok, reason, _ = engine._guard("confirm_refund",
                                      {"order_id": "SSG-1", "user_confirmed": True},
                                      mem(awaiting=True, quoted_amount=50000))
        self.assertFalse(ok)
        self.assertIn("형식 오류", reason)

    def test_no_foreign_value_raises(self):
        """어떤 조합이 와도 가드는 예외를 던지지 않는다(fail-safe)."""
        junk = [None, "", "x", {"a": 1}, [1], True, 1.5, object()]
        for amt in junk:
            for q in junk:
                for mx in junk:
                    ok, reason, esc = engine._guard(
                        "confirm_refund",
                        {"order_id": "SSG-1", "refund_amount": amt, "user_confirmed": True},
                        mem(awaiting=True, quoted_amount=q, max_refund=mx))
                    self.assertIsInstance(ok, bool)
                    self.assertIsInstance(reason, str)
                    self.assertIsInstance(esc, bool)
                    if ok:
                        # 통과했다면 금액·견적이 숫자로 읽히고 서로 같은 경우뿐이다
                        a = engine._amount(amt)
                        self.assertIsNotNone(a)
                        self.assertGreater(a, 0)
                        self.assertEqual(a, engine._amount(q))
                        # 한도는 미조회(None)일 수 있다 — 조회됐다면 넘지 않았다
                        self.assertTrue(mx is None or a <= engine._amount(mx))


# ==========================================================================
# 4) 대화 이력 해석 — 툴 결과가 JSON 이 아닐 때
# ==========================================================================
class TestMemAndContents(NoNetwork):

    def test_broken_tool_json_does_not_break_memory(self):
        m = engine._mem([
            {"role": "user", "content": "환불이요"},
            {"role": "tool", "name": "quote_refund", "content": "<html>502</html>"},
            {"role": "user", "content": "네"},
        ])
        self.assertTrue(m["awaiting"])            # 견적 호출 사실은 남는다
        self.assertIsNone(m["quoted_amount"])     # 금액은 모른다 → 가드가 막는다
        self.assertTrue(m["affirm"])

    def test_broken_tool_json_still_reaches_the_model(self):
        out = engine._to_contents([
            {"role": "tool", "name": "quote_refund", "content": "not json"},
        ])
        resp = out[0]["parts"][0]["functionResponse"]["response"]
        self.assertEqual(resp, {"result": "not json"})

    def test_quote_without_amount_is_not_confirmable(self):
        # 견적 금액을 모르는 상태에서 확정 시도 → 차단 + 전환
        m = engine._mem([
            {"role": "tool", "name": "quote_refund", "content": "<html>502</html>"},
            {"role": "user", "content": "네 환불해 주세요"},
        ])
        ok, reason, esc = engine._guard("confirm_refund", CONFIRM, m)
        self.assertFalse(ok)
        self.assertTrue(esc)

    def test_assistant_tool_calls_are_rendered_as_function_calls(self):
        out = engine._to_contents([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t", "name": "quote_refund", "input": {"order_id": "SSG-1"}}]},
        ])
        self.assertEqual(out[0]["role"], "model")
        self.assertEqual(out[0]["parts"][0]["functionCall"]["name"], "quote_refund")


# ==========================================================================
# 5) 업스트림 호출 조립 — 네트워크는 타지 않는다
# ==========================================================================
class FakeResp(object):
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCall(NoNetwork):

    def setUp(self):
        super().setUp()
        self.seen = []
        for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            self._restore.append((os.environ, k, os.environ.get(k)))
        os.environ.pop("GEMINI_API_KEY", None)

        def fake_urlopen(req, timeout=None):
            self.seen.append((req, timeout))
            return FakeResp({"candidates": [{"content": {"parts": [{"text": "네"}]}}]})

        self.patch(urllib.request, "urlopen", fake_urlopen)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            if obj is os.environ:
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
            else:
                setattr(obj, name, old)
        self._restore = []

    def test_missing_key_raises_without_calling_out(self):
        os.environ.pop("GOOGLE_API_KEY", None)
        with self.assertRaises(RuntimeError) as cm:
            engine._call("gemini-2.5-flash", {"contents": []})
        self.assertIn("GOOGLE_API_KEY", str(cm.exception))
        self.assertEqual(self.seen, [])

    def test_request_is_posted_with_timeout_and_json_body(self):
        os.environ["GOOGLE_API_KEY"] = "test-key-abc"
        out = engine._call("gemini-2.5-flash", {"contents": [{"role": "user"}]})
        self.assertEqual(out["candidates"][0]["content"]["parts"][0]["text"], "네")
        req, timeout = self.seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 30)
        self.assertIn("gemini-2.5-flash:generateContent", req.full_url)
        self.assertEqual(req.headers.get("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode()), {"contents": [{"role": "user"}]})

    def test_gemini_api_key_is_accepted_as_fallback(self):
        os.environ.pop("GOOGLE_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = "fallback-key"
        engine._call("m", {})
        self.assertIn("fallback-key", self.seen[0][0].full_url)

    def test_key_is_not_placed_in_headers_or_body(self):
        os.environ["GOOGLE_API_KEY"] = "test-key-abc"
        engine._call("m", {"contents": []})
        req = self.seen[0][0]
        self.assertNotIn("test-key-abc", json.dumps(dict(req.headers)))
        self.assertNotIn("test-key-abc", req.data.decode())


# ==========================================================================
# 6) run_turn — 견적 기억 갱신
# ==========================================================================
def call_tool(name, args):
    return {"candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


def say(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


class TestRunTurnQuote(NoNetwork):

    def setUp(self):
        super().setUp()
        self.responses = []

        def fake_call(model, payload):
            if not self.responses:
                raise AssertionError("대역 응답 소진 — 예상보다 많은 LLM 호출")
            return self.responses.pop(0)

        self.patch(engine, "_call", fake_call)

    def test_quote_in_same_turn_enables_and_pins_the_amount(self):
        """같은 턴에서 견적 → 확정. 견적 금액이 기억돼 금액 재확인이 작동해야 한다."""
        items = [{"name": "[생방송] 한우 1++ 선물세트 1.6kg", "qty": 1}]
        self.responses = [
            call_tool("quote_refund", {"order_id": "SSG-1", "missing_items": items}),
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 1,
                                         "user_confirmed": True}),
            say("처리해 드렸습니다."),
        ]
        r = engine.run_turn([{"role": "user", "content": "네 환불해 주세요"}], scenario="refund")
        reasons = [a["reason"] for a in r["audit"]]
        self.assertEqual([a["decision"] for a in r["audit"]], ["block"])
        self.assertIn("불일치", reasons[0])
        self.assertEqual(r["audit"][0]["quoted_amount"], 159000)   # 견적이 기억됐다
        self.assertTrue(r["transferred"])                          # 불일치 → 상담사 전환

    def test_quote_then_matching_confirm_is_accepted(self):
        items = [{"name": "[생방송] 한우 1++ 선물세트 1.6kg", "qty": 1}]
        self.responses = [
            call_tool("quote_refund", {"order_id": "SSG-1", "missing_items": items}),
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 159000,
                                         "user_confirmed": True}),
            say("환불 접수했습니다."),
        ]
        r = engine.run_turn([{"role": "user", "content": "네 환불해 주세요"}], scenario="refund")
        self.assertEqual([a["decision"] for a in r["audit"]], ["allow"])
        self.assertFalse(r["transferred"])
        self.assertEqual(r["reply"], "환불 접수했습니다.")

    def test_audit_keeps_no_caller_number(self):
        phone = "01099998888"
        self.responses = [
            call_tool("confirm_refund", {"order_id": "SSG-1", "refund_amount": 1,
                                         "user_confirmed": False}),
            say("확인해 드릴게요."),
        ]
        r = engine.run_turn([{"role": "user", "content": "환불이요"}], phone=phone,
                            scenario="refund")
        self.assertNotIn(phone, json.dumps(r["audit"], ensure_ascii=False))


# ==========================================================================
# 7) import 자립 — 깨끗한 인터프리터에서도 로드된다
# ==========================================================================
SNIPPET = r"""
import importlib.util, os, sys
p = os.path.join(%r, "_engine.py")
spec = importlib.util.spec_from_file_location("_engine_probe", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("QUEUE", m._ESC_QUEUE is not None)
print("BACKEND", m.get_backend().name)
"""


class TestImportBootstrap(unittest.TestCase):

    def test_loads_without_the_caller_preparing_sys_path(self):
        """`api/` 가 sys.path 에 없어도 로드돼야 한다.

        예전에는 importer 가 sys.path 를 손봐 둔 것에 기대고, 실패하면
        `api.order_backend` 로 폴백했다. 그 모듈은 `_order_backend` 로 이름이 바뀐 뒤
        존재하지 않아 폴백이 성립하지 않았고(ModuleNotFoundError), 에스컬레이션은
        같은 이유로 `_ESC_QUEUE=None` 으로 떨어져 **상담사 전환 티켓이 조용히 사라지는**
        경로가 됐다.
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["CALLBOT_LOG"] = "off"
        p = subprocess.run([sys.executable, "-c", SNIPPET % (API,)],
                           capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertIn("QUEUE True", p.stdout)
        self.assertIn("BACKEND demo", p.stdout)

    def test_source_has_no_stale_package_fallbacks(self):
        """드리프트 차단: 존재하지 않는 모듈로의 폴백을 다시 심지 않는다."""
        with open(os.path.join(API, "_engine.py"), encoding="utf-8") as f:
            src = f.read()
        # 주석(위 결함을 설명하는 문장)은 제외하고 실코드만 본다
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for dead in ("api.order_backend", "api.escalation", "from api."):
            self.assertNotIn(dead, code)
        self.assertIn("sys.path.insert", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
