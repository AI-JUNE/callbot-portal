# -*- coding: utf-8 -*-
"""설정에서 온 아웃바운드 주소 회귀 — `_vstudio`(VOICE_ENGINE_URL)·`_monitoring`(SENTRY_DSN).

23차가 남긴 제안의 회귀다. 20차는 *요청 본문*에서 온 URL 에 가드를 붙였고, 23차는
`ORDER_API_BASE`(설정)를 봤다. 남은 설정 유래 출구가 이 둘이다 — 전자는 **HMAC 서명**을,
후자는 **DSN 공개키와 오류 봉투**를 들고 나가는데 어느 쪽도 가드를 거치지 않았다.

둘 다 아직 미승인(`VOICE_ENGINE_URL`·`SENTRY_DSN` 미설정이 기본)이지만, 사람이
환경변수를 켜는 순간 전부 라이브다. 오타·잘못 복사한 값 하나로 합성할 문장과 서명,
오류 봉투가 평문·사설망·클라우드 메타데이터로 나간다.

검증 대상
  1) 주소 가드 — 평문 http·루프백·사설·링크로컬(메타데이터)·숫자표기 우회는 거부하고
     **요청 자체를 보내지 않는다**(blind SSRF 미성립). 탈출구는 개발 플래그·화이트리스트.
  2) 리다이렉트 이탈 — 검증을 통과한 주소가 302 로 허용 밖을 가리키면 본문을 쓰지 않는다.
  3) 응답 상한 — 워커가 거대한 본문을 주면 통화가 메모리로 죽는다.
  4) 거짓 보고 금지 — 거부된 설정을 「연결 전」·「미설정」으로 뭉개지 않는다
     (`_vstudio.status`·`/api/health.monitoring`). 사유에 주소·비밀값은 싣지 않는다.

네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

실행: python -m pytest tests/test_outbound_config.py -q
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _errors                                  # noqa: E402
import _monitoring as monitoring                # noqa: E402
import _vstudio                                 # noqa: E402
import health                                   # noqa: E402

# 내부를 가리키는 주소 모음 — 가드가 모두 거부해야 한다.
BAD_HOSTS = (
    "http://gpu.example.com",            # 평문(서명·발화가 그대로 흐른다)
    "https://127.0.0.1",                 # 루프백
    "https://localhost:8080",            # 이름으로 가리키는 루프백
    "https://10.0.0.5",                  # 사설
    "https://192.168.1.10",              # 사설
    "https://169.254.169.254",           # 클라우드 메타데이터
    "https://2130706433",                # 10진 표기 루프백(ip_address 가 ValueError)
    "https://0177.0.0.1",                # 8진 표기 루프백
    "https://worker.internal",           # 이름 접미사
    "ftp://gpu.example.com",             # http(s) 아님
    "https://",                          # 호스트 없음
)


class Fake(object):
    """응답 흉내 — `read(amt)`·`geturl()`."""

    def __init__(self, body=b"{}", url=""):
        self._b = body
        self._url = url

    def read(self, amt=None):
        return self._b if amt is None else self._b[:amt]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NoNetwork(unittest.TestCase):
    """urlopen 호출을 전부 가로채 기록한다(실제 소켓은 열리지 않는다)."""

    ENV = ()

    def setUp(self):
        self._env = dict(os.environ)
        for k in self.ENV:
            os.environ.pop(k, None)
        self.calls = []
        self.response = Fake()
        self._urlopen = urllib.request.urlopen

        def fake(req, timeout=None):
            self.calls.append(req)
            if isinstance(self.response, Exception):
                raise self.response
            if isinstance(self.response, Fake) and not self.response._url:
                self.response._url = req.full_url
            return self.response

        urllib.request.urlopen = fake

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        os.environ.clear()
        os.environ.update(self._env)


# ==========================================================================
# A) 보이스 스튜디오 — VOICE_ENGINE_URL
# ==========================================================================
VS_ENV = ("VOICE_ENGINE_URL", "VOICE_ENGINE_SECRET", "VOICE_STUDIO_VOICES",
          "VOICE_STUDIO_VOICE_SOURCE", "VOICE_ENGINE_ALLOW_INSECURE",
          "VOICE_ENGINE_HOSTS", "CALLBOT_DEBUG_ERRORS")

GOOD = {"VOICE_ENGINE_URL": "https://gpu.example.com",
        "VOICE_ENGINE_SECRET": "s3cret",
        "VOICE_STUDIO_VOICES": json.dumps([{"id": "v-ok", "consent": "ok"}])}

CATALOG = {"ok": True, "voices": [{"id": "v-eng", "consent": "ok", "kind": "clone"}],
           "unavailable": []}


class VsBase(NoNetwork):
    ENV = VS_ENV

    def setUp(self):
        super().setUp()
        _vstudio._voices_cache_clear()

    def tearDown(self):
        _vstudio._voices_cache_clear()
        super().tearDown()

    def env(self, **kw):
        return dict(GOOD, **kw)


class TestEngineUrlGuard(VsBase):

    def test_public_https_is_ready(self):
        self.assertTrue(_vstudio.engine_ready(self.env()))
        self.assertEqual(_vstudio.engine_status(self.env()), (True, ""))

    def test_internal_and_plaintext_urls_are_not_ready(self):
        for url in BAD_HOSTS:
            ready, why = _vstudio.engine_status(self.env(VOICE_ENGINE_URL=url))
            self.assertFalse(ready, url)
            self.assertTrue(why, "거부에는 사유가 있어야 한다: %s" % url)

    def test_missing_settings_is_not_a_rejection(self):
        """미설정은 「연결 전」이고 거부가 아니다 — 사유를 만들어 붙이지 않는다."""
        self.assertEqual(_vstudio.engine_status({}), (False, ""))
        self.assertEqual(_vstudio.engine_status({"VOICE_ENGINE_URL": GOOD["VOICE_ENGINE_URL"]}),
                         (False, ""))
        self.assertEqual(_vstudio.engine_status({"VOICE_ENGINE_SECRET": "s"}), (False, ""))

    def test_dev_flag_opens_loopback_only_when_set_to_one(self):
        env = self.env(VOICE_ENGINE_URL="http://127.0.0.1:8080")
        self.assertFalse(_vstudio.engine_ready(env))
        self.assertTrue(_vstudio.engine_ready(dict(env, VOICE_ENGINE_ALLOW_INSECURE="1")))
        for off in ("0", "true", "yes", "", " "):
            self.assertFalse(_vstudio.engine_ready(dict(env, VOICE_ENGINE_ALLOW_INSECURE=off)), off)

    def test_host_allowlist_is_the_strictest_setting(self):
        env = self.env(VOICE_ENGINE_HOSTS="gpu.example.com")
        self.assertTrue(_vstudio.engine_ready(env))
        self.assertFalse(_vstudio.engine_ready(
            dict(env, VOICE_ENGINE_URL="https://other.example.com")))

    def test_flags_are_read_from_the_given_env_not_the_process(self):
        """호출자가 넘긴 env 로 판단한다(배포 후 변경·테스트가 반영된다)."""
        os.environ["VOICE_ENGINE_ALLOW_INSECURE"] = "1"
        self.assertFalse(_vstudio.engine_ready(self.env(VOICE_ENGINE_URL="http://127.0.0.1")))


class TestEngineUrlNoRequest(VsBase):

    def test_rejected_url_sends_no_request_for_catalog(self):
        for url in BAD_HOSTS:
            _vstudio._voices_cache_clear()
            self.assertIsNone(_vstudio.fetch_catalog(self.env(VOICE_ENGINE_URL=url)), url)
            self.assertEqual(_vstudio.fetch_voices(self.env(VOICE_ENGINE_URL=url)), [])
        self.assertEqual(self.calls, [], "거부된 주소로는 요청을 보내지 않는다")

    def test_rejected_url_blocks_synth_before_sending(self):
        """2차 방어 — 합성할 문장과 서명 헤더를 거부된 주소로 보내지 않는다."""
        for url in BAD_HOSTS:
            with self.assertRaises(PermissionError, msg=url):
                _vstudio.synth_clone("안녕하세요", "v-ok", 1.0, self.env(VOICE_ENGINE_URL=url))
        self.assertEqual(self.calls, [])

    def test_blocked_synth_is_our_misconfiguration_not_upstream(self):
        status, code = _errors.classify(PermissionError("voice engine url blocked"))
        self.assertEqual((status, code), (500, "INTERNAL_ERROR"))

    def test_allowed_url_does_send(self):
        self.response = Fake(json.dumps(CATALOG).encode())
        cat = _vstudio.fetch_catalog(self.env())
        self.assertEqual([v["id"] for v in cat["voices"]], ["v-eng"])
        self.assertEqual(self.calls[0].full_url, "https://gpu.example.com/v1/voices")


class TestEngineRedirectAndSize(VsBase):

    def test_redirect_to_internal_host_discards_the_body(self):
        self.response = Fake(json.dumps(CATALOG).encode(), url="http://169.254.169.254/v1/voices")
        self.assertIsNone(_vstudio.fetch_catalog(self.env()))

    def test_redirect_inside_the_allowed_space_is_fine(self):
        self.response = Fake(json.dumps(CATALOG).encode(),
                             url="https://gpu.example.com/v2/voices")
        self.assertIsNotNone(_vstudio.fetch_catalog(self.env()))

    def test_redirect_to_internal_host_fails_synth(self):
        self.response = Fake(json.dumps({"audio_b64": "AAAA"}).encode(),
                             url="https://10.0.0.5/v1/synthesize")
        with self.assertRaises(urllib.error.URLError):
            _vstudio.synth_clone("안녕", "v-ok", 1.0, self.env())

    def test_unreadable_final_url_does_not_break_the_call(self):
        """최종 주소를 못 읽으면 판단 재료가 없다 — 정상 응답을 버리지는 않는다."""
        class NoGeturl(object):
            def read(self, amt=None):
                return json.dumps(CATALOG).encode()[:amt]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        self.response = NoGeturl()
        self.assertIsNotNone(_vstudio.fetch_catalog(self.env()))

    def test_oversize_catalog_is_refused_not_truncated(self):
        big = json.dumps({"ok": True, "voices": [], "pad": "x" * _vstudio.MAX_VOICES_RESPONSE})
        self.response = Fake(big.encode())
        self.assertIsNone(_vstudio.fetch_catalog(self.env()))

    def test_oversize_audio_is_refused(self):
        self.response = Fake(b"x" * (_vstudio.MAX_AUDIO_RESPONSE + 2))
        with self.assertRaises(urllib.error.URLError):
            _vstudio.synth_clone("안녕", "v-ok", 1.0, self.env())

    def test_limits_leave_normal_payloads_alone(self):
        self.assertGreaterEqual(_vstudio.MAX_AUDIO_RESPONSE, 1 << 20,
                                "한 줄 오디오를 담을 수 있어야 한다")
        self.response = Fake(json.dumps({"audio_b64": "QUJD"}).encode())
        data, mime = _vstudio.synth_clone("안녕", "v-ok", 1.0, self.env())
        self.assertEqual((data, mime), (b"ABC", "audio/wav"))


class TestStatusTellsTheTruth(VsBase):

    def test_rejected_config_is_not_reported_as_not_connected(self):
        st = _vstudio.status(self.env(VOICE_ENGINE_URL="https://169.254.169.254"))
        self.assertTrue(st["clone_blocked"])
        self.assertIn("거부", st["clone_note"])
        self.assertEqual(st["clone"], [])
        self.assertFalse(st["clone_ready"])

    def test_unset_config_is_reported_as_not_connected(self):
        st = _vstudio.status({})
        self.assertFalse(st["clone_blocked"])
        self.assertIn("연결 전", st["clone_note"])

    def test_working_config_is_not_marked_blocked(self):
        st = _vstudio.status(self.env())
        self.assertFalse(st["clone_blocked"])
        self.assertEqual([v["id"] for v in st["clone"]], ["v-ok"])
        self.assertNotIn("clone_blocked_reason", st)

    def test_reason_is_a_setting_hint_so_it_needs_the_debug_flag(self):
        env = self.env(VOICE_ENGINE_URL="https://10.0.0.5")
        self.assertNotIn("clone_blocked_reason", _vstudio.status(env))
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        self.assertIn("clone_blocked_reason", _vstudio.status(env))

    def test_status_never_leaks_the_url_or_secret(self):
        os.environ["CALLBOT_DEBUG_ERRORS"] = "1"
        env = self.env(VOICE_ENGINE_URL="https://10.0.0.5/private/path")
        blob = json.dumps(_vstudio.status(env), ensure_ascii=False)
        for leak in ("10.0.0.5", "/private/path", "s3cret"):
            self.assertNotIn(leak, blob, leak)


# ==========================================================================
# B) 모니터링 — SENTRY_DSN
# ==========================================================================
MON_ENV = ("SENTRY_DSN", "SENTRY_ALLOW_INSECURE", "SENTRY_HOSTS")
SAAS_DSN = "https://pubkey123@o0.ingest.sentry.io/4507"

BAD_DSNS = (
    "http://k@sentry.example.com/1",      # 평문(봉투와 공개키가 그대로 흐른다)
    "https://k@127.0.0.1/1",              # 루프백
    "https://k@localhost/1",              # 이름으로 가리키는 루프백
    "https://k@10.1.2.3/1",               # 사설
    "https://k@169.254.169.254/1",        # 클라우드 메타데이터
    "https://k@2130706433/1",             # 10진 표기 루프백
    "https://k@sentry.internal/1",        # 이름 접미사
)


class MonBase(NoNetwork):
    ENV = MON_ENV


class TestDsnGuard(MonBase):

    def test_saas_dsn_is_enabled(self):
        os.environ["SENTRY_DSN"] = SAAS_DSN
        self.assertTrue(monitoring.enabled())
        self.assertEqual(monitoring.blocked_reason(), "")
        url, key = monitoring.target()
        self.assertEqual(url, "https://o0.ingest.sentry.io/api/4507/envelope/")
        self.assertEqual(key, "pubkey123")

    def test_plaintext_and_internal_dsns_are_blocked(self):
        for dsn in BAD_DSNS:
            os.environ["SENTRY_DSN"] = dsn
            self.assertFalse(monitoring.enabled(), dsn)
            self.assertTrue(monitoring.blocked_reason(), dsn)
            self.assertIsNone(monitoring.target(), dsn)

    def test_blocked_dsn_sends_nothing(self):
        """거부된 주소로는 봉투를 만들지도, 보내지도 않는다(blind 전송 미성립)."""
        for dsn in BAD_DSNS:
            os.environ["SENTRY_DSN"] = dsn
            self.assertIsNone(monitoring.capture_error(ValueError("boom 010-1234-5678")), dsn)
        self.assertEqual(self.calls, [])

    def test_malformed_dsn_is_distinguished_from_a_blocked_one(self):
        os.environ["SENTRY_DSN"] = "https://o0.ingest.sentry.io/4507"   # 키 없음
        self.assertIn("형식", monitoring.blocked_reason())

    def test_unset_dsn_has_no_reason(self):
        self.assertEqual(monitoring.blocked_reason(), "")
        os.environ["SENTRY_DSN"] = "   "
        self.assertEqual(monitoring.blocked_reason(), "")

    def test_dev_flag_allows_self_hosted_plaintext(self):
        os.environ["SENTRY_DSN"] = "http://k@127.0.0.1:9000/1"
        self.assertFalse(monitoring.enabled())
        os.environ["SENTRY_ALLOW_INSECURE"] = "1"
        self.assertTrue(monitoring.enabled())

    def test_host_allowlist_is_the_strictest_setting(self):
        os.environ["SENTRY_DSN"] = SAAS_DSN
        os.environ["SENTRY_HOSTS"] = "o0.ingest.sentry.io"
        self.assertTrue(monitoring.enabled())
        os.environ["SENTRY_HOSTS"] = "sentry.gowon.co.kr"
        self.assertFalse(monitoring.enabled())

    def test_gate_is_read_at_call_time(self):
        """배포 후 환경변수 변경이 반영된다 — 끄는 쪽이 듣지 않으면 비상정지가 없다."""
        os.environ["SENTRY_DSN"] = SAAS_DSN
        self.assertTrue(monitoring.enabled())
        os.environ["SENTRY_HOSTS"] = "nowhere.example"
        self.assertFalse(monitoring.enabled())

    def test_allowed_dsn_does_send(self):
        os.environ["SENTRY_DSN"] = SAAS_DSN
        self.assertIsNotNone(monitoring.capture_error(ValueError("boom")))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].full_url,
                         "https://o0.ingest.sentry.io/api/4507/envelope/")

    def test_status_never_leaks_the_dsn_even_when_blocked(self):
        os.environ["SENTRY_DSN"] = "https://pubkey123@10.1.2.3/4507"
        st = monitoring.status()
        blob = json.dumps(st, ensure_ascii=False)
        self.assertTrue(st["dsn_present"])
        self.assertFalse(st["enabled"])
        for leak in ("pubkey123", "10.1.2.3", "4507"):
            self.assertNotIn(leak, blob, leak)


class TestHealthDoesNotFakeMonitoring(MonBase):

    def dep(self):
        for d in health._dependencies({}, health._monitoring(), False):
            if d["name"] == "monitoring":
                return d
        raise AssertionError("monitoring 의존성이 보고되지 않았다")

    def test_unset_dsn_is_not_configured(self):
        d = self.dep()
        self.assertEqual(d["status"], health.NOT_CONFIGURED)
        self.assertIn("미설정", d["detail"])

    def test_blocked_dsn_is_misconfigured_not_not_configured(self):
        """등록해 둔 DSN 으로 수집이 안 되는 이유를 헬스에서 알 수 있어야 한다."""
        os.environ["SENTRY_DSN"] = "https://pubkey123@169.254.169.254/4507"
        d = self.dep()
        self.assertEqual(d["status"], health.MISCONFIGURED)
        self.assertIn("거부", d["detail"])
        self.assertNotIn("미설정", d["detail"])

    def test_blocked_detail_carries_no_credentials_or_address(self):
        os.environ["SENTRY_DSN"] = "https://pubkey123@10.1.2.3/4507"
        d = self.dep()
        for leak in ("pubkey123", "10.1.2.3", "4507"):
            self.assertNotIn(leak, d["detail"], leak)

    def test_working_dsn_is_ok(self):
        os.environ["SENTRY_DSN"] = SAAS_DSN
        self.assertEqual(self.dep()["status"], health.OK)

    def test_health_and_the_sender_agree(self):
        """헬스가 「정상」이라고 말하는 동안 전송만 가드에서 막히는 엇갈림을 만들지 않는다."""
        for dsn in (SAAS_DSN,) + BAD_DSNS + ("", "not-a-url"):
            os.environ["SENTRY_DSN"] = dsn
            ok_health = self.dep()["status"] == health.OK
            self.assertEqual(ok_health, monitoring.target() is not None, dsn)


class TestConsoleAgreesWithStatus(unittest.TestCase):
    """화면 태그가 「연결 전」이라고 적는 동안 안내문만 「거부」라고 말하면 엇갈림이다."""

    def setUp(self):
        with open(os.path.join(ROOT, "public", "admin.html"), encoding="utf-8") as f:
            h = f.read()
        i = h.find("var VS={st:null")
        self.js = h[i:h.find("</script>", i)]

    def test_console_reads_the_blocked_flag(self):
        self.assertIn("j.clone_blocked", self.js)
        self.assertIn("설정 거부됨", self.js)

    def test_console_still_distinguishes_not_connected(self):
        self.assertIn("복제 엔진 연결 전", self.js)
        self.assertIn("복제 목소리 엔진 연결됨", self.js)

    def test_console_does_not_hardcode_the_note(self):
        """§8 — 화면은 서버 응답만 그린다(가짜 수치·가짜 상태를 만들지 않는다)."""
        self.assertIn("j.clone_note", self.js)
        self.assertNotIn("VOICE_ENGINE_URL", self.js, "설정 변수명을 화면에 적지 않는다")


# ==========================================================================
# C) 가드 구현이 한 곳인지 — 규칙을 두 군데 적어 두면 한쪽만 고쳐진다
# ==========================================================================
class TestSingleImplementation(unittest.TestCase):

    def test_both_modules_route_through_urlguard(self):
        for name in ("_vstudio.py", "_monitoring.py"):
            with open(os.path.join(ROOT, "api", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("_urlguard", src, name)

    def test_release_gate_requires_the_guard_in_both(self):
        """게이트가 가드 경유를 강제하지 않으면 다음 사람이 다시 떼어낼 수 있다."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import verify
        for name in ("_vstudio.py", "_monitoring.py"):
            self.assertIn(name, verify.URLGUARD_REQUIRED, name)
            self.assertIn(name, verify.OUTBOUND, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
