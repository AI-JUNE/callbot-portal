# -*- coding: utf-8 -*-
"""명시 동의 판정 회귀 (33차) — LLM·네트워크 미호출.

왜 이 파일이 필요한가
  환불 확정(`confirm_refund`)은 2단계 관문이다. 1단계는 **LLM 이 세우는**
  `user_confirmed` 플래그이고, 2단계는 **고객 발화에서 규칙으로 읽는** 명시
  동의다. 2단계의 존재 이유는 「모델 말을 믿지 않는 독립 확인」이다.

  그 2단계가 부분 일치 정규식 한 줄이었다:

      _AFFIRM = re.compile(r"(네|예|응|좋아|그래|맞아|해주세요|할게|동의)")

  한글은 1글자 대안이 다른 낱말 **안에서** 걸린다. 그래서

      「환불 취소할게요」     → '할게'  → 동의
      「동의하지 않습니다」   → '동의'  → 동의
      「예전에 받은 주문이요」 → '예'    → 동의
      「그건 좋아하지 않아요」 → '좋아'  → 동의
      「네?」(못 들었어요)     → '네'    → 동의

  거절 문장이 전부 동의로 읽혔다 — 2단계가 사실상 **없는 것**이었다. 뒤이은
  금액 일치·한도 검사가 돈을 막고는 있었지만, 그 검사들은 금액이 맞는 한
  통과하므로 「고객이 거절했는데 금액은 맞는」 조합에서 환불이 나간다.

  고친 방향: (1) 1글자 감탄사는 **어절 단위**로만, (2) **거절 우선**.
  틀리는 방향을 고른 것이다 — 동의를 놓치면 봇이 한 번 더 묻고(회복 가능),
  거절을 동의로 읽으면 환불이 나간다(되돌릴 수 없다).

실행: python3 -m pytest tests/test_consent_rules.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _engine as engine  # noqa: E402


def mem(**kw):
    m = engine._mem_blank()
    m.update(kw)
    return m


class TestAffirmIsRecognized(unittest.TestCase):
    """정상 동의를 놓치면 통화가 실패한다 — 과차단도 결함이다."""

    YES = ("네", "네.", "네, 그렇게 해주세요", "네 환불해 주세요",
           "네 동의합니다", "네 50만 원 환불해 주세요", "네 좋아요",
           "예, 맞습니다", "예 알겠습니다", "응 그래", "그래요",
           "맞아요", "좋아요", "동의합니다", "동의해요",
           "그렇게 해주세요", "그렇게 해 주세요", "재배달 해 주세요",
           "알겠습니다 진행해주세요", "접수해 주세요", "환불 받을게요",
           "부탁드립니다", "부탁드려요", "오케이", "OK", "네네")

    def test_every_plain_consent_is_read_as_consent(self):
        for s in self.YES:
            with self.subTest(utterance=s):
                self.assertTrue(engine._affirm_hit(s))

    def test_spacing_variants_count(self):
        """「해주세요」와 「해 주세요」는 같은 말이다(STT 는 띄어쓰기를 가린다)."""
        self.assertTrue(engine._affirm_hit("환불해주세요"))
        self.assertTrue(engine._affirm_hit("환불해 주세요"))


class TestRefusalIsNotConsent(unittest.TestCase):
    """거절·보류·되묻기는 동의가 아니다(돈이 나가는 방향)."""

    NO = ("환불 취소할게요", "취소해주세요", "동의하지 않습니다",
          "그건 좋아하지 않아요", "아니요", "아니에요", "아뇨", "싫습니다",
          "안 할게요", "하지 마세요", "그렇게 하면 안 되는데요",
          "환불 말고 재배달로 해주세요", "나중에 다시 연락 주세요",
          "생각해볼게요", "잘 모르겠어요", "필요 없어요", "그만 하겠습니다",
          "잠깐만요", "네?", "뭐라고요?", "잘못 아신 것 같아요")

    def test_every_refusal_is_not_consent(self):
        for s in self.NO:
            with self.subTest(utterance=s):
                self.assertFalse(engine._affirm_hit(s))

    def test_refusal_wins_even_when_consent_words_follow(self):
        """거절 우선 — 한 발화에 둘이 섞이면 동의로 읽지 않는다."""
        for s in ("아니요 네 그건 아니고", "환불은 취소하고 재배달 해 주세요",
                  "동의는 하지만 나중에 할게요"):
            with self.subTest(utterance=s):
                self.assertFalse(engine._affirm_hit(s))


class TestWordBoundary(unittest.TestCase):
    """1글자 감탄사는 어절 단위 — 낱말 안에서 걸리지 않는다."""

    NOT_CONSENT = ("예전에 받은 주문이요", "예약 배송이었어요", "예상보다 늦었어요",
                   "네이버에서 주문했어요", "응답이 늦었네요", "그래프를 봤어요",
                   "편의점에서 받았어요")

    def test_words_that_merely_contain_an_interjection(self):
        for s in self.NOT_CONSENT:
            with self.subTest(utterance=s):
                self.assertFalse(engine._affirm_hit(s))

    def test_the_interjection_alone_is_still_consent(self):
        """경계를 좁혔어도 「예」 한 마디는 동의다."""
        self.assertTrue(engine._affirm_hit("예"))
        self.assertTrue(engine._affirm_hit("예."))

    def test_non_string_and_blank_are_not_consent(self):
        for bad in (None, 5, [], {}, "", "   "):
            with self.subTest(value=repr(bad)):
                self.assertFalse(engine._affirm_hit(bad))


class TestMemoryAndGuard(unittest.TestCase):
    """판정이 `_mem` → `_guard` 로 이어지는지(배선까지 본다)."""

    def last(self, utterance):
        return engine._mem([{"role": "user", "content": utterance}])

    def test_mem_affirm_follows_the_last_utterance(self):
        self.assertTrue(self.last("네 동의합니다")["affirm"])
        self.assertFalse(self.last("환불 취소할게요")["affirm"])

    def confirm(self, utterance, amount=1000):
        m = self.last(utterance)
        m.update(awaiting=True, quoted_amount=amount, max_refund=amount)
        return engine._guard("confirm_refund",
                             {"order_id": "A-1", "refund_amount": amount,
                              "user_confirmed": True}, m)

    def test_a_refusal_blocks_the_refund_even_when_the_amount_matches(self):
        """이것이 이번 수정의 핵심이다 — 금액이 맞아도 거절이면 막힌다."""
        ok, reason, esc = self.confirm("환불 취소할게요")
        self.assertFalse(ok)
        self.assertIn("명시 동의", reason)
        self.assertFalse(esc)

    def test_a_real_consent_still_passes(self):
        ok, reason, esc = self.confirm("네 그렇게 해주세요")
        self.assertTrue(ok, reason)

    def test_redelivery_also_needs_consent(self):
        for utterance, want in (("재배달 해 주세요", True),
                                ("재배달은 안 할게요", False)):
            with self.subTest(utterance=utterance):
                ok, _r, _e = engine._guard("request_redelivery",
                                           {"order_id": "A-1", "items": []},
                                           self.last(utterance))
                self.assertEqual(ok, want)

    def test_the_llm_flag_alone_is_not_enough(self):
        """1단계(모델 플래그)만으로는 통과하지 못한다 — 2단계가 독립 확인이다."""
        m = self.last("아니요 괜찮습니다")
        m.update(awaiting=True, quoted_amount=1000, max_refund=1000)
        ok, reason, _e = engine._guard(
            "confirm_refund",
            {"order_id": "A-1", "refund_amount": 1000, "user_confirmed": True}, m)
        self.assertFalse(ok)
        self.assertIn("명시 동의", reason)


class TestTheOldShapeIsGone(unittest.TestCase):
    """같은 결함이 다시 들어오지 않게 — 모양 자체를 고정한다."""

    def test_the_loose_single_syllable_regex_is_not_back(self):
        src = open(os.path.join(ROOT, "api", "_engine.py"),
                   encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("(네|예|응|", code)

    def test_deny_and_affirm_are_both_wired(self):
        self.assertTrue(hasattr(engine, "_DENY"))
        self.assertTrue(hasattr(engine, "_AFFIRM_TOKENS"))
        self.assertTrue(hasattr(engine, "_AFFIRM_PHRASE"))

    def test_affirm_tokens_are_matched_whole_not_substring(self):
        """등록된 토큰이 늘어나도 어절 단위라는 성질은 유지돼야 한다."""
        for tok in engine._AFFIRM_TOKENS:
            with self.subTest(token=tok):
                self.assertTrue(engine._affirm_hit(tok))
                # 앞뒤에 한글이 붙으면 더는 그 토큰이 아니다(거절어가 아닌 글자로 감싼다).
                if len(tok) == 1 and tok not in ("ok",):
                    self.assertFalse(engine._affirm_hit("주문" + tok + "건"))


if __name__ == "__main__":
    unittest.main()
