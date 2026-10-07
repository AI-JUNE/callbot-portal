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
#   - 나가는 주소는 `_urlguard`(안부 웹훅·녹음 다운로드와 공용)를 거친다 —
#     평문 http·사설/루프백/메타데이터 주소로는 주문 정보와 Bearer 키를 보내지 않는다.
# ==========================================================================
from __future__ import annotations
import os, sys, json, copy, hashlib, urllib.request
from urllib.parse import urlencode

# 평면 import(Vercel 서버리스는 api/ 안에서 모듈을 찾는다)를 이 모듈이 스스로 보장한다
# — `_engine` 과 같은 방식(importer 가 손봐 둔 sys.path 에 기대지 않는다).
_d = os.path.dirname(os.path.abspath(__file__))
if _d not in sys.path:                        # pragma: no cover - importer 가 이미 넣어 둔다
    sys.path.insert(0, _d)

# 아웃바운드 주소 검증은 보안 통제라 폴백을 두지 않는다 — 모듈을 못 불러오면
# 검증 없이 나가는 것보다 import 시점에 드러나는 편이 안전하다(fail-closed).
# `wellbeing`·`voice` 와 **같은 함수**를 쓰므로 한쪽만 고쳐지는 일이 없다.
import _urlguard  # noqa: E402

# 응답 상한 — 툴 결과는 그대로 LLM 컨텍스트로 들어가고 서버리스 메모리를 쓴다.
# 고객사 API 가 거대한(혹은 끝나지 않는) 본문을 주면 통화가 메모리로 죽는다.
MAX_RESPONSE = 1 << 20          # 1MiB
MAX_QUERY_VALUE = 64            # 쿼리에 실리는 값 길이 상한(모델이 만든 인자)

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


# --------------------------------------------------------------------------
# 아웃바운드 주소 검증 — base URL 은 환경변수에서 오지만 검증은 한다.
#   · 평문 http 면 주문 정보와 `Authorization: Bearer <키>` 가 그대로 흐른다.
#   · 오타·잘못 복사한 값(사설 IP·`169.254.169.254`)이면 서버가 내부를 두드린다.
#   · 쓰기 승인(ORDER_API_ALLOW_WRITE=1)이 켜진 뒤에는 **환불 접수가 엉뚱한
#     주소로 나가는** 것이므로 되돌리기 어렵다.
# 판정은 `_urlguard` 한 곳이고 `health` 도 같은 함수를 쓴다 — 헬스가 「정상」이라고
# 말하는 동안 실제 호출만 가드에서 막히는 엇갈림을 만들지 않는다.
# --------------------------------------------------------------------------
def check_api_base(url):
    """(ok, reason). 주문 백엔드로 나가도 되는 주소인가.

    `ORDER_API_ALLOW_INSECURE=1` 은 로컬 개발용(평문 http·localhost 허용),
    `ORDER_API_HOSTS` 는 호스트 화이트리스트(가장 엄격한 운영 설정).
    """
    return _urlguard.check(url, label="ORDER_API_BASE",
                           allow_insecure=_urlguard.env_flag("ORDER_API_ALLOW_INSECURE"),
                           allowlist=_urlguard.env_hosts("ORDER_API_HOSTS"))


def _query(**pairs):
    """퍼센트 인코딩된 쿼리 문자열.

    값은 **모델이 만든 툴 인자**다(발신자 발화에 끌려간다). f-string 으로 그대로
    이어 붙이면 `&` 한 글자로 고객사 API 에 우리가 의도하지 않은 파라미터를
    덧붙이고 `#` 한 글자로 뒤를 잘라낼 수 있다 — 조회 조건이 조용히 바뀐다.
    """
    out = {}
    for k, v in pairs.items():
        s = "" if v is None else str(v)
        out[k] = s.strip()[:MAX_QUERY_VALUE]
    return urlencode(out)


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
        # 깊은 복사 — 얕은 복사면 items 리스트가 모듈 전역 DEMO_ORDER 와 공유돼
        # 호출자가 결과를 고치는 순간 인스턴스 수명 동안 데모 주문이 변조된다(회귀로 발견).
        return {"found": True, **copy.deepcopy(self.order)}

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
      ORDER_API_HOSTS=a,b       (선택) 호스트 화이트리스트. 설정하면 이 호스트만
      ORDER_API_ALLOW_INSECURE=1 (선택) 평문 http·localhost 허용 — **로컬 개발 전용**
    미설정 시 get_backend() 가 자동으로 demo 로 폴백한다.

    한계(숨기지 않고 적어 둔다): `lookup_recent_order` 는 고객사 API 규격상
    발신번호를 쿼리로 보내므로 **상대 서버의 접근로그에 번호가 남는다**(우리 쪽
    로그·응답에는 남지 않는다). 번호를 본문으로 옮기려면 고객사 API 변경이 필요하다.
    """

    name = "http"

    def __init__(self, base: str, key: str = "", allow_write: bool = False, timeout: int = 10):
        self.base = base.rstrip("/")
        self.key = key
        self.allow_write = allow_write
        self.timeout = timeout

    @staticmethod
    def _fail(detail: str) -> dict:
        """연동 실패를 툴 오류로 표면화한다(LLM 이 상담사 전환하도록).

        `detail` 은 **분류 토큰만** 담는다. 이 값은 LLM 컨텍스트로 들어가고
        `/api/chat` 응답의 `log[].out` 으로 화면까지 그대로 나간다 — 예전에는
        `str(e)[:200]` 이라 `HTTPError` 가 들고 있는 **요청 URL(`?phone=`
        발신번호)** 과 고객사 주소·경로가 그 길로 흘러나갔다. 저장소의 규약은
        예외 문구를 응답에 싣지 않는 것이다(10차·17차·20차와 같은 결함 계열).
        """
        return {"error": "backend_unavailable", "detail": detail}

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        ok, _reason = check_api_base(url)
        if not ok:
            # 사유(설정 힌트)는 툴 결과에 싣지 않는다 — 운영자는 /api/health 에서 본다.
            # 거부되면 요청 자체를 보내지 않는다(blind SSRF 도 성립하지 않는다).
            return self._fail("blocked_url")
        data = json.dumps(body or {}, ensure_ascii=False).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                # `urlopen` 은 리다이렉트를 따라간다 — 최종 주소를 다시 보지 않으면
                # 302 한 번으로 가드가 무력화된다(`wellbeing._redirect_escape` 와 같은 규약).
                # 한계: 중간 요청 자체는 이미 나간 뒤다(응답 본문을 쓰지 않을 뿐).
                # 그걸 막는 것은 아웃바운드 프록시·호스트 화이트리스트 몫이다.
                final = ""
                try:
                    final = r.geturl() or ""
                except Exception:
                    final = ""              # 최종 주소를 못 읽으면 판단 재료가 없다
                if final and final != url and not check_api_base(final)[0]:
                    return self._fail("redirect_escape")
                raw = r.read(MAX_RESPONSE + 1)
        except Exception as e:
            return self._fail(type(e).__name__)
        if raw is not None and len(raw) > MAX_RESPONSE:
            return self._fail("response_too_large")
        try:
            out = json.loads((raw or b"").decode("utf-8", "replace") or "{}")
        except Exception as e:
            return self._fail(type(e).__name__)
        if not isinstance(out, dict):
            # 배열·숫자·문자열을 그대로 돌려주면 호출부(`_engine`)의 `out.get(...)` 이
            # AttributeError 로 터져 통화가 500 으로 끝난다 — 계약은 dict 다(6차와 같은 판단).
            return self._fail("non_object_response")
        return out

    def lookup_recent_order(self, inp):
        return self._req("GET", "/orders/recent?" + _query(phone=inp.get("phone", "")))

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
    key = (os.environ.get("ORDER_API_KEY") or "").strip()
    # 캐시 식별자에 **키 지문**까지 넣는다. 예전에는 (kind, base, allow_write) 뿐이라
    # 키를 회전해도 이미 뜬 인스턴스가 **낡은 키를 계속 보냈다** — 유출된 키를 바꾸는
    # 쪽이 듣지 않는다(게이트를 import 시점에 얼려 두던 15차·8차 결함과 같은 계열).
    # 지문만 넣어 평문 사본을 모듈 전역에 하나 더 만들지 않는다.
    ck = (kind, base, allow_write,
          hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else "")
    if _CACHE.get("key") == ck and _CACHE.get("obj") is not None:
        return _CACHE["obj"]
    if kind == "http" and base:
        obj = HttpOrderBackend(base, key, allow_write)
    else:
        obj = DemoOrderBackend()  # 폴백 포함: http 인데 BASE 없으면 데모
    _CACHE["key"] = ck
    _CACHE["obj"] = obj
    return obj
