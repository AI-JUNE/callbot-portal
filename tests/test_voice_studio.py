# -*- coding: utf-8 -*-
"""보이스 스튜디오(09-29) — IVR·안내 멘트 음성 제작.

확인하는 것
  1) 대본 형식(파일이름 | 문장)을 음성복제도구와 같은 규칙으로 읽는다(주석·빈 줄·중복 이름·쓸 수 없는 글자)
  2) 복제 목소리는 엔진 연결 + 동의 확인(consent="ok")이 모두 있어야만 목록에 오르고, 아니면 409 로 거절한다
  3) 서명은 음성합성 SaaS 워커와 같은 식(hex(HMAC-SHA256(secret, ts + "\\n" + body)))
  4) 함수 수를 늘리지 않는다 — speech.py(mode=studio)로 들어오고 rewrite·요청 제한 등급이 걸려 있다
  5) 관리 콘솔 연결(nav·titles·MENU·section)·AI 생성 표시·동의 안내
실행: python3 -m pytest tests/test_voice_studio.py -q
"""
import hashlib
import hmac
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

import _ratelimit   # noqa: E402
import _vstudio     # noqa: E402
import speech       # noqa: E402


class Req(speech.handler):
    def __init__(self, path, body=b""):
        self.path = path
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"x-api-key": os.environ["CALLBOT_API_KEY"], "content-length": str(len(body))}
        self.status = None

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class Script(unittest.TestCase):
    def test_parse(self):
        rows = _vstudio.parse_script("# 주석\n\nwelcome | 안녕하십니까\n이름 없는 줄\nwelcome | 두 번째   # 메모\n a/b | 슬래시\n")
        self.assertEqual([r["name"] for r in rows], ["welcome", "002", "welcome-2", "a_b"])
        self.assertEqual(rows[2]["text"], "두 번째")

    def test_validate(self):
        self.assertFalse(_vstudio.validate({"text": ""})[0])
        self.assertFalse(_vstudio.validate({"text": "가" * 301})[0])
        ok, v = _vstudio.validate({"text": "안녕", "voice": "ko-KR-SunHiNeural", "rate": 9})
        self.assertTrue(ok)
        self.assertEqual((v["kind"], v["rate"]), ("standard", 1.25))
        self.assertEqual(_vstudio.validate({"text": "안녕", "voice": "v-kim"})[1]["kind"], "clone")


class Clone(unittest.TestCase):
    ENV = {"VOICE_ENGINE_URL": "https://gpu.example.com", "VOICE_ENGINE_SECRET": "s",
           "VOICE_STUDIO_VOICES": json.dumps([{"id": "v-ok", "name": "김상담", "consent": "ok"}, {"id": "v-no", "name": "동의 전"}])}

    def test_only_consented(self):
        self.assertEqual([v["id"] for v in _vstudio.clone_voices(self.ENV)], ["v-ok"])
        st = _vstudio.status(self.ENV)
        self.assertTrue(st["clone_ready"])
        self.assertEqual([v["id"] for v in st["clone"]], ["v-ok"])

    def test_not_ready_without_engine(self):
        st = _vstudio.status({"VOICE_STUDIO_VOICES": self.ENV["VOICE_STUDIO_VOICES"]})
        self.assertFalse(st["clone_ready"])
        self.assertEqual(st["clone"], [])
        self.assertIn("동의", st["clone_note"])
        self.assertFalse(_vstudio.engine_ready({"VOICE_ENGINE_URL": "http://insecure", "VOICE_ENGINE_SECRET": "s"}), "https 만")

    def test_signature_matches_worker(self):
        body = b'{"a":1}'
        self.assertEqual(_vstudio.sign("sec", 1700000000, body),
                         hmac.new(b"sec", b"1700000000\n" + body, hashlib.sha256).hexdigest())


class Http(unittest.TestCase):
    def test_mode(self):
        self.assertEqual(speech._mode("/api/speech?mode=studio&op=status"), "studio")
        self.assertEqual(speech._mode("/api/voice-studio?op=status"), "studio")
        self.assertEqual(speech._mode("/api/speech?mode=tts"), "tts")

    def test_status_get(self):
        r = Req("/api/speech?mode=studio&op=status")
        r.do_GET()
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertTrue(b["ok"])
        self.assertGreaterEqual(len(b["standard"]), 2)
        self.assertNotIn("VOICE_ENGINE_SECRET", json.dumps(b))

    def test_synth_rejects_unconsented_clone(self):
        body = json.dumps({"text": "안녕하십니까", "voice": "v-unknown"}).encode()
        r = Req("/api/speech?mode=studio&op=synth", body)
        r.do_POST()
        self.assertEqual(r.status, 409)
        self.assertIn("동의", r.body()["error"])

    def test_synth_standard(self):
        orig = _vstudio.synth_standard
        _vstudio.synth_standard = lambda t, v, rt: (b"ID3fake", "audio/mpeg")
        try:
            r = Req("/api/speech?mode=studio&op=synth", json.dumps({"text": "안녕하십니까", "voice": "ko-KR-SunHiNeural"}).encode())
            r.do_POST()
        finally:
            _vstudio.synth_standard = orig
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["kind"], "standard")

    def test_bad_request(self):
        r = Req("/api/speech?mode=studio&op=synth", b"not json")
        r.do_POST()
        self.assertEqual(r.status, 400)


class Wiring(unittest.TestCase):
    def test_no_new_function_and_rewrite(self):
        fns = [f for f in os.listdir(API) if f.endswith(".py") and not f.startswith("_")]
        self.assertLessEqual(len(fns), 12, "Vercel Hobby 함수 상한 12개")
        vj = json.load(open(os.path.join(ROOT, "vercel.json"), encoding="utf-8"))
        self.assertIn({"source": "/api/voice-studio", "destination": "/api/speech?mode=studio"}, vj["rewrites"])
        self.assertEqual(_ratelimit.ROUTE_CLASS.get("voice-studio"), "speech")

    def test_console(self):
        h = io.open(os.path.join(ROOT, "public", "admin.html"), encoding="utf-8").read()
        self.assertIn('data-v="vstudio"', h)
        self.assertIn("vstudio:['보이스 스튜디오'", h)
        self.assertIn("['vstudio','보이스 스튜디오']", h)
        i = h.find('<section id="view-vstudio"')
        self.assertGreater(i, 0)
        sec = h[i:h.find("</section>", i)]
        self.assertIn("동의 녹음을 마친 목소리만", sec)
        for fid in ("vsVoice", "vsRate", "vsFormat", "vsScript", "vsNotice"):
            self.assertIn('for="%s"' % fid, sec, "라벨 연결: " + fid)
        self.assertIn("'/api/voice-studio?op=synth'", h)
        self.assertIn("AI 생성 음성", h, "WAV 메타데이터에 AI 생성 표시")
        self.assertIn("vsIsClone()", h, "복제 목소리는 AI 안내 음성이 기본으로 켜진다")


if __name__ == "__main__":
    unittest.main()
