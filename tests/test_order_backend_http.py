# -*- coding: utf-8 -*-
"""api/_order_backend.py HttpOrderBackend 아웃바운드 안전성 + api/health.py deep 탐침 회귀.

20차가 남긴 제안 「`_urlguard` 를 쓰지 않는 나머지 아웃바운드 경로 점검(`health` deep
탐침·`order_backend` HTTP)」의 회귀다. 녹음 다운로드·안부 콜백은 요청 본문의 URL 이라
가드를 붙였지만, **설정에서 오는 주소**(ORDER_API_BASE)는 검증 없이 나가고 있었고
쿼리에는 모델이 만든 값이 그대로 이어 붙고 있었다.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

검증 대상
  1) 실패 상세 — 예외 '문구'를 돌려주지 않는다(타입명·분류 토큰만). 그 값은 LLM
     컨텍스트와 `/api/chat` 응답(`log[].out`)으로 화면까지 그대로 나간다.
  2) 쿼리 인코딩 — `phone` 은 모델이 만든 인자다. `&`·`#`·공백·비문자열이 와도
     파라미터가 덧붙거나 뒤가 잘리지 않는다(퍼센트 인코딩 + 길이 상한).
  3) 주소 가드 — 평문 http·사설/루프백/메타데이터·숫자표기 우회는 **요청을 보내지
     않고** 거부, 화이트리스트(ORDER_API_HOSTS)·개발 플래그(ORDER_API_ALLOW_INSECURE).
  4) 리다이렉트 이탈 — 검증을 통과한 주소가 302 로 내부를 가리키면 본문을 버린다.
  5) 응답 계약 — 상한(1MiB) 초과·dict 아닌 JSON 은 호출부로 흘리지 않는다.
  6) 캐시 — ORDER_API_KEY 를 회전하면 새 인스턴스가 나온다(낡은 키를 계속 보내지 않는다).
  7) health — 거부되는 base 는 MISCONFIGURED(OK 로 꾸미지 않음), deep 탐침은 포트
     표기를 존중하고(`:8443`·http 80) 거부된 주소는 아예 찔러 보지 않는다.

실행: python -m pytest tests/test_order_backend_http.py -q
"""
import os
import sys
import json
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _urlguard                            # noqa: E402
import _order_backend as ob                 # noqa: E402
import health                               # noqa: E402

ENV = ("ORDER_BACKEND", "ORDER_API_BASE", "ORDER_API_KEY", "ORDER_API_ALLOW_WRITE",
       "ORDER_API_HOSTS", "ORDER_API_ALLOW_INSECURE", "HEALTH_DEEP",
       "GOOGLE_API_KEY", "GEMINI_API_KEY")


class NetworkTouched(AssertionError):
    pass


class Resp(object):
    """실제 응답 객체 흉내 — `read(amt)`·`geturl()` 을 갖는다."""

    def __init__(self, body=b"{}", url="https://api.example.test/cs/v1/x"):
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


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in ENV:
            os.environ.pop(k, None)
        self.calls = []
        self.resp = Resp()
        self._urlopen = urllib.request.urlopen

        def fake(req, timeout=None):
            self.calls.append((req, timeout))
            if isinstance(self.resp, Exception):
                raise self.resp
            return self.resp

        urllib.request.urlopen = fake
        ob._CACHE.clear()

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        ob._CACHE.clear()
        os.environ.clear()
        os.environ.update(self._env)

    def backend(self, base="https://api.example.test/cs/v1", **kw):
        return ob.HttpOrderBackend(base, **kw)

    def urls(self):
        return [c[0].full_url for c in self.calls]


# --------------------------------------------------------------------------
# 1) 실패 상세에 예외 문구를 싣지 않는다
# --------------------------------------------------------------------------
class TestFailureDetail(Base):
    def test_detail_is_exception_type_name_only(self):
        self.resp = urllib.error.URLError("connection refused to https://orders.example.com/v1")
        r = self.backend().get_refund_policy({"order_id": "x"})
        self.assertEqual(r["error"], "backend_unavailable")
        self.assertEqual(r["detail"], "URLError")

    def test_http_error_text_with_url_and_phone_is_not_echoed(self):
        """`HTTPError` 는 문구에 **요청 URL** 을 들고 온다 — 그 안에 발신번호가 있다.

        예전 구현은 `str(e)[:200]` 이라 번호와 고객사 경로가 툴 결과로 나가고,
        그 툴 결과는 `/api/chat` 응답의 `log[].out` 으로 화면까지 갔다.
        """
        url = "https://orders.example.com/v1/orders/recent?phone=01012345678"
        self.resp = urllib.error.HTTPError(url, 500, "Internal Server Error %s" % url, {}, None)
        r = self.backend().lookup_recent_order({"phone": "01012345678"})
        blob = json.dumps(r, ensure_ascii=False)
        self.assertEqual(r["detail"], "HTTPError")
        for leak in ("01012345678", "orders.example.com", "/v1/orders"):
            self.assertNotIn(leak, blob)

    def test_broken_json_body_reports_type_not_text(self):
        self.resp = Resp(b"<html>backend login page</html>")
        r = self.backend().quote_refund({})
        self.assertEqual(r["error"], "backend_unavailable")
        self.assertNotIn("html", json.dumps(r))
        self.assertIn(r["detail"], ("JSONDecodeError", "ValueError"))


# --------------------------------------------------------------------------
# 2) 쿼리 인코딩 — 값은 모델이 만든다
# --------------------------------------------------------------------------
class TestQueryEncoding(Base):
    def test_ampersand_cannot_add_parameters(self):
        self.backend().lookup_recent_order({"phone": "01000001111&admin=1"})
        url = self.urls()[0]
        self.assertNotIn("&admin=1", url)
        self.assertIn("phone=01000001111%26admin%3D1", url)

    def test_hash_cannot_truncate_the_query(self):
        self.backend().lookup_recent_order({"phone": "#"})
        self.assertIn("phone=%23", self.urls()[0])

    def test_non_string_and_missing_values_are_safe(self):
        b = self.backend()
        b.lookup_recent_order({"phone": None})
        b.lookup_recent_order({})
        b.lookup_recent_order({"phone": {"a": 1}})
        for url in self.urls():
            self.assertTrue(url.startswith("https://api.example.test/cs/v1/orders/recent?phone="))
            self.assertNotIn(" ", url)

    def test_value_length_is_capped(self):
        self.backend().lookup_recent_order({"phone": "9" * 500})
        self.assertIn("phone=" + "9" * ob.MAX_QUERY_VALUE, self.urls()[0])
        self.assertNotIn("9" * (ob.MAX_QUERY_VALUE + 1), self.urls()[0])

    def test_normal_number_is_unchanged(self):
        """과차단 금지 — 평범한 번호는 그대로 전달된다(조회가 깨지면 통화가 깨진다)."""
        self.backend().lookup_recent_order({"phone": " 01012345678 "})
        self.assertTrue(self.urls()[0].endswith("/orders/recent?phone=01012345678"))


# --------------------------------------------------------------------------
# 3) 주소 가드 — 거부는 요청 자체를 보내지 않는다
# --------------------------------------------------------------------------
class TestUrlGuard(Base):
    REJECTED = [
        "http://api.example.test",              # 평문 — 주문 정보와 Bearer 키가 그대로 흐른다
        "https://127.0.0.1/cs",                 # 루프백
        "https://10.0.0.5/cs",                  # 사설
        "https://169.254.169.254/latest",       # 클라우드 메타데이터
        "https://2130706433/cs",                # 10진 표기 우회
        "https://localhost:8080/cs",            # 이름으로 내부 지시
        "file:///etc/passwd",                   # http(s) 아님
        "https:///cs",                          # 호스트 없음
    ]

    def test_rejected_bases_never_send_a_request(self):
        for base in self.REJECTED:
            with self.subTest(base=base):
                r = ob.HttpOrderBackend(base).get_refund_policy({"order_id": "x"})
                self.assertEqual(r["error"], "backend_unavailable")
                self.assertEqual(r["detail"], "blocked_url")
        self.assertEqual(self.calls, [], "거부된 주소로 요청이 나갔다")

    def test_rejection_detail_carries_no_config_hint(self):
        r = ob.HttpOrderBackend("http://api.example.test").get_refund_policy({})
        self.assertNotIn("http", r["detail"])
        self.assertNotIn("ORDER_API", json.dumps(r, ensure_ascii=False))

    def test_write_path_is_blocked_too(self):
        """쓰기 승인이 켜진 뒤가 더 위험하다 — 환불 접수가 엉뚱한 주소로 간다."""
        b = ob.HttpOrderBackend("https://169.254.169.254/x", allow_write=True)
        r = b.confirm_refund({"order_id": "o", "refund_amount": 1000})
        self.assertEqual(r["detail"], "blocked_url")
        self.assertEqual(self.calls, [])

    def test_allow_insecure_flag_is_development_escape(self):
        os.environ["ORDER_API_ALLOW_INSECURE"] = "1"
        ob.HttpOrderBackend("http://localhost:8080/cs").get_refund_policy({})
        self.assertEqual(len(self.calls), 1)

    def test_flag_requires_exact_1(self):
        os.environ["ORDER_API_ALLOW_INSECURE"] = "true"
        r = ob.HttpOrderBackend("http://localhost:8080/cs").get_refund_policy({})
        self.assertEqual(r["detail"], "blocked_url")

    def test_host_allowlist_closes_everything_else(self):
        os.environ["ORDER_API_HOSTS"] = "orders.example.com"
        self.assertEqual(ob.HttpOrderBackend("https://api.example.test").get_refund_policy({})["detail"],
                         "blocked_url")
        ob.HttpOrderBackend("https://orders.example.com/v1").get_refund_policy({})
        self.assertEqual(len(self.calls), 1)

    def test_gate_is_read_at_call_time(self):
        """배포 후 환경변수를 바꾸면 반영된다(import 시점 상수로 얼리지 않는다)."""
        b = ob.HttpOrderBackend("http://api.example.test")
        self.assertEqual(b.get_refund_policy({})["detail"], "blocked_url")
        os.environ["ORDER_API_ALLOW_INSECURE"] = "1"
        b.get_refund_policy({})
        self.assertEqual(len(self.calls), 1)

    def test_rule_lives_in_urlguard_only(self):
        """판정 규칙을 복제하지 않는다 — `_order_backend` 에 자체 차단 목록이 없다."""
        src = open(os.path.join(ROOT, "api", "_order_backend.py"), encoding="utf-8").read()
        self.assertIn("_urlguard.check", src)
        # 주석 속 설명(「169.254.169.254 면 내부를 두드린다」)은 규칙 복제가 아니다 —
        # 코드 줄만 본다(scripts/verify.py 의 아웃바운드 검사와 같은 방식).
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        for copied in ("is_private", "169.254", "ip_address(", "BLOCKED_HOSTS ="):
            self.assertNotIn(copied, code)

    def test_check_api_base_is_the_shared_entry_point(self):
        self.assertTrue(ob.check_api_base("https://orders.example.com/v1")[0])
        ok, reason = ob.check_api_base("https://127.0.0.1/v1")
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertNotIn("127.0.0.1", reason)       # 사유에 주소를 되비추지 않는다


# --------------------------------------------------------------------------
# 4) 리다이렉트 이탈
# --------------------------------------------------------------------------
class TestRedirectEscape(Base):
    def test_redirect_to_internal_discards_body(self):
        self.resp = Resp(b'{"found": true, "order_id": "LEAK"}',
                         url="http://169.254.169.254/latest/meta-data")
        r = self.backend().lookup_recent_order({"phone": "01000001111"})
        self.assertEqual(r["detail"], "redirect_escape")
        self.assertNotIn("LEAK", json.dumps(r))

    def test_redirect_within_allowed_host_is_fine(self):
        self.resp = Resp(b'{"ok": true}', url="https://api.example.test/cs/v1/orders/recent?x=1")
        self.assertEqual(self.backend().lookup_recent_order({"phone": "1"}), {"ok": True})

    def test_unreadable_final_url_does_not_break_the_call(self):
        class NoUrl(Resp):
            def geturl(self):
                raise RuntimeError("no url")

        self.resp = NoUrl(b'{"ok": true}')
        self.assertEqual(self.backend().get_refund_policy({}), {"ok": True})


# --------------------------------------------------------------------------
# 5) 응답 계약
# --------------------------------------------------------------------------
class TestResponseContract(Base):
    def test_oversized_body_is_refused(self):
        self.resp = Resp(b"{" + b" " * (ob.MAX_RESPONSE + 10) + b"}")
        r = self.backend().get_refund_policy({})
        self.assertEqual(r["detail"], "response_too_large")

    def test_body_at_limit_is_accepted(self):
        pad = ob.MAX_RESPONSE - len(b'{"ok": true, "pad": ""}')
        self.resp = Resp(b'{"ok": true, "pad": "' + b"x" * pad + b'"}')
        self.assertTrue(self.backend().get_refund_policy({})["ok"])

    def test_non_object_json_is_not_passed_to_caller(self):
        for body in (b"[1, 2, 3]", b'"just a string"', b"7", b"null"):
            with self.subTest(body=body):
                self.resp = Resp(body)
                r = self.backend().get_refund_policy({})
                self.assertEqual(r["detail"], "non_object_response")

    def test_empty_body_is_empty_dict(self):
        self.resp = Resp(b"")
        self.assertEqual(self.backend().get_refund_policy({}), {})

    def test_every_result_is_a_dict(self):
        """호출부(`_engine`)는 `out.get(...)` 을 한다 — 어떤 응답에도 dict 를 돌려준다."""
        bodies = [b"", b"{}", b"[]", b"nope", b'{"a": 1}']
        for body in bodies + [urllib.error.URLError("x"), RuntimeError("y")]:
            with self.subTest(body=body):
                self.resp = body if isinstance(body, Exception) else Resp(body)
                for tool in ("lookup_recent_order", "get_refund_policy", "quote_refund",
                             "escalate_to_agent"):
                    self.assertIsInstance(self.backend().dispatch(tool, {"phone": "1"}), dict)


# --------------------------------------------------------------------------
# 6) 팩토리 캐시 — 키 회전
# --------------------------------------------------------------------------
class TestFactoryCache(Base):
    def test_key_rotation_replaces_the_cached_backend(self):
        os.environ.update(ORDER_BACKEND="http", ORDER_API_BASE="https://orders.example.com",
                          ORDER_API_KEY="old-key")
        first = ob.get_backend()
        self.assertEqual(first.key, "old-key")
        os.environ["ORDER_API_KEY"] = "new-key"
        second = ob.get_backend()
        self.assertEqual(second.key, "new-key", "낡은 키를 계속 보낸다")
        self.assertIsNot(first, second)

    def test_same_settings_still_cached(self):
        os.environ.update(ORDER_BACKEND="http", ORDER_API_BASE="https://orders.example.com",
                          ORDER_API_KEY="k")
        self.assertIs(ob.get_backend(), ob.get_backend())

    def test_cache_identity_has_no_plaintext_key(self):
        os.environ.update(ORDER_BACKEND="http", ORDER_API_BASE="https://orders.example.com",
                          ORDER_API_KEY="sk-secret-value")
        ob.get_backend()
        self.assertNotIn("sk-secret-value", repr(ob._CACHE.get("key")))


# --------------------------------------------------------------------------
# 7) health — 같은 판정을 쓰고, 탐침은 포트를 존중한다
# --------------------------------------------------------------------------
class TestHealthProbe(Base):
    def setUp(self):
        super().setUp()
        self.seen = []
        self._conn = health.socket.create_connection

        class _S(object):
            def close(self_inner):
                return None

        def fake(addr, timeout=None):
            self.seen.append(addr)
            return _S()

        health.socket.create_connection = fake
        os.environ.update(GOOGLE_API_KEY="k", ORDER_BACKEND="http")

    def tearDown(self):
        health.socket.create_connection = self._conn
        super().tearDown()

    def test_rejected_base_is_misconfigured_and_not_probed(self):
        os.environ.update(HEALTH_DEEP="1", ORDER_API_BASE="http://10.0.0.5/cs")
        d = health._dep_order(True)
        self.assertEqual(d["status"], health.MISCONFIGURED)
        self.assertFalse(d["checked"])
        self.assertEqual(self.seen, [], "거부된 주소를 헬스가 찔러 봤다")

    def test_misconfigured_reason_has_no_credentials(self):
        os.environ["ORDER_API_BASE"] = "http://user:pw-SECRET@10.0.0.5/cs"
        d = health._dep_order(False)
        self.assertEqual(d["status"], health.MISCONFIGURED)
        self.assertNotIn("SECRET", json.dumps(d, ensure_ascii=False))

    def test_explicit_port_is_probed(self):
        os.environ.update(HEALTH_DEEP="1", ORDER_API_BASE="https://orders.example.com:8443/v1")
        d = health._dep_order(True)
        self.assertEqual(self.seen, [("orders.example.com", 8443)])
        self.assertEqual(d["status"], health.OK)
        self.assertIn("8443", d["detail"])

    def test_insecure_http_uses_port_80_when_allowed(self):
        os.environ.update(HEALTH_DEEP="1", ORDER_API_ALLOW_INSECURE="1",
                          ORDER_API_BASE="http://orders.example.com/v1")
        health._dep_order(True)
        self.assertEqual(self.seen, [("orders.example.com", 80)])

    def test_default_port_is_443(self):
        os.environ.update(HEALTH_DEEP="1", ORDER_API_BASE="https://orders.example.com/v1")
        health._dep_order(True)
        self.assertEqual(self.seen, [("orders.example.com", 443)])

    def test_hostport_of_rejects_what_it_cannot_judge(self):
        """호스트·포트를 모르면 `(None, None)` — 모르는 것을 찔러 보지 않는다."""
        for url in ("https:///v1", "", None, 12345, "https://h.example:port/v1"):
            with self.subTest(url=url):
                self.assertEqual(health._hostport_of(url), (None, None))

    def test_broken_port_skips_probe_without_dressing_up_status(self):
        os.environ.update(HEALTH_DEEP="1", ORDER_API_BASE="https://orders.example.com:99999/v1")
        d = health._dep_order(True)
        self.assertEqual(self.seen, [])
        self.assertFalse(d["checked"])

    def test_health_and_backend_agree(self):
        """헬스가 「정상」이라고 말하는 동안 호출만 막히는 엇갈림을 만들지 않는다."""
        for base in ("https://orders.example.com/v1", "http://orders.example.com/v1",
                     "https://127.0.0.1/v1", "https:///v1"):
            with self.subTest(base=base):
                os.environ["ORDER_API_BASE"] = base
                healthy = health._dep_order(False)["status"] != health.MISCONFIGURED
                sent = ob.HttpOrderBackend(base).get_refund_policy({}).get("detail") != "blocked_url"
                self.assertEqual(healthy, sent)

    def test_shallow_mode_opens_no_socket(self):
        os.environ["ORDER_API_BASE"] = "https://orders.example.com/v1"
        health._dep_order(False)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":   # pragma: no cover
    unittest.main(verbosity=2)
