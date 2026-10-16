# -*- coding: utf-8 -*-
"""통화 세션 보관·발화 마스킹·종료 기록 회귀 (30차) — 네트워크 미사용.

왜 이 파일이 필요한가
  이 서비스에는 영속 저장소가 없다(승인 전). 그래서 **통화 데이터는 전부
  인스턴스 메모리**에 있고, 보관 규칙이 코드에 적혀 있지 않으면 보관 기간은
  '웜 인스턴스의 수명'이 된다. 30차에 드러난 결함 3건은 전부 그 자리였다:

  1) `_Session._mem` 에 상한·만료가 없었다 — 대화 원문(전사)과 발신번호를 담은
     세션이 `completed` 이벤트가 오지 않으면 영구히 남고, 통화를 처리할수록
     메모리가 자라 결국 진행 중인 통화가 죽는다(26차가 전환 티켓에서 고친 결함).
  2) 통화 종료 요약을 `print` 로 플랫폼 로그에 **전문** 찍었다 — scrub·요청ID·
     로그 등록부를 모두 비켜간다.
  3) `?op=log` 가 발화 **원문**을 돌려줬다 — 발신번호는 마스킹했는데 같은
     레코드의 발화는 그대로였다(고객이 말한 주민번호·카드번호가 나간다).

  방어 코드는 장애·초과 때만 돌기 때문에, 회귀가 없으면 '있는 줄만 알았지
  실제로는 안 도는' 상태가 된다. 그래서 상한을 **실제로 넘겨** 확인한다.

실행: python3 -m pytest tests/test_voice_session.py -q
"""
import json
import os
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice  # noqa: E402
import _ratelimit  # noqa: E402

from test_voice import FakeHandler, SAME_ORIGIN, get  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        voice._Session._mem = {}
        del voice.RECENT[:]
        voice.SESSION_STATS.update({"expired": 0, "evicted": 0, "last_reason": None})
        _ratelimit.reset()
        self._env = dict(os.environ)
        os.environ["CALLBOT_LOG"] = "off"      # stdout 소음 제거(tearDown 에서 복원)
        self._mod = {k: getattr(voice, k) for k in
                     ("run_turn", "transcribe", "run_assist")}
        self._urlopen = urllib.request.urlopen

        def blocked(*a, **k):
            raise AssertionError("테스트가 네트워크를 호출했다: %r" % (a[:1],))

        urllib.request.urlopen = blocked

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._mod.items():
            setattr(voice, k, v)
        os.environ.clear()
        os.environ.update(self._env)
        voice._Session._mem = {}
        del voice.RECENT[:]
        voice.SESSION_STATS.update({"expired": 0, "evicted": 0, "last_reason": None})
        _ratelimit.reset()

    def sess(self, n=0):
        return {"messages": [{"role": "user", "content": "발화 %d" % n}],
                "scenario": "refund", "phone": "01011112222"}


# ==========================================================================
# 1) 세션 상한 — 통화를 처리할수록 메모리가 자라지 않는다
# ==========================================================================
class TestSessionCap(Base):
    def test_cap_is_enforced_on_put(self):
        os.environ["CALLBOT_SESSION_MAX"] = "5"
        for i in range(40):
            voice._Session.put("c%d" % i, self.sess(i))
        self.assertLessEqual(len(voice._Session._mem), 5)

    def test_eviction_drops_the_least_recently_touched(self):
        """방금 말한 고객의 맥락을 먼저 버리지 않는다."""
        os.environ["CALLBOT_SESSION_MAX"] = "2"
        now = time.time()
        voice._Session.put("old", self.sess(1))
        voice._Session._mem["old"]["seen"] = now - 100
        voice._Session.put("mid", self.sess(2))
        voice._Session._mem["mid"]["seen"] = now - 50
        voice._Session.put("new", self.sess(3))      # 상한 초과 -> old 축출
        self.assertIsNone(voice._Session._mem.get("old"))
        self.assertIsNotNone(voice._Session._mem.get("new"))

    def test_eviction_is_counted_not_silent(self):
        os.environ["CALLBOT_SESSION_MAX"] = "1"
        voice._Session.put("a", self.sess(1))
        voice._Session.put("b", self.sess(2))
        self.assertEqual(voice.SESSION_STATS["evicted"], 1)
        self.assertIn("상한", voice.SESSION_STATS["last_reason"])

    def test_broken_cap_env_falls_back_to_the_default(self):
        """잘못된 설정으로 상한이 **사라지는** 쪽으로 실패하지 않는다."""
        for bad in ("", "0", "-5", "열", "1e9999999", "None"):
            os.environ["CALLBOT_SESSION_MAX"] = bad
            self.assertEqual(
                voice._session_limit("CALLBOT_SESSION_MAX", voice.SESSION_MAX, int),
                voice.SESSION_MAX, bad)

    def test_cap_is_read_at_call_time(self):
        """배포 후 상한을 조정하면 다음 통화부터 반영된다(import 시점 상수 금지)."""
        os.environ["CALLBOT_SESSION_MAX"] = "3"
        self.assertEqual(voice._Session.stats()["max"], 3)
        os.environ["CALLBOT_SESSION_MAX"] = "7"
        self.assertEqual(voice._Session.stats()["max"], 7)

    def test_cap_scale_matches_the_metrics_collector(self):
        """같은 통화를 세는 쪽과 눈금을 같이 쓴다(한쪽만 바뀌면 드리프트)."""
        import _call_metrics
        self.assertEqual(voice.SESSION_MAX, _call_metrics.MAX_OPEN)


# ==========================================================================
# 2) 세션 만료 — 종료 이벤트가 오지 않아도 대화 원문이 남지 않는다
# ==========================================================================
class TestSessionTtl(Base):
    def test_expired_session_is_treated_as_absent(self):
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        voice._Session.put("c1", self.sess(1))
        voice._Session._mem["c1"]["seen"] = time.time() - 61
        self.assertIsNone(voice._Session.get("c1"))
        self.assertNotIn("c1", voice._Session._mem, "만료 세션이 메모리에 남았다")
        self.assertEqual(voice.SESSION_STATS["expired"], 1)

    def test_fresh_session_survives(self):
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        voice._Session.put("c1", self.sess(1))
        self.assertIsNotNone(voice._Session.get("c1"))
        self.assertEqual(voice.SESSION_STATS["expired"], 0)

    def test_touch_extends_a_live_call(self):
        """말하는 동안에는 만료되지 않는다 — 통화가 TTL 보다 길어도 맥락이 유지된다."""
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        voice._Session.put("c1", self.sess(1))
        voice._Session._mem["c1"]["seen"] = time.time() - 59
        self.assertIsNotNone(voice._Session.get("c1"))      # 접촉 -> seen 갱신
        self.assertIsNotNone(voice._Session.get("c1"))
        self.assertEqual(voice.SESSION_STATS["expired"], 0)

    def test_sweep_clears_abandoned_calls(self):
        """종료 이벤트가 유실된 통화들이 쌓여 있어도 다음 통화 1건이 정리한다."""
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        for i in range(10):
            voice._Session._mem["dead%d" % i] = dict(self.sess(i), seen=time.time() - 999)
        voice._Session.put("live", self.sess(99))
        self.assertEqual(list(voice._Session._mem), ["live"])
        self.assertEqual(voice.SESSION_STATS["expired"], 10)

    def test_non_numeric_seen_does_not_crash_the_call(self):
        """외부가 넣은 값이 섞여도 판정이 예외로 죽지 않는다(만료 쪽으로 처리)."""
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        voice._Session._mem["c1"] = dict(self.sess(1), seen="어제")
        self.assertIsNone(voice._Session.get("c1"))

    def test_broken_ttl_env_falls_back_to_the_default(self):
        for bad in ("", "0", "-1", "하루"):
            os.environ["CALLBOT_SESSION_TTL"] = bad
            self.assertEqual(
                voice._session_limit("CALLBOT_SESSION_TTL", voice.SESSION_TTL, float),
                voice.SESSION_TTL, bad)

    def test_losing_a_session_does_not_kill_the_call(self):
        """세션이 사라져도 통화는 프레시 세션으로 이어진다(맥락만 잃는다)."""
        os.environ["CALLBOT_SESSION_TTL"] = "60"
        calls = []

        def run_turn(messages, phone="", scenario="refund"):
            calls.append(list(messages))
            return {"messages": list(messages) + [{"role": "assistant", "content": "네"}],
                    "reply": "네", "transferred": False}

        voice.run_turn = run_turn
        voice._Session._mem["c1"] = dict(self.sess(1), seen=time.time() - 999)
        out = voice.handle_event({"type": "speech", "call_id": "c1", "from": "010",
                                  "scenario": "refund", "text": "여보세요"})
        self.assertIn("actions", out)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["content"], "여보세요", "만료된 대화를 이어받았다")


# ==========================================================================
# 3) 보관 현황 노출 — 버린 것이 조용히 사라지지 않는다
# ==========================================================================
class TestSessionStatsExposure(Base):
    def test_get_exposes_session_counters(self):
        os.environ["CALLBOT_SESSION_MAX"] = "1"
        voice._Session.put("a", self.sess(1))
        voice._Session.put("b", self.sess(2))
        d = get("/api/voice", headers=SAME_ORIGIN).json()
        self.assertIn("sessions", d)
        self.assertEqual(d["sessions"]["live"], 1)
        self.assertEqual(d["sessions"]["evicted"], 1)
        self.assertEqual(d["sessions"]["max"], 1)
        self.assertGreater(d["sessions"]["ttl_sec"], 0)

    def test_stats_carry_no_call_identifiers_or_speech(self):
        voice._Session.put("call-abc-1234", self.sess(1))
        voice._log_call({"from": "01011112222", "ev": "고객발화", "text": "비밀 발화"})
        body = get("/api/voice", headers=SAME_ORIGIN).text()
        self.assertNotIn("call-abc-1234", body)
        self.assertNotIn("비밀 발화", body)

    def test_stats_snapshot_is_a_copy(self):
        d = voice._Session.stats()
        d["evicted"] = 999
        self.assertEqual(voice.SESSION_STATS["evicted"], 0)


# ==========================================================================
# 4) 통화 로그의 발화 마스킹 — `?op=log` 는 인증 경로가 아니다
# ==========================================================================
class TestSpeechMasking(Base):
    CASES = (
        ("제 주민번호는 900101-1234567이에요", "900101-1234567"),
        ("카드번호 1234-5678-9012-3456 로 결제했어요", "1234-5678-9012-3456"),
        ("제 번호는 010-1111-2222 예요", "010-1111-2222"),
        ("메일은 hong@example.com 입니다", "hong@example.com"),
        ("계좌는 123-456789-01 이에요", "123-456789-01"),
    )

    def test_recent_log_masks_personal_data_in_speech(self):
        for text, secret in self.CASES:
            del voice.RECENT[:]
            voice._log_call({"from": "01011112222", "ev": "고객발화", "text": text})
            self.assertNotIn(secret, voice.RECENT[0]["text"], text)

    def test_http_log_route_does_not_leak_speech(self):
        for text, secret in self.CASES:
            del voice.RECENT[:]
            voice._log_call({"from": "01011112222", "ev": "고객발화", "text": text})
            body = get("/api/voice?op=log", headers=SAME_ORIGIN).text()
            self.assertNotIn(secret, body, text)

    def test_bot_reply_is_masked_too(self):
        """봇 응답은 고객 정보를 되읽어 주는 경로다(확인차 복창)."""
        voice._log_call({"from": "010", "ev": "봇응답",
                         "text": "010-1111-2222 번으로 안내드릴까요?"})
        self.assertNotIn("010-1111-2222", voice.RECENT[0]["text"])

    def test_masking_keeps_the_sentence_readable(self):
        """가리는 것이지 지우는 것이 아니다 — 화면의 대화 흐름 확인이 유지된다."""
        voice._log_call({"from": "010", "ev": "고객발화",
                         "text": "환불하고 싶어요. 주민번호는 900101-1234567이에요"})
        self.assertIn("환불하고 싶어요", voice.RECENT[0]["text"])

    def test_length_is_capped(self):
        voice._log_call({"from": "010", "ev": "고객발화", "text": "가" * 5000})
        self.assertLessEqual(len(voice.RECENT[0]["text"]), voice.MAX_LOG_TEXT)

    def test_retention_cap_is_named_not_a_magic_number(self):
        for i in range(voice.MAX_RECENT + 25):
            voice._log_call({"from": "010", "ev": "고객발화", "text": str(i)})
        self.assertEqual(len(voice.RECENT), voice.MAX_RECENT)

    def test_speech_through_the_live_path_is_masked(self):
        """단위 함수만 보면 호출부가 우회할 수 있다 — 실제 이벤트 경로로 확인한다."""
        def run_turn(messages, phone="", scenario="refund"):
            return {"messages": list(messages) + [{"role": "assistant", "content": "확인했습니다"}],
                    "reply": "확인했습니다", "transferred": False}

        voice.run_turn = run_turn
        voice.handle_event({"type": "speech", "call_id": "c1", "from": "01011112222",
                            "scenario": "refund",
                            "text": "주민번호 900101-1234567 입니다"})
        body = get("/api/voice?op=log", headers=SAME_ORIGIN).text()
        self.assertNotIn("900101-1234567", body)
        self.assertNotIn("01011112222", body)

    def test_empty_and_missing_text_are_untouched(self):
        voice._log_call({"from": "010", "ev": "통화연결", "text": ""})
        self.assertEqual(voice.RECENT[0]["text"], "")
        voice._log_call({"from": "010", "ev": "통화종료"})
        self.assertNotIn("text", voice.RECENT[0])

    def test_masking_fallback_does_not_pass_speech_through(self):
        """scrub 모듈이 없어도 발화를 그대로 내보내지 않는다(fail-closed).

        폴백 **함수 자체**를 호출한다 — 대역을 끼워 넣으면 모듈의 실제 폴백이
        무엇을 하는지는 검증되지 않는다(그 자리에 원문 통과가 들어와도 침묵한다).
        """
        self.assertNotIn("900101", voice._scrub_unavailable("900101-1234567"))
        self.assertEqual(voice._scrub_unavailable(""), "")
        orig = voice._scrub
        voice._scrub = voice._scrub_unavailable
        try:
            voice._log_call({"from": "010", "ev": "고객발화", "text": "900101-1234567"})
            self.assertNotIn("900101", voice.RECENT[0]["text"])
        finally:
            voice._scrub = orig


# ==========================================================================
# 5) 통화 종료 기록 — 요약 전문이 플랫폼 로그로 새지 않는다
# ==========================================================================
class TestCallDoneLog(Base):
    SECRET = "고객 홍길동(010-1111-2222)이 환불을 요청"

    def _capture(self):
        """`_log.emit` 이 받은 레코드를 모은다(실제 출력은 하지 않는다)."""
        import _log
        got = []
        orig = _log.emit
        _log.emit = lambda rec: got.append(rec)
        self.addCleanup(lambda: setattr(_log, "emit", orig))
        return got

    def test_summary_body_is_not_logged(self):
        got = self._capture()
        voice._persist_call_result("c1", self.sess(1), {"summary": self.SECRET})
        blob = json.dumps(got, ensure_ascii=False)
        self.assertNotIn("홍길동", blob)
        self.assertNotIn("010-1111-2222", blob)

    def test_raw_model_output_is_not_logged(self):
        """`assist.parse_json` 이 파싱에 실패하면 모델 원문이 그대로 담긴다."""
        got = self._capture()
        voice._persist_call_result("c1", self.sess(1), {"raw": self.SECRET})
        self.assertNotIn("홍길동", json.dumps(got, ensure_ascii=False))

    def test_call_id_is_fingerprinted_not_logged_raw(self):
        got = self._capture()
        voice._persist_call_result("01011112222", self.sess(1), {"summary": "x"})
        blob = json.dumps(got, ensure_ascii=False)
        self.assertNotIn("01011112222", blob)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].get("call"), "추적 지문이 비어 있다")

    def test_fingerprint_matches_the_metrics_collector(self):
        import _call_metrics
        self.assertEqual(voice._call_digest("c1"), _call_metrics.digest("c1"))

    def test_facts_are_recorded(self):
        got = self._capture()
        voice._persist_call_result("c1", self.sess(1), {"summary": "x"})
        self.assertEqual(got[0]["event"], "call_done")
        self.assertEqual(got[0]["result"], "summarized")
        self.assertEqual(got[0]["turn_count"], 1)
        self.assertEqual(got[0]["route"], "/api/voice")

    def test_missing_summary_is_reported_as_such(self):
        got = self._capture()
        voice._persist_call_result("c1", self.sess(1), None)
        self.assertEqual(got[0]["result"], "no_summary")

    def test_nothing_is_printed_to_stdout(self):
        """회귀: 예전엔 `print` 1줄이 요약 전문을 플랫폼 로그에 찍었다."""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            voice._persist_call_result("c1", self.sess(1), {"summary": self.SECRET})
        self.assertEqual(buf.getvalue(), "")

    def test_completed_event_still_returns_the_summary_to_the_caller(self):
        """로그에서 뺐다고 소비자에게서도 빼지 않는다(통화 주체의 데이터다)."""
        voice.run_assist = lambda task, text: {"summary": self.SECRET}
        voice._Session.put("c1", self.sess(1))
        out = voice.handle_event({"type": "completed", "call_id": "c1", "from": "010",
                                  "scenario": "refund"})
        self.assertEqual(out["summary"], {"summary": self.SECRET})
        self.assertIsNone(voice._Session.get("c1"), "종료 후 세션이 남았다")

    def test_logging_failure_does_not_break_the_response(self):
        import _log
        orig = _log.emit

        def boom(rec):
            raise RuntimeError("로그 저장소 장애")

        _log.emit = boom
        try:
            voice._persist_call_result("c1", self.sess(1), {"summary": "x"})
        finally:
            _log.emit = orig

    def test_unusable_call_id_does_not_break_the_record(self):
        """지문을 못 만드는 통화ID(빈 값·비문자열)도 예외가 아니라 None 이다."""
        for bad in ("", "   ", None, 123):
            self.assertIsNone(voice._call_digest(bad), repr(bad))
        got = self._capture()
        voice._persist_call_result("", self.sess(1), {"summary": "x"})
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["call"])

    def test_digest_without_the_metrics_module_is_none_not_an_exception(self):
        orig = voice.call_metrics
        voice.call_metrics = None
        try:
            self.assertIsNone(voice._call_digest("c1"))
        finally:
            voice.call_metrics = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
