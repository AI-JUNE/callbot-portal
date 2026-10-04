# -*- coding: utf-8 -*-
"""api/wellbeing.py 잔여 방어 분기 — 의존성 0, 네트워크 미사용.

`tests/test_wellbeing.py` 83건이 안부 흐름·서명·재시도·이력을 덮고,
`tests/test_request_log_wiring.py` 가 요청 로그 결선을 덮는다. 남은 것은
**보조 모듈이 없거나 상대 서버가 비정상일 때만 도는 코드**다. 이음 2R 심사
경로이므로, 평시에 돌지 않는 가지가 실제로는 터지는 상태로 남아 있으면
시연 중에 처음 드러난다.

여기서 고정하는 것
  1) 실제 urllib 동작 — 콜백 5xx·4xx 는 `HTTPError` 로 올라온다(대역이 아닌 실분기)
  2) 리다이렉트     — 검증을 통과한 콜백이 302 로 내부를 가리키면 '보냈다'고
                      보고하지 않는다(가드 우회 + 거짓 보고 차단)
  3) 서명 규약      — 본문을 str 로 넘겨도 bytes 와 같은 서명이 나온다(수신측 검증 예시)
  4) 흡수           — 감사·로그·URL 파싱이 터져도 안부 호출은 살아 있다
  5) 거부           — 외부 오리진 POST 는 실행 전에 막히고 이력이 남지 않는다
  6) 모듈 부재      — _monitoring 을 못 불러와도 요약 조립이 동작한다

실행: python3 -m pytest tests/test_wellbeing_edge.py -q
"""
import importlib
import io
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wellbeing as W  # noqa: E402
from test_wellbeing import Base, call  # noqa: E402  (대역 재사용)


class FakeResp(object):
    """urlopen 응답 대역."""

    def __init__(self, status=200, url="", raise_geturl=False):
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


CALLBACK = "https://eum.example.org/hook"


class Edge(Base):
    def fake_urlopen(self, resp=None, raising=None):
        calls = []

        def fake(req, timeout=None):
            calls.append(getattr(req, "full_url", req))
            if raising is not None:
                raise raising
            return resp if resp is not None else FakeResp(url=CALLBACK)

        W.urllib.request.urlopen = fake      # Base.tearDown 이 원복한다
        return calls


# ==========================================================================
# 1) 실제 urllib 분기 — 콜백이 오류 상태를 돌려줄 때
# ==========================================================================
class TestDeliverHttpErrors(Edge):

    def test_콜백_5xx_는_HTTPError_로_올라와도_재시도_대상이다(self):
        self.fake_urlopen(raising=urllib.error.HTTPError(
            CALLBACK + "?token=SECRET-TOKEN", 503, "Service Unavailable", {}, None))
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertFalse(out["delivered"])
        self.assertEqual(out["status"], 503)
        self.assertTrue(W.is_retryable(out))
        self.assertNotIn("SECRET-TOKEN", json.dumps(out, ensure_ascii=False))

    def test_콜백_4xx_는_재시도하지_않는다(self):
        self.fake_urlopen(raising=urllib.error.HTTPError(CALLBACK, 404, "Not Found", {}, None))
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertEqual(out["status"], 404)
        self.assertFalse(W.is_retryable(out))

    def test_상태코드_없는_HTTPError_도_사유를_남긴다(self):
        err = urllib.error.HTTPError(CALLBACK, 0, "broken", {}, None)
        self.fake_urlopen(raising=err)
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertFalse(out["delivered"])
        self.assertIn("오류", out["error"])

    def test_3xx_응답은_전송성공으로_치지_않는다(self):
        self.fake_urlopen(FakeResp(status=302, url=CALLBACK))
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertFalse(out["delivered"])
        self.assertIn("302", out["error"])

    def test_재시도_판정은_이상한_status_에도_예외를_내지_않는다(self):
        for st in ("오류", object(), [], {}):
            self.assertFalse(W.is_retryable({"delivered": False, "status": st}))
        self.assertTrue(W.is_retryable({"delivered": False, "status": None}))
        self.assertFalse(W.is_retryable("딕셔너리가 아님"))


# ==========================================================================
# 2) 리다이렉트 — 검증 범위를 벗어난 전송을 '성공'으로 보고하지 않는다
# ==========================================================================
class TestRedirectEscape(Edge):

    def test_내부주소로_튕기면_전송실패로_돌린다(self):
        self.fake_urlopen(FakeResp(status=200, url="http://169.254.169.254/latest/meta-data/"))
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertFalse(out["delivered"])
        self.assertIn("리다이렉트 이탈", out["error"])
        self.assertNotIn("169.254", out["error"])

    def test_이탈은_재시도하지_않는다(self):
        """같은 콜백을 다시 불러도 같은 곳으로 튕긴다 — 상대 서버를 두드리지 않는다."""
        self.fake_urlopen(FakeResp(status=200, url="https://127.0.0.1/hook"))
        out = W.deliver(CALLBACK, {"senior_id": "SR-1"})
        self.assertFalse(W.is_retryable(out))

    def test_이탈은_실패보관에_호스트만_남긴다(self):
        self.fake_urlopen(FakeResp(status=200, url="https://10.0.0.9/hook"))
        out = W.deliver_with_retry(CALLBACK, {"senior_id": "SR-1", "raw_ref": "wb_x"})
        self.assertFalse(out["delivered"])
        self.assertEqual(W.FAILED[0]["callback_host"], "eum.example.org")
        self.assertNotIn("10.0.0.9", json.dumps(W.FAILED[0], ensure_ascii=False))

    def test_같은_호스트_안에서의_리다이렉트는_정상(self):
        self.fake_urlopen(FakeResp(status=200, url="https://eum.example.org/hook/v2"))
        self.assertTrue(W.deliver(CALLBACK, {"senior_id": "SR-1"})["delivered"])

    def test_최종주소를_못_읽어도_전송은_성립한다(self):
        self.fake_urlopen(FakeResp(status=200, raise_geturl=True))
        self.assertTrue(W.deliver(CALLBACK, {"senior_id": "SR-1"})["delivered"])

    def test_안부_실행_전체경로에서도_이탈이_드러난다(self):
        self.fake_urlopen(FakeResp(status=200, url="https://metadata.google.internal/x"))
        out = W.run_wellbeing("SR-7", CALLBACK, profile="ok")
        self.assertFalse(out["ok"])
        self.assertIn("리다이렉트 이탈", out["delivery"]["error"])
        self.assertFalse(W.RECENT[0]["delivered"])      # 이력도 거짓으로 적지 않는다


# ==========================================================================
# 3) 서명 규약 — 수신측(이음) 검증 예시가 str 본문에서도 성립한다
# ==========================================================================
class TestSignEdge(Edge):

    def test_str_본문과_bytes_본문의_서명이_같다(self):
        body = '{"senior_id":"SR-1"}'
        ts1, sig1 = W.sign(body, "key", ts=1700000000)
        ts2, sig2 = W.sign(body.encode("utf-8"), "key", ts=1700000000)
        self.assertEqual((ts1, sig1), (ts2, sig2))
        self.assertTrue(W.verify_signature(body, "key", ts1, sig1, now=1700000000))

    def test_비밀키가_없으면_서명을_생략한다(self):
        ts, sig = W.sign('{"a":1}', "")
        self.assertEqual(sig, "")


# ==========================================================================
# 4) 흡수 — 보조 경로가 터져도 안부 호출은 살아 있다
# ==========================================================================
class TestAbsorbs(Edge):

    def test_감사모듈이_없어도_200(self):
        self.addCleanup(lambda a=W._audit: setattr(W, "_audit", a))
        W._audit = None
        self.assertIsNone(W._audit_ev({}, "/api/wellbeing", "GET", "allow", 200))
        self.assertEqual(call("GET").status, 200)

    def test_감사기록_실패가_안부_호출을_죽이지_않는다(self):
        class Broken(object):
            def record_request(self, *a, **kw):
                raise RuntimeError("감사 버퍼 고장")

        self.addCleanup(lambda a=W._audit: setattr(W, "_audit", a))
        W._audit = Broken()
        self.assertIsNone(W._audit_ev({}, "/api/wellbeing", "POST", "allow", 200))
        res = call("POST", {"senior_id": "SR-1", "profile": "ok"})
        self.assertEqual(res.status, 200)

    def test_경로파싱이_터져도_기본동작으로_떨어진다(self):
        self.assertEqual(W._op_from_path("//[::1/api/wellbeing/call"), "")

    def test_쿼리파싱이_터져도_예외가_새지_않는다(self):
        self.addCleanup(lambda f=W.parse_qs: setattr(W, "parse_qs", f))
        W.parse_qs = lambda q: (_ for _ in ()).throw(RuntimeError("파서 고장"))
        self.assertEqual(W._op_from_path("/api/wellbeing?op=call"), "")

    def test_로그_종료_실패가_응답을_막지_않는다(self):
        class BrokenRq(object):
            request_id = "rid-1"

            def finish(self, *a, **kw):
                raise RuntimeError("로그 고장")

            def set(self, **kw):
                return self

        self.assertIsNone(W._close(BrokenRq(), 200))

    def test_콜백호스트_추출이_터져도_빈값(self):
        self.assertEqual(W._callback_host("https://[::1"), "")
        self.assertEqual(W._callback_host(None), "")

    def test_raw_ref_없는_페이로드는_이력에_담지_않는다(self):
        W._remember_result({"senior_id": "SR-1"}, {"delivered": True})
        self.assertEqual(W.RESULTS, {})
        self.assertEqual(W._RESULT_ORDER, [])

    def test_answers_가_객체가_아니면_거부(self):
        with self.assertRaises(ValueError):
            W.run_wellbeing("SR-1", None, answers="좋아요")


# ==========================================================================
# 5) 거부 — 외부 오리진 POST 는 실행 전에 막힌다
# ==========================================================================
class TestDenyPost(Edge):

    def test_외부_오리진_POST_는_거부되고_실행되지_않는다(self):
        # 이 라우트는 웹훅 토큰 경로를 허용하므로(allow_webhook=True) 외부
        # 호출은 403 이 아니라 401(토큰 필요)로 끝난다 — 어느 쪽이든 실행 전 차단.
        res = call("POST", {"senior_id": "SR-1", "profile": "risk"},
                   headers={"sec-fetch-site": "cross-site",
                            "origin": "https://evil.example.com"})
        self.assertEqual(res.status, 401)
        self.assertEqual(W.RECENT, [])              # 판정도 이력도 남지 않는다
        self.assertEqual(W.RESULTS, {})

    def test_거부응답에_설정힌트가_새지_않는다(self):
        os.environ["CALLBOT_STRICT"] = "1"
        res = call("POST", {"senior_id": "SR-1"},
                   headers={"sec-fetch-site": "cross-site"})
        self.assertIn(res.status, (401, 403))
        self.assertNotIn("CALLBOT_API_KEY", res.wfile.data.decode("utf-8"))


# ==========================================================================
# 6) 모듈 부재 — _monitoring 을 못 불러와도 요약이 조립된다
# ==========================================================================
class TestMonitoringFallback(Edge):

    def setUp(self):
        super().setUp()
        self._saved_mon = sys.modules.get("_monitoring")
        self._saved_ns = dict(W.__dict__)
        self._saved_path = list(sys.path)
        sys.modules["_monitoring"] = None       # import 시 ImportError
        sys.path[:] = [p for p in sys.path if os.path.abspath(p) != API]
        spec = importlib.util.spec_from_file_location(
            "wellbeing", os.path.join(API, "wellbeing.py"))
        spec.loader.exec_module(W)

    def tearDown(self):
        sys.path[:] = self._saved_path
        if self._saved_mon is None:
            sys.modules.pop("_monitoring", None)
        else:
            sys.modules["_monitoring"] = self._saved_mon
        W.__dict__.clear()
        W.__dict__.update(self._saved_ns)
        super().tearDown()
        import _monitoring
        self.assertIs(W._scrub, _monitoring.scrub)   # 다른 테스트를 망가뜨리지 않는다

    def test_자기_디렉터리를_sys_path_에_넣는다(self):
        self.assertIn(API, [os.path.abspath(p) for p in sys.path])

    def test_scrub_폴백이_설치된다(self):
        self.assertEqual(W._scrub("abc"), "abc")
        self.assertEqual(W._scrub(12), "12")

    def test_폴백_상태에서도_페이로드가_조립된다(self):
        p = W.build_payload("SR-1", answered=True, answers=W.PROFILES["watch"],
                            raw_ref="wb_x")
        self.assertEqual(p["schema"], W.SCHEMA)
        self.assertTrue(p["transcript_summary"])
        self.assertNotIn("SR-1", p["transcript_summary"])

    def test_SSRF_가드는_폴백_상태에서도_살아_있다(self):
        self.assertFalse(W.check_callback_url("https://127.0.0.1/cb")[0])
        self.assertFalse(W.check_callback_url("file:///etc/passwd")[0])


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
