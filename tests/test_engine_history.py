# -*- coding: utf-8 -*-
"""모델에 보내는 대화 이력 창 회귀 (31차) — LLM·네트워크 미호출.

왜 이 파일이 필요한가
  30차의 `voice._Session` 상한은 **통화 건수** 상한이었다. 한 통화 **안쪽**에는
  상한이 없었다 — `voice.py` 는 턴마다 `sess["messages"] = r["messages"]` 로
  이력을 쌓고 `run_turn` 은 매 턴 **전체를 다시** 모델에 보낸다. 긴 통화에서는
  입력 토큰이 턴 수의 제곱으로 자라 (1) 과금이 함께 자라고 (2) 결국 컨텍스트
  한도에 부딪혀 **고객이 말하는 중에** 통화가 끊긴다. `/api/chat` 은 라우트가
  100건·8000자로 막지만 `/api/voice` 에는 막는 곳이 한 군데도 없었다.

  고치는 쪽이 더 위험할 수 있는 자리이기도 하다. 그래서 두 가지를 못 박는다.
    · **가드의 기억은 줄지 않는다** — `_mem`(견적 금액·정책 한도·대기 상태)은
      여전히 전체 이력에서 계산한다. 창을 줄였다고 금액 재확인이 꺼지면 그것은
      fail-open 이다(20차 `quoted_amount=None` 사고와 같은 자리).
    · **짝을 깨지 않는다** — functionResponse 만 남고 functionCall 이 잘리면
      업스트림이 400 을 돌려주고 통화가 끊긴다. 자르는 위치는 뒤로 밀지 않고
      앞으로 당긴다.

실행: python3 -m pytest tests/test_engine_history.py -q
"""
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _engine as engine  # noqa: E402

ENVS = ("CALLBOT_HISTORY_MAX", "CALLBOT_HISTORY_MAX_CHARS")


class Base(unittest.TestCase):
    def setUp(self):
        self._restore = []
        self._env = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)

        def watchdog(*a, **kw):
            raise AssertionError("네트워크 호출 금지")

        self.patch(urllib.request, "urlopen", watchdog)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def talk(self, n, text="안녕하세요"):
        """user/assistant 가 번갈아 오는 평범한 통화 이력."""
        out = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            out.append({"role": role, "content": "%s %d" % (text, i)})
        return out

    def capture(self, msgs, scenario="refund"):
        """run_turn 을 1홉으로 돌리고 **모델에 실제로 나간 payload** 를 잡는다."""
        seen = {}

        def fake_call(model, payload):
            seen["payload"] = payload
            return {"candidates": [{"content": {"parts": [{"text": "네."}]}}],
                    "usageMetadata": {}}

        self.patch(engine, "_call", fake_call)
        r = engine.run_turn(msgs, scenario=scenario)
        return seen["payload"], r


# ==========================================================================
# 1) 창 자체
# ==========================================================================
class TestWindow(Base):
    def test_short_history_is_untouched(self):
        msgs = self.talk(6)
        win, dropped = engine._window(msgs)
        self.assertEqual(win, msgs)
        self.assertEqual(dropped, 0)

    def test_message_count_is_capped(self):
        msgs = self.talk(engine.HISTORY_MAX_MSGS + 30)
        win, dropped = engine._window(msgs)
        self.assertEqual(len(win), engine.HISTORY_MAX_MSGS)
        self.assertEqual(dropped, 30)

    def test_the_newest_turns_are_the_ones_kept(self):
        """최근 쪽을 남긴다 — 방금 한 말을 버리면 대화가 성립하지 않는다."""
        msgs = self.talk(engine.HISTORY_MAX_MSGS + 10)
        win, _ = engine._window(msgs)
        self.assertEqual(win[-1], msgs[-1])
        self.assertNotIn(msgs[0], win)

    def test_long_bodies_are_capped_even_when_few(self):
        """건수가 적어도 본문이 길면 같은 문제다(1건 8000자 × 100건 = 80만 자)."""
        msgs = [{"role": "user", "content": "가" * 8000} for _ in range(10)]
        win, dropped = engine._window(msgs)
        self.assertGreater(dropped, 0)
        total = sum(len(m["content"]) for m in win)
        self.assertLessEqual(total, engine.HISTORY_MAX_CHARS)

    def test_one_huge_message_is_never_dropped_to_nothing(self):
        """마지막 발화까지 버리면 통화가 죽는다 — 예산을 넘겨도 1건은 남긴다."""
        msgs = [{"role": "user", "content": "나" * (engine.HISTORY_MAX_CHARS * 2)}]
        win, dropped = engine._window(msgs)
        self.assertEqual(len(win), 1)
        self.assertEqual(dropped, 0)

    def test_non_string_content_does_not_raise(self):
        msgs = [{"role": "user", "content": None}, {"role": "user"},
                "쓰레기", 5, {"role": "assistant", "content": "네."}]
        win, _ = engine._window(msgs)
        self.assertEqual(win[-1]["content"], "네.")

    def test_non_list_input_is_tolerated(self):
        for bad in (None, "x", {}, 5):
            self.assertEqual(engine._window(bad), ([], 0))


# ==========================================================================
# 2) 짝 깨짐 — 그대로 보내면 업스트림 400 으로 통화가 끊긴다
# ==========================================================================
class TestPairing(Base):
    def tool_history(self, lead):
        """앞쪽 잡담 lead 건 + functionCall 1건 + functionResponse 2건."""
        return self.talk(lead) + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "quote_refund", "name": "quote_refund", "input": {}},
                            {"id": "get_refund_policy", "name": "get_refund_policy",
                             "input": {}}]},
            {"role": "tool", "tool_call_id": "quote_refund", "name": "quote_refund",
             "content": "{\"refund_amount\": 15900}"},
            {"role": "tool", "tool_call_id": "get_refund_policy",
             "name": "get_refund_policy", "content": "{\"eligible\": true}"},
        ]

    def test_window_never_starts_with_a_dangling_tool_result(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "2"
        win, _ = engine._window(self.tool_history(10))
        self.assertEqual(win[0].get("role"), "assistant")
        self.assertTrue(win[0].get("tool_calls"))

    def test_the_cut_moves_backwards_not_forwards(self):
        """앞으로 당긴다 — 뒤로 밀면 창이 통째로 비는 경우가 생긴다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "1"
        win, dropped = engine._window(self.tool_history(10))
        self.assertEqual(len(win), 3)          # 상한(1)을 넘더라도 짝은 지킨다
        self.assertEqual(dropped, 10)

    def test_contents_carry_a_function_call_for_every_response(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "2"
        win, _ = engine._window(self.tool_history(10))
        parts = [p for c in engine._to_contents(win) for p in c["parts"]]
        calls = sum(1 for p in parts if "functionCall" in p)
        resps = sum(1 for p in parts if "functionResponse" in p)
        self.assertEqual((calls, resps), (2, 2))

    def test_char_budget_also_respects_pairing(self):
        os.environ["CALLBOT_HISTORY_MAX_CHARS"] = "10"
        win, _ = engine._window(self.tool_history(10))
        self.assertNotEqual(win[0].get("role"), "tool")


# ==========================================================================
# 3) 상한 읽기 — 설정 오타로 상한이 **사라지지** 않는다
# ==========================================================================
class TestLimits(Base):
    def test_env_overrides_the_default(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "6"
        win, _ = engine._window(self.talk(30))
        self.assertEqual(len(win), 6)

    def test_garbage_falls_back_to_the_default_not_to_no_cap(self):
        for v in ("", "   ", "많이", "0", "-5", "3.5"):
            os.environ["CALLBOT_HISTORY_MAX"] = v
            win, _ = engine._window(self.talk(engine.HISTORY_MAX_MSGS + 10))
            self.assertEqual(len(win), engine.HISTORY_MAX_MSGS, repr(v))

    def test_limits_are_read_at_call_time(self):
        """배포 후 조정이 반영된다(import 시점에 얼어붙지 않는다)."""
        self.assertEqual(len(engine._window(self.talk(30))[0]), 30)
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        self.assertEqual(len(engine._window(self.talk(30))[0]), 4)

    def test_char_budget_env(self):
        os.environ["CALLBOT_HISTORY_MAX_CHARS"] = "20"
        win, dropped = engine._window(self.talk(30, text="가나다라마바사"))
        self.assertGreater(dropped, 0)
        self.assertLessEqual(sum(len(m["content"]) for m in win), 20 + 11)


# ==========================================================================
# 4) run_turn 배선 — 모델에 **실제로** 줄여 나가는가, 그리고 조용하지 않은가
# ==========================================================================
class TestWiring(Base):
    def test_payload_contents_are_trimmed(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "5"
        payload, _r = self.capture(self.talk(40))
        self.assertEqual(len(payload["contents"]), 5)

    def test_trimming_is_reported_in_the_log(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "5"
        _payload, r = self.capture(self.talk(40))
        rows = [x for x in r["log"] if x["turn"] == "context"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["dropped"], rows[0]["kept"]), (35, 5))

    def test_the_log_row_carries_counts_only(self):
        """발화 원문이 로그·응답으로 새지 않는다(30차 마스킹과 같은 규약)."""
        os.environ["CALLBOT_HISTORY_MAX"] = "5"
        _payload, r = self.capture(self.talk(40, text="주민번호는 900101-1234567"))
        row = [x for x in r["log"] if x["turn"] == "context"][0]
        self.assertEqual(set(row), {"turn", "dropped", "kept"})

    def test_no_row_when_nothing_was_dropped(self):
        _payload, r = self.capture(self.talk(4))
        self.assertEqual([x for x in r["log"] if x["turn"] == "context"], [])

    def test_returned_messages_are_not_trimmed(self):
        """돌려주는 이력은 그대로다 — 호출부가 저장하는 것은 통화 전체다."""
        os.environ["CALLBOT_HISTORY_MAX"] = "5"
        msgs = self.talk(40)
        _payload, r = self.capture(msgs)
        self.assertEqual(len(r["messages"]), 41)      # 40 + 봇 응답 1


# ==========================================================================
# 5) fail-open 금지 — 창을 줄여도 가드의 기억은 줄지 않는다
# ==========================================================================
class TestGuardMemorySurvivesTrimming(Base):
    def history_with_quote(self, pad):
        """오래된 견적(15,900원) + 그 뒤로 잡담 pad 건 + 확정 동의."""
        return ([{"role": "user", "content": "환불해 주세요"},
                 {"role": "assistant", "content": "",
                  "tool_calls": [{"id": "quote_refund", "name": "quote_refund",
                                  "input": {}}]},
                 {"role": "tool", "tool_call_id": "quote_refund",
                  "name": "quote_refund",
                  "content": "{\"refund_amount\": 15900}"}]
                + self.talk(pad, text="그리고요")
                + [{"role": "user", "content": "네 동의합니다"}])

    def test_quoted_amount_is_remembered_past_the_window(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "3"
        msgs = self.history_with_quote(40)
        win, dropped = engine._window(msgs)
        self.assertGreater(dropped, 3)                 # 견적은 창 밖으로 밀렸다
        self.assertNotIn(msgs[2], win)
        mem = engine._mem(msgs)                        # 그래도 가드는 기억한다
        self.assertEqual(mem["quoted_amount"], 15900)
        self.assertTrue(mem["awaiting"])

    def test_confirm_matching_the_old_quote_is_allowed(self):
        mem = engine._mem(self.history_with_quote(40))
        ok, reason, esc = engine._guard(
            "confirm_refund",
            {"order_id": "A", "refund_amount": 15900, "user_confirmed": True}, mem)
        self.assertTrue(ok, reason)
        self.assertFalse(esc)

    def test_a_different_amount_is_still_blocked_after_trimming(self):
        """창이 줄었다고 금액 재확인이 꺼지면 그게 fail-open 이다."""
        mem = engine._mem(self.history_with_quote(40))
        ok, reason, esc = engine._guard(
            "confirm_refund",
            {"order_id": "A", "refund_amount": 99000, "user_confirmed": True}, mem)
        self.assertFalse(ok)
        self.assertIn("불일치", reason)
        self.assertTrue(esc)

    def test_run_turn_blocks_the_mismatch_end_to_end(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "3"
        responses = [
            {"candidates": [{"content": {"parts": [{"functionCall": {
                "name": "confirm_refund",
                "args": {"order_id": "A", "refund_amount": 99000,
                         "user_confirmed": True}}}]}}], "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "상담사 연결할게요."}]}}],
             "usageMetadata": {}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        r = engine.run_turn(self.history_with_quote(40), scenario="refund")
        self.assertEqual([a["decision"] for a in r["audit"]], ["block"])
        self.assertIn("불일치", r["audit"][0]["reason"])   # 사유까지 — 이유가 바뀌면 가드가 바뀐 것
        self.assertTrue(r["transferred"])

    # -- 한도는 창보다 **먼저** 밀려난다 — 여기가 진짜 fail-open 자리다 --------
    def history_limit_then_quote(self, pad):
        """옛 정책 한도(20,000원) → 잡담 pad 건 → 최근 견적(99,000원) → 동의.

        창이 좁으면 **견적만 남고 한도가 밀려난다**. 가드의 기억을 창에서
        계산하면 `max_refund=None` 이 되어 한도 검사 자체가 건너뛰어진다 —
        차단이 아니라 **승인** 쪽으로 틀리는 조합이다(실제 환불이 나간다).
        """
        return ([{"role": "user", "content": "환불해 주세요"},
                 {"role": "assistant", "content": "",
                  "tool_calls": [{"id": "get_refund_policy",
                                  "name": "get_refund_policy", "input": {}}]},
                 {"role": "tool", "tool_call_id": "get_refund_policy",
                  "name": "get_refund_policy",
                  "content": "{\"eligible\": true, \"max_refund\": 20000}"}]
                + self.talk(pad, text="그리고요")
                + [{"role": "assistant", "content": "",
                    "tool_calls": [{"id": "quote_refund", "name": "quote_refund",
                                    "input": {}}]},
                   {"role": "tool", "tool_call_id": "quote_refund",
                    "name": "quote_refund", "content": "{\"refund_amount\": 99000}"},
                   {"role": "user", "content": "네 동의합니다"}])

    def test_the_policy_limit_is_remembered_after_it_leaves_the_window(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        msgs = self.history_limit_then_quote(40)
        win, _ = engine._window(msgs)
        self.assertNotIn(msgs[2], win, "한도가 아직 창 안이면 이 테스트는 아무것도 못 본다")
        self.assertIn(msgs[-2], win, "견적은 창 안에 있어야 조합이 성립한다")
        self.assertEqual(engine._mem(msgs)["max_refund"], 20000)

    def test_over_limit_confirm_is_blocked_not_approved(self):
        mem = engine._mem(self.history_limit_then_quote(40))
        ok, reason, esc = engine._guard(
            "confirm_refund",
            {"order_id": "A", "refund_amount": 99000, "user_confirmed": True}, mem)
        self.assertFalse(ok, "한도를 잊어 승인했다 — fail-open")
        self.assertIn("한도 초과", reason)
        self.assertTrue(esc)

    def test_over_limit_confirm_is_blocked_end_to_end(self):
        os.environ["CALLBOT_HISTORY_MAX"] = "4"
        responses = [
            {"candidates": [{"content": {"parts": [{"functionCall": {
                "name": "confirm_refund",
                "args": {"order_id": "A", "refund_amount": 99000,
                         "user_confirmed": True}}}]}}], "usageMetadata": {}},
            {"candidates": [{"content": {"parts": [{"text": "상담사 연결할게요."}]}}],
             "usageMetadata": {}},
        ]
        self.patch(engine, "_call", lambda model, payload: responses.pop(0))
        r = engine.run_turn(self.history_limit_then_quote(40), scenario="refund")
        self.assertEqual([a["decision"] for a in r["audit"]], ["block"])
        self.assertIn("한도 초과", r["audit"][0]["reason"])
        self.assertNotIn("confirm_refund",
                         [x.get("tool") for x in r["log"] if x["turn"] == "tool"])


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
