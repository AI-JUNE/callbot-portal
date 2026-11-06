# -*- coding: utf-8 -*-
"""발화 오탐 기록부 — 44차 회귀.

의존성 0 · 네트워크 미사용.

무엇을 고정하는가
  1) `_escalation` 민감 주제 — 공백을 **전부** 지워 원문에 없던 낱말을 만들던
     결함(「주문 방법 적어 주세요」→`법적`·「주소 송파구」→`소송`·
     「전화하고 소식이」→`고소`)과 낱말 안에서 걸리던 결함(「합법적」·「고소한
     맛」·「고소득」·「고장 신고」).
  2) 좁힌 범위 — 상담원 요청 표는 **그대로** 공백을 지운 글을 본다(두 낱말에
     걸치는 꼴을 일부러 쓰고, 걸리면 사람에게 넘기는 안전한 방향이다).
  3) `_eum_request` — 38차가 `wellbeing` 에 세운 「증상이 없다는 말」 가림이
     이 파일에는 오지 않아 「숨이 차지 않아요」가 **긴급 연결**로 접수됐다.
  4) 과차단 금지 — 진짜 신호는 그대로 경보다(「고소하겠습니다」·「코피가
     멈추지 않아요」·「사고는 없었는데 어르신이 쓰러지셨어요」).
  5) 가림은 **판정 입력에만** — 티켓 사유·이음 요약 등 사람이 읽는 글은 불변.
  6) 릴리스 게이트 23단계가 **고치기 전 모양을 실제로 잡는다**.

실행: python -m pytest tests/test_utterance_decoys.py -q
"""
import os
import re
import socket
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "api"), os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _escalation as escalation    # noqa: E402
import _eum_request as eum          # noqa: E402
import wellbeing as W               # noqa: E402
import verify                       # noqa: E402


def _no_net(*a, **k):
    raise AssertionError("network call attempted")


class Base(unittest.TestCase):
    def setUp(self):
        self._urlopen, self._conn = urllib.request.urlopen, socket.create_connection
        urllib.request.urlopen = _no_net
        socket.create_connection = _no_net

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        socket.create_connection = self._conn


# ==========================================================================
# 1) _escalation — 민감 주제 오탐
# ==========================================================================
class TestEscalationDecoys(Base):
    def setUp(self):
        Base.setUp(self)
        self.pol = escalation.EscalationPolicy()

    # 공백을 전부 지워서 **만들어진** 일치
    CROSS = [
        ("주문 방법 적어서 보내 주세요", "법적"),
        ("그 방법 적용해 주세요", "법적"),
        ("주소 송파구 123번지예요", "소송"),
        ("취소 송금해 주세요", "소송"),
        ("주문 취소 송장은요", "소송"),
        ("그거 말고 소포로 보내 주세요", "고소"),
        ("전화하고 소식이 없어요", "고소"),
        ("최고 소식이네요", "고소"),
    ]
    # 낱말 **안에서** 걸린 일치
    INSIDE = [
        ("그거 합법적인가요", "법적"),
        ("불법적인 건 아니죠", "법적"),
        ("이 과자 고소하고 맛있어요", "고소"),
        ("고소한 참기름이요", "고소"),
        ("고소득자라서요", "고소"),
        ("고장 신고할게요", "신고할"),
        ("분실 신고할게요", "신고할"),
    ]

    def test_cross_word_matches_are_not_sensitive(self):
        for text, kw in self.CROSS:
            with self.subTest(text=text):
                self.assertIsNone(self.pol.evaluate(text),
                                  "「%s」 가 `%s` 로 읽혔다" % (text, kw))

    def test_word_internal_matches_are_not_sensitive(self):
        for text, kw in self.INSIDE:
            with self.subTest(text=text):
                self.assertIsNone(self.pol.evaluate(text),
                                  "「%s」 가 `%s` 로 읽혔다" % (text, kw))

    def test_real_sensitive_signals_survive(self):
        for text in ["법적으로 대응하겠습니다", "소송할 거예요", "고소하겠습니다",
                     "고소장 접수했어요", "언론에 제보할 거예요",
                     "금감원에 알리겠습니다", "소비자원에 신고할게요",
                     "불법 행위라 법적 조치하겠습니다"]:
            with self.subTest(text=text):
                hit = self.pol.evaluate(text)
                self.assertIsNotNone(hit, text)
                self.assertEqual(hit["reason"], "sensitive", text)

    def test_split_syllables_are_still_caught(self):
        """전사가 낱말을 글자로 쪼개도 민감 주제는 잡아야 한다."""
        self.assertEqual(self.pol.evaluate("소 송 할 거예요")["reason"], "sensitive")
        self.assertEqual(self.pol.evaluate("법 적 대응")["reason"], "sensitive")

    def test_squash_joins_only_single_characters(self):
        self.assertEqual(escalation._squash("상 담 사 바꿔주세요"), "상담사 바꿔주세요")
        self.assertEqual(escalation._squash("사 람 이 랑 얘기"), "사람이랑 얘기")
        self.assertEqual(escalation._squash("상담 사"), "상담사")
        self.assertEqual(escalation._squash("방법 적어서"), "방법 적어서")
        self.assertEqual(escalation._squash("주소 송파구"), "주소 송파구")
        self.assertEqual(escalation._squash(""), "")
        self.assertEqual(escalation._squash(None), "")
        self.assertEqual(escalation._squash("   "), "")

    def test_decoys_are_masked_across_spacing(self):
        """전사는 「고장 신고」를 「고장신고」로도 적는다."""
        for text in ("고장 신고할게요", "고장신고할게요", "고장  신고할게요"):
            with self.subTest(text=text):
                self.assertNotIn("신고할", escalation.sensitive_text(text))

    def test_masking_preserves_length(self):
        """길이를 보존해 가림이 다른 경계를 새로 만들지 않게 한다."""
        s = escalation._mask_decoys("고소한 참기름")
        self.assertEqual(len(s), len("고소한 참기름"))

    def test_handoff_table_still_spans_two_words(self):
        """좁힌 것은 민감 주제뿐 — 상담원 요청은 종전대로 넓게 본다."""
        for text in ["상담사 바꿔 주세요", "그냥 사람이랑 얘기할게요", "직원 연결해줘",
                     "책임자 나오라고 해", "진짜 사람 좀 바꿔요", "상 담 사 바꿔주세요",
                     "사 람 이 랑 얘기", "사람 연결해 주세요", "사람 바꿔 주세요"]:
            with self.subTest(text=text):
                hit = self.pol.evaluate(text)
                self.assertIsNotNone(hit, text)
                self.assertEqual(hit["reason"], "request", text)

    def test_sensitive_still_outranks_request(self):
        hit = self.pol.evaluate("상담원 불러요, 소송할 겁니다")
        self.assertEqual(hit["reason"], "sensitive")
        self.assertIn("소송", hit["detail"])

    def test_other_triggers_are_untouched(self):
        self.assertEqual(self.pol.evaluate("주문 조회요", confidence=0.1)["reason"],
                         "low_confidence")
        self.assertEqual(self.pol.evaluate("음...", confidence=0.9,
                                           fallback_streak=99)["reason"],
                         "repeated_fallback")
        self.assertIsNone(self.pol.evaluate("주문 조회요"))

    def test_the_ticket_text_is_not_masked(self):
        """가림은 판정 입력에만 — 장부에 남는 글은 손대지 않는다."""
        q = escalation.EscalationQueue()
        tk = q.enqueue("sess-dc", "request", "고객이 고소한 맛이라고 했습니다")
        self.assertIn("고소한 맛", tk["summary"])


# ==========================================================================
# 2) _eum_request — 「없다고 말한 증상」이 긴급으로 올라갔다
# ==========================================================================
class TestEumDenial(Base):
    QUIET = [
        ("어르신 숨이 차지 않으세요", "숨이"),
        ("숨은 안 차신다고 해요", "숨"),
        ("피가 나지 않아요", "피가"),
        ("피는 안 나요", "피"),
        ("쓰러지지는 않으셨어요", "쓰러"),
        ("다치지 않으셨어요", "다치"),
        ("다치신 데 없어요", "다치신"),
        ("넘어지지 않으셨어요", "넘어지"),
        ("사고는 없었어요", "사고"),
        ("사고 난 적 없어요", "사고"),
        ("응급실 갈 일은 없어요", "응급"),
    ]
    LOUD = ["어르신이 쓰러지셨어요", "숨이 차신다고 해요", "코피가 멈추지 않아요",
            "교통사고가 났어요", "119 불러야 할 것 같아요", "의식이 없으세요",
            "다치셨어요", "넘어지셨어요", "응급 상황이에요", "피를 많이 흘리셨어요"]

    def test_denied_symptoms_are_not_emergencies(self):
        for text, kw in self.QUIET:
            with self.subTest(text=text):
                label, hit = eum.classify(text)
                self.assertNotEqual(label, "emergency",
                                    "「%s」 가 `%s` 로 긴급이 됐다 (%r)"
                                    % (text, kw, hit))

    def test_real_emergencies_still_page(self):
        for text in self.LOUD:
            with self.subTest(text=text):
                self.assertEqual(eum.classify(text)[0], "emergency", text)

    def test_denial_masks_only_its_own_phrase(self):
        """답변 전체에 부정 판정을 걸지 않는다(38차 규약)."""
        label, hit = eum.classify("사고는 없었는데 어르신이 쓰러지셨어요")
        self.assertEqual(label, "emergency")
        self.assertIn("쓰러", hit)

    def test_masking_does_not_touch_the_summary_eum_reads(self):
        """이음 담당자가 읽는 글은 원문 그대로다(본문 4칸 불변)."""
        text = "어르신 숨이 차지 않으세요. 그래도 한번 봐 주세요"
        body, meta = eum.build_payload("p-1", text)
        self.assertIn("숨이 차지 않으세요", body["summary"])
        self.assertEqual(sorted(body), ["kind_label", "participant_id",
                                        "ref", "summary"])
        self.assertNotEqual(meta["label"], "emergency")

    def test_decoys_from_34th_round_still_masked(self):
        for text in ["어르신이 커피를 드시고 싶다고 하세요", "한숨을 길게 쉬세요",
                     "무사고로 끝났습니다", "돈가스 드셨어요"]:
            with self.subTest(text=text):
                self.assertNotEqual(eum.classify(text)[0], "emergency", text)

    def test_blank_and_non_string_inputs(self):
        for bad in ("", None, 7, {}):
            self.assertEqual(eum.classify(bad)[0], "other", bad)


# ==========================================================================
# 3) 세 판정기가 같은 기록부를 따른다 (38차 (d) 규약의 문장판)
# ==========================================================================
def _alarm(text):
    out = []
    hit = escalation.EscalationPolicy().evaluate(text) or {}
    if hit.get("reason") == "sensitive":
        out.append("escalation")
    if eum.classify(text)[0] == "emergency":
        out.append("eum")
    for slot in W.QUESTION_KEYS:
        ans = {k: (text if k == slot else "") for k in W.QUESTION_KEYS}
        if W.score_answers(ans)["risk_level"] == W.RISK_HIGH:
            out.append("wellbeing:" + slot)
            break
    return out


class TestAllThreeJudges(Base):
    def test_every_recorded_decoy_is_quiet_everywhere(self):
        for text in sorted(verify.UTTERANCE_DECOYS):
            with self.subTest(text=text):
                self.assertEqual(_alarm(text), [],
                                 "「%s」 — %s" % (text, verify.UTTERANCE_DECOYS[text]))

    def test_every_recorded_signal_still_alarms(self):
        for text in sorted(verify.UTTERANCE_REAL):
            with self.subTest(text=text):
                self.assertTrue(_alarm(text),
                                "「%s」 — %s" % (text, verify.UTTERANCE_REAL[text]))


# ==========================================================================
# 4) 릴리스 게이트 23단계 — 고치기 전 모양을 실제로 잡는가
# ==========================================================================
class _PreFixEscalation(object):
    """44차 **이전** 모양 — 공백을 전부 지우고 오탐을 가리지 않는다."""

    class EscalationPolicy(object):
        def evaluate(self, text="", confidence=1.0, fallback_streak=0):
            t = (text or "").replace(" ", "")
            for kw in escalation.SENSITIVE_KEYWORDS:
                if kw in t:
                    return {"reason": "sensitive", "detail": kw}
            return None


class _PreFixEum(object):
    """44차 **이전** 모양 — 「없다」는 말을 가리지 않는다."""

    @staticmethod
    def classify(text):
        t = eum._mask_decoys(re.sub(r"\s+", " ", str(text or "")))
        for label, words in eum._RULES:
            hit = [w for w in words if w in t]
            if hit:
                return label, hit
        return "other", []


class _MasksTooMuch(object):
    """가림이 넓어진 모양 — 「고소」를 통째로 지운다."""

    class EscalationPolicy(object):
        def evaluate(self, text="", confidence=1.0, fallback_streak=0):
            t = escalation.sensitive_text(text).replace("고소", "  ")
            for kw in escalation.SENSITIVE_KEYWORDS:
                if kw in t:
                    return {"reason": "sensitive", "detail": kw}
            return None


class TestGate(Base):
    def test_real_repository_passes(self):
        ok, msg = verify.check_utterance_decoys()
        self.assertTrue(ok, msg)

    def test_pre_fix_escalation_is_caught(self):
        ok, msg = verify.check_utterance_decoys(
            mods={"_escalation.py": _PreFixEscalation})
        self.assertFalse(ok)
        self.assertIn("오탐 경보", msg)
        self.assertIn("escalation", msg)

    def test_pre_fix_eum_is_caught(self):
        ok, msg = verify.check_utterance_decoys(mods={"_eum_request.py": _PreFixEum})
        self.assertFalse(ok)
        self.assertIn("오탐 경보", msg)
        self.assertIn("숨이 차지", msg)

    def test_over_masking_is_caught(self):
        ok, msg = verify.check_utterance_decoys(
            mods={"_escalation.py": _MasksTooMuch})
        self.assertFalse(ok)
        self.assertIn("과차단", msg)

    def test_unloadable_judge_is_caught(self):
        class Broken(object):
            pass

        ok, msg = verify.check_utterance_decoys(mods={"wellbeing.py": Broken})
        self.assertFalse(ok)
        self.assertIn("판정 중 예외", msg)

    def test_registry_is_wired_both_ways(self):
        for key, (fname, why) in verify.UTTERANCE_JUDGES.items():
            with self.subTest(judge=key):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, "api", fname)),
                                fname)
                self.assertTrue(why.strip(), "사유를 적을 것")
        self.assertEqual(set(verify.UTTERANCE_DECOYS) & set(verify.UTTERANCE_REAL),
                         set(), "같은 문장이 오탐과 진짜 신호 양쪽에 있다")

    def test_gate_step_is_wired(self):
        import contextlib
        import io as _io
        import json as _json
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
        names = [s["step"] for s in _json.loads(out)["steps"]]
        self.assertIn("utterance_decoys", names)


class EumDecoyRound45(unittest.TestCase):
    """45차 — `_eum_request` 긴급 낱말이 평범한 낱말·숫자열 안에서 걸렸다."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
        import _eum_request
        cls.er = _eum_request

    def test_평범한_말은_긴급이_아니다(self):
        for t in ("의식주 지원 물품을 받았어요", "사고력 놀이 활동을 했어요",
                  "사고방식이 바뀌신 것 같아요", "주소가 1190번지예요",
                  "잠깐 숨을 돌리고 계세요"):
            with self.subTest(t=t):
                self.assertNotEqual(self.er.classify(t)[0], "emergency")

    def test_진짜_신호는_그대로_긴급이다(self):
        for t in ("119 불렀어요", "119에 전화했어요", "의식이 없으세요",
                  "교통사고가 났어요", "숨을 못 쉬세요",
                  "의식주 지원 중 어르신이 쓰러지셨어요"):
            with self.subTest(t=t):
                self.assertEqual(self.er.classify(t)[0], "emergency")


if __name__ == "__main__":
    unittest.main(verbosity=2)
