"""상담원 에스컬레이션(폴백) 큐 — 백로그 P0-4.

원칙: build now, activate on approval.
- 순수 모듈(핸들러 없음) → Vercel 함수로 노출되지 않음. 실개인정보 저장 없음.
- 트리거: (1) 고객의 상담원 연결 요청 키워드 (2) 낮은 신뢰도 (3) 연속 폴백 (4) 금지·민감 주제.
- engine.py 의 escalate_to_agent 툴 결과·sim_call 의 handoff 시나리오와 정합.
- 큐는 in-memory. 실제 상담원 배정·CTI 연동은 [승인 필요] — 여기서는 상태 전이만.

2026-10-11(26차) 보강 — 이 파일이 적어 둔 두 문장("실개인정보 저장 없음",
감사 기록은 "메타만")이 **사실이 아니었다**:
  (1) `summary` 는 LLM 이 만든 자유 문장이고 시스템 프롬프트에 발신번호가 들어
      있다 — 번호·성명이 그대로 섞여 들어올 수 있는데 200자로 **자르기만** 했다.
      이제 `_monitoring.scrub()` 으로 마스킹한 뒤에 자르고, 마스킹이 불가능하면
      요약을 싣지 않는다(가릴 수 없는 것은 보관하지 않는다 · `summary_dropped`).
  (2) 티켓 사전·감사 목록에 **상한이 없었다**. 웜 인스턴스가 전환을 처리할수록
      메모리가 자라서 결국 **진행 중인 통화**가 죽는다. 상한을 두고, 버린 것은
      `dropped`(그중 대기·배정 중이던 것은 `dropped_waiting`)로 **센다** —
      조용히 사라지는 쪽이 더 나쁘다(`_call_metrics`·`_audit` 와 같은 규약).
또한 `stats()` 가 **휘발성을 말한다**(`scope="instance"`·`volatile=True`) —
숫자만 내보내면 소비자가 영속 큐로 오해한다. 재시작하면 대기 중 티켓은 사라지고,
영속 저장소는 [승인 필요]다.

사용:
  from _escalation import EscalationPolicy, EscalationQueue
  pol = EscalationPolicy()
  hit = pol.evaluate(text="상담사 바꿔 주세요", confidence=0.9, fallback_streak=0)
  if hit: ticket = QUEUE.enqueue(session_id="s1", reason=hit["reason"], summary="...")

셀프테스트:
  python api/_escalation.py
"""
import os
import sys
import time
import itertools

_d = os.path.dirname(os.path.abspath(__file__))
if _d not in sys.path:                     # 다른 모듈(_log·_audit)과 같은 자립 부트스트랩
    sys.path.insert(0, _d)

try:
    from _monitoring import scrub as _scrub
except Exception:                          # pragma: no cover - 모듈 부재 폴백
    # 폴백에서 원문을 통과시키면 **조용한 강등**이다(마스킹한다고 적힌 자리에
    # 원문이 남는다). 가릴 수 없으면 아예 싣지 않는다 — `_summary()` 참조.
    _scrub = None

# 상담원 연결 요청으로 간주할 키워드(공백 제거 후 부분 일치)
HANDOFF_KEYWORDS = [
    "상담원", "상담사", "사람이랑", "사람과", "직원", "사람연결", "사람바꿔",
    "매니저", "책임자", "진짜사람",
]
# 자동응대를 중단하고 즉시 전환할 민감 주제(콜봇 응대 범위 밖)
SENSITIVE_KEYWORDS = ["법적", "소송", "고소", "언론", "신고할", "금감원"]

CONF_THRESHOLD = 0.55   # 이 미만이면 저신뢰
FALLBACK_LIMIT = 2      # 연속 폴백 허용 횟수(초과 시 전환)

# 보관 상한 — 인스턴스 메모리다(`_call_metrics.MAX_RECORDS` 와 같은 눈금 규약).
MAX_TICKETS = 500       # 티켓 사전 상한(초과 시 종결된 것부터 버린다)
MAX_AUDIT = 2000        # 상태 전이 기록 상한
MAX_SUMMARY = 200       # 요약 길이 상한(마스킹 후 측정)
MAX_LABEL = 64          # 라벨류(세션·사유·시나리오·메모) 길이 상한

# 아직 사람이 집어가지 않은 상태 — 이 티켓이 사라지면 '기다리는 고객'이 사라진다.
OPEN_STATES = ("queued", "assigned")


def _label(v):
    """라벨류 한 칸 — 마스킹 + 길이 상한. 어떤 입력에도 예외를 내지 않는다."""
    if v is None:
        return ""
    try:
        s = v if isinstance(v, str) else str(v)
    except Exception:                      # pragma: no cover - str() 자체가 실패
        return "-"
    if _scrub is not None:
        try:
            s = _scrub(s)
        except Exception:                  # pragma: no cover - 마스킹 장애
            return "-"
    return s[:MAX_LABEL]


def _summary(v):
    """요약 한 칸 — **마스킹한 뒤에** 자른다.

    요약은 LLM 이 만든 자유 문장이다(시스템 프롬프트에 발신번호가 들어 있어
    번호·성명이 섞일 수 있다). 자르고 나서 마스킹하면 경계에서 잘린 번호를
    패턴으로 알아볼 수 없으므로 순서가 중요하다. 마스킹 자체가 불가능하면
    요약을 버린다 — 가릴 수 없는 것은 보관하지 않는다.
    """
    if v is None:
        return ""
    if _scrub is None:
        return None                        # 호출부가 summary_dropped 로 센다
    try:
        s = _scrub(v)
    except Exception:
        return None
    return s[:MAX_SUMMARY]

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
        self._audit = []        # 상태 전이 감사 기록(마스킹된 메타만)
        self._t0 = time.time()  # 이 인스턴스가 장부를 시작한 시각
        self.dropped = 0            # 상한으로 버린 티켓 수
        self.dropped_waiting = 0    # 그중 아직 처리되지 않았던 티켓(대기·배정)
        self.audit_dropped = 0      # 상한으로 버린 전이 기록 수
        self.summary_dropped = 0    # 마스킹 불가로 싣지 않은 요약 수
        self.record_errors = 0      # 호출부가 기록을 실패한 횟수(note_failure)
        self.last_error = ""        # 마지막 실패 사유(예외 타입명 등, 원문 아님)

    def enqueue(self, session_id, reason, summary="", scenario=""):
        self._evict()
        tid = "ESC-%04d" % next(self._seq)
        s = _summary(summary)
        if s is None:
            self.summary_dropped += 1
            s = ""
        tk = {
            "id": tid, "session_id": _label(session_id), "reason": _label(reason),
            "summary": s, "scenario": _label(scenario),
            "state": "queued", "ts": time.time(),
        }
        self._tickets[tid] = tk
        self._log(tid, None, "queued", reason)
        return dict(tk)

    def note_failure(self, reason=""):
        """큐 기록이 실패했다는 **사실 자체**를 센다.

        호출부(`_engine._esc_enqueue`)는 통화를 끊지 않기 위해 예외를 삼킨다 —
        그래서 아무도 세지 않으면 "고객에게 상담사 연결을 약속했는데 티켓이
        없다"가 흔적 없이 지나간다. 집계는 `stats().record_errors` 로 드러난다.
        """
        try:
            self.record_errors += 1
            self.last_error = _label(reason)
        except Exception:                  # pragma: no cover - 집계가 통화를 죽이지 않는다
            pass

    def _evict(self):
        """상한 초과 시 오래된 것부터 버린다 — **종결된 티켓이 먼저**.

        대기·배정 중인 티켓을 먼저 버리면 기다리는 고객이 장부에서 사라진다.
        종결분이 하나도 없으면 가장 오래된 것을 버리되 `dropped_waiting` 으로
        따로 센다(그 숫자가 0 이 아니면 큐 용량이 실제로 모자란다는 뜻이다).
        """
        while len(self._tickets) >= MAX_TICKETS:
            victim = min(self._tickets.values(),
                         key=lambda t: (t["state"] in OPEN_STATES, t["ts"]))
            del self._tickets[victim["id"]]
            self.dropped += 1
            if victim["state"] in OPEN_STATES:
                self.dropped_waiting += 1

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
        """현황 + **이 장부의 성질**.

        숫자만 내보내면 소비자는 영속 큐로 읽는다. `scope`·`volatile` 로
        "인스턴스 메모리이고 재시작하면 대기 티켓이 사라진다"를 함께 말한다
        (`_call_metrics.summary().collector.scope` 와 같은 규약).
        """
        s = {"queued": 0, "assigned": 0, "resolved": 0, "abandoned": 0}
        for t in self._tickets.values():
            s[t["state"]] = s.get(t["state"], 0) + 1
        s["total"] = len(self._tickets)
        s["dropped"] = self.dropped
        s["dropped_waiting"] = self.dropped_waiting
        s["audit_dropped"] = self.audit_dropped
        s["summary_dropped"] = self.summary_dropped
        s["record_errors"] = self.record_errors
        s["capacity"] = MAX_TICKETS
        s["scope"] = "instance"      # 클러스터 합산 아님
        s["volatile"] = True         # 재시작 시 대기 티켓 소멸(영속 저장소 [승인 필요])
        s["started_ts"] = int(self._t0)
        return s

    def audit_log(self):
        # 항목까지 복사한다 — 호출자가 반환값을 고쳐도 감사 기록 원본은 불변.
        return [dict(e) for e in self._audit]

    def _log(self, tid, old, new, note):
        # note 는 사유·actor 라벨이다 — 원문이 섞일 수 있으므로 마스킹을 거친다.
        self._audit.append({"ts": time.time(), "ticket": tid, "from": old,
                            "to": new, "note": _label(note)})
        if len(self._audit) > MAX_AUDIT:
            # append-only 는 유지하되 상한은 둔다(무한 성장은 통화를 죽인다).
            # 버린 사실은 `audit_dropped` 로 드러난다.
            cut = len(self._audit) - MAX_AUDIT
            del self._audit[:cut]
            self.audit_dropped += cut


QUEUE = EscalationQueue()  # 모듈 전역(프로세스 단위 sim 큐)


if __name__ == "__main__":     # pragma: no cover
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

    # 요약은 마스킹된 뒤에 보관된다(원문 번호가 남으면 '메타만'이 거짓이 된다)
    q2 = EscalationQueue()
    t2 = q2.enqueue("sess-2", "request", "고객 010-1234-5678 환불 요청")
    assert "010-1234-5678" not in t2["summary"], t2["summary"]
    assert q2.stats()["scope"] == "instance" and q2.stats()["volatile"] is True

    # 상한을 넘기면 종결분부터 버리고, 버린 사실을 센다
    q3 = EscalationQueue()
    for i in range(MAX_TICKETS + 5):
        tk = q3.enqueue("s", "request")
        if i % 2 == 0:                     # 절반은 종결 처리
            q3.transition(tk["id"], "abandoned")
    s3 = q3.stats()
    assert s3["total"] <= MAX_TICKETS and s3["dropped"] > 0, s3
    assert s3["dropped_waiting"] == 0, s3   # 대기 중인 티켓은 버리지 않았다
    q3.note_failure("RuntimeError")
    assert q3.stats()["record_errors"] == 1
    print("SELF-TEST OK:", q.stats())
