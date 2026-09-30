# -*- coding: utf-8 -*-
"""보이스 스튜디오·음성 디스패처의 **거부·실패 경로** 회귀 (2026-09-30).

앞선 회귀(tests/test_voice_studio.py)는 정상 제작 경로를 덮었다. 여기서 덮는 것은
평시에 돌지 않는 쪽 — 요청 한도, 깨진 본문, 합성 실패, 업스트림 이상이다.
이 경로들이 저장소 표준(표준 에러 봉투 · 오류 삼키기 금지 · 내부 문구 미노출)을
지키는지 고정한다. 실제로 다음 다섯 가지가 어긋나 있었다.

  (1) 거부 응답이 표준 봉투가 아니었다 — 429 에 `Retry-After` 가 없어서 대본
      13줄부터 막히는 배치 제작이 "얼마나 기다리면 되는지"를 알 수 없었다.
  (2) 합성 실패를 전부 502 한 문장으로 삼켰다 — edge-tts 미설치(내부 문제)와
      네트워크 장애가 같은 코드로 보고되고 모니터링에도 남지 않았다.
  (3) 스튜디오 오류가 `/api/stt` 로 기록됐다 — 모니터링에서 STT 장애를 쫓게 된다.
  (4) 깨진 `Content-Length` 가 500(+모니터링 알림)이 됐다 — 클라이언트 입력 오류다.
  (5) 복제 엔진 대기 55초가 서버리스 응답 한도(30초)보다 길어, 우리 502 대신
      플랫폼 오류가 먼저 나갔다.

네트워크는 쓰지 않는다 — edge_tts·urlopen 은 가짜로 갈아 끼운다.
실행: python -m pytest tests/test_voice_studio_edge.py -q
"""
import io
import json
import os
import sys
import types
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)
os.environ.setdefault("CALLBOT_API_KEY", "test-key")
os.environ.setdefault("CALLBOT_STT_PROVIDER", "sim")
os.environ.setdefault("CALLBOT_TTS_PROVIDER", "sim")

import _errors          # noqa: E402
import _monitoring      # noqa: E402
import _ratelimit       # noqa: E402
import _vstudio         # noqa: E402
import speech           # noqa: E402


class Req(speech.handler):
    """네트워크 없이 핸들러를 태우는 최소 껍데기(상태·헤더를 기록한다)."""

    def __init__(self, path, body=b"", headers=None, auth=True):
        self.path = path
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        h = {"content-length": str(len(body))}
        if auth:
            h["x-api-key"] = os.environ["CALLBOT_API_KEY"]
        h.update(headers or {})
        self.headers = h
        self.status = None
        self.sent = {}

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent[str(k)] = str(v)

    def end_headers(self):
        pass

    def raw(self):
        return self.wfile.getvalue().decode("utf-8", "replace")

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def run(path, body=b"", method="GET", headers=None, auth=True, reset=True):
    if reset:
        _ratelimit._HITS.clear()
    r = Req(path, body, headers=headers, auth=auth)
    getattr(r, "do_" + method)()
    return r


SYNTH = "/api/speech?mode=studio&op=synth"


def payload(**kw):
    d = {"text": "안녕하십니까", "voice": "ko-KR-SunHiNeural"}
    d.update(kw)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------
# (1) 거부 — 표준 봉투 · Retry-After
# --------------------------------------------------------------------------
class Deny(unittest.TestCase):
    def test_cross_origin_post_is_standard_envelope(self):
        r = run(SYNTH, payload(), "POST", auth=False)
        self.assertEqual(r.status, 403)
        b = r.body()
        self.assertEqual((b["ok"], b["code"], b["status"]), (False, "FORBIDDEN", 403))
        self.assertIn("Access-Control-Allow-Origin", r.sent)

    def test_cross_origin_get_is_standard_envelope(self):
        r = run("/api/speech?mode=studio&op=status", auth=False)
        self.assertEqual(r.status, 403)
        self.assertEqual(r.body()["code"], "FORBIDDEN")

    def test_deny_does_not_leak_config_hint(self):
        """거부 사유 문구(설정 힌트)는 디버그 모드가 아니면 나가지 않는다."""
        r = run(SYNTH, payload(), "POST", auth=False)
        self.assertNotIn("cross-origin", r.raw())
        self.assertNotIn("CALLBOT", r.raw())

    def test_rate_limited_carries_retry_after(self):
        """대본이 길면 분당 한도에 걸린다 — 얼마나 기다릴지 알려줘야 한다."""
        saved = os.environ.get("CALLBOT_RATE_LIMIT_SPEECH")
        os.environ["CALLBOT_RATE_LIMIT_SPEECH"] = "1"
        try:
            _ratelimit._HITS.clear()
            run("/api/speech?mode=studio&op=status", reset=False)      # 1회 소진
            r = run(SYNTH, payload(), "POST", reset=False)
        finally:
            if saved is None:
                os.environ.pop("CALLBOT_RATE_LIMIT_SPEECH", None)
            else:
                os.environ["CALLBOT_RATE_LIMIT_SPEECH"] = saved
            _ratelimit._HITS.clear()
        self.assertEqual(r.status, 429)
        self.assertEqual(r.body()["code"], "RATE_LIMITED")
        self.assertIn("Retry-After", r.sent, "429 에 Retry-After 가 없으면 대기 시간을 알 수 없다")
        self.assertGreaterEqual(int(r.sent["Retry-After"]), 1)
        self.assertIn("X-RateLimit-Limit", r.sent)


# --------------------------------------------------------------------------
# (2)(3) 합성 실패 — 분류 · 모니터링 · 라우트 귀속
# --------------------------------------------------------------------------
class SynthFailure(unittest.TestCase):
    def setUp(self):
        self.orig = _vstudio.synth_standard
        self.captured = []
        self._cap = _monitoring.capture_error

        def cap(exc, route="", method="", request_id=None, **kw):
            self.captured.append({"exc": type(exc).__name__, "route": route, "method": method})
            return "evt-1"
        _monitoring.capture_error = cap

    def tearDown(self):
        _vstudio.synth_standard = self.orig
        _monitoring.capture_error = self._cap

    def _fail_with(self, exc):
        _vstudio.synth_standard = lambda t, v, rt: (_ for _ in ()).throw(exc)
        return run(SYNTH, payload(), "POST")

    def test_import_error_is_500_and_reported(self):
        r = self._fail_with(ImportError("edge_tts"))
        self.assertEqual(r.status, 500)
        b = r.body()
        self.assertEqual(b["code"], "INTERNAL_ERROR")
        self.assertEqual(b.get("event_id"), "evt-1", "5xx 는 모니터링에 남아야 한다")
        self.assertEqual(len(self.captured), 1, "실패를 삼키지 않는다")

    def test_route_is_voice_studio_not_stt(self):
        """스튜디오 오류가 /api/stt 로 적히면 STT 장애를 쫓게 된다."""
        self._fail_with(ImportError("edge_tts"))
        self.assertEqual(self.captured[0]["route"], "/api/voice-studio")
        self.assertEqual(self.captured[0]["method"], "POST")

    def test_network_error_is_502(self):
        r = self._fail_with(urllib.error.URLError("dns"))
        self.assertEqual(r.status, 502)
        self.assertEqual(r.body()["code"], "UPSTREAM_ERROR")

    def test_timeout_is_504(self):
        r = self._fail_with(TimeoutError("slow"))
        self.assertEqual(r.status, 504)
        self.assertEqual(r.body()["code"], "UPSTREAM_TIMEOUT")

    def test_empty_audio_is_not_200(self):
        """빈 오디오를 200 으로 내보내면 브라우저가 알 수 없는 오류로 죽는다."""
        _vstudio.synth_standard = lambda t, v, rt: (b"", "audio/mpeg")
        r = run(SYNTH, payload(), "POST")
        self.assertEqual(r.status, 500)
        self.assertTrue(self.captured)

    def test_no_internal_text_in_response(self):
        r = self._fail_with(ImportError("edge_tts /var/task/api/_vstudio.py"))
        self.assertNotIn("edge_tts", r.raw())
        self.assertNotIn("/var/task", r.raw())
        self.assertNotIn("Traceback", r.raw())


# --------------------------------------------------------------------------
# (4) 입력 오류 — 400/413 과 필드 지목
# --------------------------------------------------------------------------
class BadInput(unittest.TestCase):
    def test_broken_content_length_is_400_not_500(self):
        r = run(SYNTH, payload(), "POST", headers={"content-length": "abc"})
        self.assertEqual(r.status, 400, "클라이언트 입력 오류가 500 이면 알림 노이즈가 된다")
        self.assertEqual(r.body()["code"], "INVALID_REQUEST")

    def test_missing_body_is_400(self):
        r = run(SYNTH, b"", "POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "body")

    def test_oversized_body_is_413(self):
        big = json.dumps({"text": "가" * 4000, "voice": "ko-KR-SunHiNeural"}).encode("utf-8")
        r = run(SYNTH, big, "POST")
        self.assertEqual(r.status, 413, "크기 문제를 400 '형식 확인'으로 알리면 고칠 수 없다")
        self.assertEqual(r.body()["code"], "PAYLOAD_TOO_LARGE")

    def test_bad_json_is_400_with_field(self):
        r = run(SYNTH, b"not json", "POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "body")

    def test_empty_text_points_at_text(self):
        r = run(SYNTH, payload(text="   "), "POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "text")

    def test_too_long_text_points_at_text(self):
        r = run(SYNTH, payload(text="가" * (_vstudio.MAX_CHARS + 1)), "POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "text")
        self.assertIn(str(_vstudio.MAX_CHARS), r.body()["error"])

    def test_missing_voice_is_input_error_not_consent_refusal(self):
        """목소리 미선택은 '동의 확인' 문제가 아니다 — 예전에는 409 로 엉뚱하게 안내했다."""
        r = run(SYNTH, json.dumps({"text": "안녕하십니까"}).encode("utf-8"), "POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "voice")
        self.assertNotIn("동의", r.body()["error"])

    def test_non_object_body_rejected(self):
        ok, v = _vstudio.validate(["안녕"])
        self.assertFalse(ok)
        self.assertEqual(v["field"], "body")

    def test_unknown_op_is_404_envelope(self):
        for method, path in (("GET", "/api/speech?mode=studio&op=nope"),
                             ("POST", "/api/speech?mode=studio&op=nope")):
            r = run(path, payload(), method)
            self.assertEqual(r.status, 404, path)
            self.assertEqual(r.body()["code"], "NOT_FOUND", path)

    def test_clone_refusal_is_409_with_field(self):
        r = run(SYNTH, payload(voice="v-unknown"), "POST")
        self.assertEqual(r.status, 409)
        b = r.body()
        self.assertEqual(b["code"], "CLONE_NOT_AVAILABLE")
        self.assertEqual(b["details"][0]["field"], "voice")
        self.assertIn("동의", b["error"])


# --------------------------------------------------------------------------
# 속도(rate) 정리 — 값이 그대로 합성기에 들어가지 않는다
# --------------------------------------------------------------------------
class Rate(unittest.TestCase):
    def test_clamped(self):
        self.assertEqual(_vstudio.validate({"text": "안", "voice": "v", "rate": 9})[1]["rate"], 1.25)
        self.assertEqual(_vstudio.validate({"text": "안", "voice": "v", "rate": 0})[1]["rate"], 0.8)

    def test_non_numeric_falls_back(self):
        for bad in ("빠르게", None, [1], {"a": 1}):
            self.assertEqual(_vstudio.validate({"text": "안", "voice": "v", "rate": bad})[1]["rate"], 1.0, bad)

    def test_nan_and_inf_never_reach_synth(self):
        """NaN 은 비교가 모두 False 다 — min(상한, 값) 순서가 바뀌면 그대로 통과한다."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            rate = _vstudio.validate({"text": "안", "voice": "v", "rate": bad})[1]["rate"]
            self.assertTrue(0.8 <= rate <= 1.25, bad)
            self.assertEqual(rate, rate, "NaN 이 통과했다")


# --------------------------------------------------------------------------
# (5) 복제 엔진 — 예산 · 서명 · 업스트림 이상
# --------------------------------------------------------------------------
class CloneEngine(unittest.TestCase):
    ENV = {"VOICE_ENGINE_URL": "https://gpu.example.com/",
           "VOICE_ENGINE_SECRET": " s3cret ",
           "VOICE_STUDIO_VOICES": json.dumps([{"id": "v-ok", "consent": "ok"}])}

    def setUp(self):
        self.calls = []
        self._urlopen = urllib.request.urlopen

        def fake(req, timeout=None):
            self.calls.append({"url": req.full_url, "timeout": timeout,
                               "headers": {k.lower(): v for k, v in req.headers.items()},
                               "body": req.data})
            return self.response

        class Res(object):
            def __init__(self, payload):
                self._p = payload

            def read(self):
                return json.dumps(self._p).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        self.Res = Res
        self.response = Res({"audio_b64": "AAAA"})
        urllib.request.urlopen = fake

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    def test_budget_fits_serverless_limit(self):
        self.assertLessEqual(_vstudio.CLONE_TIMEOUT, 28,
                             "서버리스 응답 한도(30초) 안에서 표준 봉투로 답해야 한다")
        _vstudio.synth_clone("안녕", "v-ok", 1.0, self.ENV)
        self.assertEqual(self.calls[0]["timeout"], _vstudio.CLONE_TIMEOUT)

    def test_signature_and_url(self):
        _vstudio.synth_clone("안녕", "v-ok", 1.0, self.ENV)
        c = self.calls[0]
        self.assertEqual(c["url"], "https://gpu.example.com/v1/synthesize")
        ts = c["headers"]["x-timestamp"]
        self.assertEqual(c["headers"]["x-signature"],
                         _vstudio.sign("s3cret", ts, c["body"]), "비밀값 앞뒤 공백은 제거된다")

    def test_worker_without_audio_is_upstream_error(self):
        """워커가 다른 형태로 답한 것은 우리 버그가 아니라 업스트림 장애다."""
        for bad in ({}, {"audio_b64": ""}, {"error": "busy"}, ["nope"]):
            self.response = self.Res(bad)
            with self.assertRaises(urllib.error.URLError, msg=repr(bad)):
                _vstudio.synth_clone("안녕", "v-ok", 1.0, self.ENV)

    def test_secret_never_in_status(self):
        self.assertNotIn("s3cret", json.dumps(_vstudio.status(self.ENV), ensure_ascii=False))


# --------------------------------------------------------------------------
# 표준 음성 합성 — edge_tts 는 가짜로 갈아 끼운다(네트워크 미사용)
# --------------------------------------------------------------------------
class SynthStandard(unittest.TestCase):
    def setUp(self):
        self.seen = {}
        outer = self

        class Communicate(object):
            def __init__(self, text, voice, rate=None):
                outer.seen = {"text": text, "voice": voice, "rate": rate}

            async def stream(self):
                for ch in outer.chunks:
                    yield ch
        self.chunks = [{"type": "WordBoundary"}, {"type": "audio", "data": b"ID3"},
                       {"type": "audio", "data": b"xx"}]
        self.mod = types.ModuleType("edge_tts")
        self.mod.Communicate = Communicate
        self._saved = sys.modules.get("edge_tts")
        sys.modules["edge_tts"] = self.mod

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = self._saved

    def test_audio_chunks_only(self):
        data, mime = _vstudio.synth_standard("안녕", "ko-KR-SunHiNeural", 1.0)
        self.assertEqual((data, mime), (b"ID3xx", "audio/mpeg"))

    def test_rate_string_format(self):
        _vstudio.synth_standard("안녕", "v", 1.25)
        self.assertEqual(self.seen["rate"], "+25%")
        _vstudio.synth_standard("안녕", "v", 0.8)
        self.assertEqual(self.seen["rate"], "-20%")
        _vstudio.synth_standard("안녕", "v", 1.0)
        self.assertEqual(self.seen["rate"], "+0%")

    def test_empty_stream_raises(self):
        self.chunks = [{"type": "WordBoundary"}]
        with self.assertRaises(RuntimeError):
            _vstudio.synth_standard("안녕", "v", 1.0)


# --------------------------------------------------------------------------
# 디스패처 — 모드 판별 장애 · 위임 실패 · OPTIONS
# --------------------------------------------------------------------------
class Dispatcher(unittest.TestCase):
    def test_mode_never_raises(self):
        for weird in (123, object(), b"/api/tts"):
            self.assertIn(speech._mode(weird), ("stt", "tts", "studio"), repr(weird))

    def test_delegation_failure_is_standard_envelope(self):
        for method, name in (("GET", "handle_get"), ("POST", "handle_post")):
            orig = getattr(_vstudio, name)
            setattr(_vstudio, name, lambda h: (_ for _ in ()).throw(RuntimeError("boom /var/task")))
            try:
                r = run("/api/speech?mode=studio&op=%s" % ("status" if method == "GET" else "synth"),
                        payload(), method)
            finally:
                setattr(_vstudio, name, orig)
            self.assertEqual(r.status, 500, method)
            self.assertEqual(r.body()["code"], "INTERNAL_ERROR", method)
            self.assertNotIn("/var/task", r.raw())

    def test_delegation_failure_route_matches_mode(self):
        seen = []
        real = speech._errors

        class Shim(object):
            def handle(self, h, exc, route="", method="", rq=None):
                seen.append(route)
                return real.send(h, status=500)
        speech._errors = Shim()
        try:
            for mode, want in (("studio", "/api/voice-studio"), ("tts", "/api/tts"), ("stt", "/api/stt")):
                orig_g, orig_p = _vstudio.handle_get, _vstudio.handle_post
                _vstudio.handle_get = lambda h: (_ for _ in ()).throw(RuntimeError("x"))
                _vstudio.handle_post = _vstudio.handle_get
                boom = lambda self_: (_ for _ in ()).throw(RuntimeError("x"))
                saved = {}
                for mod, meth in ((speech._tts, "do_GET"), (speech._stt, "do_GET"), (speech._stt, "do_POST")):
                    saved[(mod, meth)] = getattr(mod.handler, meth)
                    setattr(mod.handler, meth, boom)
                try:
                    run("/api/speech?mode=%s&op=synth" % mode, payload(), "POST")
                    run("/api/speech?mode=%s&op=status" % mode, method="GET")
                finally:
                    for (mod, meth), fn in saved.items():
                        setattr(mod.handler, meth, fn)
                    _vstudio.handle_get, _vstudio.handle_post = orig_g, orig_p
                self.assertEqual(set(seen), {want}, mode)
                seen[:] = []
        finally:
            speech._errors = real

    def test_options_is_204_with_methods(self):
        r = run("/api/speech?mode=studio", method="OPTIONS")
        self.assertEqual(r.status, 204)
        self.assertIn("POST", r.sent.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(r.sent.get("Content-Length"), "0")
        self.assertIn("Access-Control-Allow-Origin", r.sent)

    def test_options_delegates_when_stt_defines_it(self):
        """_stt 가 do_OPTIONS 를 갖게 되면 디스패처는 그쪽을 써야 한다(규약 고정)."""
        seen = []
        speech._stt.handler.do_OPTIONS = lambda self_: seen.append(1)
        try:
            run("/api/speech?mode=stt", method="OPTIONS")
        finally:
            del speech._stt.handler.do_OPTIONS
        self.assertEqual(seen, [1])

    def test_options_failure_is_silent(self):
        """OPTIONS 응답 기록이 실패해도 프리플라이트가 예외로 터지지 않는다."""
        class Broken(Req):
            def send_response(self, *a):
                raise RuntimeError("socket gone")
        _ratelimit._HITS.clear()
        Broken("/api/speech?mode=studio").do_OPTIONS()   # 예외가 새어나오면 실패

    def test_success_response_carries_cors_and_no_store(self):
        """오류만 CORS 를 달면 허용 오리진에서 성공 응답만 못 읽는 엇갈림이 생긴다."""
        r = run("/api/speech?mode=studio&op=status")
        self.assertEqual(r.status, 200)
        self.assertIn("Access-Control-Allow-Origin", r.sent)
        self.assertEqual(r.sent.get("Cache-Control"), "no-store")


# --------------------------------------------------------------------------
# 목소리 목록·대본 파싱의 방어 분기
# --------------------------------------------------------------------------
class Parsing(unittest.TestCase):
    def test_broken_voice_list_is_empty_not_crash(self):
        for bad in ("{", "null", '{"a":1}', '["x"]', ""):
            self.assertEqual(_vstudio.clone_voices({"VOICE_STUDIO_VOICES": bad}), [], bad)

    def test_voice_fields_are_capped(self):
        v = _vstudio.clone_voices({"VOICE_STUDIO_VOICES": json.dumps(
            [{"id": "i" * 200, "name": "n" * 200, "note": "t" * 200, "consent": "ok"}])})[0]
        self.assertEqual((len(v["id"]), len(v["name"]), len(v["note"])), (64, 40, 80))

    def test_script_skips_comment_only_and_empty_body(self):
        rows = _vstudio.parse_script("name |    \n#주석\n\n  \nreal | 문장\n")
        self.assertEqual([r["name"] for r in rows], ["real"])

    def test_op_of_never_raises(self):
        self.assertEqual(_vstudio.op_of(123), "")
        self.assertEqual(_vstudio.op_of(None), "")

    def test_allow_origin_closes_on_failure(self):
        class NoHeaders(object):
            @property
            def headers(self):
                raise RuntimeError("gone")
        self.assertEqual(_vstudio._allow_origin(NoHeaders()), "null")


class Console(unittest.TestCase):
    """콘솔 배치 제작이 요청 한도를 만났을 때 — 남은 줄을 계속 두드리지 않는다.

    대본 한 줄이 요청 1건이라 13줄부터는 한도(speech 12/분)에 걸린다. 예전에는
    남은 줄마다 429 를 받으며 끝까지 돌고, 화면에는 '실패'만 남았다.
    """

    @classmethod
    def setUpClass(cls):
        h = io.open(os.path.join(ROOT, "public", "admin.html"), encoding="utf-8").read()
        i = h.find("var VS={st:null")
        assert i > 0, "보이스 스튜디오 스크립트를 찾지 못했다"
        cls.js = h[i:h.find("</script>", i)]

    def test_reads_retry_after(self):
        self.assertIn("r.status===429", self.js)
        self.assertIn("Retry-After", self.js, "대기 시간은 서버 헤더에서 읽는다(임의 숫자 금지)")

    def test_batch_stops_on_rate_limit(self):
        self.assertIn("e429.rate=true", self.js)
        self.assertIn("if(e&&e.rate)VS.stop=true", self.js, "한도에 걸리면 남은 줄을 멈춘다")

    def test_wait_is_reset_each_run(self):
        self.assertIn("VS.wait=0", self.js, "지난 실행의 대기 안내가 남으면 거짓 안내가 된다")
        self.assertIn("요청 한도에 걸려 멈췄습니다", self.js)

    def test_rate_limit_class_still_speech(self):
        """한도를 우회하려고 등급을 낮추면(과금 방어 후퇴) 여기서 걸린다."""
        self.assertEqual(_ratelimit.route_class("/api/voice-studio"), "speech")
        self.assertEqual(_ratelimit.route_class("/api/speech"), "speech")


if __name__ == "__main__":
    unittest.main(verbosity=2)
