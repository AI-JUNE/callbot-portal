# -*- coding: utf-8 -*-
"""이음 3R 현장 음성 요청 — **분류 입력이 잘려 있었다**(35차). 네트워크 미사용.

결함
  `build_payload` 는 요약을 먼저 만들고(`clean_summary` = 가림 + **500자 절단**)
  그 **잘린 글**로 분류했다. 라우트는 transcript 를 4000자까지 받는다 —
  최대 3500자가 분류에서 통째로 빠져 있었다.
  음성 요청은 용건이 뒤에 오는 일이 흔하다(상황 설명 → 부탁). 그래서
  「…(앞말 500자)… 어르신이 쓰러지셨어요」가 `other` 로 접수됐다.
  34차가 고친 결함(오탐으로 **긴급 과대보고**)과 방향이 반대고, 이쪽은
  **놓치는 쪽**이라 더 무겁다 — 담당자에게 알림 자체가 가지 않는다.

고친 것
  분류는 `clean_text`(자르지 않은 가림 전문)를 본다. 이음에 보내는 `summary`
  는 그대로 500자다(담당자가 읽는 글을 바꾸지 않는다 — 34차 규약).
  요약 밖에서 걸린 근거는 숨기지 않고 `classification.evidence_outside_summary`
  로 드러내고, 잘렸다는 사실은 `truncated` 로 말한다.

여기서 고정하는 것
  1) 500자 뒤의 긴급 신호가 긴급으로 분류된다(4000자 경계 포함).
  2) 이음 본문(`payload`)의 키·요약 길이·요약 내용은 **변하지 않는다**.
  3) 오탐 가림(34차)은 전문에도 그대로 걸린다 — 과대보고가 되살아나지 않는다.
  4) 개인정보 가림은 전문·요약 양쪽에 걸린다(분류 입력으로 원문이 새지 않는다).
  5) 라우트(`POST /api/wellbeing/request`)까지 같은 결과다.

실행: python3 -m pytest tests/test_eum_request_truncation.py -q
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _eum_request as er      # noqa: E402

FILLER = "오늘 활동 잘 마쳤고 특별한 일은 없었습니다 "   # 긴급·유형 낱말이 없는 말


def long_text(tail, chars=1200):
    """앞을 `chars` 자 넘게 채우고 뒤에 용건을 붙인다(요약 500자 밖으로 민다)."""
    head = FILLER * (chars // len(FILLER) + 1)
    return head + tail


# ==========================================================================
# 1) 결함 그대로 — 잘린 뒤쪽의 긴급 신호
# ==========================================================================
class TailSignal(unittest.TestCase):

    def test_긴급이_500자_뒤에_있어도_긴급이다(self):
        p, c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요 119 불러야 할까요"))
        self.assertEqual(c["label"], "emergency")
        self.assertEqual(p["kind_label"], "emergency")

    def test_4000자_끝에_있어도_분류된다(self):
        # 라우트가 받는 최대 길이. 마지막 한 어절까지 분류가 본다.
        p, c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요", chars=3900))
        self.assertEqual(c["label"], "emergency")

    def test_다른_유형도_뒤에서_걸린다(self):
        for tail, label in (("내일 일정을 바꾸고 싶어요", "schedule_change"),
                            ("할머니가 식사를 안 하세요", "senior_concern"),
                            ("현관 문이 잠겨 있어요", "onsite_issue")):
            with self.subTest(tail=tail):
                _p, c = er.build_payload("u-par-1", long_text(tail))
                self.assertEqual(c["label"], label)

    def test_용건이_없으면_여전히_기타다(self):
        # 과차단 확인 — 길다고 아무 유형이나 붙지 않는다.
        _p, c = er.build_payload("u-par-1", long_text("그냥 안부 전해 주세요"))
        self.assertEqual(c["label"], "other")

    def test_앞쪽에_있던_긴급은_그대로다(self):
        _p, c = er.build_payload("u-par-1", "어르신이 쓰러지셨어요 " + FILLER * 40)
        self.assertEqual(c["label"], "emergency")
        self.assertEqual(c["evidence_outside_summary"], [])


# ==========================================================================
# 2) 이음에 보내는 본문은 변하지 않는다
# ==========================================================================
class PayloadUnchanged(unittest.TestCase):

    def test_본문_키는_이음_규약_4개뿐이다(self):
        p, _c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요"), "r1")
        self.assertEqual(sorted(p), ["kind_label", "participant_id", "ref", "summary"])

    def test_요약은_여전히_500자에서_잘린다(self):
        p, c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요"))
        self.assertEqual(len(p["summary"]), er.SUMMARY_MAX)
        self.assertTrue(c["truncated"])

    def test_요약_내용은_앞에서_500자_그대로다(self):
        text = long_text("어르신이 쓰러지셨어요")
        p, _c = er.build_payload("u-par-1", text)
        self.assertEqual(p["summary"], er.clean_text(text)[:er.SUMMARY_MAX])

    def test_짧은_글은_truncated_가_거짓이다(self):
        _p, c = er.build_payload("u-par-1", "내일 늦을 것 같아요")
        self.assertFalse(c["truncated"])
        self.assertEqual(c["evidence_outside_summary"], [])

    def test_요약_밖_근거를_숨기지_않는다(self):
        _p, c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요"))
        self.assertEqual(c["evidence_outside_summary"], ["쓰러"])
        self.assertTrue(set(c["evidence_outside_summary"]) <= set(c["evidence"]))

    def test_근거가_요약_안에_있으면_빈_목록이다(self):
        _p, c = er.build_payload("u-par-1", "어르신이 쓰러지셨어요")
        self.assertEqual(c["evidence_outside_summary"], [])

    def test_분류_결과_칸이_모두_있다(self):
        _p, c = er.build_payload("u-par-1", "내일 늦을 것 같아요")
        self.assertEqual(sorted(c), ["evidence", "evidence_outside_summary",
                                     "label", "label_ko", "truncated"])


# ==========================================================================
# 3) 34차 오탐 가림이 전문에도 그대로 걸린다(과대보고 복귀 방지)
# ==========================================================================
class DecoysStillMasked(unittest.TestCase):

    def test_뒤쪽_커피는_여전히_출혈이_아니다(self):
        _p, c = er.build_payload("u-par-1", long_text("어르신이 커피를 드시고 싶다고 하세요"))
        self.assertNotEqual(c["label"], "emergency")

    def test_뒤쪽_무사고도_사고가_아니다(self):
        _p, c = er.build_payload("u-par-1", long_text("오늘은 무사고로 잘 끝났습니다"))
        self.assertEqual(c["label"], "other")

    def test_뒤쪽_코피는_과차단되지_않는다(self):
        _p, c = er.build_payload("u-par-1", long_text("어르신이 코피가 계속 나세요"))
        self.assertEqual(c["label"], "emergency")

    def test_가림은_요약_밖_판정에도_같은_눈이다(self):
        # 요약 안의 「커피」가 가려진 뒤에도 근거 비교가 어긋나지 않는다.
        _p, c = er.build_payload("u-par-1", "커피 드시다가 " + FILLER * 40 + " 쓰러지셨어요")
        self.assertEqual(c["label"], "emergency")
        self.assertEqual(c["evidence_outside_summary"], ["쓰러"])


# ==========================================================================
# 4) 개인정보 — 분류 입력이 길어졌다고 원문이 새지 않는다
# ==========================================================================
class Privacy(unittest.TestCase):

    def test_뒤쪽_연락처도_가려진_채_분류된다(self):
        text = long_text("제 번호 010-1234-5678 로 연락 주세요 일정 변경이요")
        p, c = er.build_payload("u-par-1", text)
        self.assertEqual(c["label"], "schedule_change")
        self.assertNotIn("1234-5678", er.clean_text(text))
        self.assertNotIn("1234-5678", json.dumps(p, ensure_ascii=False))

    def test_clean_text_는_clean_summary_의_자르지_않은_판이다(self):
        text = long_text("일정 변경")
        self.assertEqual(er.clean_summary(text), er.clean_text(text)[:er.SUMMARY_MAX])
        self.assertGreater(len(er.clean_text(text)), er.SUMMARY_MAX)

    def test_분류_근거에는_원문이_실리지_않는다(self):
        _p, c = er.build_payload("u-par-1", long_text("어르신이 쓰러지셨어요"))
        blob = json.dumps(c, ensure_ascii=False)
        self.assertNotIn(FILLER.strip(), blob)


# ==========================================================================
# 5) 라우트까지 같은 결과
# ==========================================================================
class Route(unittest.TestCase):

    def setUp(self):
        import _ratelimit
        _ratelimit.reset()
        from test_wellbeing import call
        self.call = call
        self._env = mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "", "CALLBOT_API_KEY": "",
                                                 "CALLBOT_STRICT": "", "CPAAS_LIVE": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_긴_transcript_의_뒤쪽_긴급이_접수된다(self):
        res = self.call("POST", {"participant_id": "u-par-1",
                                 "transcript": long_text("어르신이 쓰러지셨어요")},
                        path="/api/wellbeing/request")
        self.assertEqual(res.status, 200)
        body = res.body()
        self.assertEqual(body["payload"]["kind_label"], "emergency")
        self.assertTrue(body["classification"]["truncated"])
        self.assertEqual(body["classification"]["evidence_outside_summary"], ["쓰러"])

    def test_4000자_초과는_여전히_400이다(self):
        res = self.call("POST", {"participant_id": "u-par-1", "transcript": "가" * 4001},
                        path="/api/wellbeing/request")
        self.assertEqual(res.status, 400)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
