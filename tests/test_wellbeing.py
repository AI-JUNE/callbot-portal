# -*- coding: utf-8 -*-
"""api/wellbeing.py — 이음 2R 안부 콜봇 연동 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제) · 실발신 없음.

검증 대상 (EUM_INTEGRATION.md)
  1) 웹훅 페이로드 스키마 — 이음이 읽는 6개 키의 존재·타입·값범위
  2) 채점 규칙 — 4문항 -> mood_score(1~5)·risk_level(low|mid|high), 경계·위험신호
  3) HMAC 서명 — `<ts>.<body>` 규약, 본문 변조·시각 이탈 거부, 시크릿 없으면 생략
  4) 콜백 URL 가드(SSRF) — 사설·루프백·비 https 차단, 화이트리스트
  5) 엔드포인트 — 정상 200, 입력오류 400, 전송실패 502(삼키지 않음), live 501
  6) 개인정보 — 페이로드·요약·최근목록에 성명·전화번호·주민번호가 남지 않는다
  7) 웹훅 재시도 — 5xx·타임아웃만 지수 백오프 재시도, 4xx 즉시 포기, 실패 보관
  8) 결과 이력 — raw_ref 로 판정 근거 조회(op=result), 없는 키는 404

실행: python3 -m pytest tests/test_wellbeing.py -q
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _ratelimit          # noqa: E402
import wellbeing as W      # noqa: E402


# --------------------------------------------------------------------------
# 최소 핸들러 대역 (소켓 없이 do_GET/do_POST 호출)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class FakeRFile(object):
    def __init__(self, data=b""):
        self._d = data

    def read(self, n=None):
        d = self._d if n is None else self._d[:n]
        self._d = b"" if n is None else self._d[n:]
        return d


class Res(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


def call(method, payload=None, headers=None, path="/api/wellbeing"):
    raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.9.9.9"}
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    if payload is not None:
        hdrs.setdefault("content-length", str(len(raw)))
    res = Res()
    inst = W.handler.__new__(W.handler)
    inst.headers = FakeHeaders(hdrs)
    inst.rfile = FakeRFile(raw)
    inst.wfile = res.wfile
    inst.path = path
    inst.send_response = lambda c: setattr(res, "status", c)
    inst.send_header = lambda k, v: res.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    getattr(inst, "do_" + method)()
    return res


ENV = ("CALLBACK_SECRET", "CPAAS_LIVE", "WELLBEING_CALLBACK_HOSTS",
       "WELLBEING_ALLOW_INSECURE", "CALLBOT_API_KEY", "CALLBOT_STRICT",
       "CALLBOT_DEBUG_ERRORS", "WELLBEING_WEBHOOK_RETRIES")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        _ratelimit.reset()
        del W.RECENT[:]
        del W.FAILED[:]
        W.RESULTS.clear()
        del W._RESULT_ORDER[:]
        # 어떤 테스트도 실제 HTTP 를 내지 않는다 — 호출되면 즉시 실패.
        self._urlopen = W.urllib.request.urlopen

        def _forbidden(*a, **k):
            raise AssertionError("테스트가 네트워크를 호출했습니다")

        W.urllib.request.urlopen = _forbidden

    def tearDown(self):
        W.urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()


# ==========================================================================
# 1) 채점 규칙
# ==========================================================================
class TestScoring(Base):
    def test_질문은_기분_식사_수면_통증_4문항(self):
        self.assertEqual(W.QUESTION_KEYS, ["mood", "meal", "sleep", "pain"])
        self.assertEqual(len(W.QUESTIONS), 4)

    def test_양호_프로필은_low(self):
        r = W.score_answers(W.PROFILES["ok"])
        self.assertEqual(r["risk_level"], "low")
        self.assertGreaterEqual(r["mood_score"], 4)
        self.assertEqual(r["flags"], [])

    def test_주의_프로필은_mid_이상(self):
        r = W.score_answers(W.PROFILES["watch"])
        self.assertIn(r["risk_level"], ("mid", "high"))

    def test_위험_프로필은_high_이고_신호가_남는다(self):
        r = W.score_answers(W.PROFILES["risk"])
        self.assertEqual(r["risk_level"], "high")
        self.assertTrue(r["flags"])

    def test_한_항목만_위험해도_평균에_묻히지_않는다(self):
        # 나머지는 양호한데 통증만 '거동 곤란' — 평균은 높지만 high 여야 한다
        r = W.score_answers({"mood": "좋아요", "meal": "세 끼 잘 챙겨 먹었어요",
                             "sleep": "푹 잘 잤어요", "pain": "어지러워서 잘 못 움직이겠어요"})
        self.assertEqual(r["risk_level"], "high")
        self.assertIn("immobile", r["flags"])

    def test_자해_암시는_즉시_위험(self):
        r = W.score_answers({"mood": "그냥 죽고 싶어요", "meal": "잘 먹어요",
                             "sleep": "잘 자요", "pain": "아픈 데 없어요"})
        self.assertEqual(r["risk_level"], "high")
        self.assertIn("self_harm", r["flags"])

    def test_점수는_항상_1에서_5(self):
        for prof in W.PROFILES.values():
            r = W.score_answers(prof)
            self.assertTrue(1 <= r["mood_score"] <= 5)
            for k in W.QUESTION_KEYS:
                self.assertTrue(1 <= r["dimensions"][k]["score"] <= 5)

    def test_알수없는_답변은_보통_3(self):
        self.assertEqual(W.score_one("mood", "글쎄요 뭐라 할까"), 3)
        self.assertEqual(W.score_one("mood", ""), 3)
        self.assertEqual(W.score_one("없는항목", "아무말"), 3)

    def test_같은_입력은_같은_결과(self):
        a = W.score_answers(W.PROFILES["watch"])
        b = W.score_answers(W.PROFILES["watch"])
        self.assertEqual(a, b)


# ==========================================================================
# 2) 웹훅 페이로드 스키마
# ==========================================================================
REQUIRED = ("senior_id", "answered", "mood_score", "risk_level",
            "transcript_summary", "raw_ref")


class TestPayloadSchema(Base):
    def test_응답_페이로드_필수키와_타입(self):
        p = W.build_payload("SR-0001", answered=True, answers=W.PROFILES["watch"])
        for k in REQUIRED:
            self.assertIn(k, p)
        self.assertIsInstance(p["senior_id"], str)
        self.assertIsInstance(p["answered"], bool)
        self.assertIsInstance(p["mood_score"], int)
        self.assertTrue(1 <= p["mood_score"] <= 5)
        self.assertIn(p["risk_level"], ("low", "mid", "high"))
        self.assertIsInstance(p["transcript_summary"], str)
        self.assertTrue(p["raw_ref"].startswith("wb_"))
        self.assertEqual(p["schema"], W.SCHEMA)

    def test_미응답_페이로드(self):
        p = W.build_payload("SR-0002", answered=False)
        self.assertFalse(p["answered"])
        self.assertIsNone(p["mood_score"])
        self.assertEqual(p["risk_level"], "unknown")
        self.assertIn("no_answer", p["flags"])
        for k in REQUIRED:
            self.assertIn(k, p)

    def test_페이로드는_JSON_직렬화_가능(self):
        p = W.build_payload("SR-3", answered=True, answers=W.PROFILES["risk"])
        self.assertIsInstance(json.dumps(p, ensure_ascii=False), str)

    def test_raw_ref_는_매번_다르다(self):
        a = W.build_payload("SR-4", answered=True, answers=W.PROFILES["ok"])["raw_ref"]
        b = W.build_payload("SR-4", answered=True, answers=W.PROFILES["ok"])["raw_ref"]
        self.assertNotEqual(a, b)


# ==========================================================================
# 3) 개인정보 (§QUALITY_BAR 3)
# ==========================================================================
class TestPii(Base):
    PII_ANSWERS = {
        "mood": "저는 홍길동인데요 좀 우울해요",
        "meal": "제 번호 010-1234-5678 로 연락 주세요",
        "sleep": "주민번호는 900101-1234567 이에요",
        "pain": "무릎이 쑤셔요",
    }

    def test_요약에_성명_전화번호_주민번호가_없다(self):
        p = W.build_payload("SR-9", answered=True, answers=self.PII_ANSWERS)
        s = p["transcript_summary"]
        self.assertNotIn("홍길동", s)
        self.assertNotIn("010-1234-5678", s)
        self.assertNotIn("900101-1234567", s)

    def test_페이로드_전체에_원문_발화가_실리지_않는다(self):
        p = W.build_payload("SR-9", answered=True, answers=self.PII_ANSWERS)
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn("홍길동", blob)
        self.assertNotIn("1234-5678", blob)
        self.assertNotIn("900101", blob)

    def test_최근목록에도_원문이_남지_않는다(self):
        W.run_wellbeing("SR-9", None, answers=self.PII_ANSWERS)
        blob = json.dumps(W.RECENT, ensure_ascii=False)
        self.assertNotIn("홍길동", blob)
        self.assertNotIn("1234-5678", blob)


# ==========================================================================
# 4) HMAC 서명
# ==========================================================================
class TestSignature(Base):
    def test_서명_검증_왕복(self):
        body = b'{"senior_id":"SR-1"}'
        ts, sig = W.sign(body, "s3cret", 1_700_000_000)
        self.assertTrue(sig.startswith("sha256="))
        self.assertTrue(W.verify_signature(body, "s3cret", ts, sig, now=1_700_000_000))

    def test_본문이_바뀌면_거부(self):
        ts, sig = W.sign(b"a", "s3cret", 1000)
        self.assertFalse(W.verify_signature(b"b", "s3cret", ts, sig, now=1000))

    def test_다른_키는_거부(self):
        ts, sig = W.sign(b"a", "s3cret", 1000)
        self.assertFalse(W.verify_signature(b"a", "other", ts, sig, now=1000))

    def test_오래된_타임스탬프는_거부_재전송_방지(self):
        ts, sig = W.sign(b"a", "s3cret", 1000)
        self.assertFalse(W.verify_signature(b"a", "s3cret", ts, sig, now=1000 + 3600))

    def test_타임스탬프_위조시_서명불일치(self):
        _, sig = W.sign(b"a", "s3cret", 1000)
        self.assertFalse(W.verify_signature(b"a", "s3cret", 1200, sig, now=1200))

    def test_시크릿_미설정이면_서명_생략(self):
        ts, sig = W.sign(b"a", "")
        self.assertEqual(sig, "")
        self.assertIsInstance(ts, str)
        self.assertFalse(W.verify_signature(b"a", "", ts, ""))

    def test_잘못된_타임스탬프_형식은_거부(self):
        _, sig = W.sign(b"a", "s3cret", 1000)
        self.assertFalse(W.verify_signature(b"a", "s3cret", "어제", sig))

    def test_전송헤더에_서명이_실린다(self):
        os.environ["CALLBACK_SECRET"] = "s3cret"
        seen = {}

        class R(object):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 200

        def fake_urlopen(req, timeout=None):
            seen["headers"] = dict(req.headers)
            seen["body"] = req.data
            return R()

        W.urllib.request.urlopen = fake_urlopen
        out = W.deliver("https://eum.example.org/hook", {"senior_id": "SR-1"})
        self.assertTrue(out["delivered"])
        self.assertTrue(out["signed"])
        hdrs = {k.lower(): v for k, v in seen["headers"].items()}
        self.assertIn(W.SIGNATURE_HEADER.lower(), hdrs)
        self.assertTrue(W.verify_signature(seen["body"], "s3cret",
                                           hdrs[W.TIMESTAMP_HEADER.lower()],
                                           hdrs[W.SIGNATURE_HEADER.lower()]))

    def test_시크릿_없으면_서명헤더_없이_전송(self):
        class R(object):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 200

        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["headers"] = {k.lower() for k in req.headers}
            return R()

        W.urllib.request.urlopen = fake_urlopen
        out = W.deliver("https://eum.example.org/hook", {"a": 1})
        self.assertTrue(out["delivered"])
        self.assertFalse(out["signed"])
        self.assertNotIn(W.SIGNATURE_HEADER.lower(), seen["headers"])


# ==========================================================================
# 5) 콜백 URL 가드 (SSRF)
# ==========================================================================
class TestCallbackGuard(Base):
    def test_공개_https_는_허용(self):
        self.assertTrue(W.check_callback_url("https://eum.example.org/hook")[0])

    def test_루프백_사설_링크로컬_메타데이터_차단(self):
        for u in ("https://127.0.0.1/cb", "https://localhost/cb",
                  "https://192.168.0.10/cb", "https://10.0.0.5/cb",
                  "https://169.254.169.254/latest/meta-data",
                  "https://metadata.google.internal/x",
                  "https://svc.internal/cb"):
            ok, reason = W.check_callback_url(u)
            self.assertFalse(ok, u)
            self.assertTrue(reason)

    def test_http_와_비웹_스킴_차단(self):
        self.assertFalse(W.check_callback_url("http://eum.example.org/cb")[0])
        self.assertFalse(W.check_callback_url("ftp://eum.example.org/cb")[0])
        self.assertFalse(W.check_callback_url("file:///etc/passwd")[0])
        self.assertFalse(W.check_callback_url("")[0])

    def test_로컬개발_스위치로만_http_허용(self):
        os.environ["WELLBEING_ALLOW_INSECURE"] = "1"
        self.assertTrue(W.check_callback_url("http://localhost:3000/cb")[0])

    def test_화이트리스트가_있으면_그_밖은_차단(self):
        os.environ["WELLBEING_CALLBACK_HOSTS"] = "eum.example.org"
        self.assertTrue(W.check_callback_url("https://eum.example.org/hook")[0])
        self.assertFalse(W.check_callback_url("https://evil.example.com/hook")[0])

    def test_과도하게_긴_URL_차단(self):
        self.assertFalse(W.check_callback_url("https://e.org/" + "a" * 3000)[0])


# ==========================================================================
# 6) 실행 흐름
# ==========================================================================
class TestRun(Base):
    def test_콜백_없으면_페이로드만_생성(self):
        r = W.run_wellbeing("SR-1", None, profile="ok")
        self.assertTrue(r["delivery"]["skipped"])
        self.assertEqual(r["payload"]["risk_level"], "low")
        self.assertIn("0원", r["billing"])

    def test_콜백에_페이로드가_그대로_전달된다(self):
        got = []
        r = W.run_wellbeing("SR-2", "https://eum.example.org/hook", profile="risk",
                            deliver_fn=lambda u, p: (got.append((u, p)),
                                                     {"delivered": True, "status": 200})[1])
        self.assertTrue(r["ok"])
        self.assertEqual(got[0][0], "https://eum.example.org/hook")
        self.assertEqual(got[0][1]["risk_level"], "high")

    def test_answers_직접_주입(self):
        r = W.run_wellbeing("SR-3", None, answers={"mood": "좋아요", "meal": "세 끼 잘 챙겨 먹었어요",
                                                   "sleep": "푹 잘 잤어요", "pain": "아픈 데 없어요"})
        self.assertEqual(r["payload"]["risk_level"], "low")

    def test_빈_답변은_미응답으로_보고(self):
        r = W.run_wellbeing("SR-4", None, profile="no_answer")
        self.assertFalse(r["payload"]["answered"])
        self.assertEqual(r["payload"]["risk_level"], "unknown")

    def test_잘못된_senior_id_거부(self):
        for bad in ("", "  ", "a" * 65, "SR 0001", "SR/../etc"):
            self.assertRaises(ValueError, W.run_wellbeing, bad, None)

    def test_알수없는_프로필_거부(self):
        self.assertRaises(ValueError, W.run_wellbeing, "SR-5", None, "없는프로필")

    def test_차단된_콜백은_실행_전에_거부(self):
        self.assertRaises(ValueError, W.run_wellbeing, "SR-6", "https://127.0.0.1/cb")

    def test_최근목록은_상한을_지킨다(self):
        for i in range(W._MAX_RECENT + 10):
            W.run_wellbeing("SR-%d" % i, None, profile="ok")
        self.assertEqual(len(W.RECENT), W._MAX_RECENT)

    def test_전송실패는_삼키지_않는다(self):
        def boom(req, timeout=None):
            raise OSError("연결 거부")

        W.urllib.request.urlopen = boom
        out = W.deliver("https://eum.example.org/hook", {"a": 1})
        self.assertFalse(out["delivered"])
        self.assertTrue(out["error"])
        self.assertIn("OSError", out["error"])

    def test_콜백_비정상_상태코드는_실패로_본다(self):
        class R(object):
            status = 500

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 500

        W.urllib.request.urlopen = lambda req, timeout=None: R()
        out = W.deliver("https://eum.example.org/hook", {"a": 1})
        self.assertFalse(out["delivered"])
        self.assertEqual(out["status"], 500)
        self.assertTrue(out["error"])

    def test_전송실패_사유에_URL이_새지_않는다(self):
        def boom(req, timeout=None):
            raise OSError("https://secret.example.org/hook?token=abc 연결 실패")

        W.urllib.request.urlopen = boom
        out = W.deliver("https://secret.example.org/hook?token=abc", {"a": 1})
        self.assertNotIn("token=abc", out["error"])


# ==========================================================================
# 7) 엔드포인트
# ==========================================================================
class TestEndpoint(Base):
    def test_GET_은_시나리오_안내를_준다(self):
        res = call("GET")
        self.assertEqual(res.status, 200)
        b = res.body()
        self.assertTrue(b["ok"])
        self.assertEqual(len(b["questions"]), 4)
        self.assertEqual(b["mode"], "simulation")
        self.assertEqual(b["result_schema"], W.SCHEMA)

    def test_GET_은_시크릿_원문을_노출하지_않는다(self):
        os.environ["CALLBACK_SECRET"] = "TOP-SECRET-VALUE"
        res = call("GET")
        self.assertNotIn("TOP-SECRET-VALUE", res.wfile.data.decode("utf-8"))
        self.assertTrue(res.body()["signature"]["enabled"])

    def test_외부_오리진은_거부(self):
        res = call("GET", headers={"sec-fetch-site": "cross-site",
                                   "origin": "https://evil.example.com"})
        self.assertIn(res.status, (401, 403))

    def test_POST_정상_dry_run(self):
        res = call("POST", {"senior_id": "SR-0001"}, path="/api/wellbeing/call")
        self.assertEqual(res.status, 200)
        p = res.body()["payload"]
        for k in REQUIRED:
            self.assertIn(k, p)

    def test_POST_는_하위경로와_쿼리_모두_받는다(self):
        for path in ("/api/wellbeing", "/api/wellbeing/call", "/api/wellbeing?op=call"):
            res = call("POST", {"senior_id": "SR-0001"}, path=path)
            self.assertEqual(res.status, 200, path)

    def test_POST_지원하지_않는_동작은_400(self):
        res = call("POST", {"senior_id": "SR-1"}, path="/api/wellbeing/delete")
        self.assertEqual(res.status, 400)

    def test_POST_senior_id_필수(self):
        res = call("POST", {})
        self.assertEqual(res.status, 400)
        self.assertFalse(res.body()["ok"])

    def test_POST_알수없는_프로필은_400(self):
        res = call("POST", {"senior_id": "SR-1", "profile": "없는거"})
        self.assertEqual(res.status, 400)

    def test_POST_answers_타입오류는_400(self):
        res = call("POST", {"senior_id": "SR-1", "answers": "문자열"})
        self.assertEqual(res.status, 400)

    def test_POST_차단된_콜백은_400(self):
        res = call("POST", {"senior_id": "SR-1", "callback_url": "https://10.0.0.1/cb"})
        self.assertEqual(res.status, 400)

    def test_POST_본문없음은_400(self):
        res = call("POST")
        self.assertEqual(res.status, 400)

    def test_POST_과대본문은_413(self):
        res = call("POST", {"senior_id": "SR-1", "pad": "x" * (W.MAX_BODY + 10)})
        self.assertEqual(res.status, 413)

    def test_실발신_요청은_501_승인필요(self):
        res = call("POST", {"senior_id": "SR-1", "mode": "live"})
        self.assertEqual(res.status, 501)
        self.assertIn("승인", res.body()["error"])

    def test_CPAAS_LIVE가_켜져도_시뮬레이션_외에는_막힌다(self):
        os.environ["CPAAS_LIVE"] = "1"
        res = call("POST", {"senior_id": "SR-1"})
        self.assertEqual(res.status, 501)

    def test_웹훅_전송실패는_502로_드러난다(self):
        def boom(req, timeout=None):
            raise OSError("연결 거부")

        W.urllib.request.urlopen = boom
        res = call("POST", {"senior_id": "SR-1",
                            "callback_url": "https://eum.example.org/hook"})
        self.assertEqual(res.status, 502)
        b = res.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "CALLBACK_DELIVERY_FAILED")

    def test_웹훅_성공은_200이고_전송결과를_담는다(self):
        class R(object):
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 202

        W.urllib.request.urlopen = lambda req, timeout=None: R()
        res = call("POST", {"senior_id": "SR-1",
                            "callback_url": "https://eum.example.org/hook"})
        self.assertEqual(res.status, 200)
        self.assertTrue(res.body()["delivery"]["delivered"])

    def test_recent_조회(self):
        call("POST", {"senior_id": "SR-7"})
        res = call("GET", path="/api/wellbeing?op=recent")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.body()["recent"][0]["senior_id"], "SR-7")

    def test_OPTIONS_는_204(self):
        res = call("OPTIONS")
        self.assertEqual(res.status, 204)


# ==========================================================================
# 8) 엔진 연동 (프롬프트 등록)
# ==========================================================================
class TestEngineWiring(Base):
    def test_안부_프롬프트가_엔진에_등록돼_있다(self):
        import engine
        self.assertTrue(hasattr(engine, "PROMPT_WELLBEING"))
        for kw in ("기분", "식사", "수면", "통증"):
            self.assertIn(kw, engine.PROMPT_WELLBEING)

    def test_안부_프롬프트는_의료조언을_금지한다(self):
        import engine
        self.assertIn("의료조언", engine.PROMPT_WELLBEING)

    def test_sim_call_에_안부_대본이_있다(self):
        import sim_call
        self.assertIn("wellbeing", sim_call.SCRIPTS)
        self.assertGreaterEqual(len(sim_call.SCRIPTS["wellbeing"]), 5)


# ==========================================================================
# 9) 웹훅 재시도 (지수 백오프 · 실패 보관)
# ==========================================================================
class TestRetry(Base):
    def _fn(self, results):
        seq, calls = list(results), []

        def fn(u, pl):
            calls.append(u)
            return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]

        return fn, calls

    def test_5xx는_재시도하고_성공하면_멈춘다(self):
        fn, calls = self._fn([{"delivered": False, "status": 503},
                              {"delivered": True, "status": 200}])
        out = W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_1"},
                                   attempts=3, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertTrue(out["delivered"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(len(out["retries"]), 1)

    def test_타임아웃도_재시도_대상(self):
        fn, calls = self._fn([{"delivered": False, "status": None, "error": "콜백 전송 실패(timeout)"}])
        W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_2"},
                             attempts=3, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertEqual(len(calls), 3)

    def test_4xx는_재시도하지_않는다(self):
        fn, calls = self._fn([{"delivered": False, "status": 400, "error": "bad"}])
        out = W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_3"},
                                   attempts=3, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertEqual(len(calls), 1)
        self.assertFalse(out["retryable"])

    def test_백오프는_지수적으로_늘어난다(self):
        waits = []
        fn, _ = self._fn([{"delivered": False, "status": 500}])
        W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_4"},
                             attempts=3, deliver_fn=fn, sleep_fn=waits.append)
        self.assertEqual(waits, [W.WEBHOOK_BACKOFF, W.WEBHOOK_BACKOFF * 2])
        self.assertTrue(all(w <= W.WEBHOOK_BACKOFF_MAX for w in waits))

    def test_시간예산을_넘기면_재시도를_접고_사유를_남긴다(self):
        clock = [0.0]
        fn, calls = self._fn([{"delivered": False, "status": 500}])

        def now():
            clock[0] += W.WEBHOOK_BUDGET  # 첫 시도부터 예산 소진
            return clock[0]

        out = W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_5"},
                                   attempts=3, deliver_fn=fn,
                                   sleep_fn=lambda s: None, now_fn=now)
        self.assertEqual(len(calls), 1)
        self.assertIn("gave_up", out)
        self.assertIn("예산", out["gave_up"])
        self.assertEqual(W.FAILED[0]["gave_up"], out["gave_up"])

    def test_최종실패는_보관되고_URL_경로는_남지_않는다(self):
        fn, _ = self._fn([{"delivered": False, "status": 502, "error": "콜백이 502 로 응답했습니다"}])
        W.deliver_with_retry("https://eum.example.org/hook?token=SECRET",
                             {"raw_ref": "wb_6", "senior_id": "SR-6"},
                             attempts=2, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertEqual(len(W.FAILED), 1)
        rec = W.FAILED[0]
        self.assertEqual(rec["callback_host"], "eum.example.org")
        self.assertEqual(rec["raw_ref"], "wb_6")
        self.assertEqual(rec["attempts"], 2)
        self.assertNotIn("SECRET", json.dumps(W.FAILED, ensure_ascii=False))
        self.assertNotIn("/hook", json.dumps(W.FAILED, ensure_ascii=False))

    def test_성공하면_실패보관에_남기지_않는다(self):
        fn, _ = self._fn([{"delivered": True, "status": 200}])
        W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_7"},
                             attempts=3, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertEqual(W.FAILED, [])

    def test_실패보관은_상한을_지킨다(self):
        fn, _ = self._fn([{"delivered": False, "status": 400}])
        for i in range(W._MAX_FAILED + 5):
            W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_%d" % i},
                                 attempts=1, deliver_fn=fn, sleep_fn=lambda s: None)
        self.assertEqual(len(W.FAILED), W._MAX_FAILED)

    def test_환경변수로_시도횟수를_조절한다(self):
        os.environ["WELLBEING_WEBHOOK_RETRIES"] = "2"
        self.assertEqual(W._attempts_setting(), 2)
        os.environ["WELLBEING_WEBHOOK_RETRIES"] = "99"
        self.assertEqual(W._attempts_setting(), 5)      # 상한
        os.environ["WELLBEING_WEBHOOK_RETRIES"] = "0"
        self.assertEqual(W._attempts_setting(), 1)      # 하한
        os.environ["WELLBEING_WEBHOOK_RETRIES"] = "abc"
        self.assertEqual(W._attempts_setting(), W.WEBHOOK_ATTEMPTS)

    def test_엔드포인트_실패응답에_시도횟수가_드러난다(self):
        os.environ["WELLBEING_WEBHOOK_RETRIES"] = "2"
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise OSError("연결 거부")

        W.urllib.request.urlopen = boom
        res = call("POST", {"senior_id": "SR-1", "profile": "ok",
                            "callback_url": "https://eum.example.org/hook"})
        self.assertEqual(res.status, 502)
        b = res.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["delivery"]["attempts"], 2)
        self.assertEqual(len(calls), 2)


# ==========================================================================
# 10) 결과 이력 조회 (raw_ref)
# ==========================================================================
class TestResultHistory(Base):
    def test_실행하면_raw_ref로_되짚을_수_있다(self):
        r = W.run_wellbeing("SR-1", None, profile="watch")
        ref = r["payload"]["raw_ref"]
        rec = W.get_result(ref)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["payload"]["risk_level"], r["payload"]["risk_level"])
        self.assertEqual(len(rec["questions"]), 4)

    def test_없는_ref는_None(self):
        self.assertIsNone(W.get_result("wb_없음"))
        self.assertIsNone(W.get_result(""))

    def test_이력은_상한을_지킨다(self):
        for i in range(W._MAX_RESULTS + 5):
            W.run_wellbeing("SR-%d" % i, None, profile="ok")
        self.assertEqual(len(W.RESULTS), W._MAX_RESULTS)
        self.assertEqual(len(W._RESULT_ORDER), W._MAX_RESULTS)

    def test_이력에_원문발화나_개인정보가_없다(self):
        W.run_wellbeing("SR-9", None, answers={
            "mood": "김철수인데 010-1234-5678 로 연락 주세요",
            "meal": "며칠째 못 먹었어요", "sleep": "못 자요", "pain": "어지러워요"})
        dumped = json.dumps(W.RESULTS, ensure_ascii=False)
        self.assertNotIn("김철수", dumped)
        self.assertNotIn("1234-5678", dumped)

    def test_GET_op_result_는_판정근거를_준다(self):
        r = W.run_wellbeing("SR-2", None, profile="risk")
        ref = r["payload"]["raw_ref"]
        res = call("GET", path="/api/wellbeing?op=result&ref=" + ref)
        self.assertEqual(res.status, 200)
        b = res.body()
        self.assertTrue(b["ok"])
        self.assertEqual(b["result"]["raw_ref"], ref)
        self.assertEqual(b["result"]["payload"]["risk_level"], "high")
        self.assertIn("dimensions", b["result"]["payload"])

    def test_GET_op_result_ref_없으면_400(self):
        res = call("GET", path="/api/wellbeing?op=result")
        self.assertEqual(res.status, 400)
        self.assertEqual(res.body()["code"], "VALIDATION_ERROR")

    def test_GET_op_result_없는_ref는_404(self):
        res = call("GET", path="/api/wellbeing?op=result&ref=wb_ffffffffffff")
        self.assertEqual(res.status, 404)
        self.assertEqual(res.body()["code"], "NOT_FOUND")

    def test_GET_op_failures_는_실패보관을_준다(self):
        fn, _ = self._failing()
        W.deliver_with_retry("https://eum.example.org/h", {"raw_ref": "wb_x"},
                             attempts=1, deliver_fn=fn, sleep_fn=lambda s: None)
        res = call("GET", path="/api/wellbeing?op=failures")
        self.assertEqual(res.status, 200)
        self.assertEqual(len(res.body()["failures"]), 1)

    def _failing(self):
        return (lambda u, pl: {"delivered": False, "status": 500}), []

    def test_GET_안내에_ops와_재시도정책이_있다(self):
        b = call("GET").body()
        self.assertIn("result", b["ops"])
        self.assertIn("failures", b["ops"])
        self.assertEqual(b["webhook_retry"]["attempts"], W.WEBHOOK_ATTEMPTS)

    def test_하위경로_op도_해석한다(self):
        res = call("GET", path="/api/wellbeing/recent")
        self.assertEqual(res.status, 200)
        self.assertIn("recent", res.body())


if __name__ == "__main__":
    unittest.main(verbosity=2)
