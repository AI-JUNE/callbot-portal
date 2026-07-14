# -*- coding: utf-8 -*-
# ==========================================================================
# 주문/환불 백엔드 추상화 (Order Backend Interface)
# --------------------------------------------------------------------------
# 목적: api/engine.py 에 하드코딩돼 있던 데모 주문 데이터(_ORDER)와 툴 실행부를
#       "인터페이스 + 구현체" 구조로 분리한다. 실제 고객사 연동 시 engine.py 를
#       고치지 않고 이 파일에 구현체 하나만 추가하면 된다.
#
# 기본 동작(ORDER_BACKEND 미설정): DemoOrderBackend = 기존과 100% 동일한 응답.
#   → 이번 변경은 순수 리팩터링이며 라이브 동작 변화 없음.
#
# [사람 승인 필요] HttpOrderBackend (실 고객사 API 연동)
#   - ORDER_BACKEND=http 및 ORDER_API_BASE 가 설정돼야만 활성화된다.
#   - 환불 실제 접수(confirm_refund)는 ORDER_API_ALLOW_WRITE=1 이 추가로 켜져
#     있을 때만 전송한다. 그 전에는 dry-run 응답만 반환하고 실제 호출하지 않는다.
#   - 즉 운영 반영은 사람이 환경변수를 켜는 행위로만 가능하다(자동 활성화 없음).
# ==========================================================================
from __future__ import annotations
import os, json, urllib.request

# --- 데모 주문(기존 engine._ORDER 와 동일) --------------------------------
DEMO_ORDER = {
    "order_id": "SSG-20260630-10042",
    "store_name": "온라인몰",
    "ordered_at": "어제 20:15",
    "items": [
        {"name": "[생방송] 한우 1++ 선물세트 1.6kg", "qty": 1, "price": 159000},
        {"name": "보냉백", "qty": 1, "price": 0},
    ],
    "status": "배송완료",
}


class OrderBackend:
    """주문/환불 연동 인터페이스. 모든 구현체는 아래 6개 메서드를 제공한다.

    반환 스키마는 engine.TOOLS 의 계약과 동일해야 한다(툴 결과가 그대로 LLM에 인용됨).
    """

    name = "base"

    def lookup_recent_order(self, inp: dict) -> dict:
        raise NotImplementedError

    def get_refund_policy(self, inp: dict) -> dict:
        raise NotImplementedError

    def quote_refund(self, inp: dict) -> dict:
        raise NotImplementedError

    def confirm_refund(self, inp: dict) -> dict:
        """환불 실제 접수(위험 경로). 구현체는 반드시 멱등/감사로그를 고려할 것."""
        raise NotImplementedError

    def request_redelivery(self, inp: dict) -> dict:
        raise NotImplementedError

    def escalate_to_agent(self, inp: dict) -> dict:
        raise NotImplementedError

    # 툴 이름 → 메서드 디스패치 (engine 은 이 메서드만 호출한다)
    def dispatch(self, tool: str, inp: dict) -> dict:
        fn = getattr(self, tool, None)
        if fn is None or tool.startswith("_") or tool == "dispatch":
            return {"error": f"unknown {tool}"}
        return fn(inp or {})


class DemoOrderBackend(OrderBackend):
    """데모/시뮬레이션용. 기존 engine._dispatch 와 응답이 동일하다."""

    name = "demo"

    def __init__(self, order: dict | None = None):
        self.order = order or DEMO_ORDER

    def lookup_recent_order(self, inp):
        return {"found": True, **self.order}

    def get_refund_policy(self, inp):
        mx = sum(i["price"] * i["qty"] for i in self.order["items"])
        return {
            "eligible": True,
            "options": ["refund", "redelivery"],
            "max_refund": mx,
            "reason": "불량/오배송/단순변심에 따라 환불 또는 교환 처리",
        }

    def quote_refund(self, inp):
        pm = {i["name"]: i["price"] for i in self.order["items"]}
        items = inp.get("missing_items", []) or []
        total = sum(pm.get(it.get("name"), 0) * int(it.get("qty", 1)) for it in items)
        return {"refund_amount": total, "currency": "KRW"}

    def confirm_refund(self, inp):
        return {"refund_id": "RF-DEMO1234", "status": "accepted", "eta_days": 3}

    def request_redelivery(self, inp):
        return {"redelivery_id": "RD-DEMO1234", "eta_minutes": 25}

    def escalate_to_agent(self, inp):
        return {"transferred": True, "queue_position": 2}


class HttpOrderBackend(OrderBackend):
    """[사람 승인 필요] 고객사 REST API 연동 구현체.

    환경변수
      ORDER_BACKEND=http        (필수) 이 구현체 선택
      ORDER_API_BASE=https://…  (필수) 예: https://api.example.com/cs/v1
      ORDER_API_KEY=…           (선택) Bearer 인증
      ORDER_API_ALLOW_WRITE=1   (선택) 없으면 confirm_refund/request_redelivery 는
                                dry-run 응답만 하고 실제 호출하지 않는다(기본 안전).
    미설정 시 get_backend() 가 자동으로 demo 로 폴백한다.
    """

    name = "http"

    def __init__(self, base: str, key: str = "", allow_write: bool = False, timeout: int = 10):
        self.base = base.rstrip("/")
        self.key = key
        self.allow_write = allow_write
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body or {}, ensure_ascii=False).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception as e:  # 연동 실패는 툴 오류로 표면화(LLM 이 상담사 전환하도록)
            return {"error": "backend_unavailable", "detail": str(e)[:200]}

    def lookup_recent_order(self, inp):
        return self._req("GET", f"/orders/recent?phone={inp.get('phone','')}")

    def get_refund_policy(self, inp):
        return self._req("POST", "/refunds/policy", inp)

    def quote_refund(self, inp):
        return self._req("POST", "/refunds/quote", inp)

    def confirm_refund(self, inp):
        if not self.allow_write:
            # 쓰기 미승인 상태: 실제 접수하지 않고 dry-run 만 반환한다.
            return {
                "status": "dry_run",
                "accepted": False,
                "reason": "ORDER_API_ALLOW_WRITE 미설정 — 실제 환불 접수는 사람 승인 후 활성화",
                "order_id": inp.get("order_id"),
                "refund_amount": inp.get("refund_amount"),
            }
        return self._req("POST", "/refunds/confirm", inp)

    def request_redelivery(self, inp):
        if not self.allow_write:
            return {
                "status": "dry_run",
                "accepted": False,
                "reason": "ORDER_API_ALLOW_WRITE 미설정 — 실제 재배달 접수는 사람 승인 후 활성화",
                "order_id": inp.get("order_id"),
            }
        return self._req("POST", "/redeliveries", inp)

    def escalate_to_agent(self, inp):
        return self._req("POST", "/escalations", inp)


_CACHE: dict = {}


def get_backend() -> OrderBackend:
    """환경변수로 백엔드 선택. 기본 demo(기존과 동일 동작)."""
    kind = (os.environ.get("ORDER_BACKEND") or "demo").strip().lower()
    base = (os.environ.get("ORDER_API_BASE") or "").strip()
    allow_write = os.environ.get("ORDER_API_ALLOW_WRITE") == "1"
    ck = (kind, base, allow_write)
    if _CACHE.get("key") == ck and _CACHE.get("obj") is not None:
        return _CACHE["obj"]
    if kind == "http" and base:
        obj = HttpOrderBackend(base, (os.environ.get("ORDER_API_KEY") or "").strip(), allow_write)
    else:
        obj = DemoOrderBackend()  # 폴백 포함: http 인데 BASE 없으면 데모
    _CACHE["key"] = ck
    _CACHE["obj"] = obj
    return obj
