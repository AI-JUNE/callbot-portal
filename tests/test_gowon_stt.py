# -*- coding: utf-8 -*-
"""자체 학습 전화 STT(gowon) 프로바이더 회귀 — 네트워크 0, `urlopen` 전부 대역.

배경 (2026-10-31 작업지시서 `WORKORDER_STT_GOWON_1031.md`)
창업진흥원 지원 GPU 에서 AI Hub 「저음질 전화망 음성인식」 데이터로 미세조정한
Whisper 모델을 포털의 음성 인식 경로에 붙였다. 모델은 **로컬 PC(CPU)** 에 두고
GPU 는 재학습할 때만 빌리므로 STT 서버는 꺼져 있을 수 있다 — 그래서 대체 경로가
선택이 아니라 필수다. 이 파일이 고정하는 것은 다음 네 가지다.

  1) 승인 전에는 **한 건도 나가지 않는다** — 게이트가 꺼져 있거나 주소·공유 비밀이
     없으면 PermissionError「[승인 필요]」이고, 팩토리는 sim 을 내준다.
  2) 나갈 때는 `_vstudio` 와 같은 수준으로 나간다 — `_urlguard` 통과 주소만,
     리다이렉트 미추종, 응답 상한, 서명은 `_vstudio.sign` 과 **같은 값**.
     (평문 http·사설 IP·클라우드 메타데이터 주소면 요청 자체를 만들지 않는다 —
     거부된 주소로 가면 **통화 음성과 HMAC 서명 헤더**가 그쪽으로 흘러간다.)
  3) 실패를 삼키지 않는다 — 타임아웃 504, 서버 5xx·깨진 JSON·리다이렉트 502,
     서명 거부(401)는 **우리 설정 오류**라 500(+모니터링 경보)으로 사람을 부른다.
  4) 대체 전사(레거시 Gemini)는 **사실을 적고** 이어 간다 — 응답에 `fallback`·
     `fallback_reason`, 모델 이름은 실제로 전사한 쪽. 설정 오류(401·주소 거부)는
     대체로 가리지 않는다. 로그에는 발화 원문도 공유 비밀도 남지 않는다.

실행: python -m pytest tests/test_gowon_stt.py -q
"""
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
if API not in sys.path:
    sys.path.insert(0, API)

import _errors                      # noqa: E402
import _speech_providers as sp      # noqa: E402
import _stt                         # noqa: E402
import _vstudio                     # noqa: E402

URL = "https://stt.example.org/v1/stt"
SECRET = "s3cr3t-shared-value-for-hmac"
AUDIO = "QUJDRA=="                  # 발화 원문은 base64 뒤에 있지만 로그 검사 대상이다
ENV = ("SPEECH_LIVE", "CALLBOT_STT_PROVIDER", "CALLBOT_TTS_PROVIDER",
       "GOWON_STT_URL", "GOWON_STT_SECRET", "GOWON_STT_HOSTS",
       "GOWON_STT_ALLOW_INSECURE", "GOWON_STT_MODEL_LABEL",
       "GOOGLE_API_KEY", "GEMINI_API_KEY", "CALLBOT_LOG")


class FakeRes(object):
    """urlopen 응답 대역 — 최종 주소와 본문만 흉내낸다."""

    def __init__(self, body=b"", url=None):
        self._body = body
        self._url = url

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["CALLBOT_LOG"] = "on"        # 구조화 로그를 실제로 받아 본다
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        self.calls = []
        self._transcribe = _stt.transcribe
        self._out = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        self.log_text = sys.stdout.getvalue()
        sys.stdout = self._out
        _stt.transcribe = self._transcribe
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _boom(self, *a, **kw):
        raise AssertionError("요청이 나갔다 — 이 경로는 네트워크를 쓰지 않아야 한다")

    # -- 환경 ------------------------------------------------------------
    def live(self, value="1"):
        os.environ["SPEECH_LIVE"] = value

    def configure(self, url=URL, secret=SECRET, **extra):
        if url is not None:
            os.environ["GOWON_STT_URL"] = url
        if secret is not None:
            os.environ["GOWON_STT_SECRET"] = secret
        for k, v in extra.items():
            os.environ[k] = v

    def ready(self, url=URL, **extra):
        self.live()
        self.configure(url=url, **extra)

    # -- urlopen 대역 ----------------------------------------------------
    def serve(self, body=None, url=None, raise_with=None, payload=None):
        """urlopen 을 대역으로 바꾸고 요청 객체를 기록한다."""
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        def fake(req, timeout=None):
            self.calls.append((req, timeout))
            if raise_with is not None:
                raise raise_with
            return FakeRes(body or b"{}", url)
        urllib.request.urlopen = fake

    def logs(self):
        out = []
        for line in sys.stdout.getvalue().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out


# ==========================================================================
# 1) 승인 전에는 한 건도 나가지 않는다
# ==========================================================================
class TestGate(Base):
    def test_unconfigured_denies_even_when_gate_is_on(self):
        self.live()
        with self.assertRaises(PermissionError) as cm:
            sp.GowonSTT().transcribe(AUDIO, "audio/webm")
        self.assertIn("[승인 필요]", str(cm.exception))
        self.assertIn("gowon", str(cm.exception))

    def test_url_without_secret_denies(self):
        self.live()
        self.configure(secret=None)
        self.assertRaises(PermissionError, sp.GowonSTT().transcribe, AUDIO)

    def test_secret_without_url_denies(self):
        self.live()
        self.configure(url=None)
        self.assertRaises(PermissionError, sp.GowonSTT().transcribe, AUDIO)

    def test_configured_but_gate_off_denies_direct_call(self):
        self.configure()
        with self.assertRaises(PermissionError) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertIn("[승인 필요]", str(cm.exception))

    def test_factory_hands_back_sim_while_the_gate_is_off(self):
        """게이트 OFF 면 설정이 다 있어도 sim 이다(실호출 원천 차단)."""
        self.configure()
        os.environ["CALLBOT_STT_PROVIDER"] = "gowon"
        p = sp.get_stt()
        self.assertEqual(p.name, "sim")
        self.assertTrue(p.transcribe(AUDIO)["sim"])

    def test_factory_hands_back_gowon_when_gate_on(self):
        self.ready()
        os.environ["CALLBOT_STT_PROVIDER"] = "gowon"
        self.assertEqual(sp.get_stt().name, "gowon")

    def test_kill_switch_takes_effect_immediately(self):
        self.ready()
        os.environ["CALLBOT_STT_PROVIDER"] = "gowon"
        self.assertEqual(sp.get_stt().name, "gowon")
        os.environ.pop("SPEECH_LIVE", None)        # 운영자가 게이트를 내린다
        self.assertEqual(sp.get_stt().name, "sim")

    def test_denied_call_never_touches_the_network(self):
        """setUp 의 urlopen 대역이 터지면 실패한다 — 거부는 요청 전에 끝난다."""
        self.live()
        self.assertRaises(PermissionError, sp.GowonSTT().transcribe, AUDIO)
        self.assertEqual(self.calls, [])


# ==========================================================================
# 2) 정상 전사 · 서명 · 요청 모양
# ==========================================================================
class TestTranscribe(Base):
    def ok_payload(self, **kw):
        d = {"ok": True, "text": "환불 받고 싶어요", "model": "large_v6", "ms": 180}
        d.update(kw)
        return d

    def test_normal_transcription_keeps_the_contract_and_adds_model_ms(self):
        self.ready()
        self.serve(payload=self.ok_payload())
        r = sp.GowonSTT().transcribe(AUDIO, "audio/webm", "ko")
        self.assertEqual(r["text"], "환불 받고 싶어요")
        self.assertEqual(r["provider"], "gowon")
        self.assertIs(r["sim"], False)
        self.assertEqual(r["lang"], "ko")
        self.assertEqual(r["model"], "large_v6")
        self.assertIsInstance(r["ms"], int)
        self.assertNotIn("fallback", r)

    def test_request_body_carries_audio_mime_lang(self):
        self.ready()
        self.serve(payload=self.ok_payload())
        sp.GowonSTT().transcribe(AUDIO, "audio/ogg;codecs=opus", "ko")
        req, timeout = self.calls[0]
        self.assertEqual(req.full_url, URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, sp.GOWON_TIMEOUT)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["audio"], AUDIO)
        self.assertEqual(body["mime"], "audio/ogg;codecs=opus")
        self.assertEqual(body["lang"], "ko")

    def test_signature_header_equals_vstudio_sign(self):
        """서명 규약을 두 군데 적어 두면 한쪽만 고쳐지고 서버가 우리를 거부한다."""
        self.ready()
        self.serve(payload=self.ok_payload())
        sp.GowonSTT().transcribe(AUDIO)
        req = self.calls[0][0]
        ts = req.get_header("X-timestamp")
        sig = req.get_header("X-signature")
        self.assertTrue(ts and sig)
        self.assertEqual(sig, _vstudio.sign(SECRET, int(ts), req.data))
        self.assertEqual(sp.sign(SECRET, int(ts), req.data), sig)

    def test_secret_is_not_in_the_request_body_or_url(self):
        self.ready()
        self.serve(payload=self.ok_payload())
        sp.GowonSTT().transcribe(AUDIO)
        req = self.calls[0][0]
        self.assertNotIn(SECRET, req.data.decode("utf-8"))
        self.assertNotIn(SECRET, req.full_url)

    def test_silence_is_an_empty_string_not_an_error(self):
        self.ready()
        self.serve(payload=self.ok_payload(text=""))
        self.assertEqual(sp.GowonSTT().transcribe(AUDIO)["text"], "")

    def test_model_label_falls_back_to_configured_name(self):
        """서버가 모델을 밝히지 않거나 형식을 벗어나면 설정 라벨을 쓴다(화면에 그대로 나간다)."""
        self.ready(GOWON_STT_MODEL_LABEL="turbo_v4")
        for bad in (None, "", "<script>", "x" * 50):
            self.serve(payload=self.ok_payload(model=bad))
            self.assertEqual(sp.GowonSTT().transcribe(AUDIO)["model"], "turbo_v4", bad)

    def test_empty_audio_is_rejected_before_the_request(self):
        self.ready()
        self.serve(payload=self.ok_payload())
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe("")
        self.assertEqual(_errors.classify(cm.exception)[0], 400)
        self.assertEqual(self.calls, [])

    def test_oversized_audio_is_rejected_before_the_request(self):
        self.ready()
        self.serve(payload=self.ok_payload())
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe("A" * (sp.GOWON_MAX_AUDIO_B64 + 1))
        self.assertEqual(_errors.classify(cm.exception)[0], 413)
        self.assertEqual(self.calls, [])


# ==========================================================================
# 3) 주소 가드 — 거부된 주소로는 요청을 만들지 않는다
# ==========================================================================
class TestUrlGuard(Base):
    BAD = ("http://stt.example.org/v1/stt",          # 평문 http
           "https://127.0.0.1/v1/stt",               # 루프백
           "https://10.1.2.3/v1/stt",                # 사설
           "https://169.254.169.254/latest",         # 클라우드 메타데이터
           "https://169.254.43518/latest",           # 같은 곳으로 풀리는 짧은 꼴
           "https://metadata.google.internal./x",    # 끝점 FQDN
           "file:///etc/passwd")

    def test_blocked_addresses_never_receive_the_audio(self):
        for bad in self.BAD:
            with self.subTest(url=bad):
                self.calls = []
                self.ready(url=bad)
                self.serve(payload={"ok": True, "text": "x"})
                with self.assertRaises(PermissionError) as cm:
                    sp.GowonSTT().transcribe(AUDIO)
                # 승인 거부가 아니라 **주소 거부**다(설정 오류로 분류된다).
                self.assertNotIn("[승인 필요]", str(cm.exception))
                self.assertEqual(self.calls, [], bad)
                self.assertEqual(_errors.classify(cm.exception)[0], 500)

    def test_blocked_address_is_not_papered_over_by_the_fallback(self):
        """설정 오류를 대체 전사로 가리면 아무도 고치지 않는다."""
        self.ready(url="https://127.0.0.1/v1/stt")
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체 전사"
        self.assertRaises(PermissionError, sp.GowonSTT().transcribe, AUDIO)

    def test_insecure_flag_is_for_local_development_only(self):
        self.ready(url="http://127.0.0.1:8099/v1/stt", GOWON_STT_ALLOW_INSECURE="1")
        self.serve(payload={"ok": True, "text": "로컬", "model": "large_v6"})
        self.assertEqual(sp.GowonSTT().transcribe(AUDIO)["text"], "로컬")

    def test_host_allowlist_is_the_strictest_setting(self):
        self.ready(GOWON_STT_HOSTS="only.example.org")
        self.serve(payload={"ok": True, "text": "x"})
        self.assertRaises(PermissionError, sp.GowonSTT().transcribe, AUDIO)
        self.assertEqual(self.calls, [])
        self.ready(url="https://only.example.org/v1/stt",
                   GOWON_STT_HOSTS="only.example.org")
        self.serve(payload={"ok": True, "text": "허용", "model": "large_v6"})
        self.assertEqual(sp.GowonSTT().transcribe(AUDIO)["text"], "허용")

    def test_guard_decision_comes_from_urlguard(self):
        """판정 구현이 두 곳이 되면 한쪽만 고쳐진다 — 공용 가드와 같은 답이어야 한다."""
        import _urlguard
        for u in self.BAD + (URL,):
            with self.subTest(url=u):
                self.assertEqual(sp.check_gowon_url(u)[0], _urlguard.check(u)[0])


# ==========================================================================
# 4) 리다이렉트 미추종 · 응답 상한
# ==========================================================================
class TestResponseHandling(Base):
    def test_redirect_response_is_not_used(self):
        """302 를 따라가면 urllib 이 서명 헤더를 **다시 싣는다** — 받은 본문을 쓰지 않는다."""
        self.ready()
        self.serve(payload={"ok": True, "text": "공격자가 넣은 글"},
                   url="https://evil.example/v1/stt")
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertNotIsInstance(cm.exception, PermissionError)
        self.assertEqual(_errors.classify(cm.exception)[0], 502)

    def test_redirect_is_not_papered_over_by_the_fallback(self):
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체 전사"
        self.serve(payload={"ok": True, "text": "공격자"}, url="https://evil.example/x")
        self.assertRaises(urllib.error.URLError, sp.GowonSTT().transcribe, AUDIO)

    def test_same_url_is_not_treated_as_a_redirect(self):
        self.ready()
        self.serve(payload={"ok": True, "text": "정상", "model": "large_v6"}, url=URL)
        self.assertEqual(sp.GowonSTT().transcribe(AUDIO)["text"], "정상")

    def test_response_over_the_cap_is_not_parsed(self):
        """잘린 본문을 '정상 응답'으로 파싱하지 않는다(서버리스 메모리 방어)."""
        self.ready()
        big = b'{"ok": true, "text": "' + b"x" * (sp.GOWON_MAX_RESPONSE + 10) + b'"}'
        self.serve(body=big)
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 502)


# ==========================================================================
# 5) 실패 분류 — 삼키지 않는다
# ==========================================================================
class TestFailureClassification(Base):
    def http_error(self, code):
        return urllib.error.HTTPError(URL, code, "err", {}, None)

    def test_timeout_is_504(self):
        self.ready()
        self.serve(raise_with=urllib.error.URLError(TimeoutError("timed out")))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception), (504, "UPSTREAM_TIMEOUT"))

    def test_bare_socket_timeout_is_also_504(self):
        self.ready()
        self.serve(raise_with=TimeoutError("timed out"))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 504)

    def test_server_5xx_is_502(self):
        self.ready()
        self.serve(raise_with=self.http_error(500))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception), (502, "UPSTREAM_ERROR"))

    def test_broken_json_is_502(self):
        self.ready()
        self.serve(body=b"<html>not json</html>")
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 502)

    def test_shape_drift_is_502(self):
        """`ok` 가 참이 아니거나 text 가 글이 아니면 업스트림 장애다."""
        self.ready()
        for payload in ({"ok": False, "text": "x"}, {"ok": True}, {"text": "x"},
                        {"ok": True, "text": 7}, [1, 2, 3]):
            with self.subTest(payload=payload):
                self.serve(payload=payload)
                with self.assertRaises(Exception) as cm:
                    sp.GowonSTT().transcribe(AUDIO)
                self.assertEqual(_errors.classify(cm.exception)[0], 502)

    def test_connection_refused_is_502(self):
        self.ready()
        self.serve(raise_with=urllib.error.URLError(ConnectionRefusedError()))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 502)

    def test_signature_rejected_is_a_configuration_error_with_an_alert(self):
        """401 을 502 로 적으면 「서버가 아픈 것」으로 묻힌다 — 500 은 사람을 부른다."""
        self.ready()
        self.serve(raise_with=self.http_error(401))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception), (500, "INTERNAL_ERROR"))

    def test_signature_rejected_is_not_papered_over_by_the_fallback(self):
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체 전사"
        self.serve(raise_with=self.http_error(401))
        self.assertRaises(RuntimeError, sp.GowonSTT().transcribe, AUDIO)

    def test_bad_audio_and_too_long_are_input_errors(self):
        self.ready()
        for code, status in ((400, 400), (413, 413)):
            with self.subTest(code=code):
                self.serve(raise_with=self.http_error(code))
                with self.assertRaises(Exception) as cm:
                    sp.GowonSTT().transcribe(AUDIO)
                self.assertEqual(_errors.classify(cm.exception)[0], status)

    def test_failure_messages_carry_no_secret_or_utterance(self):
        self.ready()
        for exc in (self.http_error(401), self.http_error(500),
                    TimeoutError("x"), urllib.error.URLError("boom")):
            with self.subTest(exc=type(exc).__name__):
                self.serve(raise_with=exc)
                try:
                    sp.GowonSTT().transcribe(AUDIO)
                    self.fail("실패가 조용히 성공으로 끝났다")
                except Exception as e:
                    text = "%s %s" % (type(e).__name__, e)
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn(AUDIO, text)
                    self.assertNotIn("stt.example.org", text)


# ==========================================================================
# 6) 대체 전사 — 사실을 적고 이어 간다
# ==========================================================================
class TestFallback(Base):
    def test_timeout_falls_back_and_says_so(self):
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체로 전사한 글"
        self.serve(raise_with=TimeoutError("timed out"))
        r = sp.GowonSTT().transcribe(AUDIO, "audio/webm")
        self.assertEqual(r["text"], "대체로 전사한 글")
        self.assertIs(r["fallback"], True)
        self.assertEqual(r["fallback_reason"], "timeout")
        self.assertEqual(r["fallback_provider"], "gemini")
        self.assertIs(r["sim"], False)

    def test_fallback_does_not_claim_the_trained_model(self):
        """자체 모델이 전사하지 않았는데 `large_v6` 라고 적으면 품질 지표가 거짓이 된다."""
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체"
        self.serve(raise_with=urllib.error.URLError(ConnectionRefusedError()))
        r = sp.GowonSTT().transcribe(AUDIO)
        self.assertNotEqual(r["model"], "large_v6")
        self.assertEqual(r["model"], _stt.MODEL)

    def test_fallback_receives_the_original_mime(self):
        self.ready()
        os.environ["GEMINI_API_KEY"] = "gkey"
        seen = {}

        def fake(audio, mime):
            seen.update(audio=audio, mime=mime)
            return "x"
        _stt.transcribe = fake
        self.serve(raise_with=TimeoutError("t"))
        sp.GowonSTT().transcribe(AUDIO, "audio/ogg")
        self.assertEqual(seen, {"audio": AUDIO, "mime": "audio/ogg"})

    def test_without_a_legacy_key_the_failure_surfaces(self):
        """대체할 수단이 없으면 실패를 그대로 드러낸다(빈 글을 성공으로 위장하지 않는다)."""
        self.ready()
        self.serve(raise_with=TimeoutError("t"))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 504)

    def test_fallback_failure_surfaces_the_original_reason(self):
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"

        def boom(a, m):
            raise RuntimeError("대체도 실패")
        _stt.transcribe = boom
        self.serve(raise_with=TimeoutError("t"))
        with self.assertRaises(Exception) as cm:
            sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual(_errors.classify(cm.exception)[0], 504)


# ==========================================================================
# 7) 로그 — 사유는 남고 발화·비밀은 남지 않는다
# ==========================================================================
class TestLogging(Base):
    def test_failure_leaves_a_reason_code_in_the_structured_log(self):
        self.ready()
        self.serve(raise_with=TimeoutError("t"))
        self.assertRaises(Exception, sp.GowonSTT().transcribe, AUDIO)
        rows = [r for r in self.logs() if r.get("event") == "stt.gowon.failure"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "timeout")
        self.assertIs(rows[0]["fallback"], False)
        self.assertEqual(rows[0]["provider"], "gowon")
        self.assertIsInstance(rows[0]["duration_ms"], int)

    def test_fallback_is_recorded_as_a_fallback(self):
        self.ready()
        os.environ["GOOGLE_API_KEY"] = "gkey"
        _stt.transcribe = lambda a, m: "대체"
        self.serve(raise_with=TimeoutError("t"))
        sp.GowonSTT().transcribe(AUDIO)
        rows = [r for r in self.logs() if r.get("event") == "stt.gowon.failure"]
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]["fallback"], True)

    def test_log_carries_no_utterance_no_secret_no_address(self):
        self.ready()
        self.serve(payload={"ok": True, "text": "홍길동 고객님 환불 접수되었습니다",
                            "model": "large_v6"})
        sp.GowonSTT().transcribe(AUDIO)
        self.serve(raise_with=urllib.error.URLError("boom"))
        self.assertRaises(Exception, sp.GowonSTT().transcribe, AUDIO)
        text = sys.stdout.getvalue()
        for needle in ("홍길동", AUDIO, SECRET, "stt.example.org"):
            self.assertNotIn(needle, text, needle)

    def test_success_writes_no_failure_line(self):
        self.ready()
        self.serve(payload={"ok": True, "text": "정상", "model": "large_v6"})
        sp.GowonSTT().transcribe(AUDIO)
        self.assertEqual([r for r in self.logs()
                          if r.get("event") == "stt.gowon.failure"], [])


# ==========================================================================
# 8) health 리포트 — 설정이 없으면 pending, 있으면서 게이트가 켜지면 ready
# ==========================================================================
class TestHealthReport(Base):
    def entry(self, kind="stt", provider="gowon"):
        rep = sp.health_report(kind)
        return {p["provider"]: p for p in rep[kind]["providers"]}[provider]

    def test_unconfigured_is_pending_approval(self):
        self.live()
        e = self.entry()
        self.assertEqual(e["status"], "pending_approval")
        self.assertFalse(e["ok"])
        self.assertIn("[승인 필요]", e["note"])
        self.assertIn("승인 후 활성화", e["note"])

    def test_configured_but_gate_off_is_still_pending(self):
        self.configure()
        e = self.entry()
        self.assertEqual(e["status"], "pending_approval")
        self.assertTrue(e["configured"])
        self.assertFalse(e["blocked"])

    def test_configured_and_gate_on_is_ready(self):
        self.ready()
        e = self.entry()
        self.assertEqual(e["status"], "ready")
        self.assertTrue(e["ok"])
        self.assertNotIn("[승인 필요]", e["note"])

    def test_blocked_address_is_not_reported_as_not_connected(self):
        """등록해 둔 주소로 전사가 안 되는 이유를 헬스에서 알 수 있어야 한다."""
        self.ready(url="https://127.0.0.1/v1/stt")
        e = self.entry()
        self.assertEqual(e["status"], "pending_approval")
        self.assertTrue(e["configured"])
        self.assertTrue(e["blocked"])
        self.assertIn("주소", e["note"])

    def test_report_carries_the_model_name_but_not_the_address_or_secret(self):
        self.ready(GOWON_STT_MODEL_LABEL="turbo_v4")
        blob = json.dumps(sp.health_report(), ensure_ascii=False)
        self.assertIn("turbo_v4", blob)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("stt.example.org", blob)
        for needle in ("SECRET", "KEY", "TOKEN", "CREDENTIAL"):
            self.assertNotIn(needle, blob.upper())

    def test_tts_side_has_no_gowon_entry(self):
        self.ready()
        rep = sp.health_report()
        self.assertNotIn("gowon", [p["provider"] for p in rep["tts"]["providers"]])

    def test_health_module_and_provider_report_agree(self):
        """`/api/health` 가 ready 라고 답하는 동안 전사만 거부되는 엇갈림을 막는다."""
        import health
        for setup in (lambda: None, self.live, self.configure, self.ready):
            for k in ("SPEECH_LIVE", "GOWON_STT_URL", "GOWON_STT_SECRET"):
                os.environ.pop(k, None)
            setup()
            with self.subTest(setup=getattr(setup, "__name__", "none")):
                g = health._speech()["stt"]["gowon"]
                e = self.entry()
                self.assertEqual(g["status"], e["status"])
                self.assertEqual(g["ready"], e["ok"])
                self.assertEqual(g["model"], e["model"])

    def test_instance_health_mirrors_the_report(self):
        self.ready()
        h = sp.GowonSTT().health()
        self.assertEqual(h["provider"], "gowon")
        self.assertTrue(h["ok"])
        self.assertEqual(h["model"], "large_v6")

    def test_reading_the_report_does_not_touch_the_network(self):
        self.ready()
        sp.health_report()
        sp.gowon_health()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
