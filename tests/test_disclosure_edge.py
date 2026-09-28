# -*- coding: utf-8 -*-
"""api/disclosure.py 잔여 방어 분기 회귀 — 네트워크 미사용, 의존성 0.

95% 가 이미 덮여 있다. 남은 것은 **모듈 부재·장애 때만 도는 방어 분기**다.
AI 고지 문구는 법정 요구 화면이므로, 장부·조회가 하위 모듈 장애로 죽지 않는지 고정한다.

검증 대상
  1) 저장 입력검증  — text 가 문자열이 아니면 거부(조용히 저장하지 않음)
  2) 스코프 격리    — partners 모듈이 없거나 터져도 목록 조회가 죽지 않는다(None = 미적용)
  3) 감사 격리      — 감사 모듈 부재·장애가 요청을 죽이지 않고 문구도 새지 않는다
  4) 주체 식별 격리 — actor(감사) 산출 실패 시 익명으로 진행(요청은 산다)
  5) HTTP 잔여 경로 — 저장 상한 초과가 200 이 아니라 400(details[].field)으로 드러난다

실행: python3 -m pytest tests/test_disclosure_edge.py -q
"""
import os
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import disclosure   # noqa: E402
import _ratelimit   # noqa: E402

SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.5"}
GOOD = "안녕하세요, {brand} AI 상담원입니다. 상담사 연결도 가능합니다. 무엇을 도와드릴까요?"


def _no_net(*a, **k):
    raise AssertionError("network call attempted")


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
        self._audit = disclosure._audit
        self._partners_mod = sys.modules.get("partners")

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        disclosure._audit = self._audit
        if self._partners_mod is None:
            sys.modules.pop("partners", None)
        else:
            sys.modules["partners"] = self._partners_mod
        disclosure._clear_for_tests()
        os.environ.clear()
        os.environ.update(self._env)


# ==========================================================================
# 1) 저장 입력검증 잔여
# ==========================================================================
class TestSetTextInputValidation(Base):
    def test_non_string_text_rejected(self):
        with self.assertRaises(ValueError):
            disclosure.set_text("shop", 12345)

    def test_none_text_rejected(self):
        with self.assertRaises(ValueError):
            disclosure.set_text("shop", None)


# ==========================================================================
# 2) 스코프 격리 — partners 모듈 부재·장애
# ==========================================================================
class _BrokenPartners(object):
    def scope_tenants(self, *a, **k):
        raise RuntimeError("장부 저장소 장애")


class TestScopeIsolation(Base):
    def test_missing_partners_module_returns_none(self):
        sys.modules["partners"] = None          # import partners → ImportError
        self.assertIsNone(disclosure._scope_tenants(["shop"], "partner_admin", "ch-alpha", None))

    def test_broken_partners_module_returns_none(self):
        sys.modules["partners"] = _BrokenPartners()
        self.assertIsNone(disclosure._scope_tenants(["shop"], "partner_admin", "ch-alpha", None))

    def test_healthy_partners_module_returns_a_verdict(self):
        s = disclosure._scope_tenants(["shop"], "partner_admin", "ch-alpha", None)
        self.assertIsNotNone(s)
        self.assertIn("note", s)


# ==========================================================================
# 3~4) 감사·주체 식별 격리
# ==========================================================================
class _BrokenAudit(object):
    def record_request(self, *a, **k):
        raise RuntimeError("감사 저장소 장애 /var/secret/audit.log")

    def actor(self, headers):
        raise RuntimeError("actor 산출 장애")


class TestAuditIsolation(Base):
    def test_no_audit_module_still_serves(self):
        disclosure._audit = None
        h = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(h.status, 200)

    def test_audit_failure_does_not_break_the_request(self):
        disclosure._audit = _BrokenAudit()
        h = call("GET", headers=SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body()["ok"])

    def test_audit_failure_message_does_not_leak_into_response(self):
        disclosure._audit = _BrokenAudit()
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "text": GOOD})
        self.assertEqual(h.status, 200)
        self.assertNotIn("audit.log", json.dumps(h.body(), ensure_ascii=False))

    def test_actor_failure_falls_back_to_anonymous_and_still_saves(self):
        disclosure._audit = _BrokenAudit()
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "shop", "text": GOOD})
        self.assertEqual(h.status, 200)          # 익명으로라도 진행된다
        hist = call("GET", "/api/disclosure?op=history", SAME_ORIGIN).body()["history"]
        self.assertEqual(hist[-1]["actor"], "")


# ==========================================================================
# 5) HTTP 잔여 경로 — 저장 상한
# ==========================================================================
class TestTenantCapViaHttp(Base):
    def test_cap_overflow_is_400_not_swallowed(self):
        for i in range(disclosure.MAX_TENANTS):
            disclosure.set_text("t%d" % i, GOOD)
        h = call("POST", headers=SAME_ORIGIN, payload={"tenant_id": "overflow", "text": GOOD})
        self.assertEqual(h.status, 400)
        b = h.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["details"][0]["field"], "tenant_id")
        ids = {r["tenant_id"] for r in disclosure.list_tenants()}
        self.assertNotIn("overflow", ids)
        self.assertEqual(len(ids), disclosure.MAX_TENANTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
