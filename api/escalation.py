"""상담원 에스컬레이션(폴백) 큐 — 백로그 P0-4.

원칙: build now, activate on approval.
- 순수 모듈(핸들러 없음) → Vercel 함수로 노출되지 않음. 실개인정보 저장 없음.
- 트리거: (1) 고객의 상담원 연결 요청 키워드 (2) 낮은 신뢰도 (3) 연속 폴백 (4) 금지·민감 주제.
- engine.py 의 escalate_to_agent 툴 결과·sim_call 의 handoff 시나리오와 정합.
- 큐는 in-memory. 실제 상담원 배정·CTI 연동은 [승인 필요] — 여기서는 상태 전이만.

사용:
  from escalation import EscalationPolicy, EscalationQueue
  pol = EscalationPolicy()
  hit = pol.evaluate(text="상담사 바꿔 주세요", confidence=0.9, fallback_streak=0)
  if hit: ticket = QUEUE.enqueue(session_id="s1", reason=hit["reason"], summary="...")

셀프테스트:
  python api/escalation.py
"""
import time
import itertools

# 상담원 연결 요청으로 간주할 키워드(공백 제거 후 부분 일치)
HANDOFF_KEYWORDS = [
    "상담원", "상담사", "사람이랑", "사람과", "직원", "사람연결", "사람바꿔",
    "매니저", "책임자", "진짜사람",
]
# 자동응대를 중단하고 즉시 전환할 민감 주제(콜봇 응대 범위 밖)
SENSITIVE_KEYWORDS = ["법적", "소송", "고소", "언론", "신고할", "금감원"]

CONF_THRESHOLD = 0.55   # 이 미만이면 저신뢰
FALLBACK_LIMIT = 2      # 연속 폴백 허용 횟수(초과 시 전환)

# 티켓 상태 전이: queued → assigned → resolved / abandoned
_VALID_NEXT = {
    "queued": ("assigned", "abandoned"),
    "assigned": ("resolved", "abandoned"),
    "resolved": (),
    "abandoned": (),
}


class EscalationPolicy:
    """규칙 기반 에스컬레이션 판단. 반환: None 또는 {reason, detail}."""

    def __init__(self, conf_threshold=CONF_THRESHOLD, fallback_limit=FALLBACK_LIMIT):
        self.conf_threshold = conf_threshold
        self.fallback_limit = fallback_limit

    def evaluate(self, text="", confidence=1.0, fallback_streak=0):
        t = (text or "").replace(" ", "")
        for kw in SENSITIVE_KEYWORDS:
            if kw in t:
                return {"reason": "sensitive", "detail": "민감 주제 키워드: %s" % kw}
        for kw in HANDOFF_KEYWORDS:
            if kw in t:
                return {"reason": "request", "detail": "상담원 요청 키워드: %s" % kw}
        if confidence is not None and confidence < self.conf_threshold:
            return {"reason": "low_confidence", "detail": "신뢰도 %.2f < %.2f" % (confidence, self.conf_threshold)}
        if fallback_streak > self.fallback_limit:
            return {"reason": "repeated_fallback", "detail": "연속 폴백 %d회 초과" % self.fallback_limit}
        return None


class EscalationQueue:
    """in-memory 에스컬레이션 큐. 실배정/CTI 연동 없음(상태 전이만). [승인 필요] 전까지 sim."""

    def __init__(self):
        self._seq = itertools.count(1)
        self._tickets = {}      # id -> ticket dict
        self._audit = []        # 상태 전이 감사 기록(메타만, 개인정보 없음)

    def enqueue(self, session_id, reason, summary="", scenario=""):
        tid = "ESC-%04d" % next(self._seq)
        tk = {
            "id": tid, "session_id": session_id, "reason": reason,
            "summary": (summary or "")[:200], "scenario": scenario,
            "state": "queued", "ts": time.time(),
        }
        self._tickets[tid] = tk
        self._log(tid, None, "queued", reason)
        return dict(tk)

    def transition(self, tid, new_state, actor="system"):
        tk = self._tickets.get(tid)
        if not tk:
            raise KeyError("ticket not found: %s" % tid)
        if new_state not in _VALID_NEXT.get(tk["state"], ()):
            raise ValueError("invalid transition %s -> %s" % (tk["state"], new_state))
        old = tk["state"]
        tk["state"] = new_state
        self._log(tid, old, new_state, actor)
        return dict(tk)

    def list(self, state=None):
        out = [dict(t) for t in self._tickets.values() if state is None or t["state"] == state]
        return sorted(out, key=lambda t: t["ts"])

    def stats(self):
        s = {"queued": 0, "assigned": 0, "resolved": 0, "abandoned": 0}
        for t in self._tickets.values():
            s[t["state"]] = s.get(t["state"], 0) + 1
        s["total"] = len(self._tickets)
        return s

    def audit_log(self):
        return list(self._audit)

    def _log(self, tid, old, new, note):
        self._audit.append({"ts": time.time(), "ticket": tid, "from": old, "to": new, "note": note})


QUEUE = EscalationQueue()  # 모듈 전역(프로세스 단위 sim 큐)


if __name__ == "__main__":
    pol = EscalationPolicy()
    assert pol.evaluate("상담사 바꿔 주세요")["reason"] == "request"
    assert pol.evaluate("그냥 사람이랑 얘기할게요")["reason"] == "request"
    assert pol.evaluate("소송할 거예요")["reason"] == "sensitive"
    assert pol.evaluate("주문 조회요", confidence=0.3)["reason"] == "low_confidence"
    assert pol.evaluate("음...", confidence=0.9, fallback_streak=3)["reason"] == "repeated_fallback"
    assert pol.evaluate("주문 조회요", confidence=0.9, fallback_streak=0) is None

    q = EscalationQueue()
    t = q.enqueue("sess-1", "request", "고객이 상담원 연결 요청")
    assert t["state"] == "queued"
    t = q.transition(t["id"], "assigned", actor="agent-01")
    t = q.transition(t["id"], "resolved", actor="agent-01")
    assert t["state"] == "resolved"
    try:
        q.transition(t["id"], "assigned")
        print("FAIL: invalid transition allowed")
    except ValueError:
        pass
    assert q.stats()["resolved"] == 1
    assert len(q.audit_log()) == 3
    print("SELF-TEST OK:", q.stats())
