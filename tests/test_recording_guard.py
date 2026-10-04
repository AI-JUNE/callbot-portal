# -*- coding: utf-8 -*-
"""녹음 다운로드 SSRF 가드 + api/voice.py 잔여 방어 분기 — 의존성 0, 네트워크 미사용.

`tests/test_voice.py` 65건은 `_transcribe_url` 을 **대역으로 바꿔 놓고** 웹훅
계약을 검증한다. 그래서 정작 그 함수 안쪽 — 외부가 준 URL 로 서버가 직접
나가는 유일한 경로 — 는 실패 한 줄(`== ""`)만 덮여 있었다.

여기서 고정하는 것
  1) 주소 규칙   — `file://`·평문 http·사설·루프백·메타데이터·숫자표기 우회 거부
  2) 거부 즉시   — 거부된 주소로는 **요청 자체를 보내지 않는다**(blind SSRF 차단)
  3) 리다이렉트  — 검증을 통과한 주소가 내부로 튕기면 받은 본문을 버린다
  4) 상한        — 녹음 크기 상한 초과는 거부(메모리·STT 과금 보호)
  5) 삼키지 않음 — 거부·실패가 카운터와 구조화 로그 1줄로 드러난다(URL 미기록)
  6) 결선        — clawops 이벤트·TwiML 폼 양쪽에서 같은 가드가 돈다
  7) 격하 운전   — 엔진·STT·고지·감사 모듈이 없어도 통화가 죽지 않고,
                   AI 고지가 빈 문자열이 되지 않는다

실행: python3 -m pytest tests/test_recording_guard.py -q
"""
import base64
import importlib
import io
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice  # noqa: E402
import _urlguard  # noqa: E402
from test_voice import VoiceBase, FakeHandler, get, form_post, SAME_ORIGIN  # noqa: E402


class FakeResp(object):
    """urlopen 응답 대역. `geturl` 은 리다이렉트 최종 주소를 흉내낸다."""

    def __init__(self, data=b"AUDIO", url="", raise_geturl=False):
        self._data = data
        self._url = url
        self._raise = raise_geturl
        self.status = 200

    def read(self, n=None):
        return self._data if n is None else self._data[:n]

    def geturl(self):
        if self._raise:
            raise RuntimeError("geturl 미구현")
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GuardBase(VoiceBase):
    """녹음 집계·로그를 테스트마다 초기화한다."""

    def setUp(self):
        super().setUp()
        self._stats = dict(voice.RECORDING_STATS)
        voice.RECORDING_STATS.update({"fetched": 0, "rejected": 0, "failed": 0,
                                      "last_reason": None})
        self.addCleanup(lambda: voice.RECORDING_STATS.update(self._stats))

    def fake_urlopen(self, resp=None, **kw):
        """urlopen 을 대역으로 교체하고 호출 인자를 모은다(실제 통신 없음)."""
        calls = []

        def fake(req, timeout=None):
            calls.append({"url": getattr(req, "full_url", req), "timeout": timeout})
            return resp if resp is not None else FakeResp(**kw)

        urllib.request.urlopen = fake      # VoiceBase.tearDown 이 원복한다
        return calls

    def capture_log(self):
        """CALLBOT_LOG 를 켜고 stdout 의 JSON 줄을 모은다."""
        os.environ["CALLBOT_LOG"] = "on"   # VoiceBase.tearDown 이 환경을 원복한다
        buf = io.StringIO()
        orig = sys.stdout
        sys.stdout = buf
        self.addCleanup(lambda: setattr(sys, "stdout", orig))
        return buf

    @staticmethod
    def log_lines(buf):
        out = []
        for line in buf.getvalue().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


# ==========================================================================
# 1) 주소 규칙
# ==========================================================================
class TestRecordingUrlRules(GuardBase):

    def test_공개_https_녹음주소는_허용(self):
        ok, reason = voice.check_recording_url("https://cdn.clawops.io/rec/1.wav")
        self.assertTrue(ok, reason)

    def test_로컬파일_스킴_거부(self):
        """`file:///etc/passwd` 는 서버가 자기 디스크를 읽어 STT 로 보내는 경로다."""
        for u in ("file:///etc/passwd", "file://C:/Windows/win.ini",
                  "ftp://cdn.clawops.io/1.wav", "gopher://x/1", "data:audio/wav;base64,AA"):
            ok, _ = voice.check_recording_url(u)
            self.assertFalse(ok, u)

    def test_평문_http_는_기본_거부_개발플래그로만_허용(self):
        self.assertFalse(voice.check_recording_url("http://cdn.clawops.io/1.wav")[0])
        os.environ["CPAAS_ALLOW_INSECURE_RECORDING"] = "1"
        self.assertTrue(voice.check_recording_url("http://cdn.clawops.io/1.wav")[0])

    def test_게이트는_1_정확일치만(self):
        for v in ("true", "yes", "0", " 1 x", ""):
            os.environ["CPAAS_ALLOW_INSECURE_RECORDING"] = v
            self.assertFalse(voice.check_recording_url("http://cdn.clawops.io/1.wav")[0], v)

    def test_루프백_사설_링크로컬_메타데이터_거부(self):
        for u in ("https://127.0.0.1/1.wav", "https://[::1]/1.wav",
                  "https://10.1.2.3/1.wav", "https://192.168.0.5/1.wav",
                  "https://172.16.0.9/1.wav", "https://169.254.169.254/latest/meta-data/",
                  "https://localhost/1.wav", "https://metadata.google.internal/x",
                  "https://vault.internal/1.wav", "https://db.local/1.wav",
                  "https://0.0.0.0/1.wav", "https://224.0.0.1/1.wav"):
            ok, _ = voice.check_recording_url(u)
            self.assertFalse(ok, u)

    def test_숫자표기_우회_거부(self):
        """10진·8진·16진 표기는 리졸버가 127.0.0.1 로 풀어준다 — IP 검사가 비켜가면 안 된다."""
        for u in ("https://2130706433/1.wav",            # 127.0.0.1 10진
                  "https://0177.0.0.1/1.wav",            # 8진
                  "https://0x7f.0.0.1/1.wav",            # 16진
                  "https://0xa9fea9fe/latest",           # 169.254.169.254 16진
                  "https://2852039166/latest"):          # 169.254.169.254 10진
            ok, _ = voice.check_recording_url(u)
            self.assertFalse(ok, u)

    def test_숫자라벨_정상도메인은_막지_않는다(self):
        """`123.example.com` 은 정상 도메인이다 — 과차단은 통화 실패가 된다."""
        for u in ("https://123.example.com/1.wav", "https://1.cdn.clawops.io/1.wav"):
            self.assertTrue(voice.check_recording_url(u)[0], u)

    def test_빈값_과대길이_호스트없음_거부(self):
        self.assertFalse(voice.check_recording_url("")[0])
        self.assertFalse(voice.check_recording_url(None)[0])
        self.assertFalse(voice.check_recording_url("https://e.io/" + "a" * 3000)[0])
        self.assertFalse(voice.check_recording_url("https:///1.wav")[0])

    def test_호스트_화이트리스트(self):
        os.environ["CPAAS_RECORDING_HOSTS"] = "cdn.clawops.io, rec.twilio.com"
        self.assertTrue(voice.check_recording_url("https://cdn.clawops.io/1.wav")[0])
        self.assertTrue(voice.check_recording_url("https://REC.twilio.com/1.wav")[0])
        self.assertFalse(voice.check_recording_url("https://evil.example.com/1.wav")[0])

    def test_거부사유에_URL_이_실리지_않는다(self):
        ok, reason = voice.check_recording_url(
            "https://169.254.169.254/latest?token=SECRET-TOKEN")
        self.assertFalse(ok)
        self.assertNotIn("SECRET-TOKEN", reason)
        self.assertNotIn("169.254", reason)

    def test_깨진_IPv6_표기는_형식오류로_닫는다(self):
        """해석 실패를 '통과'로 바꾸지 않는다(파싱 예외 = 거부)."""
        for u in ("https://[::1", "https://[v6", "http://[:::"):
            ok, reason = _urlguard.check(u, label="recording_url")
            self.assertFalse(ok, u)
            self.assertIn("형식", reason)

    def test_숫자표기_해석_경계(self):
        """IP 표기가 아닌 것을 IP 로 단정하지 않는다(과차단 방지)."""
        self.assertIsNone(_urlguard.as_ip("1..2"))          # 빈 라벨
        self.assertIsNone(_urlguard.as_ip("1.2.3"))         # 라벨 3개
        self.assertIsNone(_urlguard.as_ip("1.2"))           # 라벨 2개
        self.assertIsNone(_urlguard.as_ip("99999999999"))   # 32비트 초과
        self.assertIsNone(_urlguard.as_ip("08.0.0.1"))      # 8진 리터럴이 아님
        self.assertIsNone(_urlguard.as_ip("cdn.clawops.io"))
        self.assertEqual(str(_urlguard.as_ip("2130706433")), "127.0.0.1")
        self.assertEqual(str(_urlguard.as_ip("0177.0.0.1")), "127.0.0.1")

    def test_안부_콜백과_같은_규칙을_쓴다(self):
        """규칙이 두 군데 적혀 있으면 한쪽만 고쳐진다 — 같은 모듈을 쓰는지 고정."""
        import wellbeing
        self.assertIs(wellbeing._urlguard, _urlguard)
        self.assertIs(voice._urlguard, _urlguard)
        for u in ("file:///etc/passwd", "https://127.0.0.1/x", "https://2130706433/x"):
            self.assertFalse(wellbeing.check_callback_url(u)[0], u)
            self.assertFalse(voice.check_recording_url(u)[0], u)


# ==========================================================================
# 2) 거부 즉시 — 요청 자체를 보내지 않는다
# ==========================================================================
class TestRejectedUrlIsNeverFetched(GuardBase):

    def test_거부주소로는_요청하지_않는다(self):
        calls = self.fake_urlopen()
        self.assertEqual(voice._transcribe_url("https://169.254.169.254/latest"), "")
        self.assertEqual(calls, [])                       # blind SSRF 도 성립하지 않는다
        self.assertEqual(voice.RECORDING_STATS["rejected"], 1)
        self.assertEqual(voice.RECORDING_STATS["fetched"], 0)

    def test_STT_모듈이_없으면_내려받지도_않는다(self):
        voice.transcribe = None
        calls = self.fake_urlopen()
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")
        self.assertEqual(calls, [])
        self.assertEqual(voice.RECORDING_STATS["last_reason"], "STT 모듈 미적재")

    def test_거부가_구조화로그_한줄로_남는다(self):
        buf = self.capture_log()
        voice._transcribe_url("file:///etc/passwd?token=SECRET-TOKEN")
        recs = [r for r in self.log_lines(buf) if r.get("event") == "recording_fetch"]
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["result"], recs[0]["level"], recs[0]["route"]),
                         ("rejected", "warn", "/api/voice"))
        self.assertNotIn("SECRET-TOKEN", json.dumps(recs[0], ensure_ascii=False))
        self.assertNotIn("passwd", json.dumps(recs[0], ensure_ascii=False))


# ==========================================================================
# 3) 다운로드 경로 — 성공·리다이렉트·상한·실패
# ==========================================================================
class TestFetchPath(GuardBase):

    def test_정상_다운로드는_base64_로_STT_에_넘어간다(self):
        stt = self.stub_stt("환불하고 싶어요")
        calls = self.fake_urlopen(FakeResp(b"\x00RIFFDATA", url="https://cdn.clawops.io/1.wav"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "환불하고 싶어요")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout"], voice.RECORDING_TIMEOUT)
        self.assertEqual(stt[0][0], base64.b64encode(b"\x00RIFFDATA").decode())
        self.assertEqual(stt[0][1], "audio/wav")
        self.assertEqual(voice.RECORDING_STATS["fetched"], 1)

    def test_리다이렉트가_내부로_튕기면_본문을_버린다(self):
        stt = self.stub_stt("들렸어요")
        self.fake_urlopen(FakeResp(b"SECRET-CREDS", url="http://169.254.169.254/latest/x"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")
        self.assertEqual(stt, [])                         # 받은 바이트가 LLM 으로 가지 않는다
        self.assertEqual(voice.RECORDING_STATS["failed"], 1)
        self.assertEqual(voice.RECORDING_STATS["last_reason"], "PermissionError")

    def test_허용주소로의_리다이렉트는_통과(self):
        self.stub_stt("네")
        self.fake_urlopen(FakeResp(b"A", url="https://cdn2.clawops.io/1.wav"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "네")

    def test_최종주소를_못_읽어도_동작한다(self):
        self.stub_stt("네")
        self.fake_urlopen(FakeResp(b"A", raise_geturl=True))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "네")

    def test_상한_초과_녹음은_거부(self):
        self.stub_stt("길어요")
        self.fake_urlopen(FakeResp(b"X" * 64, url="https://cdn.clawops.io/1.wav"))
        with self.assertRaises(ValueError):
            voice._fetch_recording("https://cdn.clawops.io/1.wav", max_bytes=16)

    def test_상한은_호출시점에_읽는다(self):
        """상수를 기본값으로 굳혀 두면 운영 중 조정이 반영되지 않는다."""
        self.stub_stt("길어요")
        self.fake_urlopen(FakeResp(b"X" * 64, url="https://cdn.clawops.io/1.wav"))
        orig = voice.RECORDING_MAX_BYTES
        voice.RECORDING_MAX_BYTES = 16
        self.addCleanup(lambda: setattr(voice, "RECORDING_MAX_BYTES", orig))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")
        self.assertEqual(voice.RECORDING_STATS["last_reason"], "ValueError")

    def test_상한_정각은_통과(self):
        self.fake_urlopen(FakeResp(b"X" * 16, url="https://cdn.clawops.io/1.wav"))
        self.assertEqual(voice._fetch_recording("https://cdn.clawops.io/1.wav", max_bytes=16),
                         b"X" * 16)

    def test_빈_녹음은_실패로_드러낸다(self):
        stt = self.stub_stt("아무말")
        self.fake_urlopen(FakeResp(b"", url="https://cdn.clawops.io/1.wav"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")
        self.assertEqual(stt, [])                         # 0바이트를 STT 에 보내지 않는다
        self.assertEqual(voice.RECORDING_STATS["last_reason"], "빈 녹음")

    def test_다운로드_실패는_예외_타입명만_남긴다(self):
        def boom(req, timeout=None):
            raise OSError("https://cdn.clawops.io/1.wav?token=SECRET-TOKEN 연결 실패")

        urllib.request.urlopen = boom
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav?token=SECRET-TOKEN"), "")
        dumped = json.dumps(voice.RECORDING_STATS, ensure_ascii=False)
        self.assertIn("OSError", dumped)
        self.assertNotIn("SECRET-TOKEN", dumped)

    def test_STT_예외도_삼키지_않고_집계된다(self):
        def boom(audio_b64, mime="audio/webm"):
            raise RuntimeError("010-1234-5678 전사 실패")

        voice.transcribe = boom
        self.fake_urlopen(FakeResp(b"A", url="https://cdn.clawops.io/1.wav"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")
        dumped = json.dumps(voice.RECORDING_STATS, ensure_ascii=False)
        self.assertEqual(voice.RECORDING_STATS["last_reason"], "RuntimeError")
        self.assertNotIn("010", dumped)

    def test_전사결과가_None_이어도_문자열을_돌려준다(self):
        voice.transcribe = lambda a, m="audio/wav": None
        self.fake_urlopen(FakeResp(b"A", url="https://cdn.clawops.io/1.wav"))
        self.assertEqual(voice._transcribe_url("https://cdn.clawops.io/1.wav"), "")


# ==========================================================================
# 4) 결선 — 이벤트·폼 양쪽에서 같은 가드가 돈다
# ==========================================================================
class TestWiring(GuardBase):

    def test_clawops_녹음URL_이벤트가_가드를_거친다(self):
        self.stub_engine()
        calls = self.fake_urlopen()
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "01011112222",
                                  "scenario": "refund",
                                  "recording_url": "http://169.254.169.254/latest"})
        self.assertIn("못 들었", json.dumps(out, ensure_ascii=False))
        self.assertEqual(calls, [])
        self.assertEqual(voice.RECORDING_STATS["rejected"], 1)

    def test_clawops_녹음URL_정상경로(self):
        """허용 주소면 전사 결과가 엔진으로 넘어간다(`recording_url` 분기)."""
        engine = self.stub_engine(reply="확인해 드릴게요.")
        self.stub_stt("환불해 주세요")
        self.fake_urlopen(FakeResp(b"A", url="https://cdn.clawops.io/1.wav"))
        out = voice.handle_event({"type": "speech", "call_id": "c2", "from": "01011112222",
                                  "scenario": "refund",
                                  "recording_url": "https://cdn.clawops.io/1.wav"})
        self.assertEqual(engine[0]["messages"][-1]["content"], "환불해 주세요")
        self.assertIn("확인해 드릴게요.", json.dumps(out, ensure_ascii=False))

    def test_twiml_RecordingUrl_도_가드를_거친다(self):
        self.stub_engine()
        calls = self.fake_urlopen()
        r = form_post({"CallId": "c3", "From": "01011112222",
                       "RecordingUrl": "file:///etc/passwd", "RecordingDuration": "3"},
                      headers=SAME_ORIGIN)
        self.assertEqual(r.status, 200)
        self.assertIn("못 들었", r.text())
        self.assertEqual(calls, [])

    def test_상태_엔드포인트가_집계를_노출한다(self):
        voice._transcribe_url("https://127.0.0.1/1.wav")
        body = get("/api/voice", headers=SAME_ORIGIN).json()
        self.assertEqual(body["recording"]["rejected"], 1)
        self.assertIn("fetched", body["recording"])

    def test_집계에_대상_URL_이_남지_않는다(self):
        voice._transcribe_url("https://127.0.0.1/rec?token=SECRET-TOKEN")
        text = get("/api/voice", headers=SAME_ORIGIN).text()
        self.assertNotIn("SECRET-TOKEN", text)
        self.assertNotIn("127.0.0.1", text)


# ==========================================================================
# 5) 잔여 방어 분기 — 보조 모듈이 없거나 터질 때
# ==========================================================================
class TestDefensiveBranches(GuardBase):

    def test_지표_수집기가_없어도_통화가_돈다(self):
        orig = voice.call_metrics
        voice.call_metrics = None
        self.addCleanup(lambda: setattr(voice, "call_metrics", orig))
        self.assertIsNone(voice._metric("start", "c1"))
        self.stub_engine()
        self.stub_transcribe_url("환불")
        xml = voice.handle_twilio({"CallId": "c1", "From": "01011112222",
                                   "RecordingUrl": "https://cdn/1.wav",
                                   "RecordingDuration": "3"})
        self.assertIn("<Response>", xml)

    def test_고지모듈_장애여도_AI고지가_비지_않는다(self):
        class Broken(object):
            def effective(self, t):
                raise RuntimeError("저장소 장애")

            def greeting(self, t):
                raise RuntimeError("저장소 장애")

        self.addCleanup(lambda d=voice._disclosure: setattr(voice, "_disclosure", d))
        voice._disclosure = Broken()
        os.environ.pop("CALLBOT_GREETING", None)
        g = voice.greeting(None)
        self.assertTrue(g.strip())
        self.assertIn("인공지능", g)          # 법정 고지가 빠지는 쪽으로 실패하지 않는다

    def test_고지모듈_장애시_환경변수_인사말을_존중한다(self):
        class Broken(object):
            def effective(self, t):
                raise RuntimeError("저장소 장애")

        self.addCleanup(lambda d=voice._disclosure: setattr(voice, "_disclosure", d))
        voice._disclosure = Broken()
        os.environ["CALLBOT_GREETING"] = "AI 상담원입니다(운영자 설정)"
        self.assertEqual(voice.greeting(None), "AI 상담원입니다(운영자 설정)")

    def test_감사모듈이_없어도_웹훅이_200(self):
        self.addCleanup(lambda a=voice._audit: setattr(voice, "_audit", a))
        voice._audit = None
        self.assertIsNone(voice._audit_ev({}, "/api/voice", "GET", "allow", 200))
        self.assertEqual(get("/api/voice", headers=SAME_ORIGIN).status, 200)

    def test_감사기록_실패가_통화를_죽이지_않는다(self):
        class Broken(object):
            def record_request(self, *a, **kw):
                raise RuntimeError("감사 버퍼 고장")

        self.addCleanup(lambda a=voice._audit: setattr(voice, "_audit", a))
        voice._audit = Broken()
        self.assertIsNone(voice._audit_ev({}, "/api/voice", "GET", "allow", 200))
        self.assertEqual(get("/api/voice", headers=SAME_ORIGIN).status, 200)

    def test_로그_종료_실패가_응답을_막지_않는다(self):
        class BrokenRq(object):
            request_id = "rid-1"

            def finish(self, *a, **kw):
                raise RuntimeError("로그 고장")

        fh = FakeHandler(SAME_ORIGIN, b"", "/api/voice")
        fh._rq = BrokenRq()
        voice.handler._send(fh, {"ok": True}, 200)
        self.assertEqual(fh.status, 200)
        self.assertEqual(fh.json()["ok"], True)

    def test_음수_content_length_는_400(self):
        r = FakeHandler(dict(SAME_ORIGIN, **{"content-length": "-1",
                                             "content-type": "application/json"}),
                        b"", "/api/voice")
        voice.handler.do_POST(r)
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json()["details"][0]["field"], "content-length")

    def test_통화결과_기록_실패를_흡수한다(self):
        self.assertIsNone(voice._persist_call_result("c1", {}, object()))

    def test_캠페인은_게이트가_켜져도_실제로_걸지_않는다(self):
        """`trigger_campaign` 은 계획만 만든다 — live 분기에서도 네트워크를 쓰지 않는다."""
        out = voice.trigger_campaign(["01000000000"], scenario="care")
        self.assertEqual(out["queued"], 0)
        self.assertIn("dry-run", out["mode"])
        self.addCleanup(lambda v=voice.LIVE: setattr(voice, "LIVE", v))
        voice.LIVE = True
        out = voice.trigger_campaign(["01000000000", "01011112222"], scenario="care")
        self.assertEqual((out["mode"], out["queued"]), ("live", 2))
        self.assertEqual(self.network_calls, [])


# ==========================================================================
# 6) 격하 운전 — 두뇌·STT·고지 모듈이 통째로 없을 때
# ==========================================================================
class TestDegradedImports(GuardBase):
    """`api/` 의 보조 모듈을 못 불러온 상태로 voice 를 다시 올린다.

    서버리스 배포에서 한 파일이 빠지거나 import 가 깨지는 일은 실제로 있다.
    그때 통화가 500 으로 끊기는지, 아니면 안내 후 종료되는지가 갈린다.
    """

    BROKEN = ("_engine", "_stt", "assist", "disclosure")

    def setUp(self):
        super().setUp()
        self._saved_mods = {k: sys.modules.get(k) for k in self.BROKEN}
        self._saved_ns = dict(voice.__dict__)
        for k in self.BROKEN:
            sys.modules[k] = None          # import 시 ImportError 를 일으킨다
        spec = importlib.util.spec_from_file_location("voice", os.path.join(API, "voice.py"))
        spec.loader.exec_module(voice)

    def tearDown(self):
        for k, v in self._saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        voice.__dict__.clear()
        voice.__dict__.update(self._saved_ns)
        super().tearDown()

    def test_폴백이_설치된다(self):
        self.assertIsNone(voice.run_turn)
        self.assertIsNone(voice.transcribe)
        self.assertIsNone(voice.run_assist)
        self.assertIsNone(voice._disclosure)

    def test_AI고지는_모듈이_없어도_비지_않는다(self):
        os.environ.pop("CALLBOT_GREETING", None)
        g = voice.greeting(None)
        self.assertIn("인공지능", g)

    def test_STT_가_없으면_재청취를_안내한다(self):
        xml = voice.handle_twilio({"CallId": "c1", "From": "01011112222",
                                   "RecordingUrl": "https://cdn.clawops.io/1.wav",
                                   "RecordingDuration": "3"})
        self.assertIn("못 들었", xml)
        self.assertEqual(self.network_calls, [])      # STT 가 없으면 내려받지도 않는다

    def test_엔진이_없으면_점검안내로_끝낸다(self):
        # 전사까지는 됐다고 가정하고 두뇌(run_turn)만 빠진 상태를 본다.
        voice._transcribe_url = lambda u: "환불해 주세요"   # tearDown 이 네임스페이스를 원복한다
        xml = voice.handle_twilio({"CallId": "c1", "From": "01011112222",
                                   "RecordingUrl": "https://cdn.clawops.io/1.wav",
                                   "RecordingDuration": "3"})
        self.assertIn("점검", xml)
        self.assertIn("<Hangup/>", xml)

    def test_이벤트_경로도_상담사_안내로_끝낸다(self):
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "01011112222",
                                  "scenario": "refund", "text": "환불해 주세요"})
        self.assertIn("점검", json.dumps(out, ensure_ascii=False))

    def test_상태_엔드포인트가_격하를_솔직하게_알린다(self):
        body = get("/api/voice", headers=SAME_ORIGIN).json()
        self.assertFalse(body["engine"])
        self.assertFalse(body["stt"])

    def test_SSRF_가드는_격하_상태에서도_살아_있다(self):
        """보안 통제는 폴백이 없다 — 모듈이 빠져도 검증 없이 나가지 않는다."""
        self.assertFalse(voice.check_recording_url("file:///etc/passwd")[0])
        self.assertFalse(voice.check_recording_url("https://169.254.169.254/latest")[0])


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
