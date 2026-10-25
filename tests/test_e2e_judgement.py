# -*- coding: utf-8 -*-
"""로컬 E2E (38차) — **실제 소켓**으로 판정 결과가 HTTP 로 어떻게 나가는지 본다.

왜 단위 테스트로 부족한가
  38차 결함 둘은 **값이 밖으로 나가는 모양**의 문제였다. 안부 판정의 오탐은
  이음이 받는 `risk_level`·`flags` 를 바꾸고(이음이 그 값으로 follow_up 을
  만든다), 과금 라인 합계는 정산 화면이 그리는 숫자를 바꾼다. 둘 다 함수
  반환값이 아니라 **라우트 응답**이 계약이므로, 라우팅·직렬화·가드를 전부
  거친 뒤의 본문을 확인한다.

네트워크
  **루프백(127.0.0.1) + 임의 포트만** 사용한다. 외부로 나가는 요청이 없고
  과금 경로(LLM·STT·TTS·CPaaS)도 타지 않는다 — 안부는 콜백 URL 없이 부르므로
  dry-run(페이로드만 생성)이고, 정산 라인은 읽기 전용이다.

실행: python3 -m pytest tests/test_e2e_judgement.py -q
"""
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import settlement   # noqa: E402
import wellbeing    # noqa: E402

ENVS = ("CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_ALLOWED_ORIGINS",
        "CPAAS_LIVE", "SETTLEMENT_SOURCE")


class Server(object):
    """핸들러 하나를 루프백에 띄운다. 포트는 OS 가 고른다(충돌 없음)."""

    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def call(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.origin + path, data=data,
                                     method=method or ("POST" if data else "GET"))
        # 브라우저가 보내는 모양 그대로 — 동일출처(Origin == Host)여야 가드를 지난다
        req.add_header("Origin", self.origin)
        req.add_header("Sec-Fetch-Site", "same-origin")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class Base(unittest.TestCase):
    handler = None

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)
        cls.srv = Server(cls.handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestWellbeingJudgementOverHttp(Base):
    handler = wellbeing.handler

    def _payload(self, body):
        status, data = self.srv.call("/api/wellbeing/call", body)
        self.assertEqual(status, 200, data)
        return data

    def test_decoy_answers_do_not_reach_eum_as_high(self):
        """「커피가」·「며칠째 잘 먹어요」 — 결함 당시 risk high 로 나갔다."""
        data = self._payload({"senior_id": "e2e-decoy", "answers": {
            "mood": "며칠째 기분이 좋아요",
            "meal": "며칠째 잘 챙겨 먹고 있어요",
            "sleep": "푹 잘 잤어요",
            "pain": "커피가 식어서 다시 데웠어요"}})
        p = data["payload"]
        self.assertEqual(p["risk_level"], "low", p)
        self.assertEqual(p["flags"], [])
        # 원문 발화는 응답 어디에도 실리지 않는다(종전 규약 유지)
        self.assertNotIn("커피", json.dumps(data, ensure_ascii=False))

    def test_denied_symptoms_do_not_reach_eum_as_high(self):
        data = self._payload({"senior_id": "e2e-denial", "answers": {
            "mood": "아주 좋아요",
            "meal": "굶지 않았어요",
            "sleep": "어지럽지 않고 잘 자요",
            "pain": "숨이 차지 않아요"}})
        p = data["payload"]
        self.assertNotEqual(p["risk_level"], "high", p)
        self.assertEqual(p["flags"], [])

    def test_real_risk_profile_still_high(self):
        data = self._payload({"senior_id": "e2e-risk", "profile": "risk"})
        p = data["payload"]
        self.assertEqual(p["risk_level"], "high")
        self.assertEqual(sorted(p["flags"]), ["dizzy", "immobile", "starving"])
        self.assertEqual(p["mood_score"], 1)

    def test_denied_self_harm_still_high(self):
        data = self._payload({"senior_id": "e2e-sh", "answers": {
            "mood": "죽고 싶지 않아요", "meal": "세 끼 잘 먹어요",
            "sleep": "푹 잘 잤어요", "pain": "아픈 데 없어요"}})
        p = data["payload"]
        self.assertEqual(p["risk_level"], "high")
        self.assertIn("self_harm", p["flags"])

    def test_dimensions_carry_the_evidence(self):
        """담당자가 점수 근거를 되짚을 수 있다 — 칸이 사라지지 않았다."""
        data = self._payload({"senior_id": "e2e-dim", "profile": "watch"})
        dims = data["payload"]["dimensions"]
        self.assertEqual(set(dims), set(wellbeing.QUESTION_KEYS))
        for d in dims.values():
            self.assertEqual(set(d), {"score", "label", "negated"})

    def test_live_call_is_still_blocked(self):
        status, data = self.srv.call("/api/wellbeing/call",
                                     {"senior_id": "e2e-live", "mode": "live"})
        self.assertEqual(status, 501, data)


class TestSettlementTotalsOverHttp(Base):
    handler = settlement.handler

    def test_mixed_units_report_no_single_count(self):
        status, data = self.srv.call("/api/settlement?op=lines&month=2026-08")
        self.assertEqual(status, 200, data)
        t = data["totals"]
        self.assertIsNone(t["count"])
        self.assertTrue(t["units_mixed"])
        self.assertEqual(t["counts_by_unit"],
                         {"voice_units": 2105, "sessions": 550})
        self.assertTrue(data["demo"])          # 데모 표식은 그대로

    def test_single_unit_keeps_its_count(self):
        status, data = self.srv.call(
            "/api/settlement?op=lines&month=2026-08&tenant=demo-tenant-c")
        self.assertEqual(status, 200, data)
        t = data["totals"]
        self.assertEqual(t["count"], 42)
        self.assertFalse(t["units_mixed"])

    def test_input_error_still_points_at_the_field(self):
        status, data = self.srv.call("/api/settlement?op=lines&month=nope")
        self.assertEqual(status, 400, data)
        self.assertTrue(any(d.get("field") == "month"
                            for d in (data.get("details") or [])), data)


if __name__ == "__main__":
    unittest.main()
