# -*- coding: utf-8 -*-
"""이음 3R 현장 음성 요청 — **실제 전송 분기**와 분류 오탐 회귀. 네트워크 미사용.

`tests/test_eum_request.py` 는 분류·dry-run 스위치·서명 꼴만 덮었다(3건).
그래서 **이음에 실제로 닿는 유일한 함수**(`_eum_request.deliver`)는 한 줄도
검증되지 않은 채로 있었다(커버리지 69%, 미커버 32줄이 전부 그 함수다).
20차가 안부 웹훅에서 같은 자리를 지적했다 — 「재시도 회귀가 전부 대역이라
실제 urllib 동작으로는 한 번도 검증되지 않았다」. 그 쪽만 고쳐졌고 이 쪽은
그대로였다(규칙이 절반만 지켜진 상태).

여기서 고정하는 것
  1) 분류 오탐 — 「커**피를**」·「한**숨을**」·「**무사고**」·「**돈**가스」가
     긴급·현장으로 걸리지 않는다. 동시에 **과차단도 아니다**(코피가·교통사고가).
  2) 가림은 분류에만 쓰이고 **이음에 보내는 요약은 손대지 않는다**.
  3) `deliver()` 전 분기 — 200·비 2xx·HTTPError(4xx/5xx)·타임아웃·리다이렉트 이탈·
     서명 불가. 사유에 URL·토큰·비밀이 실리지 않는다.
  4) 공유 비밀이 없으면 **보내지 않는다**(fail-closed) — 재시도 대상도 아니다.
  5) 실패 보관 기록이 **어느 경로의 실패인지** 말한다(`kind`·`ref`) — 안부 결과
     웹훅과 같은 보관함을 쓰기 때문이다.
  6) `ref` 는 그대로 이음 본문에 실리므로 형식을 검증한다.

실행: python3 -m pytest tests/test_eum_request_deliver.py -q
"""
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _eum_request as er      # noqa: E402
import wellbeing as W          # noqa: E402

URL = "https://eum.gowon.co.kr/api/channel/aicc/request"
SECRET = "k" * 32


class FakeResp(object):
    """urlopen 응답 대역 — 소켓을 열지 않는다."""

    def __init__(self, status=200, url=URL, raise_geturl=False):
        self.status = status
        self._url = url
        self._raise = raise_geturl

    def getcode(self):
        return self.status

    def geturl(self):
        if self._raise:
            raise RuntimeError("geturl 미구현")
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net(unittest.TestCase):
    """기본은 **네트워크 금지** — 대역을 깔지 않은 테스트가 밖으로 나가면 즉시 실패."""

    def setUp(self):
        self._saved = er.urllib.request.urlopen

        def _forbidden(*a, **k):
            raise AssertionError("테스트가 네트워크를 호출했습니다")

        er.urllib.request.urlopen = _forbidden
        del W.FAILED[:]

    def tearDown(self):
        er.urllib.request.urlopen = self._saved
        del W.FAILED[:]

    def fake(self, resp=None, raising=None):
        """보낸 요청을 모아 돌려준다(헤더·본문 검증용)."""
        seen = []

        def _fake(req, timeout=None):
            seen.append(req)
            if raising is not None:
                raise raising
            return resp if resp is not None else FakeResp()

        er.urllib.request.urlopen = _fake
        return seen


# ==========================================================================
# 1) 분류 오탐 — 한글 부분 일치가 다른 낱말 안에서 걸렸다
# ==========================================================================
class Decoys(unittest.TestCase):

    def test_커피는_출혈이_아니다(self):
        self.assertNotEqual(er.classify("어르신이 커피를 드시고 싶다고 하세요")[0], "emergency")
        self.assertNotEqual(er.classify("커피가 식었다고 하시네요")[0], "emergency")

    def test_한숨은_호흡곤란이_아니다(self):
        self.assertNotEqual(er.classify("요즘 한숨을 자주 쉬세요")[0], "emergency")
        self.assertNotEqual(er.classify("한숨이 늘었어요")[0], "emergency")

    def test_무사고는_사고가_아니다(self):
        self.assertNotEqual(er.classify("오늘은 무사고로 잘 끝났습니다")[0], "emergency")

    def test_무의식적은_의식불명이_아니다(self):
        self.assertNotEqual(er.classify("무의식적으로 그러셨대요")[0], "emergency")

    def test_돈가스는_금전문제가_아니다(self):
        for t in ("점심에 돈가스 드셨어요", "돈까스 좋아하신대요"):
            self.assertEqual(er.classify(t)[0], "other", t)

    def test_과차단이_아니다_진짜_긴급은_그대로_긴급이다(self):
        for t, w in (("코피가 조금 났어요", "피가"),
                     ("교통사고가 났어요", "사고"),
                     ("어르신이 쓰러지셨어요", "쓰러"),
                     ("숨이 가빠 하세요", "숨이"),
                     ("119 불렀어요", "119")):
            label, ev = er.classify(t)
            self.assertEqual(label, "emergency", t)
            self.assertIn(w, ev)

    def test_과차단이_아니다_커피를_마시다_쓰러지면_긴급이다(self):
        self.assertEqual(er.classify("커피 드시다가 쓰러지셨어요")[0], "emergency")

    def test_돈_자체는_현장_문제로_남는다(self):
        self.assertEqual(er.classify("돈을 잃어버리셨대요")[0], "onsite_issue")

    def test_가림이_다른_유형을_지우지_않는다(self):
        self.assertEqual(er.classify("커피를 쏟아서 바닥이 문제예요")[0], "onsite_issue")

    def test_가림_목록은_판정_낱말을_실제로_품고_있다(self):
        """등록부 드리프트 방지 — 무관한 낱말을 가림 목록에 넣으면 과차단이 된다.

        판정 낱말에는 조사가 붙어 있다(`피가`·`숨을`). 가림 낱말이 그 낱말을
        품는 것은 조사가 붙은 꼴에서다(「커피」+「를」 = 「커피를」 ⊃ 「피를」).
        """
        words = [w for _l, ws in er._RULES for w in ws]
        for decoy in er._DECOY:
            forms = [decoy + j for j in ("", "을", "를", "이", "가", "은", "는", "도", "로", "에")]
            self.assertTrue(any(w in f for w in words for f in forms),
                            "%s 는 판정 낱말을 품고 있지 않다(가릴 이유가 없다)" % decoy)

    def test_같은_글은_같은_답이다(self):
        t = "커피를 쏟으셨는데 다치지는 않으셨어요"
        self.assertEqual(er.classify(t), er.classify(t))

    def test_가림은_이음에_보내는_요약을_바꾸지_않는다(self):
        p, cls = er.build_payload("u-par-1", "어르신이 커피를 드시고 싶다고 하세요")
        self.assertIn("커피를", p["summary"])      # 담당자가 읽는 글은 그대로다
        self.assertNotEqual(cls["label"], "emergency")


# ==========================================================================
# 2) deliver() — 이음에 실제로 닿는 유일한 함수
# ==========================================================================
class DeliverOnce(Net):

    def test_200_이면_전송_성공이다(self):
        seen = self.fake(FakeResp(status=200))
        out = er.deliver(URL, {"participant_id": "u-par-1", "ref": "vr_1"}, SECRET)
        self.assertTrue(out["delivered"])
        self.assertTrue(out["signed"])
        self.assertEqual(out["status"], 200)
        self.assertIsNone(out["error"])
        self.assertIn("duration_ms", out)
        self.assertEqual(len(seen), 1)

    def test_헤더와_서명이_이음_규약과_같다(self):
        seen = self.fake()
        payload = {"participant_id": "u-par-1", "kind_label": "emergency",
                   "summary": "어르신이 쓰러지셨어요", "ref": "vr_1"}
        er.deliver(URL, payload, SECRET)
        req = seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("X-channel-id"), "phone")
        ts = req.get_header("X-channel-timestamp")
        sig = req.get_header("X-channel-signature")
        # 서명 대상은 **실제로 보낸 본문** 이다(다른 바이트를 서명하면 이음이 거부한다)
        self.assertEqual(er.sign(req.data, SECRET, ts), (ts, sig))
        self.assertTrue(sig.startswith("sha256="))

    def test_본문은_한글을_그대로_담은_JSON_이다(self):
        seen = self.fake()
        er.deliver(URL, {"summary": "어르신이 쓰러지셨어요"}, SECRET)
        body = seen[0].data.decode("utf-8")
        self.assertIn("어르신이 쓰러지셨어요", body)
        self.assertEqual(json.loads(body)["summary"], "어르신이 쓰러지셨어요")

    def test_비_2xx_응답은_성공이_아니다(self):
        self.fake(FakeResp(status=500))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertFalse(out["delivered"])
        self.assertIn("500", out["error"])
        self.assertTrue(W.is_retryable(out))

    def test_3xx_응답도_성공이_아니다(self):
        self.fake(FakeResp(status=302, url=URL))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertFalse(out["delivered"])
        self.assertIn("302", out["error"])

    def test_5xx_는_HTTPError_로_올라와도_재시도_대상이다(self):
        self.fake(raising=urllib.error.HTTPError(
            URL + "?token=SECRET-TOKEN", 503, "Service Unavailable", {}, None))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertEqual(out["status"], 503)
        self.assertTrue(W.is_retryable(out))
        self.assertNotIn("SECRET-TOKEN", json.dumps(out, ensure_ascii=False))

    def test_4xx_는_재시도하지_않는다(self):
        self.fake(raising=urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertEqual(out["status"], 401)
        self.assertFalse(W.is_retryable(out))
        self.assertIn("401", out["error"])

    def test_상태코드_없는_HTTPError_도_사유를_남긴다(self):
        self.fake(raising=urllib.error.HTTPError(URL, 0, "broken", {}, None))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertFalse(out["delivered"])
        self.assertIn("오류", out["error"])

    def test_타임아웃은_타입명만_남긴다(self):
        self.fake(raising=urllib.error.URLError("timed out: https://eum.gowon.co.kr/x?t=TOK"))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertFalse(out["delivered"])
        self.assertIsNone(out["status"])
        self.assertIn("URLError", out["error"])
        self.assertNotIn("TOK", json.dumps(out, ensure_ascii=False))
        self.assertTrue(W.is_retryable(out))      # 연결 실패는 다시 보낸다

    def test_리다이렉트_이탈은_보냈다고_보고하지_않는다(self):
        self.fake(FakeResp(status=200, url="https://evil.example.com/collect"))
        with mock.patch.dict(os.environ, {"EUM_REQUEST_HOSTS": ""}):
            out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertFalse(out["delivered"])
        self.assertIn("리다이렉트", out["error"])
        self.assertFalse(W.is_retryable(out))     # 다시 보내도 같은 곳으로 간다

    def test_같은_호스트_안의_리다이렉트는_성공이다(self):
        self.fake(FakeResp(status=200, url=URL + "/v2"))
        with mock.patch.dict(os.environ, {"EUM_REQUEST_HOSTS": ""}):
            out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertTrue(out["delivered"])

    def test_geturl_이_터져도_전송_판정은_살아_있다(self):
        self.fake(FakeResp(status=200, raise_geturl=True))
        out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertTrue(out["delivered"])

    def test_허용_호스트_목록을_바꾸면_리다이렉트_검증도_그_목록을_쓴다(self):
        self.fake(FakeResp(status=200, url="https://stage.eum.example.org/x"))
        with mock.patch.dict(os.environ, {"EUM_REQUEST_HOSTS": "stage.eum.example.org"}):
            out = er.deliver(URL, {"ref": "vr_1"}, SECRET)
        self.assertTrue(out["delivered"])

    def test_비밀이_없으면_보내지_않는다(self):
        seen = self.fake()
        for secret in (None, "", "short"):
            out = er.deliver(URL, {"ref": "vr_1"}, secret)
            self.assertFalse(out["delivered"], secret)
            self.assertFalse(out["signed"], secret)
            self.assertFalse(W.is_retryable(out), secret)
            self.assertIn("EUM_CHANNEL_SECRET", out["error"])
        self.assertEqual(seen, [])                # 상대 서버를 두드리지 않았다

    def test_str_본문도_bytes_와_같은_서명이_나온다(self):
        """수신측(이음)이 본문을 문자열로 들고 검증해도 같은 값이어야 한다."""
        body = '{"participant_id":"u-par-1","summary":"어르신이 쓰러지셨어요"}'
        self.assertEqual(er.sign(body, SECRET, 1700000000),
                         er.sign(body.encode("utf-8"), SECRET, 1700000000))

    def test_결과에_비밀도_주소도_실리지_않는다(self):
        self.fake(raising=urllib.error.HTTPError(URL, 500, "x", {}, None))
        out = er.deliver(URL + "?token=TOK", {"ref": "vr_1"}, SECRET)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("TOK", blob)
        self.assertNotIn("eum.gowon.co.kr", blob)


# ==========================================================================
# 3) 재시도·실패 보관 — 안부 웹훅과 같은 보관함을 쓴다
# ==========================================================================
class FailureRecord(Net):

    def _run(self, delivery, payload_extra=None, attempts=3):
        calls = []

        def fn(u, pl):
            calls.append((u, pl))
            return dict(delivery)

        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": SECRET,
                                          "EUM_REQUEST_URL": "", "EUM_REQUEST_HOSTS": "",
                                          "WELLBEING_WEBHOOK_RETRIES": str(attempts)}):
            out = er.run("u-par-1", "어르신이 쓰러지셨어요", ref="vr_fix1", send=True,
                         deliver_fn=fn,
                         retry_fn=lambda u, pl, deliver_fn=None: W.deliver_with_retry(
                             u, pl, deliver_fn=deliver_fn, sleep_fn=lambda s: None))
        return out, calls

    def test_실패_기록이_어느_경로인지_말한다(self):
        out, calls = self._run({"delivered": False, "status": 503, "error": "이음이 503"})
        self.assertFalse(out["ok"])
        self.assertEqual(len(calls), 3)                      # 5xx 는 재시도 대상
        self.assertEqual(len(W.FAILED), 1)
        rec = W.FAILED[0]
        self.assertEqual(rec["kind"], "eum.request")
        self.assertEqual(rec["ref"], "vr_fix1")              # 무엇이 실패했는지 알 수 있다
        self.assertEqual(rec["callback_host"], "eum.gowon.co.kr")

    def test_실패_기록에_발화_원문과_참여자_키가_없다(self):
        self._run({"delivered": False, "status": 503, "error": "이음이 503"})
        blob = json.dumps(W.FAILED[0], ensure_ascii=False)
        self.assertNotIn("쓰러", blob)
        self.assertNotIn("u-par-1", blob)
        self.assertNotIn("/api/channel", blob)               # 경로·토큰 미기록

    def test_보내지_않은_실패는_재시도하지_않는다(self):
        out, calls = self._run({"delivered": False, "status": None, "retryable": False,
                                "error": "공유 비밀(EUM_CHANNEL_SECRET)이 없어 보내지 않았습니다"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(W.FAILED[0]["kind"], "eum.request")

    def test_안부_실패_기록은_종전_칸을_그대로_유지한다(self):
        W.deliver_with_retry("https://eum.example.org/hook",
                             {"senior_id": "SR-1", "raw_ref": "wb_1"},
                             deliver_fn=lambda u, pl: {"delivered": False, "status": 404,
                                                       "error": "콜백이 404"})
        rec = W.FAILED[0]
        self.assertEqual(rec["kind"], "wellbeing.result")
        self.assertEqual(rec["raw_ref"], "wb_1")             # 하위호환
        self.assertEqual(rec["senior_id"], "SR-1")
        self.assertEqual(rec["ref"], "wb_1")

    def test_종류를_모르는_페이로드는_unknown_이다(self):
        W.deliver_with_retry("https://eum.example.org/hook", {"x": 1},
                             deliver_fn=lambda u, pl: {"delivered": False, "status": 404})
        self.assertEqual(W.FAILED[0]["kind"], "unknown")
        self.assertEqual(W.record_kind(None), "unknown")

    def test_종류별_집계는_보관된_건수와_같다(self):
        self._run({"delivered": False, "status": 404, "error": "이음이 404"})
        W.deliver_with_retry("https://eum.example.org/hook",
                             {"senior_id": "SR-1", "raw_ref": "wb_1"},
                             deliver_fn=lambda u, pl: {"delivered": False, "status": 404})
        self.assertEqual(W.failure_kinds(), {"eum.request": 1, "wellbeing.result": 1})
        self.assertEqual(sum(W.failure_kinds().values()), len(W.FAILED))

    def test_집계_칸이_응답에도_나온다(self):
        from test_wellbeing import call
        import _ratelimit
        _ratelimit.reset()
        self._run({"delivered": False, "status": 404, "error": "이음이 404"})
        res = call("GET", path="/api/wellbeing?op=failures")
        _ratelimit.reset()
        self.assertEqual(res.status, 200)
        b = res.body()
        self.assertEqual(b["kinds"], {"eum.request": 1})
        self.assertEqual(b["failures"][0]["kind"], "eum.request")


# ==========================================================================
# 4) ref — 검증 없이 이음 본문에 실리던 칸
# ==========================================================================
class RefField(unittest.TestCase):

    def test_허용되지_않은_ref_는_거부한다(self):
        for bad in ("현장 요청 1", "a" * 65, "ref with space", "ref\n주민번호"):
            with self.assertRaises(ValueError, msg=bad):
                er.build_payload("u-par-1", "일정 변경 부탁드려요", bad)

    def test_정상_ref_는_그대로_실린다(self):
        p, _ = er.build_payload("u-par-1", "일정 변경 부탁드려요", "vr_abc-1.2:3")
        self.assertEqual(p["ref"], "vr_abc-1.2:3")

    def test_빈_ref_는_우리가_만든다(self):
        for empty in (None, "", "   "):
            p, _ = er.build_payload("u-par-1", "일정 변경 부탁드려요", empty)
            self.assertTrue(p["ref"].startswith("vr_"))

    def test_라우트는_잘못된_ref_를_400_으로_돌린다(self):
        from test_wellbeing import call
        import _ratelimit
        _ratelimit.reset()
        with mock.patch.dict(os.environ, {"EUM_CHANNEL_SECRET": "", "CALLBOT_API_KEY": "",
                                          "CALLBOT_STRICT": ""}):
            res = call("POST", {"participant_id": "u-par-1", "transcript": "일정 변경",
                                "ref": "현장 요청"}, path="/api/wellbeing/request")
        _ratelimit.reset()
        self.assertEqual(res.status, 400)
        b = res.body()
        self.assertEqual(b["code"], "INVALID_REQUEST")
        self.assertEqual([d["field"] for d in b["details"]], ["request"])


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
