# -*- coding: utf-8 -*-
"""운영 대시보드 집계(간이) — 백로그 P0-6.

원칙: build now, activate on approval.
- 읽기 전용. 부작용 없음. 실개인정보·원문 미포함(집계 숫자만).
- 집계 소스: escalation.QUEUE.stats() + recording_audit.STORE.stats() (프로세스 내 sim)
  + 데모 기준선(DEMO_BASELINE/DEMO_PERIODS). 서버리스 특성상 프로세스 스코프 값은 참고용.
- 노출 지표: 콜 인입 / 자동처리율 / 연결감축(봇 완결로 상담원 연결 회피) / 대기 / SLA.
- B132: ?period=today|week|month 기간 필터. calls.total=기간 합계(데모),
  calls.today는 기존 호출부 호환을 위해 일 단위 값 유지.
- 실 CTI·실통계 연동은 [승인 필요] — 여기서는 sim 집계만.

사용:
  from ops_stats import get_ops_summary
  get_ops_summary()                    # dict (오늘)
  get_ops_summary(period="week")      # 주간(데모)

셀프테스트:
  python api/ops_stats.py
"""
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))

# 데모 기준선(콘솔 모니터 뷰 수치와 정합). 실데이터 아님.
DEMO_BASELINE = {
    "calls_today": 214,        # 오늘 인입(데모)
    "auto_rate": 0.72,         # 봇 자동완결률(데모)
    "wait_avg_sec": 12,        # 평균 대기(데모)
    "sla_target_sec": 1.2,     # 응대 목표(초)
    "sla_attain": 0.94,        # SLA 준수율(데모)
}

# 기간별 데모 배수(콘솔 리포트 뷰 '주간 인입 1,486'과 정합). 실데이터 아님.
DEMO_PERIODS = {
    "today": {},
    "week": {"calls": 1486, "auto_rate": 0.71, "wait_avg_sec": 13, "sla_attain": 0.93},
    "month": {"calls": 6120, "auto_rate": 0.72, "wait_avg_sec": 13, "sla_attain": 0.93},
}


def _safe_stats(mod_name, obj_name):
    """sim 스토어 stats() 를 best-effort 로 수집(실패해도 집계는 계속)."""
    try:
        mod = __import__(mod_name)
        return getattr(mod, obj_name).stats()
    except Exception:
        return None


def get_ops_summary(baseline=None, period="today"):
    period = period if period in DEMO_PERIODS else "today"
    b = dict(DEMO_BASELINE)
    if baseline:
        b.update(baseline)
    p = DEMO_PERIODS[period]
    calls_today = int(b["calls_today"])
    total = int(p.get("calls", calls_today))     # 기간 합계(오늘=일 값)
    auto_rate = float(p.get("auto_rate", b["auto_rate"]))
    auto_done = int(round(total * auto_rate))
    esc = _safe_stats("escalation", "QUEUE")
    rec = _safe_stats("recording_audit", "STORE")
    return {
        "ok": True,
        "ts": int(time.time()),
        "mode": "sim",  # 실통계 연동 전까지 항상 sim
        "period": period,
        "calls": {
            "today": calls_today,   # 하위호환: 일 단위 값
            "total": total,         # B132: 선택 기간 합계
            "auto_done": auto_done,
            "auto_rate": round(auto_rate, 3),
            # 연결감축: 봇이 완결해 상담원 연결을 회피한 콜 수(데모 추정)
            "agent_connect_saved": auto_done,
        },
        "wait": {"avg_sec": p.get("wait_avg_sec", b["wait_avg_sec"])},
        "sla": {
            "target_sec": b["sla_target_sec"],
            "attain_rate": round(float(p.get("sla_attain", b["sla_attain"])), 3),
        },
        # 프로세스 내 sim 큐/저장소 현황(서버리스에선 인스턴스 단위 참고치)
        "escalation": esc or {"total": 0},
        "recording": rec or {"total": 0},
    }


from http.server import BaseHTTPRequestHandler
import _guard


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.end_headers()

    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        try:
            q = parse_qs(urlparse(self.path).query)
            period = (q.get("period", ["today"])[0] or "today").strip().lower()
            self._send(200, get_ops_summary(period=period))
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    s = get_ops_summary()
    assert s["ok"] and s["mode"] == "sim" and s["period"] == "today"
    assert s["calls"]["total"] == s["calls"]["today"] == 214
    assert s["calls"]["auto_done"] == round(214 * 0.72)
    assert s["calls"]["agent_connect_saved"] == s["calls"]["auto_done"]
    assert "queued" in s["escalation"] or s["escalation"]["total"] == 0
    s2 = get_ops_summary({"calls_today": 100, "auto_rate": 0.5})
    assert s2["calls"]["auto_done"] == 50
    w = get_ops_summary(period="week")
    assert w["period"] == "week" and w["calls"]["total"] == 1486 and w["calls"]["today"] == 214
    assert w["calls"]["auto_done"] == round(1486 * 0.71)
    m = get_ops_summary(period="month")
    assert m["period"] == "month" and m["calls"]["total"] == 6120
    bad = get_ops_summary(period="yyy")
    assert bad["period"] == "today"
    print("ops_stats selftest OK:", json.dumps(w, ensure_ascii=False)[:160], "...")
