# -*- coding: utf-8 -*-
"""/api/speech 디스패처 — stt·tts 를 함수 하나로 합친 뒤에도 예전과 똑같이 답하는가.

왜 필요한가: Vercel Hobby 는 배포당 서버리스 함수 12개가 상한인데 `api/*.py` 가
그 위에 있었다. 줄이면서 stt·tts 를 `speech.py` 하나로 묶었다. 이때
**위임만 하고 상속을 안 하면** 원래 핸들러의 보조 메서드(`_send`)가 없어
`?health=1` 같은 경로가 AttributeError -> 500 으로 깨진다. 실제로 한 번 깨졌고
기존 테스트 1381건이 전부 통과하는 바람에 못 잡았다. 그래서 여기서 직접 태운다.

확인하는 것
  1) mode 판별 (rewrite 로 붙는 ?mode= · 직접 경로 · 기본값)
  2) tts·stt 양쪽이 200 을 돌려주고 본문 형태가 맞는가
  3) `_stt.handler` 의 보조 메서드가 실제로 상속됐는가 (위 사고의 직접 원인)
  4) 요청 제한 등급이 예전 stt·tts 와 같은 "speech" 인가 (합치면서 완화되면 안 된다)
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)

os.environ.setdefault("CALLBOT_API_KEY", "test-key")
os.environ.setdefault("CALLBOT_STT_PROVIDER", "sim")
os.environ.setdefault("CALLBOT_TTS_PROVIDER", "sim")

import _ratelimit          # noqa: E402
import _stt                # noqa: E402
import speech              # noqa: E402


class Req(speech.handler):
    """핸들러를 네트워크 없이 태우기 위한 최소 껍데기."""

    def __init__(self, path, body=b""):
        self.path = path
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"x-api-key": os.environ["CALLBOT_API_KEY"],
                        "content-length": str(len(body))}
        self.status = None
        self.sent_headers = {}

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass

    def body(self):
        return self.wfile.getvalue()

    def json(self):
        return json.loads(self.body().decode("utf-8"))


def run(path, body=b"", method="GET"):
    _ratelimit._HITS.clear()          # 연속 호출이 요청 제한에 걸리지 않게
    r = Req(path, body)
    getattr(r, "do_" + method)()
    return r


class TestMode(unittest.TestCase):
    def test_rewrite_query(self):
        self.assertEqual(speech._mode("/api/speech?mode=tts&text=x"), "tts")
        self.assertEqual(speech._mode("/api/speech?mode=stt"), "stt")

    def test_direct_path(self):
        """rewrite 를 거치지 않고 옛 경로가 직접 들어와도 알아본다."""
        self.assertEqual(speech._mode("/api/tts?text=x"), "tts")
        self.assertEqual(speech._mode("/api/stt"), "stt")

    def test_default_is_stt(self):
        self.assertEqual(speech._mode("/api/speech"), "stt")
        self.assertEqual(speech._mode(""), "stt")
        self.assertEqual(speech._mode("/api/speech?mode=%EA%B9%A8%EC%A7%90"), "stt")


class TestInheritance(unittest.TestCase):
    """위임만 하고 상속을 빠뜨리면 여기서 걸린다(그 사고의 직접 원인)."""

    def test_stt_helpers_are_inherited(self):
        self.assertTrue(issubclass(speech.handler, _stt.handler),
                        "speech.handler 는 _stt.handler 를 상속해야 한다")
        for m in ("_send",):
            self.assertTrue(hasattr(speech.handler, m),
                            "_stt.handler 의 보조 메서드 %s 가 없다" % m)

    def test_tts_handler_needs_no_helpers(self):
        """_tts 에 보조 메서드가 생기면 디스패처도 함께 손봐야 한다 — 그때 여기서 알린다."""
        import _tts
        extra = [n for n in vars(_tts.handler)
                 if not n.startswith("__") and not n.startswith("do_")]
        self.assertEqual(extra, [],
                         "_tts.handler 에 보조 메서드가 생겼다(%s). speech.handler 위임을 점검하라" % extra)


class TestResponses(unittest.TestCase):
    def test_tts_synthesize(self):
        r = run("/api/speech?mode=tts&text=안녕하세요")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["provider"], "sim")

    def test_tts_health(self):
        r = run("/api/speech?mode=tts&health=1")
        self.assertEqual(r.status, 200)
        self.assertIn("gate", r.json())

    def test_stt_health(self):
        """합치기 전 /api/stt?health=1 은 200 이었다. 500 이면 회귀다."""
        r = run("/api/speech?mode=stt&health=1")
        self.assertEqual(r.status, 200, r.body()[:200])
        self.assertTrue(r.json()["ok"])

    def test_tts_missing_text_is_400_not_500(self):
        r = run("/api/speech?mode=tts")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["code"], "INVALID_REQUEST")

    def test_old_path_still_works(self):
        r = run("/api/tts?text=hi")
        self.assertEqual(r.status, 200)

    def test_stt_post_rejects_bad_body(self):
        r = run("/api/speech?mode=stt", json.dumps({"audio": ""}).encode(), method="POST")
        self.assertEqual(r.status, 400)
        self.assertFalse(r.json()["ok"])

    def test_no_raw_exception_text_leaks(self):
        for path in ("/api/speech?mode=tts", "/api/speech?mode=stt&health=1"):
            r = run(path)
            self.assertNotIn("Traceback", r.body().decode("utf-8", "replace"))


class TestRateLimitClass(unittest.TestCase):
    def test_speech_class_preserved(self):
        """합치면서 등급이 default(더 느슨함)로 떨어지면 안 된다."""
        for p in ("/api/speech", "/api/stt", "/api/tts"):
            self.assertEqual(_ratelimit.route_class(p), "speech", p)


class TestFunctionCount(unittest.TestCase):
    def test_at_most_12_serverless_functions(self):
        """Vercel Hobby 상한. 밑줄로 시작하는 파일은 함수로 배포되지 않는다."""
        fns = sorted(f for f in os.listdir(API)
                     if f.endswith(".py") and not f.startswith("_"))
        self.assertLessEqual(len(fns), 12,
                             "서버리스 함수 %d개 — Hobby 상한 12 초과: %s" % (len(fns), fns))

    def test_library_modules_are_underscored(self):
        """핸들러가 없는 모듈이 api/ 에 노출되면 쓸데없이 함수 1칸을 먹는다."""
        bad = []
        for f in os.listdir(API):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            src = open(os.path.join(API, f), encoding="utf-8").read()
            if "class handler" not in src:
                bad.append(f)
        self.assertEqual(bad, [], "핸들러가 없는데 함수로 배포된다 → _ 접두사를 붙여라: %s" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
