# -*- coding: utf-8 -*-
"""api/tts.py 음성합성 엔드포인트 + api/recording_audit.py 녹취 감사 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시 + edge_tts 대역으로 강제).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — tts → recording_audit 순서)
  1) TTS 접근 가드 — 오리진 없는 호출 403, CORS 되비침 금지, 요청 제한 429
  2) TTS 입력검증 — text 필수(400·details[].field)·1000자 상한, 봉투 규약
  3) TTS 프로바이더 분기 — 기본/edge 는 합성 경로, sim 은 메타 JSON(오디오 0),
     실프로바이더는 SPEECH_LIVE 게이트 OFF 에서 sim 강제(과금 0)
  4) TTS health — 합성 없이 200, 실패해도 200·내부 문구 미노출
  5) TTS 장애 분류 — ImportError/일반 500, URLError 502, timeout 504,
     예외 문구(입력 원문·전화번호) 응답 미노출
  6) 녹취 게이트 — RECORDING_LIVE 기본 off, 켜져 있어도 미동의면 원문 참조 폐기
  7) 녹취 접근 — 목적 화이트리스트, 거부·미스도 감사기록, 반환값은 사본
  8) 보관·파기 — 만료 산정, hard_delete/anonymize, 미지 방식 거부, 재파기 무기록
  9) 감사 로그 — append-only(반환 사본 변조 불가), 원문 필드 부재 스키마

실행: python3 -m pytest tests/test_tts_recording.py -q
"""
import os
import sys
import json
import base64
import time
import socket
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _guard             # noqa: E402
import _ratelimit         # noqa: E402
import speech_providers   # noqa: E402
import tts                # noqa: E402
import recording_audit    # noqa: E402


class NetworkTouched(AssertionError):
    pass


# --------------------------------------------------------------------------
# 테스트용 최소 핸들러 (BaseHTTPRequestHandler 대역)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class Resp(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def text(self):
        return self.wfile.data.decode("utf-8")

    def body(self):
        return json.loads(self.text())


def ttscall(headers=None, query=""):
    """tts.handler.do_GET 을 소켓 없이 호출한다."""
    r = Resp()
    path = "/api/tts" + ("?" + query if query else "")
    inst = tts.handler.__new__(tts.handler)
    inst.headers = FakeHeaders(headers or {})
    inst.wfile = r.wfile
    inst.path = path
    inst.send_response = lambda c: setattr(r, "status", c)
    inst.send_header = lambda k, v: r.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    inst.do_GET()
    return r


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.1"}

ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CALLBOT_TTS_PROVIDER", "CALLBOT_STT_PROVIDER", "SPEECH_LIVE",
       "RECORDING_LIVE", "SENTRY_DSN",
       "PII_MASTER_KEY", "PII_MASTER_KEY_OLD")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        _ratelimit.reset()
        # 게이트는 import 시점 상수 — 테스트는 항상 OFF(sim) 전제
        self._live = speech_providers.SPEECH_LIVE
        speech_providers.SPEECH_LIVE = False
        # 어떤 경로에서도 네트워크를 건드리면 즉시 실패
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom
        # 실제 합성(edge_tts)은 대역으로 — 호출 여부·인자를 기록
        self._synth = tts._synth
        self.synth_calls = []
        tts._synth = self._fake_synth

    def tearDown(self):
        tts._synth = self._synth
        urllib.request.urlopen = self._urlopen
        speech_providers.SPEECH_LIVE = self._live
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()

    def _boom(self, *a, **k):
        raise NetworkTouched("네트워크 호출 발생 — 테스트는 네트워크를 쓰지 않는다")

    def _fake_synth(self, text):
        self.synth_calls.append(text)
        return b"ID3\x03\x00fake-mp3"


# ==========================================================================
# 1) 접근 가드
# ==========================================================================
class TestTtsGuard(Base):
    def test_denied_without_origin(self):
        r = ttscall({}, "text=안녕하세요")
        self.assertEqual(r.status, 403)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["status"], 403)
        self.assertEqual(self.synth_calls, [], "거부된 요청이 합성(과금)을 일으켰다")

    def test_health_also_behind_guard(self):
        r = ttscall({}, "health=1")
        self.assertEqual(r.status, 403)

    def test_api_key_passes_cross_origin(self):
        os.environ["CALLBOT_API_KEY"] = "k-secret"
        r = ttscall({"x-api-key": "k-secret", "origin": "https://evil.example"}, "text=안녕")
        self.assertEqual(r.status, 200)
        # 미허용 오리진을 되비추면 CORS 우회 — 기본 허용 오리진으로 고정돼야 한다
        self.assertNotEqual(r.header("Access-Control-Allow-Origin"), "https://evil.example")
        self.assertIn(r.header("Access-Control-Allow-Origin"), _guard.ALLOWED)

    def test_allowed_origin_is_echoed(self):
        r = ttscall({"origin": _guard.ALLOWED[0]}, "text=안녕")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Access-Control-Allow-Origin"), _guard.ALLOWED[0])

    def test_rate_limit_speech_class(self):
        per_ip = _ratelimit.limits(_ratelimit.route_class("/api/tts"))[0]
        self.assertGreater(per_ip, 0)
        last = None
        for _ in range(per_ip):
            last = ttscall(SAME_ORIGIN, "health=1")
            self.assertEqual(last.status, 200)
        r = ttscall(SAME_ORIGIN, "health=1")
        self.assertEqual(r.status, 429)
        self.assertIsNotNone(r.header("Retry-After"))
        self.assertEqual(r.body()["status"], 429)


# ==========================================================================
# 2) 입력검증
# ==========================================================================
class TestTtsValidation(Base):
    def test_missing_text_is_400_with_field(self):
        r = ttscall(SAME_ORIGIN, "")
        self.assertEqual(r.status, 400)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "INVALID_REQUEST")
        self.assertEqual([d["field"] for d in b["details"]], ["text"])
        self.assertEqual(self.synth_calls, [])

    def test_blank_text_is_400(self):
        r = ttscall(SAME_ORIGIN, "text=%20%20")
        self.assertEqual(r.status, 400)
        self.assertEqual(self.synth_calls, [])

    def test_text_over_limit_is_400(self):
        r = ttscall(SAME_ORIGIN, "text=" + "가" * 1001)
        self.assertEqual(r.status, 400)
        self.assertEqual(r.body()["details"][0]["field"], "text")
        self.assertEqual(self.synth_calls, [])

    def test_text_at_limit_is_accepted(self):
        r = ttscall(SAME_ORIGIN, "text=" + "가" * 1000)
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.synth_calls[0]), 1000)

    def test_text_is_stripped_and_first_value_used(self):
        r = ttscall(SAME_ORIGIN, "text=%20안녕하세요%20&text=두번째")
        self.assertEqual(r.status, 200)
        self.assertEqual(self.synth_calls, ["안녕하세요"])

    def test_error_envelope_has_no_debug_by_default(self):
        r = ttscall(SAME_ORIGIN, "")
        self.assertNotIn("debug", r.body())


# ==========================================================================
# 3) 프로바이더 분기
# ==========================================================================
class TestTtsProviders(Base):
    def test_default_path_synthesizes_audio(self):
        r = ttscall(SAME_ORIGIN, "text=안녕하세요")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), "audio/mpeg")
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertEqual(r.wfile.data, b"ID3\x03\x00fake-mp3")
        self.assertEqual(self.synth_calls, ["안녕하세요"])

    def test_edge_provider_is_legacy_path(self):
        os.environ["CALLBOT_TTS_PROVIDER"] = "edge"
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), "audio/mpeg")
        self.assertEqual(self.synth_calls, ["안녕"])

    def test_sim_provider_returns_meta_json_without_audio(self):
        os.environ["CALLBOT_TTS_PROVIDER"] = "sim"
        r = ttscall(SAME_ORIGIN, "text=안내 문구")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("Content-Type").startswith("application/json"))
        self.assertEqual(r.header("Cache-Control"), "no-store")
        self.assertEqual(r.header("Content-Length"), str(len(r.wfile.data)))
        b = r.body()
        self.assertIs(b["sim"], True)
        self.assertEqual(b["provider"], "sim")
        self.assertEqual(b["chars"], len("안내 문구"))
        self.assertEqual(b["voice"], tts.VOICE)
        self.assertEqual(self.synth_calls, [], "sim 경로가 실합성을 호출했다")

    def test_live_provider_forced_to_sim_when_gate_off(self):
        """SPEECH_LIVE 미설정이면 clova 를 요구해도 sim — 실호출·과금이 없다."""
        for want in ("clova", "google", "aws", "CLOVA "):
            os.environ["CALLBOT_TTS_PROVIDER"] = want
            r = ttscall(SAME_ORIGIN, "text=안녕")
            self.assertEqual(r.status, 200, want)
            b = r.body()
            self.assertEqual(b["provider"], "sim", want)
            self.assertIs(b["sim"], True, want)
        self.assertEqual(self.synth_calls, [])

    def test_unknown_provider_falls_back_to_sim(self):
        os.environ["CALLBOT_TTS_PROVIDER"] = "nope"
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["provider"], "sim")

    def test_alt_provider_none_for_default_and_edge(self):
        self.assertIsNone(tts._alt_provider())
        os.environ["CALLBOT_TTS_PROVIDER"] = " Edge "
        self.assertIsNone(tts._alt_provider())
        os.environ["CALLBOT_TTS_PROVIDER"] = "sim"
        self.assertIsInstance(tts._alt_provider(), speech_providers.SimTTS)


# ==========================================================================
# 4) health
# ==========================================================================
class TestTtsHealth(Base):
    def test_health_is_json_and_does_not_synthesize(self):
        r = ttscall(SAME_ORIGIN, "health=1&text=이건 합성하면 안 됨")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("Content-Type").startswith("application/json"))
        self.assertEqual(r.header("Cache-Control"), "no-store")
        b = r.body()
        self.assertEqual(b["gate"], "SPEECH_LIVE")
        self.assertIs(b["speech_live"], False)
        self.assertEqual(b["voice"], tts.VOICE)
        self.assertIn("tts", b)
        self.assertNotIn("stt", b, "tts health 에 stt 가 섞였다")
        self.assertEqual(self.synth_calls, [])

    def test_health_truthy_variants(self):
        for v in ("true", "yes", "1"):
            self.assertEqual(ttscall(SAME_ORIGIN, "health=" + v).status, 200)
        # health=0 은 일반 경로 → text 없으니 400
        self.assertEqual(ttscall(SAME_ORIGIN, "health=0").status, 400)

    def test_health_pending_providers_marked_for_approval(self):
        b = ttscall(SAME_ORIGIN, "health=1").body()
        provs = {p["provider"]: p for p in b["tts"]["providers"]}
        self.assertEqual(provs["sim"]["status"], "ready")
        for n in ("clova", "google", "aws"):
            self.assertEqual(provs[n]["status"], "pending_approval")
            self.assertIn("[승인 필요]", provs[n]["note"])

    def test_health_failure_stays_200_without_internal_message(self):
        orig = speech_providers.health_report

        def broken(kind="all"):
            raise RuntimeError("secret internal path /srv/keys/clova.json")
        speech_providers.health_report = broken
        try:
            r = ttscall(SAME_ORIGIN, "health=1")
        finally:
            speech_providers.health_report = orig
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertIs(b["ok"], False)
        self.assertNotIn("clova.json", r.text())
        self.assertNotIn("/srv", r.text())


# ==========================================================================
# 5) 장애 분류 · 문구 미노출
# ==========================================================================
class TestTtsFailures(Base):
    def _raise(self, exc):
        def f(text):
            raise exc
        tts._synth = f

    def test_missing_engine_is_500_envelope(self):
        self._raise(ImportError("No module named 'edge_tts'"))
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertEqual(r.status, 500)
        b = r.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "INTERNAL_ERROR")
        self.assertNotIn("edge_tts", r.text())
        self.assertNotIn("debug", b)

    def test_upstream_failure_is_502(self):
        self._raise(urllib.error.URLError("connection refused"))
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertEqual(r.status, 502)
        self.assertEqual(r.body()["code"], "UPSTREAM_ERROR")

    def test_upstream_timeout_is_504(self):
        self._raise(socket.timeout("timed out"))
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertEqual(r.status, 504)
        self.assertEqual(r.body()["code"], "UPSTREAM_TIMEOUT")

    def test_exception_text_with_pii_is_not_echoed(self):
        """예외 문구에 입력 원문(전화번호)이 섞여도 응답에 실리지 않는다."""
        self._raise(RuntimeError("synth failed for '홍길동 010-1234-5678'"))
        r = ttscall(SAME_ORIGIN, "text=홍길동 010-1234-5678")
        self.assertEqual(r.status, 500)
        self.assertNotIn("1234-5678", r.text())
        self.assertNotIn("홍길동", r.text())

    def test_failure_response_is_json_not_partial_audio(self):
        self._raise(RuntimeError("x"))
        r = ttscall(SAME_ORIGIN, "text=안녕")
        self.assertTrue(r.header("Content-Type").startswith("application/json"))
        self.assertEqual(r.body()["status"], 500)


# ==========================================================================
# 6) 녹취 게이트
# ==========================================================================
class TestRecordingGate(Base):
    def test_default_off(self):
        self.assertFalse(recording_audit.recording_live())

    def test_truthy_and_falsy_values(self):
        for v in ("1", "true", "on", " TRUE ", "On"):
            os.environ["RECORDING_LIVE"] = v
            self.assertTrue(recording_audit.recording_live(), v)
        for v in ("0", "", "off", "yes", "false", "no"):
            os.environ["RECORDING_LIVE"] = v
            self.assertFalse(recording_audit.recording_live(), v)

    def test_live_off_discards_refs_even_with_consent(self):
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 10, consent=True,
                        audio_ref="s3://a", transcript_ref="s3://t")
        self.assertIsNone(r["audio_ref"])
        self.assertIsNone(r["transcript_ref"])
        self.assertIs(r["consent"], True)

    def test_live_on_without_consent_discards_refs(self):
        os.environ["RECORDING_LIVE"] = "1"
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 10, consent=False,
                        audio_ref="s3://a", transcript_ref="s3://t")
        self.assertIsNone(r["audio_ref"])
        self.assertIsNone(r["transcript_ref"])

    def test_live_on_with_consent_without_key_drops_refs(self):
        """암호화 키가 없으면 참조를 **평문으로 저장하지 않고 폐기**한다(폴백 금지)."""
        os.environ["RECORDING_LIVE"] = "1"
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 10, consent=True,
                        audio_ref="s3://a", transcript_ref="s3://t")
        self.assertIsNone(r["audio_ref"])
        self.assertIsNone(r["transcript_ref"])
        self.assertEqual(r["protection"], "unavailable")
        self.assertTrue(any(a["action"] == "register_refs_dropped" for a in st.audit_log()),
                        "참조를 버린 사실이 감사기록에 없다")

    def test_live_on_with_consent_seals_refs_only(self):
        """참조는 봉인되어 남고, 원문 오디오·전사 필드는 애초에 존재하지 않는다."""
        os.environ["RECORDING_LIVE"] = "1"
        os.environ["PII_MASTER_KEY"] = base64.b64encode(b"K" * 32).decode()
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 10, consent=True,
                        audio_ref="s3://a", transcript_ref="s3://t")
        self.assertEqual(r["protection"], "sealed")
        self.assertNotIn("s3://", repr(r))
        self.assertTrue(r["audio_ref"].startswith("pv1."))
        self.assertEqual(st.reveal("agent-01", r["record_id"], "qa")["audio_ref"], "s3://a")
        for k in r:
            self.assertFalse(k in ("audio", "transcript", "audio_bytes", "text"), k)

    def test_register_does_not_flip_gate(self):
        st = recording_audit.RecordingStore()
        st.register("sim-1", consent=True, audio_ref="s3://a")
        self.assertIsNone(os.environ.get("RECORDING_LIVE"))
        self.assertFalse(recording_audit.recording_live())

    def test_register_audit_note_records_gate_state(self):
        st = recording_audit.RecordingStore()
        st.register("sim-1", consent=True)
        a = st.audit_log()[-1]
        self.assertEqual(a["action"], "register")
        self.assertEqual(a["actor"], "system")
        self.assertIn("live=False", a["note"])
        self.assertIn("consent=True", a["note"])


# ==========================================================================
# 7) 등록·접근
# ==========================================================================
class TestRecordingAccess(Base):
    def setUp(self):
        Base.setUp(self)
        self.st = recording_audit.RecordingStore(retention_days=30)
        self.rec = self.st.register("sim-1", "refund", 42, consent=True, now=1000.0)

    def test_record_id_sequence_and_fields(self):
        self.assertEqual(self.rec["record_id"], "REC-0001")
        r2 = self.st.register("sim-2", now=1000.0)
        self.assertEqual(r2["record_id"], "REC-0002")
        self.assertEqual(self.rec["scenario"], "refund")
        self.assertEqual(self.rec["duration_sec"], 42)
        self.assertEqual(self.rec["started_at"], 1000.0)
        self.assertEqual(self.rec["expires_at"], 1000.0 + 30 * 86400)
        self.assertEqual(self.rec["state"], "active")
        self.assertEqual(set(self.rec), set(recording_audit._meta_schema()),
                         "레코드 키가 문서화된 스키마와 어긋난다")

    def test_duration_coercion(self):
        self.assertEqual(self.st.register("s", duration_sec=None)["duration_sec"], 0)
        self.assertEqual(self.st.register("s", duration_sec="12")["duration_sec"], 12)
        self.assertEqual(self.st.register("s", duration_sec=7.9)["duration_sec"], 7)

    def test_separate_stores_have_separate_sequences(self):
        other = recording_audit.RecordingStore()
        self.assertEqual(other.register("x")["record_id"], "REC-0001")
        self.assertEqual(other.stats()["total"], 1)
        self.assertEqual(self.st.stats()["total"], 1)

    def test_register_returns_copy(self):
        self.rec["state"] = "purged"
        self.rec["session_id"] = "tampered"
        got = self.st.access("agent-01", "REC-0001", "qa")
        self.assertEqual(got["state"], "active")
        self.assertEqual(got["session_id"], "sim-1")

    def test_access_returns_copy(self):
        got = self.st.access("agent-01", "REC-0001", "qa")
        got["consent"] = False
        self.assertIs(self.st.access("agent-01", "REC-0001", "qa")["consent"], True)

    def test_all_allowed_purposes_pass_and_are_logged(self):
        for p in recording_audit.ALLOWED_PURPOSES:
            got = self.st.access("agent-01", "REC-0001", p)
            self.assertEqual(got["record_id"], "REC-0001")
        acts = [(a["action"], a["note"], a["actor"]) for a in self.st.audit_log()
                if a["action"] == "access"]
        self.assertEqual(acts, [("access", p, "agent-01")
                                for p in recording_audit.ALLOWED_PURPOSES])

    def test_disallowed_purpose_denied_and_logged(self):
        with self.assertRaises(PermissionError):
            self.st.access("agent-02", "REC-0001", "marketing")
        a = self.st.audit_log()[-1]
        self.assertEqual(a["action"], "access_denied")
        self.assertEqual(a["actor"], "agent-02")
        self.assertEqual(a["record"], "REC-0001")
        self.assertIn("marketing", a["note"])

    def test_purpose_is_exact_match(self):
        for p in ("QA", " qa", "qa ", "", None):
            with self.assertRaises(PermissionError, msg=repr(p)):
                self.st.access("agent-01", "REC-0001", p)

    def test_unknown_record_is_keyerror_and_logged(self):
        with self.assertRaises(KeyError):
            self.st.access("agent-01", "REC-9999", "audit")
        a = self.st.audit_log()[-1]
        self.assertEqual(a["action"], "access_miss")
        self.assertEqual(a["record"], "REC-9999")

    def test_purpose_check_precedes_existence_check(self):
        """불허 목적으로는 레코드 존재 여부조차 알 수 없다(열거 방지)."""
        with self.assertRaises(PermissionError):
            self.st.access("agent-01", "REC-9999", "marketing")
        self.assertEqual(self.st.audit_log()[-1]["action"], "access_denied")


# ==========================================================================
# 8) 보관·파기
# ==========================================================================
class TestRecordingRetention(Base):
    def setUp(self):
        Base.setUp(self)
        self.st = recording_audit.RecordingStore(retention_days=90)
        self.old = self.st.register("sim-old", "refund", 10, consent=True, now=0.0)
        self.new = self.st.register("sim-new", "refund", 10, consent=True, now=50 * 86400.0)

    def test_only_expired_are_purged(self):
        purged = self.st.purge_due(now=90 * 86400.0)
        self.assertEqual(purged, ["REC-0001"])
        self.assertEqual(self.st.stats(), {"active": 1, "purged": 1, "total": 2})
        self.assertEqual(self.st.access("a", "REC-0002", "qa")["state"], "active")

    def test_boundary_expires_at_equal_now_is_purged(self):
        self.assertEqual(self.st.purge_due(now=self.old["expires_at"]), ["REC-0001"])
        self.assertEqual(self.st.purge_due(now=self.old["expires_at"] - 1), [])

    def test_purged_record_not_accessible(self):
        self.st.purge_due(now=10 ** 9)
        with self.assertRaises(KeyError):
            self.st.access("a", "REC-0001", "qa")
        self.assertEqual(self.st.audit_log()[-1]["action"], "access_miss")

    def test_hard_delete_clears_refs_keeps_session(self):
        os.environ["RECORDING_LIVE"] = "1"
        r = self.st.register("sim-live", consent=True, audio_ref="s3://a",
                             transcript_ref="s3://t", now=0.0)
        self.st.purge_due(now=10 ** 9)
        rec = self.st._records[r["record_id"]]
        self.assertEqual(rec["state"], "purged")
        self.assertIsNone(rec["audio_ref"])
        self.assertIsNone(rec["transcript_ref"])
        self.assertEqual(rec["session_id"], "sim-live")

    def test_anonymize_replaces_session_id(self):
        self.st.purge_due(now=10 ** 9, method="anonymize")
        for rec in self.st._records.values():
            self.assertEqual(rec["session_id"], "anon")
            self.assertEqual(rec["state"], "purged")
        notes = [a["note"] for a in self.st.audit_log() if a["action"] == "purge"]
        self.assertEqual(notes, ["anonymize", "anonymize"])

    def test_unknown_method_is_rejected_before_any_purge(self):
        with self.assertRaises(ValueError):
            self.st.purge_due(now=10 ** 9, method="anonymise")
        self.assertEqual(self.st.stats()["purged"], 0)
        self.assertEqual([a for a in self.st.audit_log() if a["action"] == "purge"], [])

    def test_second_purge_is_noop_without_duplicate_audit(self):
        self.st.purge_due(now=10 ** 9)
        self.assertEqual(self.st.purge_due(now=10 ** 9), [])
        purges = [a for a in self.st.audit_log() if a["action"] == "purge"]
        self.assertEqual(len(purges), 2)

    def test_stats_empty_store(self):
        self.assertEqual(recording_audit.RecordingStore().stats(),
                         {"active": 0, "purged": 0, "total": 0})

    def test_default_policy_constants(self):
        self.assertEqual(recording_audit.DEFAULT_RETENTION_DAYS, 90)
        self.assertIn(recording_audit.DEFAULT_PURGE_METHOD, recording_audit.PURGE_METHODS)
        self.assertIsInstance(recording_audit.STORE, recording_audit.RecordingStore)


# ==========================================================================
# 9) 감사 로그 · 스키마
# ==========================================================================
class TestRecordingAudit(Base):
    def test_audit_log_is_append_only_copy(self):
        st = recording_audit.RecordingStore()
        st.register("sim-1", consent=True)
        log = st.audit_log()
        log[0]["action"] = "tampered"
        log[0]["actor"] = "mallory"
        log.clear()
        fresh = st.audit_log()
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["action"], "register")
        self.assertEqual(fresh[0]["actor"], "system")

    def test_no_delete_api_on_store(self):
        for name in dir(recording_audit.RecordingStore):
            self.assertFalse(name.lower().startswith(("delete", "remove", "clear")), name)

    def test_audit_entries_have_timestamp_and_shape(self):
        st = recording_audit.RecordingStore()
        before = time.time()
        st.register("sim-1")
        a = st.audit_log()[0]
        self.assertEqual(set(a), {"ts", "actor", "record", "action", "note"})
        self.assertGreaterEqual(a["ts"], before)

    def test_schema_has_no_raw_content_fields(self):
        keys = set(recording_audit._meta_schema())
        self.assertIn("audio_ref", keys)
        self.assertIn("transcript_ref", keys)
        for banned in ("audio", "transcript", "text", "phone", "caller", "name"):
            self.assertNotIn(banned, keys)

    def test_no_network_touched(self):
        """모듈 어디에도 네트워크·파일 저장이 없다(sim 저장소)."""
        st = recording_audit.RecordingStore()
        st.register("s", consent=True)
        st.access("a", "REC-0001", "qa")
        st.purge_due(now=10 ** 9)
        # urlopen 감시가 걸려 있으므로 여기까지 예외 없이 왔다는 것 자체가 검증


if __name__ == "__main__":
    unittest.main()
