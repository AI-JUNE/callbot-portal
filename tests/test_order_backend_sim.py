# -*- coding: utf-8 -*-
"""api/order_backend.py · api/sim_call.py 회귀 테스트.

의존성 0 · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).

검증 대상 (COMMERCIAL_READINESS '테스트 커버리지' — order_backend 55%·sim_call 60% 보강)
  1) 인터페이스 계약 — 기반 클래스는 전부 NotImplementedError, dispatch 는 비공개·미지 툴 거부,
     engine.TOOLS 의 툴 이름이 전부 구현체에 있다(드리프트 감시)
  2) DemoOrderBackend — 조회·정책(한도=합계)·견적(이름 매칭·수량·미지 상품 0)·접수·전환 응답 형태
  3) HttpOrderBackend — URL·헤더(키 있을 때만 Bearer)·본문 직렬화·응답 파싱·빈 본문·실패 시
     backend_unavailable(키·URL 미노출), 쓰기 미승인(ORDER_API_ALLOW_WRITE 없음)은 dry-run 만
  4) get_backend 팩토리 — 기본 demo, http 인데 BASE 없으면 demo 폴백, 같은 설정은 캐시, 설정 바뀌면 교체
  5) sim 대본 — 목록 고정·비어있지 않음·chat 시나리오/콘솔 버튼과의 드리프트
  6) simulate — 발화 순서·이력 누적·전환 시 조기 종료·phone/scenario 전달·미지 시나리오 거부·엔진 부재
  7) HTTP 계약 — 403/200/400/500, 표준 봉투, 요청ID·no-store, CORS 되비침 금지, 요율 등급 llm
  8) 안전 — 실패 경로에서 LLM 미호출, 예외 문구·내부 경로 미노출

실행: python3 -m pytest tests/test_order_backend_sim.py -q
"""
import os
import re
import sys
import json
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _errors        # noqa: E402
import _ratelimit     # noqa: E402
import engine         # noqa: E402
import order_backend  # noqa: E402
import sim_call       # noqa: E402
import chat           # noqa: E402


class NetworkTouched(AssertionError):
    pass


def _no_net(*a, **k):
    raise NetworkTouched("network call attempted")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        for k in ("ORDER_BACKEND", "ORDER_API_BASE", "ORDER_API_KEY", "ORDER_API_ALLOW_WRITE",
                  "CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_DEBUG_ERRORS"):
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = _no_net
        order_backend._CACHE.clear()
        _ratelimit.reset()

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        order_backend._CACHE.clear()
        os.environ.clear()
        os.environ.update(self._env)


# --------------------------------------------------------------------------
# 1) 인터페이스 계약
# --------------------------------------------------------------------------
TOOL_NAMES = [t["name"] for t in engine.TOOLS]


class TestInterface(Base):
    def test_base_methods_not_implemented(self):
        b = order_backend.OrderBackend()
        for name in TOOL_NAMES:
            with self.assertRaises(NotImplementedError, msg=name):
                getattr(b, name)({})

    def test_dispatch_unknown_tool(self):
        b = order_backend.DemoOrderBackend()
        self.assertEqual(b.dispatch("explode", {}), {"error": "unknown explode"})

    def test_dispatch_refuses_private_and_self(self):
        b = order_backend.DemoOrderBackend()
        self.assertIn("error", b.dispatch("__init__", {}))
        self.assertIn("error", b.dispatch("dispatch", {}))
        self.assertIn("error", b.dispatch("_req", {}))

    def test_dispatch_none_input(self):
        b = order_backend.DemoOrderBackend()
        self.assertTrue(b.dispatch("lookup_recent_order", None)["found"])

    def test_every_engine_tool_implemented(self):
        # engine.TOOLS 에 툴을 추가하고 구현체를 빠뜨리면 LLM 호출 시점에야 터진다
        for cls in (order_backend.DemoOrderBackend, order_backend.HttpOrderBackend):
            for name in TOOL_NAMES:
                self.assertTrue(callable(getattr(cls, name, None)), "%s.%s" % (cls.__name__, name))

    def test_engine_dispatch_uses_backend(self):
        self.assertEqual(engine._dispatch("lookup_recent_order", {})["order_id"],
                         order_backend.DEMO_ORDER["order_id"])
        self.assertIs(engine._ORDER, order_backend.DEMO_ORDER)


# --------------------------------------------------------------------------
# 2) DemoOrderBackend
# --------------------------------------------------------------------------
class TestDemo(Base):
    def setUp(self):
        super().setUp()
        self.b = order_backend.DemoOrderBackend()

    def test_name(self):
        self.assertEqual(self.b.name, "demo")

    def test_lookup(self):
        r = self.b.lookup_recent_order({"phone": "01012345678"})
        self.assertTrue(r["found"])
        self.assertEqual(r["order_id"], order_backend.DEMO_ORDER["order_id"])
        self.assertEqual(len(r["items"]), 2)

    def test_policy_max_equals_item_total(self):
        r = self.b.get_refund_policy({"order_id": "x", "issue_type": "damaged"})
        total = sum(i["price"] * i["qty"] for i in order_backend.DEMO_ORDER["items"])
        self.assertTrue(r["eligible"])
        self.assertEqual(r["max_refund"], total)
        self.assertEqual(r["options"], ["refund", "redelivery"])

    def test_quote_matches_by_name(self):
        r = self.b.quote_refund({"missing_items": [{"name": "[생방송] 한우 1++ 선물세트 1.6kg", "qty": 1}]})
        self.assertEqual(r, {"refund_amount": 159000, "currency": "KRW"})

    def test_quote_qty_multiplies_and_default_qty(self):
        r = self.b.quote_refund({"missing_items": [{"name": "[생방송] 한우 1++ 선물세트 1.6kg", "qty": 2}]})
        self.assertEqual(r["refund_amount"], 318000)
        r = self.b.quote_refund({"missing_items": [{"name": "[생방송] 한우 1++ 선물세트 1.6kg"}]})
        self.assertEqual(r["refund_amount"], 159000)

    def test_quote_unknown_item_is_zero(self):
        # LLM 이 지어낸 상품명은 0원 — 존재하지 않는 상품에 금액을 매기지 않는다
        r = self.b.quote_refund({"missing_items": [{"name": "다이아몬드", "qty": 3}]})
        self.assertEqual(r["refund_amount"], 0)

    def test_quote_no_items(self):
        self.assertEqual(self.b.quote_refund({})["refund_amount"], 0)
        self.assertEqual(self.b.quote_refund({"missing_items": None})["refund_amount"], 0)

    def test_confirm_redelivery_escalate_shapes(self):
        c = self.b.confirm_refund({"order_id": "x", "refund_amount": 1, "user_confirmed": True})
        self.assertEqual(c["status"], "accepted")
        self.assertTrue(c["refund_id"].startswith("RF-"))
        d = self.b.request_redelivery({"order_id": "x", "items": []})
        self.assertTrue(d["redelivery_id"].startswith("RD-"))
        e = self.b.escalate_to_agent({"reason": "r", "summary": "s"})
        self.assertTrue(e["transferred"])

    def test_custom_order_injection(self):
        b = order_backend.DemoOrderBackend({"order_id": "T-1", "items": [{"name": "a", "qty": 2, "price": 100}]})
        self.assertEqual(b.lookup_recent_order({})["order_id"], "T-1")
        self.assertEqual(b.get_refund_policy({})["max_refund"], 200)

    def test_demo_order_not_mutated_by_lookup(self):
        # 얕은 복사였을 때 호출자의 변조가 모듈 전역 DEMO_ORDER 로 전파됐다(회귀로 발견)
        r = self.b.lookup_recent_order({})
        r["items"].append({"name": "x", "qty": 1, "price": 1})
        r["items"][0]["price"] = 1
        self.assertEqual(len(order_backend.DEMO_ORDER["items"]), 2)
        self.assertEqual(order_backend.DEMO_ORDER["items"][0]["price"], 159000)
        self.assertEqual(self.b.get_refund_policy({})["max_refund"], 159000)


# --------------------------------------------------------------------------
# 3) HttpOrderBackend — urlopen 대역
# --------------------------------------------------------------------------
class FakeResp(object):
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestHttp(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.reply = b'{"found": true, "order_id": "H-1"}'

        def fake_urlopen(req, timeout=None):
            self.calls.append((req, timeout))
            if isinstance(self.reply, Exception):
                raise self.reply
            return FakeResp(self.reply)

        urllib.request.urlopen = fake_urlopen
        self.b = order_backend.HttpOrderBackend("https://api.example.test/cs/v1/", key="sk-secret", timeout=7)

    def test_base_trailing_slash_stripped(self):
        self.assertEqual(self.b.base, "https://api.example.test/cs/v1")

    def test_lookup_get(self):
        r = self.b.lookup_recent_order({"phone": "01000001111"})
        self.assertEqual(r["order_id"], "H-1")
        req, timeout = self.calls[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.startswith("https://api.example.test/cs/v1/orders/recent?phone="))
        self.assertIsNone(req.data)
        self.assertEqual(timeout, 7)

    def test_bearer_only_when_key(self):
        self.b.get_refund_policy({"order_id": "x"})
        self.assertEqual(self.calls[0][0].get_header("Authorization"), "Bearer sk-secret")
        nokey = order_backend.HttpOrderBackend("https://api.example.test")
        nokey.get_refund_policy({"order_id": "x"})
        self.assertIsNone(self.calls[1][0].get_header("Authorization"))

    def test_post_serialises_body_utf8(self):
        self.b.quote_refund({"order_id": "x", "missing_items": [{"name": "한우", "qty": 1}]})
        req = self.calls[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.example.test/cs/v1/refunds/quote")
        self.assertEqual(json.loads(req.data.decode("utf-8"))["missing_items"][0]["name"], "한우")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_empty_response_body_is_empty_dict(self):
        self.reply = b""
        self.assertEqual(self.b.get_refund_policy({}), {})

    def test_failure_surfaces_backend_unavailable(self):
        self.reply = urllib.error.URLError("connection refused")
        r = self.b.lookup_recent_order({"phone": "01000001111"})
        self.assertEqual(r["error"], "backend_unavailable")
        self.assertIn("detail", r)
        self.assertLessEqual(len(r["detail"]), 200)

    def test_failure_detail_has_no_secret(self):
        self.reply = RuntimeError("boom sk-secret should not appear")
        r = self.b.escalate_to_agent({"reason": "r"})
        # 상대 서버가 키를 되비추는 극단 케이스까지 막지는 못하지만 우리 쪽 키는 예외 문구에 넣지 않는다
        self.assertEqual(r["error"], "backend_unavailable")
        self.assertNotIn("Bearer", r["detail"])

    def test_confirm_refund_dry_run_without_write_approval(self):
        r = self.b.confirm_refund({"order_id": "x", "refund_amount": 1000, "user_confirmed": True})
        self.assertEqual(r["status"], "dry_run")
        self.assertFalse(r["accepted"])
        self.assertEqual(r["refund_amount"], 1000)
        self.assertEqual(self.calls, [])          # 실제 접수 미호출

    def test_redelivery_dry_run_without_write_approval(self):
        r = self.b.request_redelivery({"order_id": "x", "items": []})
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual(self.calls, [])

    def test_write_paths_post_when_approved(self):
        b = order_backend.HttpOrderBackend("https://api.example.test", allow_write=True)
        b.confirm_refund({"order_id": "x", "refund_amount": 1})
        b.request_redelivery({"order_id": "x", "items": []})
        self.assertEqual([c[0].full_url.rsplit("/", 1)[-1] for c in self.calls], ["confirm", "redeliveries"])
        self.assertTrue(all(c[0].get_method() == "POST" for c in self.calls))

    def test_escalate_posts(self):
        self.b.escalate_to_agent({"reason": "r", "summary": "s"})
        self.assertEqual(self.calls[0][0].full_url, "https://api.example.test/cs/v1/escalations")


# --------------------------------------------------------------------------
# 4) get_backend 팩토리
# --------------------------------------------------------------------------
class TestFactory(Base):
    def test_default_is_demo(self):
        self.assertIsInstance(order_backend.get_backend(), order_backend.DemoOrderBackend)

    def test_http_without_base_falls_back_to_demo(self):
        os.environ["ORDER_BACKEND"] = "http"
        self.assertIsInstance(order_backend.get_backend(), order_backend.DemoOrderBackend)

    def test_http_with_base(self):
        os.environ["ORDER_BACKEND"] = " HTTP "
        os.environ["ORDER_API_BASE"] = " https://api.example.test/ "
        os.environ["ORDER_API_KEY"] = " k1 "
        b = order_backend.get_backend()
        self.assertIsInstance(b, order_backend.HttpOrderBackend)
        self.assertEqual(b.base, "https://api.example.test")
        self.assertEqual(b.key, "k1")
        self.assertFalse(b.allow_write)

    def test_allow_write_exact_one_only(self):
        os.environ["ORDER_BACKEND"] = "http"
        os.environ["ORDER_API_BASE"] = "https://api.example.test"
        os.environ["ORDER_API_ALLOW_WRITE"] = "true"
        self.assertFalse(order_backend.get_backend().allow_write)
        os.environ["ORDER_API_ALLOW_WRITE"] = "1"
        self.assertTrue(order_backend.get_backend().allow_write)

    def test_cache_same_config_same_object(self):
        a = order_backend.get_backend()
        self.assertIs(a, order_backend.get_backend())

    def test_cache_invalidated_on_env_change(self):
        a = order_backend.get_backend()
        os.environ["ORDER_BACKEND"] = "http"
        os.environ["ORDER_API_BASE"] = "https://api.example.test"
        b = order_backend.get_backend()
        self.assertIsNot(a, b)
        os.environ.pop("ORDER_BACKEND")
        self.assertIsInstance(order_backend.get_backend(), order_backend.DemoOrderBackend)

    def test_unknown_kind_is_demo(self):
        os.environ["ORDER_BACKEND"] = "grpc"
        self.assertIsInstance(order_backend.get_backend(), order_backend.DemoOrderBackend)


# --------------------------------------------------------------------------
# 5) sim 대본
# --------------------------------------------------------------------------
class TestScripts(Base):
    def test_scenarios_sorted_and_match_scripts(self):
        self.assertEqual(sim_call.SCENARIOS, tuple(sorted(sim_call.SCRIPTS)))
        self.assertIn(sim_call.DEFAULT_SCENARIO, sim_call.SCRIPTS)

    def test_scripts_non_empty_strings(self):
        for k, v in sim_call.SCRIPTS.items():
            self.assertIsInstance(v, list, k)
            self.assertTrue(v, k)
            self.assertTrue(all(isinstance(u, str) and u.strip() for u in v), k)

    def test_chat_scenarios_have_scripts(self):
        # chat 이 받는 시나리오(한글 별칭 제외)는 전부 sim 대본이 있어야 콘솔에서 검증할 수 있다
        for s in chat.SCENARIOS:
            if re.search(r"[가-힣]", s):
                continue
            self.assertIn(s, sim_call.SCRIPTS, s)

    def test_console_buttons_match_scripts(self):
        p = os.path.join(ROOT, "public", "admin.html")
        with open(p, encoding="utf-8") as f:
            html = f.read()
        keys = set(re.findall(r"cpaasSim\('([a-z_]+)'\)", html))
        self.assertTrue(keys, "콘솔에 sim 버튼이 없다")
        self.assertEqual(keys - set(sim_call.SCRIPTS), set())

    def test_scripts_have_no_real_phone_or_rrn(self):
        blob = json.dumps(sim_call.SCRIPTS, ensure_ascii=False)
        self.assertIsNone(re.search(r"01[016789]-?\d{3,4}-?\d{4}", blob))
        self.assertIsNone(re.search(r"\d{6}-?[1-4]\d{6}", blob))


# --------------------------------------------------------------------------
# 6) simulate — engine.run_turn 대역
# --------------------------------------------------------------------------
class SimBase(Base):
    def setUp(self):
        super().setUp()
        self._run_turn = engine.run_turn
        self.seen = []
        self.transfer_at = None       # n번째 호출에서 transferred=True
        self.raise_exc = None

        def fake_run_turn(msgs, phone="x", scenario="refund", **kw):
            self.seen.append({"n": len(msgs), "phone": phone, "scenario": scenario,
                              "last": msgs[-1]["content"]})
            if self.raise_exc:
                raise self.raise_exc
            out = list(msgs) + [{"role": "assistant", "content": "답변 %d" % len(self.seen)}]
            tr = self.transfer_at is not None and len(self.seen) >= self.transfer_at
            return {"reply": "답변 %d" % len(self.seen), "messages": out, "transferred": tr}

        engine.run_turn = fake_run_turn

    def tearDown(self):
        engine.run_turn = self._run_turn
        super().tearDown()


class TestSimulate(SimBase):
    def test_all_utterances_in_order(self):
        out = sim_call.simulate("order")
        self.assertTrue(out["ok"])
        self.assertEqual(out["billing"], sim_call.BILLING)
        self.assertEqual([t["user"] for t in out["turns"]], sim_call.SCRIPTS["order"])
        self.assertEqual([s["last"] for s in self.seen], sim_call.SCRIPTS["order"])
        self.assertFalse(out["transferred"])

    def test_history_accumulates(self):
        sim_call.simulate("order")
        # user+assistant 가 쌓이므로 n번째 호출의 메시지 수는 2n-1
        self.assertEqual([s["n"] for s in self.seen], [1, 3, 5])

    def test_phone_and_scenario_forwarded(self):
        sim_call.simulate("overdue", phone="01099998888")
        self.assertTrue(all(s["phone"] == "01099998888" and s["scenario"] == "overdue" for s in self.seen))

    def test_transfer_stops_early(self):
        self.transfer_at = 2
        out = sim_call.simulate("integrity")
        self.assertTrue(out["transferred"])
        self.assertEqual(len(out["turns"]), 2)
        self.assertTrue(out["turns"][-1]["transferred"])
        self.assertEqual(len(self.seen), 2)

    def test_unknown_scenario_rejected_before_llm(self):
        with self.assertRaises(ValueError):
            sim_call.simulate("no-such")
        self.assertEqual(self.seen, [])

    def test_engine_missing_is_sim_error(self):
        saved = sys.modules.get("engine")
        sys.modules["engine"] = None       # import 실패 유도
        try:
            with self.assertRaises(sim_call.SimError):
                sim_call.simulate("refund")
        finally:
            sys.modules["engine"] = saved

    def test_run_turn_error_propagates(self):
        self.raise_exc = RuntimeError("upstream down")
        with self.assertRaises(RuntimeError):
            sim_call.simulate("refund")


# --------------------------------------------------------------------------
# 7) HTTP 계약 — 소켓 없는 핸들러 대역
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class Recorder(object):
    def __init__(self, headers=None):
        self.headers = FakeHeaders(headers or {})
        self.wfile = FakeWFile()
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


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.9"}


def call(path, headers=None):
    h = Recorder(headers)
    inst = sim_call.handler.__new__(sim_call.handler)
    inst.headers = h.headers
    inst.wfile = h.wfile
    inst.path = path
    inst.send_response = h.send_response
    inst.send_header = h.send_header
    inst.end_headers = h.end_headers
    inst.do_GET()
    return h


class TestHttpContract(SimBase):
    def test_cross_origin_403_no_llm(self):
        h = call("/api/sim_call?scenario=refund", {"origin": "https://evil.example"})
        self.assertEqual(h.status, 403)
        b = h.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["status"], 403)
        self.assertEqual(self.seen, [])
        self.assertNotEqual(h.header("Access-Control-Allow-Origin"), "https://evil.example")

    def test_same_origin_200(self):
        h = call("/api/sim_call?scenario=handoff", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        b = h.body()
        self.assertTrue(b["ok"])
        self.assertEqual(b["scenario"], "handoff")
        self.assertEqual(len(b["turns"]), len(sim_call.SCRIPTS["handoff"]))
        self.assertEqual(h.header("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(h.header("Cache-Control"), "no-store")
        self.assertTrue(h.header("X-Request-Id"))
        self.assertEqual(h.header("Content-Length"), str(len(h.wfile.data)))

    def test_missing_scenario_defaults(self):
        h = call("/api/sim_call", SAME_ORIGIN)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body()["scenario"], sim_call.DEFAULT_SCENARIO)

    def test_unknown_scenario_400_with_field(self):
        h = call("/api/sim_call?scenario=drop_table", SAME_ORIGIN)
        self.assertEqual(h.status, 400)
        b = h.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "INVALID_REQUEST")
        self.assertEqual(b["details"][0]["field"], "scenario")
        self.assertEqual(self.seen, [])          # 잘못된 요청에 LLM 토큰을 쓰지 않는다

    def test_request_id_inherited(self):
        h = call("/api/sim_call", dict(SAME_ORIGIN, **{"x-request-id": "abc-123"}))
        self.assertEqual(h.header("X-Request-Id"), "abc-123")

    def test_engine_error_500_envelope_no_message(self):
        self.raise_exc = RuntimeError("secret /var/task/api/engine.py exploded")
        h = call("/api/sim_call?scenario=refund", SAME_ORIGIN)
        self.assertEqual(h.status, 500)
        b = h.body()
        self.assertFalse(b["ok"])
        self.assertEqual(b["code"], "INTERNAL_ERROR")
        self.assertIsInstance(b["error"], str)   # 콘솔의 d.error 분기 유지
        self.assertNotIn("/var/task", h.wfile.data.decode("utf-8"))
        self.assertNotIn("exploded", h.wfile.data.decode("utf-8"))

    def test_upstream_url_error_502(self):
        self.raise_exc = urllib.error.URLError("gemini unreachable")
        h = call("/api/sim_call?scenario=refund", SAME_ORIGIN)
        self.assertEqual(h.status, 502)
        self.assertEqual(h.body()["code"], "UPSTREAM_ERROR")

    def test_engine_missing_500(self):
        saved = sys.modules.get("engine")
        sys.modules["engine"] = None
        try:
            h = call("/api/sim_call?scenario=refund", SAME_ORIGIN)
        finally:
            sys.modules["engine"] = saved
        self.assertEqual(h.status, 500)
        self.assertFalse(h.body()["ok"])

    def test_rate_class_is_llm(self):
        # sim 1회 = LLM 3~8회. default 등급(40/분)이면 chat(20/분)보다 훨씬 많은 토큰이 열린다
        self.assertEqual(_ratelimit.route_class("/api/sim_call?scenario=refund"), "llm")
        self.assertEqual(_ratelimit.route_class("/api/sim_call.py"), "llm")

    def test_rate_limited_429(self):
        os.environ["CALLBOT_RATE_LIMIT_LLM"] = "1"
        h1 = call("/api/sim_call", SAME_ORIGIN)
        h2 = call("/api/sim_call", SAME_ORIGIN)
        self.assertEqual(h1.status, 200)
        self.assertEqual(h2.status, 429)
        self.assertTrue(h2.header("Retry-After"))
        self.assertEqual(len(self.seen), len(sim_call.SCRIPTS["refund"]))   # 두 번째는 LLM 미호출

    def test_strict_mode_requires_key(self):
        os.environ["CALLBOT_STRICT"] = "1"
        os.environ["CALLBOT_API_KEY"] = "k"
        self.assertEqual(call("/api/sim_call", SAME_ORIGIN).status, 401)
        self.assertEqual(call("/api/sim_call", {"x-api-key": "k"}).status, 200)


if __name__ == "__main__":
    unittest.main()
