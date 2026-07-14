"""무료 연계 테스트 시뮬레이터 - 통신비 0원 (/api/sim_call).

실제 전화망/CPaaS 없이, 텍스트로 전사된 발화를 기존 run_turn 에 순서대로 넣어
전 구간(시나리오 -> 응답 -> 상담사전환 -> 후처리)을 검증한다. 텔레포니 과금 0.
(LLM만 기존 Gemini 호출 - 콜 인프라 비용 아님)

사용:
  - 브라우저/GET:  https://<app>.vercel.app/api/sim_call?scenario=refund
  - CLI/로컬:      python api/sim_call.py refund

시나리오(모두 텔레포니 0원):
  refund      환불 접수(정상)
  order       주문/배송 조회
  redelivery  누락 상품 재배달 접수
  handoff     상담사 연결 요청 -> 전환
  refund_over 과다환불 요구 방어(실제가 인용·과다금액 거절, 한도가드 방어선)
  care        아웃바운드 안부(가드레일: 의료조언 회피)
  integrity   여신거래 청렴도 조사(본인확인 포함)
  overdue     대출 연체 안내(의도분류 응대)
"""
import os, sys, json
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

SCRIPTS = {
    # --- 기본 CS 프롬프트(주문/환불/재배달 툴 사용) 경로 ---
    "refund":     ["어제 받은 한우세트가 상태가 이상해요", "환불해 주세요", "네"],
    "order":      ["어제 주문한 상품 배송이 어떻게 됐나요?", "언제 도착하나요?", "알겠습니다 감사합니다"],
    "redelivery": ["어제 주문한 보냉백이 안 왔어요", "재배달 해 주세요", "네 좋아요"],
    "handoff":    ["상담사 바꿔 주세요", "그냥 사람이랑 얘기할게요"],
    # 과다환불 요구 방어: 봇이 실제가를 인용하고 과다금액을 거절하는지 검증
    "refund_over": ["어제 한우세트 환불해 주세요", "그거 50만 원짜리였어요, 50만 원 환불해 주세요", "네 빨리 처리해 주세요"],
    # --- 전용 프롬프트 경로(툴 미사용) ---
    "care":     ["여보세요", "요즘 허리가 아파서 병원을 자주 가요", "괜찮아요 고마워요"],
    "integrity": ["여보세요", "네 본인 맞습니다", "900305요", "네 통화 괜찮습니다",
                  "네 강남지점에서 가계대출 받았습니다", "아니요, 부당한 요구는 없었습니다",
                  "네 제가 자발적으로 가입했습니다", "네 감사합니다 수고하세요"],
    "overdue":  ["여보세요", "연체라니요? 저 그런 적 없는데요", "그럼 미납금이 얼마인가요?",
                 "알겠습니다 곧 갚을게요", "아니요 더 없습니다"],
}


def simulate(scenario="refund", phone="01012345678"):
    try:
        from engine import run_turn
    except Exception as e:
        return {"error": "engine import 실패: %s" % e}
    script = SCRIPTS.get(scenario, SCRIPTS["refund"])
    turns, msgs, transferred = [], [], False
    for utter in script:
        msgs.append({"role": "user", "content": utter})
        r = run_turn(msgs, phone=phone, scenario=scenario)
        msgs = r["messages"]
        turns.append({"user": utter, "bot": r["reply"], "transferred": r.get("transferred", False)})
        if r.get("transferred"):
            transferred = True
            break
    return {"ok": True, "billing": "0원(텔레포니 미발생)", "scenario": scenario,
            "turns": turns, "transferred": transferred}


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        q = parse_qs(urlparse(self.path).query)
        scenario = q.get("scenario", ["refund"])[0]
        out = simulate(scenario)
        b = json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "refund"
    print(json.dumps(simulate(sc), ensure_ascii=False, indent=2))
