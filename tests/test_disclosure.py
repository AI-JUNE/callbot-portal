# -*- coding: utf-8 -*-
"""api/disclosure.py — AI 고지 문구 테넌트별 설정 회귀 테스트.

의존성 0 · 네트워크 미사용.

검증 대상 (COMMERCIAL_READINESS 'AI 고지 문구 테넌트별 설정 화면 (법정 요구)')
  1) 기본 문구 — 항상 요건 충족, {brand} 치환, 녹음 게이트에 따라 녹음 고지 문장 유무
  2) 규칙 검사 — AI 명시·운영주체·상담사 안내(권고)·녹음 고지 일치·길이·PII·금지어·마크업
  3) 저장소 — tenant_id 형식, 검사 실패 시 저장 거부(조용히 저장 안 함), 버전 증가, 상한, reset,
     append-only 이력, 반환값이 사본(외부 변조 격리)
  4) 실효 문구 — 설정 없으면 default, 있으면 tenant, greeting() 은 항상 문자열
  5) voice 배선 — 통화 첫 발화가 고지 문구, 테넌트 설정이 반영, CALLBOT_GREETING 하위호환,
     이벤트 파서가 tenant_id 를 전달(비문자열은 무시)
  6) HTTP 계약 — 403/200/400(details[].field)/reset, no-store·요청ID, CORS 되비침 금지, 감사 기록
  7) 금지어 목록이 scripts/verify.py 와 같다(드리프트 감시)

실행: python3 -m pytest tests/test_disclosure.py -q
"""
import os
import sys
import json
import unittest
import importlib.util
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _audit       # noqa: E402
import _ratelimit   # noqa: E402
import disclosure   # noqa: E402
import voice        # noqa: E402


def _no_net(*a, **k):
    raise AssertionError("network call attempted")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in ("RECORDING_LIVE", "CALLBOT_GREETING", "CALLBOT_STRICT", "CALLBOT_API_KEY",
                  "CALLBOT_DEBUG_ERRORS"):
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = _no_net
        disclosure._clear_for_tests()
        _ratelimit.reset()

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        disclosure._clear_for_tests()
        os.environ.clear()
        os.environ.update(self._env)


GOOD = "안녕하세요, {brand} AI 상담원입니다. 상담사 연결도 가능합니다. 무엇을 도와드릴까요?"


# --------------------------------------------------------------------------
# 1) 기본 문구
# --------------------------------------------------------------------------
class TestDefault(Base):
    def test_default_passes_all_checks(self):
        res = disclosure.check(disclosure.default_text(), disclosure.DEFAULT_BRAND)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["warnings"], [])

    def test_brand_rendered(self):
        self.assertIn("AICC Portal AI 상담원", disclosure.default_text())
        self.assertIn("온라인몰 AI 상담원", disclosure.default_text("온라인몰"))
        self.assertNotIn("{brand}", disclosure.default_text("온라인몰"))

    def test_recording_sentence_follows_gate(self):
        self.assertNotIn("녹음", disclosure.default_text())
        os.environ["RECORDING_LIVE"] = "1"
        t = disclosure.default_text()
        self.assertIn(disclosure.RECORDING_SENTENCE, t)
        self.assertTrue(disclosure.check(t)["ok"])

    def test_default_ok_under_both_gates(self):
        for v in ("", "1"):
            os.environ["RECORDING_LIVE"] = v
            self.assertTrue(disclosure.check(disclosure.default_text())["ok"], v)


# --------------------------------------------------------------------------
# 2) 규칙 검사
# --------------------------------------------------------------------------
class TestCheck(Base):
    def _errs(self, text, brand="AICC Portal", **kw):
        return [e["key"] for e in disclosure.check(text, brand, **kw)["errors"]]

    def test_missing_ai_identity(self):
        # 브랜드 'AICC Portal' 의 'AI' 부분문자열이 고지로 인정되면 안 된다(회귀로 발견)
        self.assertIn("ai_identity", self._errs("안녕하세요, {brand} 상담센터입니다. 무엇을 도와드릴까요?"))
        self.assertIn("ai_identity", self._errs("안녕하세요, AICC 상담센터입니다. 무엇을 도와드릴까요?", "AICC"))
        self.assertIn("ai_identity", self._errs("안녕하세요, {brand} 상담센터입니다. 문의: MAIL 참고", "몰"))

    def test_ai_keywords_accepted(self):
        for w in ("AI", "인공지능", "자동응답", "콜봇"):
            self.assertNotIn("ai_identity", self._errs("안녕하세요 {brand} %s 상담입니다" % w), w)

    def test_missing_operator(self):
        self.assertIn("operator", self._errs("안녕하세요, AI 상담원입니다. 무엇을 도와드릴까요?"))

    def test_operator_by_brand_name_without_placeholder(self):
        self.assertNotIn("operator", self._errs("안녕하세요, 온라인몰 AI 상담원입니다.", "온라인몰"))

    def test_human_option_is_warning_not_error(self):
        res = disclosure.check("안녕하세요, {brand} AI 응대 통화입니다. 무엇을 도와드릴까요?")
        self.assertTrue(res["ok"])
        self.assertEqual([w["key"] for w in res["warnings"]], ["human_option"])

    def test_recording_claim_without_recording_is_error(self):
        # 녹음하지 않으면서 "녹음됩니다" = 허위 고지
        self.assertIn("recording_consistency", self._errs(GOOD + " 통화는 녹음됩니다."))

    def test_recording_required_when_live(self):
        os.environ["RECORDING_LIVE"] = "1"
        self.assertIn("recording_consistency", self._errs(GOOD))
        self.assertNotIn("recording_consistency", self._errs(GOOD + " 통화 내용이 녹취됩니다."))

    def test_recording_param_overrides_env(self):
        self.assertNotIn("recording_consistency", self._errs(GOOD + " 녹음됩니다.", recording=True))

    def test_length_bounds(self):
        self.assertIn("length", self._errs("AI {brand"))   # 9자
        self.assertNotIn("length", self._errs("AI {brand}"))  # 10자 경계
        self.assertIn("length", self._errs(GOOD + "가" * 300))
        self.assertNotIn("length", self._errs(GOOD))

    def test_pii_rejected(self):
        self.assertIn("no_pii", self._errs(GOOD + " 문의 010-1234-5678"))
        self.assertIn("no_pii", self._errs(GOOD + " 900101-1234567"))

    def test_banned_rejected_and_named(self):
        res = disclosure.check(GOOD + " " + disclosure.BANNED[0] + " 도입")
        keys = [e["key"] for e in res["errors"]]
        self.assertIn("no_banned", keys)
        self.assertIn(disclosure.BANNED[0], [e for e in res["errors"] if e["key"] == "no_banned"][0]["reason"])

    def test_markup_rejected_but_placeholder_ok(self):
        self.assertIn("no_markup", self._errs(GOOD + " <b>x</b>"))
        self.assertIn("no_markup", self._errs(GOOD + " {phone}"))
        self.assertNotIn("no_markup", self._errs(GOOD))

    def test_checks_table_covers_requirements(self):
        keys = [c["key"] for c in disclosure.check(GOOD)["checks"]]
        for r in disclosure.REQUIREMENTS:
            self.assertIn(r["key"], keys)

    def test_non_string_text(self):
        res = disclosure.check(None)
        self.assertFalse(res["ok"])


# --------------------------------------------------------------------------
# 3) 저장소
# --------------------------------------------------------------------------
class TestStore(Base):
    def test_tenant_id_format(self):
        self.assertEqual(disclosure.validate_tenant_id(" Shop-01 "), "shop-01")
        for bad in ("", "-x", "a" * 41, "한글", "a b", None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                disclosure.validate_tenant_id(bad)

    def test_set_and_effective(self):
        rec = disclosure.set_text("shop", GOOD, "온라인몰")
        self.assertEqual(rec["version"], 1)
        eff = disclosure.effective("shop")
        self.assertEqual(eff["source"], "tenant")
        self.assertEqual(eff["text"], GOOD.replace("{brand}", "온라인몰"))
        self.assertTrue(eff["ok"])

    def test_invalid_text_not_saved(self):
        with self.assertRaises(ValueError) as cm:
            disclosure.set_text("shop", "안녕하세요 온라인몰입니다. 무엇을 도와드릴까요?", "온라인몰")
        self.assertTrue(cm.exception.details)
        self.assertEqual(disclosure.effective("shop")["source"], "default")
        self.assertEqual(disclosure.history(), [])

    def test_version_increments_and_history_append_only(self):
        disclosure.set_text("shop", GOOD)
        disclosure.set_text("shop", GOOD + " 감사합니다.")
        self.assertEqual(disclosure.effective("shop")["version"], 2)
        h = disclosure.history()
        self.assertEqual([x["op"] for x in h], ["set", "set"])
        self.assertEqual([x["from_version"] for x in h], [0, 1])
        h[0]["text"] = "변조"
        self.assertNotEqual(disclosure.history()[0]["text"], "변조")

    def test_reset(self):
        self.assertFalse(disclosure.reset("shop"))
        disclosure.set_text("shop", GOOD)
        self.assertTrue(disclosure.reset("shop"))
        self.assertEqual(disclosure.effective("shop")["source"], "default")
        self.assertEqual(disclosure.history()[-1]["op"], "reset")

    def test_tenant_cap(self):
        for i in range(disclosure.MAX_TENANTS):
            disclosure.set_text("t%d" % i, GOOD)
        with self.assertRaises(ValueError):
            disclosure.set_text("overflow", GOOD)
        disclosure.set_text("t0", GOOD + " 네.")     # 기존 테넌트 갱신은 허용

    def test_brand_markup_rejected(self):
        with self.assertRaises(ValueError):
            disclosure.set_text("shop", GOOD, "<b>몰</b>")

    def test_returned_records_are_copies(self):
        rec = disclosure.set_text("shop", GOOD)
        rec["text"] = "변조"
        self.assertNotEqual(disclosure.effective("shop")["template"], "변조")
        lst = disclosure.list_tenants()
        lst[0]["text"] = "변조"
        self.assertNotEqual(disclosure.effective("shop")["template"], "변조")

    def test_greeting_always_string(self):
        self.assertIsInstance(disclosure.greeting(None), str)
        self.assertIsInstance(disclosure.greeting("!!bad!!"), str)   # 잘못된 id 도 고지가 빠지지 않는다
        self.assertIn("AI", disclosure.greeting("!!bad!!"))

    def test_env_override_reported(self):
        self.assertIsNone(disclosure.env_override())
        os.environ["CALLBOT_GREETING"] = "안녕하세요, 콜센터입니다."
        ov = disclosure.env_override()
        self.assertTrue(ov["set"])
        self.assertFalse(ov["ok"])
        self.assertIn("ai_identity", [e["key"] for e in ov["errors"]])


# --------------------------------------------------------------------------
# 5) voice 배선
# --------------------------------------------------------------------------
class TestVoiceWiring(Base):
    def setUp(self):
        super().setUp()
        voice._Session._mem.clear()

    def test_first_utterance_is_default_disclosure(self):
        out = voice.handle_event({"type": "answered", "call_id": "c1", "from": "01011112222", "scenario": "care"})
        self.assertEqual(out["actions"][0]["text"], disclosure.default_text())
        self.assertIn("AI", out["actions"][0]["text"])

    def test_tenant_setting_reflected_in_call(self):
        disclosure.set_text("shop", GOOD, "온라인몰")
        out = voice.handle_event({"type": "answered", "call_id": "c2", "from": "010", "scenario": "refund",
                                  "tenant": "shop"})
        self.assertEqual(out["actions"][0]["text"], GOOD.replace("{brand}", "온라인몰"))
        out = voice.handle_event({"type": "answered", "call_id": "c3", "from": "010", "scenario": "refund"})
        self.assertEqual(out["actions"][0]["text"], disclosure.default_text())

    def test_env_greeting_kept_for_compat(self):
        os.environ["CALLBOT_GREETING"] = "안녕하세요 운영자 문구입니다"
        self.assertEqual(voice.greeting(None), "안녕하세요 운영자 문구입니다")
        disclosure.set_text("shop", GOOD)
        self.assertEqual(voice.greeting("shop"), GOOD.replace("{brand}", "AICC Portal"))   # 테넌트가 env 보다 우선

    def test_twiml_first_leg_uses_disclosure(self):
        out = voice.handle_twilio({"CallId": "c9", "From": "01011112222", "CallStatus": "in-progress"})
        self.assertIn("AI 상담원", out)

    def test_parse_event_passes_tenant(self):
        ev = voice._parse_event({"type": "answered", "call_id": "x", "tenant_id": "shop"})
        self.assertEqual(ev["tenant"], "shop")
        ev = voice._parse_event({"type": "answered", "call_id": "x", "tenant_id": {"$gt": ""}})
        self.assertIsNone(ev["tenant"])
        ev = voice._parse_event({"type": "answered", "call_id": "x"})
        self.assertIsNone(ev["tenant"])

    def test_greeting_never_empty(self):
        os.environ["CALLBOT_GREETING"] = "   "
        self.assertTrue(voice.greeting(None).strip())


# --------------------------------------------------------------------------
# 6) HTTP 계약
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


class Recorder(object):
    def __init__(self, headers=None, body=b""):
        self.headers = FakeHeaders(headers or {})
        self.wfile = FakeWFile()
        self.rfile = FakeRFile(body)
        self.status = None
        self.sent = []

    def send_response(self, c):
        self.status = c

    def send_header(self, k, v):
        self.sent.append((k, str(v)))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.5"}


def call(method, path="/api/disclosure", headers=None, payload=None):
    data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = dict(headers or {})
    if data:
        hdrs.setdefault("content-length", str(len(data)))
        hdrs.setdefault("content-type", "application/json")
    h = Recorder(hdrs, data)
    inst = disclosure.handler.__new__(disclosure.handler)
    inst.headers = h.headers
    inst.wfile = h.wfile
    inst.rfile = h.rfile
    inst.path = path
    inst.send_response = h.send_response
    inst.send_header = h.send_header
    inst.end_headers = h.end_headers
    getattr(inst, "do_" + method)()
    return h


class TestHttp(Base):
    def test_cross_origin_403_and_audited(self):
        before = _audit.counters()["deny"]
        h = call("GET", headers={"origin": "https://evil.example"})
        self.assertEqual(h.status, 403)
        self.assertFalse(h.body()["ok"])
        self.assertNotEqual(h.header("Access-Control-Allow-Origin"), "https://evil.example")
        self.assertEqual(_audit.counters()["deny"], before + 1)

    def test_info(self):
        h = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        b = h.body()
        self.assertTrue(b["ok"])
        self.assertEqual(b["default"]["source"], "default")
        self.assertTrue(b["default"]["ok"])
        self.assertEqual([r["key"] for r in b["requirements"]], [r["key"] for r in disclosure.REQUIREMENTS])
        self.assertIn("[승인 필요]", b["persistence"])
        self.assertEqual(h.header("Cache-Control"), "no-store")
        self.assertTrue(h.header("X-Request-Id"))

    def test_options_preflight(self):
        h = call("OPTIONS", headers=SAME_ORIGIN)
        self.assertEqual(h.status, 204)

    def test_tenant_query_default_when_unset(self):
        h = call("GET", "/api/disclosure?tenant=shop", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body()["source"], "default")
        self.assertEqual(h.body()["tenant_id"], "shop")

    def test_tenant_query_invalid_400(self):
        h = call("GET", "/api/disclosure?tenant=한글", SAME_ORIGIN)
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["details"][0]["field"], "tenant")

    def test_unknown_op_400(self):
        h = call("GET", "/api/disclosure?op=drop", SAME_ORIGIN)
        self.assertEqual(h.status, 400)

    def test_post_set_then_get(self):
        before = _audit.counters()["allow"]
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "Shop", "text": GOOD, "brand": "온라인몰"})
        self.assertEqual(h.status, 200, h.wfile.data)
        b = h.body()
        self.assertTrue(b["ok"])
        self.assertFalse(b["dry_run"])
        self.assertEqual(b["saved"]["version"], 1)
        self.assertEqual(b["effective"]["text"], GOOD.replace("{brand}", "온라인몰"))
        self.assertEqual(_audit.counters()["allow"], before + 1)
        g = call("GET", "/api/disclosure?tenant=shop", SAME_ORIGIN).body()
        self.assertEqual(g["source"], "tenant")
        lst = call("GET", "/api/disclosure?op=list", SAME_ORIGIN).body()
        self.assertEqual(lst["count"], 1)
        hist = call("GET", "/api/disclosure?op=history", SAME_ORIGIN).body()["history"]
        self.assertEqual(hist[-1]["tenant_id"], "shop")
        self.assertEqual(hist[-1]["actor"], "origin:same-origin")

    def test_post_dry_run_does_not_save(self):
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "text": GOOD, "dry_run": True})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body()["dry_run"])
        self.assertEqual(disclosure.list_tenants(), [])

    def test_post_invalid_text_400_with_keys(self):
        before = _audit.counters()["error"]
        h = call("POST", headers=SAME_ORIGIN,
                 payload={"tenant_id": "shop", "text": "안녕하세요 온라인몰입니다. 무엇을 도와드릴까요?", "brand": "온라인몰"})
        self.assertEqual(h.status, 400)
        b = h.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "INVALID_REQUEST")
        self.assertIn("ai_identity", [d["key"] for d in b["details"]])
        self.assertTrue(all(d["field"] == "text" for d in b["details"]))
        self.assertEqual(disclosure.list_tenants(), [])
        self.assertEqual(_audit.counters()["error"], before + 1)

    def test_post_missing_tenant_400(self):
        h = call("POST", headers=SAME_ORIGIN, payload={"text": GOOD})
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["details"][0]["field"], "tenant_id")

    def test_post_bad_tenant_400(self):
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "Bad Id!", "text": GOOD})
        self.assertEqual(h.status, 400)
        self.assertEqual(h.body()["details"][0]["field"], "tenant_id")

    def test_post_reset(self):
        call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "text": GOOD})
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "op": "reset"})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body()["reset"])
        self.assertEqual(h.body()["effective"]["source"], "default")
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "op": "reset"})
        self.assertFalse(h.body()["reset"])

    def test_post_cross_origin_403_not_saved(self):
        h = call("POST", headers={"origin": "https://evil.example"}, payload={"tenant_id": "shop", "text": GOOD})
        self.assertEqual(h.status, 403)
        self.assertEqual(disclosure.list_tenants(), [])

    def test_post_non_json_400(self):
        hdrs = dict(SAME_ORIGIN, **{"content-length": "3", "content-type": "application/json"})
        h = Recorder(hdrs, b"{{{")
        inst = disclosure.handler.__new__(disclosure.handler)
        inst.headers, inst.wfile, inst.rfile, inst.path = h.headers, h.wfile, h.rfile, "/api/disclosure"
        inst.send_response, inst.send_header, inst.end_headers = h.send_response, h.send_header, h.end_headers
        inst.do_POST()
        self.assertEqual(h.status, 400)

    def test_recording_live_reflected_in_info(self):
        os.environ["RECORDING_LIVE"] = "1"
        b = call("GET", headers=SAME_ORIGIN).body()
        self.assertTrue(b["recording_live"])
        self.assertIn("녹음", b["default"]["text"])


# --------------------------------------------------------------------------
# 7) 금지어 드리프트
# --------------------------------------------------------------------------
class TestBannedDrift(unittest.TestCase):
    def test_same_as_verify(self):
        spec = importlib.util.spec_from_file_location("verify", os.path.join(ROOT, "scripts", "verify.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(tuple(mod.BANNED), tuple(disclosure.BANNED))


if __name__ == "__main__":
    unittest.main()
