# -*- coding: utf-8 -*-
"""안부 판정의 부정문 처리 회귀 (33차) — 네트워크 미사용.

왜 이 파일이 필요한가
  안부 판정(`score_answers`)은 LLM 문장이 아니라 **규칙**이다(그래서 같은 답변이
  같은 점수를 내고 담당자가 근거를 검증할 수 있다). 그 규칙은 키워드 **부분
  일치**인데, 한국어의 부정은 동사 **뒤**에 오거나(…지 않다/…지 못하다) 앞에
  떨어져 온다(안/못). 그래서 긍정 키워드가 부정문 안에서 그대로 걸렸다:

      「잘 자지 못해요」      → sleep 4 (수면 양호)     ← '잘 자'
      「잘 챙겨 먹지 못해요」 → meal  5 (식사 규칙적)   ← '잘 챙겨'
      「하나도 안 좋아요」    → mood  4 (기분 양호)     ← '좋아'
      「안 괜찮아요」         → pain  4 (통증 거의 없음) ← '괜찮'

  낮은 점수 규칙이 먼저 걸리는 표현(「못 자」·「못 먹」)만 기존 회귀에 있었기
  때문에 침묵했다. 이 경로의 목적은 **위험을 담당자에게 올리는 것**인데, 위
  네 문장은 risk_level 을 낮춰 이음이 follow_up 을 만들지 않게 한다 — 통제가
  반대로 돈다(30차 `scrub` 가 한국어에서 아예 돌지 않던 것과 같은 계열:
  있는 줄만 알았던 통제다).

  고친 범위는 좁다 — **긍정 규칙(4·5점)이 걸렸을 때만** 부정을 보고, 그러면
  2점(부족)으로 읽는다. 낮은 점수 규칙은 손대지 않았다(그쪽을 건드리면
  위험을 낮추는 쪽으로 틀릴 수 있다).

실행: python3 -m pytest tests/test_wellbeing_negation.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import wellbeing as W  # noqa: E402


class TestNegatedPositivesAreNotGood(unittest.TestCase):
    """이번 결함 그대로 — 부정문이 '양호'로 읽히지 않는다."""

    CASES = (
        ("sleep", "잘 자지 못해요"),
        ("sleep", "잘 못 잤어요"),
        ("sleep", "통잠을 못 자요"),
        ("meal", "잘 챙겨 먹지 못해요"),
        ("meal", "밥을 잘 못 챙겨 먹어요"),
        ("mood", "하나도 안 좋아요"),
        ("mood", "기분이 안 좋아요"),
        ("mood", "요즘 영 좋지 않아요"),
        ("pain", "안 괜찮아요"),
        ("pain", "괜찮지 않아요"),
    )

    def test_negated_answers_score_as_shortfall(self):
        for key, text in self.CASES:
            with self.subTest(key=key, answer=text):
                self.assertLessEqual(W.score_one(key, text), W.NEGATED_SCORE)

    def test_the_label_follows_the_score(self):
        """점수만 내리고 라벨이 '양호'로 남으면 담당자가 거꾸로 읽는다."""
        d = W.score_answers({"sleep": "잘 자지 못해요"})["dimensions"]
        self.assertEqual(d["sleep"]["score"], W.NEGATED_SCORE)
        self.assertEqual(d["sleep"]["label"], W._LABEL["sleep"][W.NEGATED_SCORE])

    def test_the_reason_is_carried_in_the_dimensions(self):
        """근거가 남아야 담당자가 검증할 수 있다(부가 키 `negated`)."""
        d = W.score_answers({"sleep": "잘 자지 못해요", "pain": "아픈 데 없어요"})
        self.assertTrue(d["dimensions"]["sleep"]["negated"])
        self.assertFalse(d["dimensions"]["pain"]["negated"])
        for k in W.QUESTION_KEYS:               # 칸은 항상 있다(모양이 흔들리지 않게)
            self.assertIn("negated", d["dimensions"][k])

    def test_all_four_negated_raises_the_risk(self):
        """네 항목 모두 부정이면 high — 예전에는 low 로 나갔다."""
        before = {"mood": "하나도 안 좋아요", "meal": "잘 챙겨 먹지 못해요",
                  "sleep": "잘 자지 못해요", "pain": "안 괜찮아요"}
        r = W.score_answers(before)
        self.assertEqual(r["risk_level"], "high")
        self.assertLessEqual(r["mood_score"], 2)


class TestNoOverReaction(unittest.TestCase):
    """과차단도 결함이다 — 멀쩡한 답변을 '부족'으로 올리면 알림이 무의미해진다."""

    GOOD = (("mood", "그냥 좋아요, 오늘 산책도 했어요", 4),
            ("mood", "아주 좋아요", 5),
            ("mood", "편안해요", 4),
            ("meal", "세 끼 잘 챙겨 먹었어요", 5),
            ("meal", "잘 먹어요", 4),
            ("sleep", "푹 잘 잤어요", 5),
            ("sleep", "잘 잤어요", 4),
            ("pain", "아픈 데 없어요", 5),
            ("pain", "아무렇지 않아요", 5),
            ("pain", "괜찮아요", 4))

    def test_plain_positive_answers_keep_their_score(self):
        for key, text, want in self.GOOD:
            with self.subTest(key=key, answer=text):
                self.assertEqual(W.score_one(key, text), want)

    def test_positive_idioms_that_use_a_negation_form(self):
        """「아무렇지 않아요」는 '괜찮다'는 뜻이다 — `지 않`만 보면 뒤집힌다."""
        self.assertFalse(W._negated("아무렇지 않아요"))
        self.assertFalse(W._negated("아무렇지도 않아요"))
        self.assertFalse(W._negated("아픈 데 없어요"))
        self.assertFalse(W._negated("편안해요"))     # '편**안**' 은 부정이 아니다
        self.assertTrue(W._negated("잘 자지 못해요"))

    def test_the_demo_profiles_are_unchanged(self):
        """시연 대본의 판정이 흔들리면 심사 중 설명이 달라진다."""
        self.assertEqual(W.score_answers(W.PROFILES["ok"])["risk_level"], "low")
        self.assertEqual(W.score_answers(W.PROFILES["risk"])["risk_level"], "high")
        self.assertIn(W.score_answers(W.PROFILES["watch"])["risk_level"],
                      ("mid", "high"))
        self.assertGreaterEqual(W.score_answers(W.PROFILES["ok"])["mood_score"], 4)
        self.assertEqual(W.score_answers(W.PROFILES["ok"])["flags"], [])

    def test_low_score_rules_are_untouched(self):
        """낮은 점수 규칙은 손대지 않았다 — 위험 쪽 판정이 약해지면 안 된다."""
        self.assertEqual(W.score_one("mood", "그냥 죽고 싶어요"), 1)
        self.assertEqual(W.score_one("meal", "며칠째 제대로 못 먹었어요"), 1)
        self.assertEqual(W.score_one("sleep", "밤새 한숨도 못 잤어요"), 1)
        self.assertEqual(W.score_one("pain", "어지러워서 잘 못 움직이겠어요"), 1)
        self.assertEqual(W.score_one("mood", "그럭저럭이요, 좀 심심해요"), 3)

    def test_unmatched_answers_are_still_neutral(self):
        """규칙에 없는 말은 3(보통) — 부정 표현이 있어도 억지로 내리지 않는다."""
        self.assertEqual(W.score_one("mood", "글쎄요 뭐라 할까"), 3)
        self.assertEqual(W.score_one("mood", "말하고 싶지 않아요"), 3)
        self.assertEqual(W.score_one("pain", "안 아파요"), 3)


class TestDeterminismAndPayload(unittest.TestCase):

    def test_same_answer_same_score(self):
        a = W.score_answers({"sleep": "잘 자지 못해요"})
        b = W.score_answers({"sleep": "잘 자지 못해요"})
        self.assertEqual(a, b)

    def test_score_one_still_returns_a_plain_int(self):
        """`score_one` 은 공개 모양이다 — 호출부가 (점수, 부정) 튜플을 받으면 깨진다."""
        self.assertIsInstance(W.score_one("mood", "좋아요"), int)
        self.assertEqual(W.score_detail("mood", "좋아요"), (4, False))

    def test_payload_keeps_its_shape(self):
        p = W.build_payload("SR-33", answered=True,
                            answers={"sleep": "잘 자지 못해요"})
        self.assertEqual(p["schema"], W.SCHEMA)
        self.assertTrue(1 <= p["mood_score"] <= 5)
        self.assertIn(p["risk_level"], ("low", "mid", "high"))
        self.assertIn("수면", p["transcript_summary"])
        # 원문 발화는 페이로드에 실리지 않는다(판정 라벨만).
        self.assertNotIn("못해요", p["transcript_summary"])

    def test_no_answer_payload_is_untouched(self):
        p = W.build_payload("SR-33", answered=False)
        self.assertIsNone(p["mood_score"])
        self.assertEqual(p["risk_level"], "unknown")
        self.assertEqual(p["dimensions"], {})


if __name__ == "__main__":
    unittest.main()
