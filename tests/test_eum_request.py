# -*- coding: utf-8 -*-
"""이음 3R 현장 음성 요청 다리(`api/_eum_request.py`) — 네트워크 미사용.

고정하는 것
  1) 분류는 규칙이다 — 같은 글이면 같은 유형, 긴급 낱말은 다른 유형보다 앞선다.
  2) 요약에 연락처 꼴이 남지 않는다(원문 발화를 그대로 보내지 않는다).
  3) 공유 비밀이 없거나 send 가 거짓이면 보내지 않는다(dry-run) — 실수로 이음에 쌓이지 않는다.
  4) 서명은 이음 `verifyChannelIntake` 와 같은 꼴이다(X-Channel-Id: phone, "<ts>.<body>").
  5) 허용 호스트 밖으로는 보내지 않는다.
  6) 라우트는 새 함수 파일 없이 wellbeing 의 하위 동작으로 붙어 있다(Hobby 함수 한도).

실행: python3 -m pytest tests/test_eum_request.py -q
"""
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _eum_request as er  # noqa: E402


class Classify(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(er.classify("내일 활동 시간을 바꾸고 싶어요")[0], "schedule_change")
        self.assertEqual(er.classify("할머니가 식사를 안 하세요")[0], "senior_concern")
        self.assertEqual(er.classify("현관 문이 잠겨 있어요")[0], "onsite_issue")
        self.assertEqual(er.classify("그냥 여쭤볼게요")[0], "other")

    def test_emergency_wins(self):
        label, ev = er.classify("일정 바꾸려다가 어르신이 쓰러지셨어요")
        self.assertEqual(label, "emergency")
        self.assertIn("쓰러", ev)

    def test_same_text_same_label(self):
        t = "어르신이 오늘 기운이 없으세요"
        self.assertEqual(er.classify(t), er.classify(t))

    def test_every_label_has_korean_name(self):
        for label in er.LABELS:
            self.assertIn(label, er.LABEL_KO)


class Payload(unittest.TestCase):
    def test_phone_is_masked(self):
        p, _ = er.build_payload("u-par-1", "제 번호 010-1234-5678 로 연락 주세요", "r1")
        self.assertNotIn("1234-5678", p["summary"])
        self.assertNotIn("01012345678", json.dumps(p, ensure_ascii=False))

    def test_summary_is_capped(self):
        p, _ = er.build_payload("u-par-1", "가" * 2000)
        self.assertLessEqual(len(p["summary"]), er.SUMMARY_MAX)

    def test_bad_participant_or_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            er.build_payload("홍길동 010", "일정 변경")
        with self.assertRaises(ValueError):
            er.build_payload("u-par-1", "   ")

    def test_ref_is_generated_when_missing(self):
        p, _ = er.build_payload("u-par-1", "일정 변경 부탁드려요")
        self.assertTrue(p["ref"].startswith("vr_"))


class Switch(unittest.TestCase):
    def test_no_secret_means_dry_run(self):
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": ""}):
            sent = []
            r = er.run("u-par-1", "내일 늦을 것 같아요", send=True,
                       deliver_fn=lambda u, pl: sent.append(1) or {"delivered": True})
        self.assertEqual(r["mode"], "dry-run")
        self.assertEqual(sent, [])

    def test_send_false_means_dry_run(self):
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "s" * 32}):
            r = er.run("u-par-1", "내일 늦을 것 같아요", send=False)
        self.assertEqual(r["mode"], "dry-run")

    def test_sends_to_default_eum_url(self):
        seen = []
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "s" * 32, "EUM_REQUEST_URL": "", "EUM_REQUEST_HOSTS": ""}):
            r = er.run("u-par-1", "어르신이 쓰러지셨어요", send=True,
                       deliver_fn=lambda u, pl: seen.append((u, pl)) or {"delivered": True, "status": 200})
        self.assertTrue(r["ok"])
        self.assertEqual(seen[0][0], er.DEFAULT_URL)
        self.assertEqual(seen[0][1]["kind_label"], "emergency")

    def test_host_outside_allowlist_is_not_sent(self):
        seen = []
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "s" * 32,
                                          "EUM_REQUEST_URL": "https://evil.example.com/x", "EUM_REQUEST_HOSTS": ""}):
            r = er.run("u-par-1", "일정 변경", send=True,
                       deliver_fn=lambda u, pl: seen.append(1) or {"delivered": True})
        self.assertFalse(r["ok"])
        self.assertEqual(seen, [])

    def test_failure_is_not_hidden(self):
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "s" * 32, "EUM_REQUEST_URL": ""}):
            r = er.run("u-par-1", "일정 변경", send=True,
                       deliver_fn=lambda u, pl: {"delivered": False, "status": 401, "error": "이음이 401 로 응답했습니다"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["delivery"]["status"], 401)


class Signature(unittest.TestCase):
    def test_matches_eum_channel_intake(self):
        body = b'{"participant_id":"u-par-1"}'
        ts, sig = er.sign(body, "k" * 16, 1700000000)
        expect = hmac.new(b"k" * 16, b"1700000000." + body, hashlib.sha256).hexdigest()
        self.assertEqual(sig, "sha256=" + expect)

    def test_channel_id_is_phone(self):
        self.assertEqual(er.CHANNEL_ID, "phone")


class Route(unittest.TestCase):
    """`POST /api/wellbeing/request` — 같은 소켓 없는 대역(test_wellbeing.call)으로 부른다."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _ratelimit
        _ratelimit.reset()
        from test_wellbeing import call
        self.call = call
        self._env = mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "", "CALLBOT_API_KEY": "",
                                                 "CALLBOT_STRICT": "", "CPAAS_LIVE": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_dry_run_returns_classified_payload(self):
        res = self.call("POST", {"participant_id": "u-par-1", "transcript": "내일 늦을 것 같아요"},
                        path="/api/wellbeing/request")
        self.assertEqual(res.status, 200)
        b = res.body()
        self.assertEqual(b["mode"], "dry-run")
        self.assertEqual(b["payload"]["kind_label"], "schedule_change")

    def test_missing_transcript_is_400(self):
        res = self.call("POST", {"participant_id": "u-par-1"}, path="/api/wellbeing/request")
        self.assertEqual(res.status, 400)

    def test_live_switch_does_not_block_request(self):
        """전화를 걸지 않는 동작이라 실발신 차단(501)과 무관하다."""
        with mock.patch.dict(os.environ, {"CPAAS_LIVE": "1"}):
            res = self.call("POST", {"participant_id": "u-par-1", "transcript": "어르신 걱정돼요"},
                            path="/api/wellbeing/request")
        self.assertEqual(res.status, 200)


class Wiring(unittest.TestCase):
    def test_route_is_a_wellbeing_sub_action(self):
        src = io.open(os.path.join(ROOT, "api", "wellbeing.py"), encoding="utf-8").read()
        self.assertIn('if op == "request":', src)
        self.assertIn("import _eum_request as er", src)
        self.assertIn('"request")', src.split("_OPS = ", 1)[1].split("\n", 1)[0])

    def test_no_new_function_file(self):
        files = [f for f in os.listdir(os.path.join(ROOT, "api")) if f.endswith(".py") and not f.startswith("_")]
        self.assertLessEqual(len(files), 12, files)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
