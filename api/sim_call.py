"""무료 연계 테스트 시뮬레이터 - 통신비 0원 (/api/sim_call).

실제 전화망/CPaaS 없이, 텍스트로 전사된 발화를 기존 run_turn 에 순서대로 넣어
전 구간(시나리오 -> 응답 -> 상담사전환 -> 후처리)을 검증한다. 텔레포니 과금 0.
(LLM만 기존 Gemini 호출 - 콜 인프라 비용 아님)

사용:
  - 브라우저/GET:  https://<app>.vercel.app/api/sim_call?scenario=refund
  - CLI/로컬:      python api/sim_call.py refund
"""
import os, sys, json
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

SCRIPTS = {
    "refund": ["어제 받은 한우세트가 상태가 이상해요", "환불해 주세요", "네"],
    "care":   ["여보세요", "요즘 허리가 아파서 병원을 자주 가요", "괜찮아요 고마워요"],
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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        scenario = q.get("scenario", ["refund"])[0]
        out = simulate(scenario)
        b = json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "refund"
    print(json.dumps(simulate(sc), ensure_ascii=False, indent=2))
