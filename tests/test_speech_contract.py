# -*- coding: utf-8 -*-
"""음성 진입점(/api/speech = stt·tts·보이스 스튜디오)의 응답·로그 계약 회귀.

왜 이 파일인가 (COMMERCIAL_READINESS '테스트 커버리지' 17차)
16차가 보이스 스튜디오의 실패 경로를 저장소 표준(`_errors`)에 맞췄지만, **같은
함수에 얹혀 있는 STT·TTS 는 그대로였다.** 세 모드가 한 서버리스 함수(`speech.py`)로
들어오는데 모드마다 응답 헤더·오류 분류·로그가 달랐다.

확인하는 것
  1) 빈 오디오를 200 으로 내보내지 않는다 — `/api/tts` 가 0바이트를 성공으로
     위장하면 호출자는 '무음'을 재생하고 운영자는 아무것도 보지 못한다.
  2) 성공 응답이 오류 응답과 **같은 CORS·캐시 헤더**를 쓴다 — 허용된 다른
     오리진에서 오류만 읽히고 성공은 못 읽는 엇갈림을 막는다(`_vstudio` 와 같은 규약).
  3) 요청 1건 = 구조화 로그 1줄 + 응답 `X-Request-Id` — 성공·거부·오류 모두.
  4) 로그·응답 어디에도 `?text=` 원문(발화)이 실리지 않는다.
  5) 기본 접근로그 침묵 배선이 **모든** 핸들러에 걸려 있다(쿼리스트링 유출 차단).
  6) `_stt` 의 방어 분기(프로바이더 health 장애·키 부재·응답 형식 이탈).

네트워크·과금: `urlopen` 감시 + `edge_tts` 미사용(합성은 전부 대역).
실행: python -m pytest tests/test_speech_contract.py -q
"""
import io
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)

import _errors            # noqa: E402
import _guard             # noqa: E402
import _log               # noqa: E402
import _ratelimit         # noqa: E402
import _stt               # noqa: E402
import _tts               # noqa: E402
import speech             # noqa: E402


class NetworkTouched(AssertionError):
    pass


class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, str(k).lower(), d)


class Resp(object):
    """핸들러를 소켓 없이 태우고 상태·헤더·본문을 들여다보는 껍데기."""

    def __init__(self, path, body=b"", headers=None):
        self.status = None
        self.sent = []
        self.wfile = io.BytesIO()
        h = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.7"}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        if body:
            h.setdefault("content-length", str(len(body)))
        inst = speech.handler.__new__(speech.handler)
        inst.path = path
        inst.headers = FakeHeaders(h)
        inst.rfile = io.BytesIO(body)
        inst.wfile = self.wfile
        inst.send_response = lambda c, *a: setattr(self, "status", c)
        inst.send_header = lambda k, v: self.sent.append((str(k), str(v)))
        inst.end_headers = lambda: None
        self.inst = inst

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def raw(self):
        return self.wfile.getvalue()

    def json(self):
        return json.loads(self.raw().decode("utf-8"))


ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CALLBOT_STT_PROVIDER", "CALLBOT_TTS_PROVIDER", "SPEECH_LIVE",
       "GOOGLE_API_KEY", "GEMINI_API_KEY", "SENTRY_DSN")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV + ("CALLBOT_LOG",)}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["CALLBOT_LOG"] = "on"      # 구조화 로그를 실제로 받아 본다
        _ratelimit.reset()
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        self._synth = _tts._synth
        _tts._synth = lambda text: b"ID3\x03\x00fake-mp3"
        self._out = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        self.log_text = sys.stdout.getvalue()
        sys.stdout = self._out
        _tts._synth = self._synth
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def _boom(self, *a, **k):
        raise NetworkTouched("네트워크 호출 발생 — 테스트는 네트워크를 쓰지 않는다")

    def call(self, path, body=b"", method="GET", headers=None):
        _ratelimit.reset()
        r = Resp(path, body, headers)
        getattr(r.inst, "do_" + method)()
        return r

    def logs(self):
        """지금까지 stdout 에 쌓인 구조화 로그(JSON 1줄)들."""
        out = []
        for line in sys.stdout.getvalue().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out


# ==========================================================================
# 1) 빈 오디오를 성공으로 위장하지 않는다
# ==========================================================================
class TestEmptyAudio(Base):
    def test_empty_audio_is_5xx_not_silent_200(self):
        _tts._synth = lambda text: b""
        r = self.call("/api/speech?mode=tts&text=안녕하세요")
        self.assertGreaterEqual(r.status, 500,
                                "0바이트 오디오가 200 으로 나갔다 — 호출자는 무음을 재생한다")
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertNotIn("audio", str(b.get("error", "")))

    def test_synth_raises_on_empty_stream(self):
        """edge_tts 가 오디오 청크를 하나도 주지 않은 경우(대역)."""
        import types
        fake = types.ModuleType("edge_tts")

        class _C(object):
            def __init__(self, *a, **k):
                pass

            async def stream(self):
                if False:
                    yield {}
                return
        fake.Communicate = _C
        sys.modules["edge_tts"] = fake
        try:
            with self.assertRaises(RuntimeError):
                self._synth("안녕")
        finally:
            sys.modules.pop("edge_tts", None)

    def test_synth_returns_audio_bytes(self):
        import types
        fake = types.ModuleType("edge_tts")

        class _C(object):
            def __init__(self, *a, **k):
                pass

            async def stream(self):
                yield {"type": "WordBoundary"}
                yield {"type": "audio", "data": b"ID3"}
        fake.Communicate = _C
        sys.modules["edge_tts"] = fake
        try:
            self.assertEqual(self._synth("안녕"), b"ID3")
        finally:
            sys.modules.pop("edge_tts", None)

    def test_normal_audio_still_200(self):
        r = self.call("/api/speech?mode=tts&text=안녕하세요")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), "audio/mpeg")
        self.assertEqual(r.header("Content-Length"), str(len(r.raw())))


# ==========================================================================
# 2) 성공 응답도 오류와 같은 CORS·캐시 계약
# ==========================================================================
class TestResponseHeaders(Base):
    def _acao_of_error(self):
        r = self.call("/api/speech?mode=stt", headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)
        return r.header("Access-Control-Allow-Origin")

    def test_stt_get_success_has_same_cors_as_error(self):
        err = self._acao_of_error()
        r = self.call("/api/speech?mode=stt")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Access-Control-Allow-Origin"), err,
                         "성공만 CORS 헤더가 없으면 허용된 오리진에서 오류만 읽힌다")
        self.assertEqual(r.header("Cache-Control"), "no-store")

    def test_stt_post_success_has_cors(self):
        os.environ["CALLBOT_STT_PROVIDER"] = "sim"
        body = json.dumps({"audio": "AAAA", "mime": "audio/webm"}).encode()
        r = self.call("/api/speech?mode=stt", body, method="POST")
        self.assertEqual(r.status, 200, r.raw()[:200])
        self.assertIsNotNone(r.header("Access-Control-Allow-Origin"))
        self.assertEqual(r.header("Cache-Control"), "no-store")

    def test_tts_audio_success_has_cors(self):
        r = self.call("/api/speech?mode=tts&text=안녕")
        self.assertEqual(r.status, 200)
        self.assertIsNotNone(r.header("Access-Control-Allow-Origin"))
        self.assertEqual(r.header("Cache-Control"), "no-store")

    def test_disallowed_origin_is_not_reflected(self):
        os.environ["CALLBOT_API_KEY"] = "k-secret"
        r = self.call("/api/speech?mode=stt",
                      headers={"x-api-key": "k-secret", "origin": "https://evil.example",
                               "sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 200)
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"), "https://evil.example")


# ==========================================================================
# 3) 요청 1건 = 로그 1줄 + X-Request-Id
# ==========================================================================
class TestRequestLogging(Base):
    def test_success_emits_one_log_line(self):
        r = self.call("/api/speech?mode=tts&text=안녕하세요")
        rows = self.logs()
        self.assertEqual(len(rows), 1, "요청 1건에 로그가 %d줄" % len(rows))
        self.assertEqual(rows[0]["route"], "/api/tts")
        self.assertEqual(rows[0]["status"], 200)
        self.assertEqual(r.header("X-Request-Id"), rows[0]["request_id"])

    def test_deny_emits_log_with_status(self):
        self.call("/api/speech?mode=stt", headers={"sec-fetch-site": "cross-site"})
        rows = self.logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], 403)
        self.assertEqual(rows[0]["route"], "/api/stt")

    def test_error_emits_log_with_error_code(self):
        def boom(text):
            raise ValueError("실패")
        _tts._synth = boom
        self.call("/api/speech?mode=tts&text=안녕")
        rows = self.logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "error")
        self.assertEqual(rows[0]["error_code"], "VALUE_ERROR")

    def test_studio_route_is_not_logged_as_stt(self):
        r = self.call("/api/speech?mode=studio")
        self.assertEqual(r.status, 200, r.raw()[:200])
        rows = self.logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "/api/voice-studio",
                         "스튜디오 요청이 STT 로 적히면 모니터링에서 엉뚱한 장애를 쫓는다")

    def test_utterance_never_reaches_log(self):
        self.call("/api/speech?mode=tts&text=홍길동 고객님 환불 접수되었습니다")
        text = sys.stdout.getvalue()
        self.assertNotIn("홍길동", text, "발화 원문이 로그에 남았다")
        self.assertNotIn("text=", text)

    def test_send_response_records_status_for_the_log(self):
        """위임받은 핸들러가 직접 쓴 상태코드도 로그에 사실대로 남아야 한다."""
        r = Resp("/api/speech?mode=stt")
        del r.inst.send_response            # 인스턴스 스텁 제거 → 실제 메서드
        r.inst.request_version = "HTTP/1.1"
        r.inst.requestline = "GET /api/speech HTTP/1.1"
        r.inst._headers_buffer = []
        r.inst.send_response(207)
        self.assertEqual(r.inst._status, 207)

    def test_close_logs_status_the_delegate_wrote(self):
        r = Resp("/api/speech?mode=stt")
        rq = r.inst._begin("stt", "GET")
        r.inst._status = 503
        r.inst._close(rq)
        self.assertEqual(self.logs()[-1]["status"], 503)

    def test_logging_failure_never_breaks_the_response(self):
        """로그가 터져도 응답은 나간다 — 세 곳의 성공 경로 모두."""
        class _BadRq(object):
            request_id = "rq-bad"
            done_flag = False

            def finish(self, *a, **kw):
                raise RuntimeError("로그 실패")

        import _vstudio
        for writer in (lambda h: _stt.handler._send(h, {"ok": True}),
                       lambda h: _tts._respond(h, b"x", "audio/mpeg"),
                       lambda h: _vstudio._send(h, 200, {"ok": True})):
            r = Resp("/api/speech?mode=stt")
            r.inst._rq = _BadRq()
            writer(r.inst)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.header("X-Request-Id"), "rq-bad")
        r2 = Resp("/api/speech?mode=stt")
        r2.inst._close(_BadRq())            # 예외 없이 통과

    def test_error_body_carries_request_id(self):
        r = self.call("/api/speech?mode=stt", headers={"sec-fetch-site": "cross-site"})
        self.assertIn("request_id", r.json())
        self.assertEqual(r.json()["request_id"], r.header("X-Request-Id"))


# ==========================================================================
# 4) 기본 접근로그 침묵 — 신규 핸들러 드리프트 방지
# ==========================================================================
class TestAccessLogSilenced(unittest.TestCase):
    def test_every_handler_silences_default_access_log(self):
        """쿼리스트링(`?text=` 발화·`?t=` 웹훅 토큰)이 stderr 로 새지 않게."""
        missing = []
        for f in sorted(os.listdir(API)):
            if not f.endswith(".py") or f.startswith("_"):
                continue
            src = open(os.path.join(API, f), encoding="utf-8").read()
            if "class handler" not in src:
                continue
            if "log_message = _log.suppress_access_log" not in src:
                missing.append(f)
        self.assertEqual(missing, [],
                         "기본 접근로그가 그대로인 핸들러: %s (MONITORING_GUIDE §접근로그)" % missing)

    def test_speech_handler_binding_is_the_suppressor(self):
        self.assertIs(speech.handler.log_message, _log.suppress_access_log)


# ==========================================================================
# 5) _stt 방어 분기
# ==========================================================================
class TestSttDefensive(Base):
    def test_provider_health_survives_module_failure(self):
        saved = sys.modules.get("_speech_providers")
        import _speech_providers as sp
        orig = sp.health_report
        sp.health_report = lambda kind: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            h = _stt._provider_health()
            self.assertFalse(h["ok"])
            self.assertNotIn("boom", json.dumps(h), "내부 예외 문구가 새어 나갔다")
        finally:
            sp.health_report = orig
            if saved is not None:
                sys.modules["_speech_providers"] = saved

    def test_transcribe_without_key_raises_before_network(self):
        with self.assertRaises(RuntimeError):
            _stt.transcribe("AAAA", "audio/webm")   # urlopen 감시 중 — 네트워크면 다른 예외

    def test_transcribe_parses_and_tolerates_odd_payload(self):
        os.environ["GOOGLE_API_KEY"] = "k"

        class _R(object):
            def __init__(self, payload):
                self._p = json.dumps(payload).encode()

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        ok = {"candidates": [{"content": {"parts": [{"text": ' "환불이요" '}]}}]}
        urllib.request.urlopen = lambda req, timeout=None: _R(ok)
        self.assertEqual(_stt.transcribe("AAAA", "audio/webm"), "환불이요")
        urllib.request.urlopen = lambda req, timeout=None: _R({"candidates": []})
        self.assertEqual(_stt.transcribe("AAAA", "audio/webm"), "")

    def test_bad_mime_is_400_with_field(self):
        body = json.dumps({"audio": "AAAA", "mime": "application/zip"}).encode()
        r = self.call("/api/speech?mode=stt", body, method="POST")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["details"][0]["field"], "mime")


# ==========================================================================
# 6) 거부·오류 응답에 요청 ID 가 붙어도 비밀은 새지 않는다
# ==========================================================================
class TestNoLeak(Base):
    def test_webhook_token_hint_not_exposed(self):
        r = self.call("/api/speech?mode=stt", headers={"sec-fetch-site": "cross-site"})
        self.assertNotIn("CPAAS_WEBHOOK_TOKEN", r.raw().decode("utf-8"))

    def test_no_traceback_in_any_response(self):
        def boom(text):
            raise ValueError("내부 경로 C:/secret")
        _tts._synth = boom
        r = self.call("/api/speech?mode=tts&text=안녕")
        text = r.raw().decode("utf-8")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
