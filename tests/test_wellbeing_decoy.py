# -*- coding: utf-8 -*-
"""안부 판정의 오탐 회귀 (38차) — 네트워크 미사용.

왜 이 파일이 필요한가
  34차는 이음 요청 분류(`_eum_request`)에서 **부분 일치 오탐**을 고쳤다 —
  「커**피를** 드시고 싶다고」가 `피를`(출혈)로 걸려 긴급으로 올라갔다.
  그런데 **같은 낱말표가 이 파일에도 있었다.** `wellbeing._RULES["pain"]` 의
  `피가` 는 그대로 남아, 38차까지 이렇게 나갔다:

      pain  「커**피가** 식었어요」        → 1점(거동 곤란) · risk high
      meal  「**며칠째** 잘 챙겨 먹어요」  → 1점(식사 거의 못 함) + starving · high
      mood  「**며칠째** 기분이 좋아요」   → starving · high

  그리고 33차 부정 처리는 **긍정 규칙(4·5점)에만** 걸려 있었기 때문에,
  어르신이 **아니라고 말한 증상**이 그대로 위험으로 보고됐다:

      pain  「어지럽지 않아요」   → 2점 + dizzy    · high
      pain  「숨이 차지 않아요」  → 1점(거동 곤란) · high
      meal  「굶지 않았어요」     → 1점 + starving · high

  놓치는 쪽만 위험한 것이 아니다(34차와 같은 판단) — 안부 전화가 멀쩡한
  어르신을 `high` 로 올리면 담당자는 이 채널의 `high` 를 믿지 않게 되고,
  그러면 **진짜 high 를 놓친다**. 이음은 `risk_level=high` 로 follow_up 을
  만들기 때문에 과대보고의 대가를 사람이 치른다.

고친 범위는 좁다
  (1) `_DECOY` — 오탐 바깥 낱말만 판정 직전에 가린다(커피·열이틀).
      「한숨」은 **넣지 않았다** — 이 파일의 `_RULES["sleep"]` 은
      「한숨도 못」을 제대로 쓴다(남의 오탐 목록을 베끼면 멀쩡한 규칙이 죽는다).
  (2) `_DENIAL` — 증상 낱말에 **부정이 바로 붙은 어구**만, 그 어구만큼만 가린다.
      답변 전체에 부정 판정을 걸지 않는다 — 「어지러워서 움직이지 못해요」가
      지워지면 그게 더 큰 결함이다.
  (3) 자해 암시(`CRITICAL_NO_DENIAL`)는 부정문이라도 올린다 —
      과소보고는 되돌릴 수 없고 과대보고는 담당자가 한 번 더 확인하면 끝난다.

실행: python3 -m pytest tests/test_wellbeing_decoy.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import wellbeing as W  # noqa: E402


class TestDecoyWordsNoLongerScoreRisk(unittest.TestCase):
    """이번 결함 그대로 — 다른 낱말 안에서 걸리지 않는다."""

    def test_coffee_is_not_bleeding(self):
        """「커피가」 안의 `피가` — 결함 당시 pain 1(거동 곤란)."""
        for text in ("커피가 식어서 다시 데웠어요", "커피를 한 잔 마셨어요",
                     "아침에 커피가 당겨요"):
            self.assertEqual(W.score_detail("pain", text)[0], 3, text)

    def test_several_days_alone_is_not_starving(self):
        """「며칠째」는 기간 부사다 — 먹는 말과 붙어야 결식이다."""
        self.assertEqual(W.score_detail("meal", "며칠째 잘 챙겨 먹고 있어요")[0], 5)
        self.assertEqual(W.score_detail("mood", "며칠째 기분이 좋아요")[0], 4)
        self.assertEqual(W.score_detail("sleep", "며칠째 푹 잘 자요")[0], 5)

    def test_fever_decoy_is_not_fever(self):
        """「열이틀」 안의 `열이` — 결함 당시 pain 2(발열)."""
        self.assertEqual(W.score_detail("pain", "열이틀 전에 아팠어요")[0], 3)

    def test_decoy_table_does_not_borrow_sigh(self):
        """「한숨」은 이 파일의 정상 규칙이다 — 가리면 수면 1점이 죽는다."""
        self.assertNotIn("한숨", W._DECOY)
        self.assertEqual(W.score_detail("sleep", "한숨도 못 잤어요")[0], 1)
        self.assertEqual(W.score_detail("sleep", "밤새 한숨도 못 잤어요")[0], 1)


class TestDeniedSymptomsAreNotRisk(unittest.TestCase):
    """「…지 않아요」로 **없다고 말한** 증상은 위험이 아니다."""

    CASES = (
        ("pain", "어지럽지 않아요"),
        ("pain", "어지럽진 않아요"),
        ("pain", "안 어지러워요"),
        ("pain", "어지럼증은 없어요"),
        ("pain", "숨이 차지 않아요"),
        ("pain", "숨 안 차요"),
        ("pain", "피가 나지 않아요"),
        ("pain", "가슴이 답답하지 않아요"),
        ("pain", "열 안 나요"),
        ("pain", "열은 없어요"),
        ("pain", "쓰러지지 않았어요"),
        ("pain", "못 움직이는 건 아니에요"),
        ("meal", "굶지 않았어요"),
        ("meal", "못 먹지 않았어요"),
    )

    def test_denied_symptom_is_neutral(self):
        for key, text in self.CASES:
            with self.subTest(text=text):
                score, _neg = W.score_detail(key, text)
                self.assertEqual(score, 3, "%s -> %d" % (text, score))

    def test_denied_symptom_raises_no_flag(self):
        base = {"mood": "아주 좋아요", "meal": "세 끼 잘 챙겨 먹었어요",
                "sleep": "푹 잘 잤어요", "pain": "아픈 데 없어요"}
        for key, text in self.CASES:
            with self.subTest(text=text):
                ans = dict(base)
                ans[key] = text
                out = W.score_answers(ans)
                self.assertEqual(out["flags"], [], "%s -> %s" % (text, out["flags"]))
                self.assertEqual(out["risk_level"], W.RISK_LOW, text)

    def test_denial_does_not_flip_a_positive_in_the_same_answer(self):
        """「어지럽지 않고 잘 자요」 — 부정은 가려진 증상 낱말에 붙은 것이었다."""
        score, neg = W.score_detail("sleep", "어지럽지 않고 잘 자요")
        self.assertEqual((score, neg), (4, False))


class TestRealRiskStillRises(unittest.TestCase):
    """가림이 **위험을 지우지 않는다** — 이쪽이 깨지면 고친 것이 아니다."""

    def test_real_symptoms_keep_their_scores(self):
        cases = (
            ("pain", "어지러워서 잘 못 움직이겠어요", 1),
            ("pain", "커피 드시다 쓰러지셨어요", 1),
            ("pain", "숨이 차요", 1),
            ("pain", "어지러워요", 2),
            ("pain", "열이 나요", 2),
            ("meal", "며칠째 제대로 못 먹었어요", 1),
            ("meal", "며칠째 굶었어요", 1),
            ("meal", "밥을 못 먹었어요", 1),
            ("mood", "죽고 싶어요", 1),
            ("sleep", "밤새 한숨도 못 잤어요", 1),
        )
        for key, text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(W.score_detail(key, text)[0], want, text)

    def test_risk_profile_script_unchanged(self):
        """시연 대본 `risk` 는 그대로 high · 세 신호 전부."""
        out = W.score_answers(W.PROFILES["risk"])
        self.assertEqual(out["risk_level"], W.RISK_HIGH)
        self.assertEqual(sorted(out["flags"]), ["dizzy", "immobile", "starving"])
        self.assertEqual(out["mood_score"], 1)

    def test_ok_and_watch_profiles_unchanged(self):
        ok = W.score_answers(W.PROFILES["ok"])
        self.assertEqual((ok["risk_level"], ok["flags"]), (W.RISK_LOW, []))
        watch = W.score_answers(W.PROFILES["watch"])
        self.assertEqual(watch["flags"], [])

    def test_symptom_in_another_answer_still_flags(self):
        """어르신은 통증을 기분 답변에 섞어 말한다 — 문항별로 쪼개지 않았다."""
        out = W.score_answers({"mood": "못 움직여서 답답해요", "meal": "세 끼 잘 먹어요",
                               "sleep": "푹 잘 잤어요", "pain": "아픈 데 없어요"})
        self.assertIn("immobile", out["flags"])
        self.assertEqual(out["risk_level"], W.RISK_HIGH)


class TestSelfHarmIgnoresDenial(unittest.TestCase):
    """자해 암시는 부정문이라도 올린다 — 과소보고는 되돌릴 수 없다."""

    def test_denied_self_harm_still_flags(self):
        out = W.score_answers({"mood": "죽고 싶지 않아요", "meal": "세 끼 잘 먹어요",
                               "sleep": "푹 잘 잤어요", "pain": "아픈 데 없어요"})
        self.assertIn("self_harm", out["flags"])
        self.assertEqual(out["risk_level"], W.RISK_HIGH)

    def test_registry_names_the_exemption(self):
        self.assertEqual(W.CRITICAL_NO_DENIAL, ("self_harm",))
        for word in W._CRITICAL["self_harm"]:
            self.assertNotIn(word, W._DENIAL.pattern)


class TestJudgeTextIsJudgementOnly(unittest.TestCase):
    """가림은 **판정용**이다 — 담당자가 읽는 글을 바꾸지 않는다."""

    def test_summary_is_built_from_labels_not_masked_text(self):
        out = W.build_payload("sr-1", answers={
            "mood": "아주 좋아요", "meal": "세 끼 잘 챙겨 먹었어요",
            "sleep": "푹 잘 잤어요", "pain": "커피가 식었어요"})
        self.assertNotIn("커피", out["transcript_summary"])
        self.assertNotIn("  ", out["transcript_summary"])   # 공백 가림이 새지 않는다
        for dim in out["dimensions"].values():
            self.assertIn(dim["label"], sum((list(v.values()) for v in W._LABEL.values()), []))

    def test_judge_text_never_raises(self):
        for bad in (None, "", "   ", "커피", "x" * 5000):
            self.assertIsInstance(W.judge_text(bad), str)

    def test_masking_preserves_length(self):
        """길이를 보존해 가린다 — 가림이 새 낱말을 만들지 않는다."""
        for text in ("커피가 식었어요", "어지럽지 않아요", "열이틀 전"):
            self.assertEqual(len(W.judge_text(text)), len(text), text)

    def test_empty_after_masking_is_normal_not_risk(self):
        self.assertEqual(W.score_detail("pain", "커피")[0], 3)


class TestReleaseGateCrossFileDecoys(unittest.TestCase):
    """게이트 15단계 (d) — 한 파일에서 밝혀진 오탐은 전부에 적용된다."""

    def setUp(self):
        import verify
        self.verify = verify

    def test_repo_passes(self):
        ok, msg = self.verify.check_keyword_tables(self.verify.api_sources())
        self.assertTrue(ok, msg)
        self.assertIn("오탐 기록부", msg)

    def test_fragment_without_mask_fails(self):
        """`피가` 를 쓰면서 「커피」를 가리지 않는 파일은 실패시킨다(38차 결함)."""
        srcs = {"fake.py": "_RULES = {\"pain\": [(1, [\"피가\", \"쓰러\", \"못 움직\"])]}\n"}
        ok, msg = self.verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("커피", msg)

    def test_word_containing_the_host_is_not_flagged(self):
        """「한숨도 못」은 바깥 낱말을 통째로 품었다 — 일부러 그 말을 본다."""
        self.assertEqual(self.verify._decoy_hosts_for("한숨도 못"), [])
        self.assertEqual(self.verify._decoy_hosts_for("커피"), [])
        self.assertIn("커피", self.verify._decoy_hosts_for("피가"))
        self.assertIn("무사고", self.verify._decoy_hosts_for("사고"))
        self.assertIn("돈가스", self.verify._decoy_hosts_for("돈"))

    def test_wellbeing_decoy_is_registered(self):
        self.assertEqual(
            self.verify.KEYWORD_TABLES[("wellbeing.py", "_DECOY")][0], "mask")
        for symbol in ("_DECOY", "_DENIAL", "def judge_text"):
            self.assertIn(symbol, self.verify.KEYWORD_MASKS["wellbeing.py"])

    def test_mask_wiring_is_code_not_comment(self):
        """등록부만 남고 배선이 사라지면 실패한다."""
        srcs = dict(self.verify.api_sources())
        srcs["wellbeing.py"] = srcs["wellbeing.py"].replace("def judge_text", "def _gone")
        ok, msg = self.verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("judge_text", msg)


if __name__ == "__main__":
    unittest.main()
