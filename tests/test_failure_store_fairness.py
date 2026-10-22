# -*- coding: utf-8 -*-
"""전송 실패 보관함의 **종류별 몫**(35차). 네트워크 미사용.

결함(34차가 한계 ④로 적어 둔 자리)
  `wellbeing.FAILED` 는 50칸짜리 보관함 하나인데 쓰는 경로가 둘이다 —
  안부 결과 웹훅(`wellbeing.result`)과 이음 3R 현장요청(`eum.request`).
  34차가 `kind` 칸을 붙여 **구분**은 되게 했지만 축출은 그대로 꼬리 절단
  (`del FAILED[_MAX_FAILED:]`)이었다. 그래서 한쪽이 50번 연속 실패하면
  다른 쪽 기록이 **전부 밀려난다**. `?op=failures` 를 보는 담당자는 사라진
  쪽을 「잘 나갔다」로 읽는다 — 실패를 드러내려고 만든 장치가 실패를 가린다.

고친 것
  버릴 때마다 **그때 가장 많은 종류**의 가장 오래된 기록을 고른다. 한 종류만
  쌓이면 예전과 같고(오래된 것부터), 섞이면 자연히 몫이 나뉜다. 버린 건수는
  종류별로 세어 `?op=failures` 의 `dropped` 로 드러낸다(조용한 삭제 금지).

여기서 고정하는 것
  1) 한 경로의 폭주가 다른 경로의 기록을 **지우지 못한다**.
  2) 상한은 그대로 50칸이다(메모리가 자라지 않는다 — `mem_caps` 규약).
  3) 한 종류만 쌓이면 **오래된 것부터** 버린다(종전 동작).
  4) 버린 건수가 실측으로 드러난다(꾸민 수치 아님).
  5) 개인정보 규약 불변 — 축출 경로가 생겼다고 경로·토큰·참여자 키가 새지 않는다.

실행: python3 -m pytest tests/test_failure_store_fairness.py -q
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _ratelimit          # noqa: E402
import wellbeing as W      # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        del W.FAILED[:]
        for k in W.FAILED_DROPPED:
            W.FAILED_DROPPED[k] = 0
        _ratelimit.reset()
        self._urlopen = W.urllib.request.urlopen

        def _forbidden(*a, **k):
            raise AssertionError("테스트가 네트워크를 호출했습니다")

        W.urllib.request.urlopen = _forbidden

    def tearDown(self):
        W.urllib.request.urlopen = self._urlopen
        del W.FAILED[:]
        for k in W.FAILED_DROPPED:
            W.FAILED_DROPPED[k] = 0

    def put(self, kind, ref):
        W._remember_failure({"ts": "t", "kind": kind, "ref": ref,
                             "callback_host": "eum.example.org", "status": 503})

    def refs(self, kind=None):
        return [f["ref"] for f in W.FAILED if kind is None or f["kind"] == kind]


# ==========================================================================
# 1) 한쪽이 다른 쪽을 밀어내지 않는다
# ==========================================================================
class Fairness(Base):

    def test_이음_폭주가_안부_기록을_지우지_못한다(self):
        for i in range(5):
            self.put("wellbeing.result", "wb_%d" % i)
        for i in range(W._MAX_FAILED * 3):
            self.put("eum.request", "er_%d" % i)
        self.assertEqual(len(self.refs("wellbeing.result")), 5)

    def test_안부_폭주도_이음_기록을_지우지_못한다(self):
        for i in range(5):
            self.put("eum.request", "er_%d" % i)
        for i in range(W._MAX_FAILED * 3):
            self.put("wellbeing.result", "wb_%d" % i)
        self.assertEqual(len(self.refs("eum.request")), 5)

    def test_둘_다_폭주하면_반반에_수렴한다(self):
        for i in range(W._MAX_FAILED * 2):
            self.put("wellbeing.result", "wb_%d" % i)
            self.put("eum.request", "er_%d" % i)
        wb, er_ = len(self.refs("wellbeing.result")), len(self.refs("eum.request"))
        self.assertEqual(wb + er_, W._MAX_FAILED)
        self.assertLessEqual(abs(wb - er_), 1, (wb, er_))

    def test_알_수_없는_종류도_몫을_받는다(self):
        self.put("unknown", "u_0")
        for i in range(W._MAX_FAILED * 2):
            self.put("eum.request", "er_%d" % i)
        self.assertIn("u_0", self.refs())

    def test_kind_가_빠진_기록도_버려질_수_있다(self):
        # 과거 형식(34차 이전)의 기록이 섞여 있어도 무한히 쌓이지 않는다.
        for i in range(W._MAX_FAILED + 10):
            W._remember_failure({"ts": "t", "ref": "old_%d" % i})
        self.assertEqual(len(W.FAILED), W._MAX_FAILED)
        self.assertEqual(W.failure_dropped()["unknown"], 10)


# ==========================================================================
# 2) 상한·순서는 그대로
# ==========================================================================
class CapAndOrder(Base):

    def test_상한을_넘지_않는다(self):
        for i in range(W._MAX_FAILED * 4):
            self.put("eum.request", "er_%d" % i)
        self.assertEqual(len(W.FAILED), W._MAX_FAILED)

    def test_한_종류만_쌓이면_오래된_것부터_버린다(self):
        for i in range(W._MAX_FAILED + 3):
            self.put("eum.request", "er_%d" % i)
        kept = self.refs()
        self.assertEqual(kept[0], "er_%d" % (W._MAX_FAILED + 2))   # 최신이 앞
        self.assertNotIn("er_0", kept)
        self.assertNotIn("er_2", kept)
        self.assertIn("er_3", kept)

    def test_최신이_항상_앞에_있다(self):
        for i in range(W._MAX_FAILED + 5):
            self.put("wellbeing.result", "wb_%d" % i)
        self.assertEqual(W.FAILED[0]["ref"], "wb_%d" % (W._MAX_FAILED + 4))

    def test_상한_아래에서는_아무것도_버리지_않는다(self):
        for i in range(W._MAX_FAILED):
            self.put("eum.request", "er_%d" % i)
        self.assertEqual(W.failure_dropped()["total"], 0)

    def test_같은_입력이면_같은_결과다(self):
        def run():
            del W.FAILED[:]
            for i in range(W._MAX_FAILED):
                self.put("wellbeing.result", "wb_%d" % i)
                self.put("eum.request", "er_%d" % i)
            return self.refs()
        self.assertEqual(run(), run())


# ==========================================================================
# 3) 버린 건수를 숨기지 않는다
# ==========================================================================
class Dropped(Base):

    def test_버린_건수가_종류별로_센다(self):
        for i in range(W._MAX_FAILED + 7):
            self.put("eum.request", "er_%d" % i)
        d = W.failure_dropped()
        self.assertEqual(d["eum.request"], 7)
        self.assertEqual(d["wellbeing.result"], 0)
        self.assertEqual(d["total"], 7)

    def test_failures_응답에_드러난다(self):
        for i in range(W._MAX_FAILED + 2):
            self.put("eum.request", "er_%d" % i)
        with mock.patch.dict(os.environ, {"CALLBOT_API_KEY": "", "CALLBOT_STRICT": ""}):
            from test_wellbeing import call
            res = call("GET", path="/api/wellbeing?op=failures")
        self.assertEqual(res.status, 200)
        body = res.body()
        self.assertEqual(body["dropped"]["eum.request"], 2)
        self.assertEqual(body["kinds"]["eum.request"], W._MAX_FAILED)
        self.assertIn("dropped", body["note"])

    def test_집계는_상수가_아니다(self):
        with mock.patch.dict(os.environ, {"CALLBOT_API_KEY": "", "CALLBOT_STRICT": ""}):
            from test_wellbeing import call
            res = call("GET", path="/api/wellbeing?op=failures")
        self.assertEqual(res.body()["dropped"], {"wellbeing.result": 0, "eum.request": 0,
                                                 "unknown": 0, "total": 0})

    def test_등록된_종류는_RECORD_KINDS_와_같다(self):
        self.assertEqual(sorted(W.FAILED_DROPPED), sorted(W.RECORD_KINDS))


# ==========================================================================
# 4) 실제 전송 실패 경로에서도 같다 + 개인정보 규약 불변
# ==========================================================================
class ThroughDeliver(Base):

    def fail(self, payload, url="https://eum.example.org/hook?token=abc"):
        return W.deliver_with_retry(url, payload, attempts=1, sleep_fn=lambda s: None,
                                    deliver_fn=lambda u, pl: {"delivered": False, "status": 500})

    def test_두_경로가_섞여도_서로_남는다(self):
        self.fail({"raw_ref": "wb_1", "senior_id": "SR-1"})
        for i in range(W._MAX_FAILED * 2):
            self.fail({"participant_id": "u-par-1", "kind_label": "other", "ref": "er_%d" % i})
        wb = [f for f in W.FAILED if f["kind"] == "wellbeing.result"]
        self.assertEqual(len(wb), 1)
        self.assertEqual(wb[0]["ref"], "wb_1")

    def test_라우트는_이음_전송_실패를_502로_드러내고_보관한다(self):
        """전송 실패를 숨기지 않는다 — 그리고 보관 기록이 어느 경로인지 말한다."""
        import _eum_request as er
        env = {"EUM_CHANNEL_SECRET": "k" * 32, "EUM_REQUEST_URL": "",
               "EUM_REQUEST_HOSTS": "", "CALLBOT_API_KEY": "", "CALLBOT_STRICT": ""}
        saved = er.deliver
        er.deliver = lambda u, pl, s, timeout=None: {"delivered": False, "status": 503,
                                                     "error": "이음이 503 로 응답했습니다"}
        try:
            with mock.patch.dict(os.environ, env):
                from test_wellbeing import call
                res = call("POST", {"participant_id": "u-par-1", "transcript": "어르신이 쓰러지셨어요",
                                    "send": True}, path="/api/wellbeing/request")
        finally:
            er.deliver = saved
        self.assertEqual(res.status, 502)
        body = res.body()
        self.assertEqual(body["code"], "EUM_DELIVERY_FAILED")
        self.assertFalse(body["ok"])
        self.assertEqual(W.FAILED[0]["kind"], "eum.request")
        self.assertEqual(W.FAILED[0]["ref"], body["payload"]["ref"])

    def test_축출이_생겨도_경로_토큰_참여자키가_새지_않는다(self):
        for i in range(W._MAX_FAILED + 5):
            self.fail({"participant_id": "u-par-%d" % i, "kind_label": "other",
                       "ref": "er_%d" % i})
        blob = json.dumps(W.FAILED, ensure_ascii=False)
        self.assertNotIn("token=abc", blob)
        self.assertNotIn("u-par-", blob)
        self.assertEqual(W.FAILED[0]["callback_host"], "eum.example.org")


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
