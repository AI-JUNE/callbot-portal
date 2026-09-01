# -*- coding: utf-8 -*-
"""운영 대시보드 집계(간이) — 백로그 P0-6.

원칙: build now, activate on approval.
- 읽기 전용. 부작용 없음. 실개인정보·원문 미포함(집계 숫자만).
- 집계 소스: escalation.QUEUE.stats() + recording_audit.STORE.stats() (프로세스 내 sim)
  + 데모 기준선(DEMO_BASELINE/DEMO_PERIODS). 서버리스 특성상 프로세스 스코프 값은 참고용.
- 노출 지표: 콜 인입 / 자동처리율 / 연결감축(봇 완결로 상담원 연결 회피) / 대기 / SLA.
- B132: ?period=today|week|month 기간 필터. calls.total=기간 합계(데모),
  calls.today는 기존 호출부 호환을 위해 일 단위 값 유지.
- B146: escalation/recording 을 고정 스키마로 정규화(항상 같은 키 · 정수 · source 표기)
  + gates(RECORDING_LIVE/CPAAS_LIVE/SPEECH_LIVE) 환경 플래그를 읽기 전용으로 노출.
  플래그는 **읽기만** 한다 — 여기서 활성화하지 않는다. 실활성화는 [승인 필요].
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

# B146: 콘솔이 의존할 수 있도록 키 집합을 고정한다(소스가 죽어도 스키마 불변).
ESCALATION_KEYS = ("queued", "assigned", "resolved", "abandoned", "total")
RECORDING_KEYS = ("active", "purged", "total")
GATE_FLAGS = ("RECORDING_LIVE", "CPAAS_LIVE", "SPEECH_LIVE")


def _safe_stats(mod_name, obj_name):
    """sim 스토어 stats() 를 best-effort 로 수집(실패해도 집계는 계속)."""
    try:
        mod = __import__(mod_name)
        return getattr(mod, obj_name).stats()
    except Exception:
        return None


def _norm_stats(raw, keys):
    """B146: sim stats 를 고정 스키마·정수로 정규화.

    소스를 못 읽었으면 전부 0 + source="unavailable". 예외를 던지지 않는다.
    """
    ok = isinstance(raw, dict)
    out = {}
    for k in keys:
        v = raw.get(k) if ok else None
        try:
            out[k] = int(v)
        except Exception:
            out[k] = 0
    out["source"] = "sim" if ok else "unavailable"
    return out


def _flag(name):
    """환경 플래그를 **읽기만** 한다(설정·활성화 없음)."""
    try:
        return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "on", "yes")
    except Exception:
        return False


def gate_flags():
    """B146: 활성화 게이트 현황(읽기 전용). 값 변경 없음."""
    return {n.lower(): _flag(n) for n in GATE_FLAGS}


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
    esc = _norm_stats(_safe_stats("escalation", "QUEUE"), ESCALATION_KEYS)
    rec = _norm_stats(_safe_stats("recording_audit", "STORE"), RECORDING_KEYS)
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
        "escalation": esc,
        "recording": rec,
        # B146: 활성화 게이트 현황(읽기 전용 · 여기서 켜지 않음)
        "gates": gate_flags(),
    }


from http.server import BaseHTTPRequestHandler
import _guard
import monitoring


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
            # 오류 모니터링: DSN 미설정이면 no-op, 전송 실패해도 응답에 영향 없음
            _eid = monitoring.capture_error(e, route="/api/ops_stats", method="GET")
            _out = {"ok": False, "error": str(e)}
            if _eid:
                _out["event_id"] = _eid
            self._send(500, _out)


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

    # --- B146: 정규화·게이트 ---
    for k in ESCALATION_KEYS:
        assert k in s["escalation"] and isinstance(s["escalation"][k], int), k
    for k in RECORDING_KEYS:
        assert k in s["recording"] and isinstance(s["recording"][k], int), k
    assert s["escalation"]["source"] in ("sim", "unavailable")
    assert s["recording"]["source"] in ("sim", "unavailable")
    # 소스 없음 → 전부 0 · unavailable
    n = _norm_stats(None, ESCALATION_KEYS)
    assert n["source"] == "unavailable" and all(n[k] == 0 for k in ESCALATION_KEYS)
    # 깨진 값·누락 키도 0 으로 방어
    n2 = _norm_stats({"queued": "3", "assigned": None, "total": "x"}, ESCALATION_KEYS)
    assert n2["queued"] == 3 and n2["assigned"] == 0 and n2["total"] == 0
    assert n2["resolved"] == 0 and n2["source"] == "sim"
    # 게이트는 읽기 전용 · 기본 OFF(환경 미설정 시)
    g = s["gates"]
    assert set(g.keys()) == {"recording_live", "cpaas_live", "speech_live"}
    assert all(isinstance(v, bool) for v in g.values())
    _saved = os.environ.get("SPEECH_LIVE")
    os.environ["SPEECH_LIVE"] = "1"
    assert gate_flags()["speech_live"] is True
    os.environ["SPEECH_LIVE"] = "off"
    assert gate_flags()["speech_live"] is False
    if _saved is None:
        os.environ.pop("SPEECH_LIVE", None)
    else:
        os.environ["SPEECH_LIVE"] = _saved
    # 직렬화 가능
    json.dumps(s, ensure_ascii=False)
    print("ops_stats selftest OK:", json.dumps(w, ensure_ascii=False)[:160], "...")
