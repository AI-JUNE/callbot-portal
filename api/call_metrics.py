# -*- coding: utf-8 -*-
"""실측 통화 지표 수집기 — 운영 대시보드 '실데이터 연결 준비'.

## 왜 별도 모듈인가
`ops_stats` 가 내보내는 수치는 전부 **데모 기준선**이다(`data_source="demo"`).
실 CTI 연동은 [승인 필요]지만, 그 전에 **우리 서버가 실제로 처리한 통화**는
지금도 사실로 존재한다. 이 모듈은 그 사실만 세어 둔다. 데모 수치와 **섞지 않고**
별도 블록(`measured`)으로 내보내, 보는 사람이 어느 쪽이 실측인지 구분할 수 있게 한다.

## 절대 하지 않는 것
- **없는 수치를 만들지 않는다.** 표본이 0건이면 비율은 `null` 이다 — 0.0 으로 내리거나
  데모 값으로 대신 채우지 않는다(0% 자동처리도 '주장'이다).
- **원문을 담지 않는다.** 전화번호·발화·성명·통화ID 원문 모두 저장하지 않는다.
  통화ID 는 중복 집계 방지용 지문(HMAC 앞 12자)으로만 쓰고 밖으로 내보내지 않는다.
- **보관 창보다 넓은 기간을 채워 넣지 않는다.** 2시간치로 '월간'을 만들면 거짓말이다.
  요청 기간이 실제 관측 창보다 넓으면 `partial=true` 와 `window_sec` 를 함께 실어
  소비자가 '부분 표본'임을 알 수 있게 한다.
- **오류를 삼키지 않는다.** 호출부는 수집 실패로 통화를 끊으면 안 되므로 예외를
  전파하지 않지만, 삼킨 횟수를 `collector_errors` 로 드러낸다.

## 한계 (정직하게)
- 서버리스 인스턴스 메모리다. 인스턴스가 재활용되면 표본은 사라지고, 여러 인스턴스의
  수치가 합쳐지지 않는다. 그래서 이 값은 **회계·SLA 근거가 아니라 관측치**다.
  영속 집계(공유 저장소·실 CTI)는 [승인 필요] — 교체점은 `SINK` 하나다.

사용:
    import call_metrics as cm
    cm.start("call-1", scenario="refund")
    cm.mark_turn("call-1")
    cm.finish("call-1", outcome="bot_completed")
    cm.summary("today")

셀프테스트:
    python3 api/call_metrics.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time

# 통화 종료 사유 — 화이트리스트. 미지 값은 조용히 '기타'로 바꾸지 않고 거부한다.
OUTCOMES = ("bot_completed", "transferred", "abandoned", "failed")

# 기간 정의(초). ops_stats.PERIODS 와 같은 이름을 쓴다(드리프트 회귀로 강제).
PERIOD_SEC = {"today": 86400, "week": 7 * 86400, "month": 30 * 86400}

MAX_RECORDS = 2000          # 메모리 상한(오래된 것부터 버린다)
MAX_OPEN = 500              # 진행 중 통화 상한(종료 이벤트 유실 대비)
RETENTION_SEC = 30 * 86400  # 보관 상한 — 이보다 오래된 관측치는 버린다
MAX_TURNS = 500             # 턴 카운터 상한(비정상 루프 방어)


def _now():
    return time.time()


def _salt():
    """통화ID 지문용 키. 미설정이면 프로세스 임시값 — 인스턴스 밖에서 대조 불가."""
    v = os.environ.get("CALLBOT_METRICS_SALT")
    if v:
        return str(v).encode("utf-8")
    return _EPHEMERAL_SALT


_EPHEMERAL_SALT = os.urandom(16)


def digest(call_id):
    """통화ID → 지문. 원문은 어디에도 남기지 않는다(역산 불가·외부 미노출).

    통화ID 는 CPaaS 가 주는 외부 식별자다. 형식이 보장되지 않으므로 번호 같은 값이
    섞여 들어와도 저장소에 원문이 남지 않도록 항상 해시해서 키로 쓴다.
    """
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError("call_id 는 비어 있지 않은 문자열이어야 합니다")
    return hmac.new(_salt(), call_id.strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()[:12]


def _label(v, limit=32):
    """시나리오·테넌트 라벨 정규화. 숫자 나열(번호류)은 라벨로 받지 않는다."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    s = s[:limit]
    digits = sum(ch.isdigit() for ch in s)
    if digits >= 7:          # 전화번호·주민번호가 라벨 자리에 들어오는 것을 막는다
        return None
    return s


class _Store(object):
    """관측 저장소. 인스턴스 메모리 · 락으로 보호."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = {}        # ref -> 진행 중 통화
        self._done = []        # 종료된 통화(오래된 것이 앞)
        self._since = None     # 첫 관측 시각(관측 창 계산용)
        self.errors = 0        # 수집 중 삼킨 예외 수(드러낸다)
        self.orphan_finishes = 0   # start 없이 도착한 종료 이벤트
        self.dropped = 0       # 상한·보관기간으로 버린 관측치

    # -- 기록 -------------------------------------------------------------
    def start(self, call_id, scenario=None, tenant=None, ts=None):
        ref = digest(call_id)
        t = float(ts if ts is not None else _now())
        with self._lock:
            if self._since is None or t < self._since:
                self._since = t
            cur = self._open.get(ref)
            if cur is not None:
                # answered 재배달 — 새 통화로 세지 않는다(중복 집계 금지).
                return ref
            if len(self._open) >= MAX_OPEN:
                oldest = min(self._open, key=lambda k: self._open[k]["started"])
                self._open.pop(oldest, None)
                self.dropped += 1
            self._open[ref] = {"started": t, "turns": 0, "outcome": None,
                               "scenario": _label(scenario), "tenant": _label(tenant)}
        return ref

    def mark_turn(self, call_id):
        ref = digest(call_id)
        with self._lock:
            rec = self._open.get(ref)
            if rec is None:
                return False
            if rec["turns"] < MAX_TURNS:
                rec["turns"] += 1
            return True

    def mark_outcome(self, call_id, outcome):
        """종료 전에 확정되는 사유(예: 상담사 전환)를 미리 못 박는다."""
        if outcome not in OUTCOMES:
            raise ValueError("미지의 outcome: %r" % (outcome,))
        ref = digest(call_id)
        with self._lock:
            rec = self._open.get(ref)
            if rec is None:
                return False
            rec["outcome"] = outcome
            return True

    def finish(self, call_id, outcome=None, ts=None):
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError("미지의 outcome: %r" % (outcome,))
        ref = digest(call_id)
        t = float(ts if ts is not None else _now())
        with self._lock:
            rec = self._open.pop(ref, None)
            if rec is None:
                # 종료만 도착 — 시작을 못 본 통화는 '있었던 것으로' 세지 않는다.
                # (지속시간·턴수를 지어내야 하기 때문). 사실은 카운터로 남긴다.
                self.orphan_finishes += 1
                return None
            # 종료 사유를 못 받았을 때의 판정 규칙(지어내지 않고 **관측으로만** 정한다):
            #   상담사 전환이 찍혀 있으면 transferred,
            #   봇이 한 턴이라도 응대하고 끝났으면 bot_completed(= 연결 없이 종료),
            #   응대 턴이 0이면 abandoned(대화가 성립하지 않았다).
            # '봇 자동완결'을 봇의 성공으로 부풀리지 않으려면 이 정의를 소비자도 알아야
            # 하므로 ops_stats 응답에 `definition` 으로 함께 싣는다.
            final = outcome or rec["outcome"] or (
                "bot_completed" if rec["turns"] > 0 else "abandoned")
            dur = t - rec["started"]
            if dur < 0:
                dur = 0.0
            item = {"ended": t, "started": rec["started"], "dur": dur,
                    "turns": rec["turns"], "outcome": final,
                    "scenario": rec["scenario"], "tenant": rec["tenant"]}
            self._done.append(item)
            if len(self._done) > MAX_RECORDS:
                del self._done[:len(self._done) - MAX_RECORDS]
                self.dropped += 1
            return dict(item)

    # -- 조회 -------------------------------------------------------------
    def _prune(self, now):
        cut = now - RETENTION_SEC
        if self._done and self._done[0]["ended"] < cut:
            keep = [r for r in self._done if r["ended"] >= cut]
            self.dropped += len(self._done) - len(keep)
            self._done = keep

    def summary(self, period="today", now=None):
        t = float(now if now is not None else _now())
        span = PERIOD_SEC.get(period if isinstance(period, str) else "", PERIOD_SEC["today"])
        with self._lock:
            self._prune(t)
            rows = [r for r in self._done if r["ended"] >= t - span]
            since = self._since
            open_n = len(self._open)
            errors, orphans, dropped = self.errors, self.orphan_finishes, self.dropped
        n = len(rows)
        counts = {k: 0 for k in OUTCOMES}
        for r in rows:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        # 관측 창: 첫 관측부터 지금까지. 요청 기간보다 짧으면 부분 표본이다.
        window = 0.0 if since is None else max(0.0, t - since)
        window = min(window, float(span))
        payload = {
            "data_source": "measured",   # 데모와 섞이지 않는 표식
            "period": period if isinstance(period, str) and period in PERIOD_SEC else "today",
            "sample_size": n,
            "window_sec": int(window),
            # 관측 창이 요청 기간을 못 채웠으면 부분 표본 — 기간 값으로 읽히면 안 된다.
            "partial": bool(n == 0 or window < span - 1),
            "calls": {
                "total": n,
                "bot_completed": counts["bot_completed"],
                "transferred": counts["transferred"],
                "abandoned": counts["abandoned"],
                "failed": counts["failed"],
                "in_progress": open_n,
            },
            # 표본이 없으면 비율·평균은 null. 0 으로 내리지 않는다(0도 주장이다).
            "auto_rate": (round(counts["bot_completed"] / n, 3) if n else None),
            "transfer_rate": (round(counts["transferred"] / n, 3) if n else None),
            "avg_duration_sec": (round(sum(r["dur"] for r in rows) / n, 1) if n else None),
            "avg_turns": (round(sum(r["turns"] for r in rows) / n, 2) if n else None),
            "collector": {
                "errors": errors,              # 삼킨 수집 예외 — 0 이 아니면 조사 대상
                "orphan_finishes": orphans,    # 시작을 못 본 종료 이벤트
                "dropped": dropped,            # 상한·보관기간으로 버린 관측치
                "retention_sec": RETENTION_SEC,
                "scope": "instance",           # 인스턴스 메모리 — 클러스터 합산 아님
            },
        }
        return payload

    def reset(self):
        with self._lock:
            self._open.clear()
            self._done = []
            self._since = None
            self.errors = 0
            self.orphan_finishes = 0
            self.dropped = 0


SINK = _Store()   # 교체점: 영속 집계·실 CTI 배선 시 여기만 갈아 끼운다 [승인 필요]


# -- 호출부용 안전 래퍼 ----------------------------------------------------
# 통화 처리 경로에서 쓰인다. 수집 실패가 통화를 끊으면 안 되므로 예외를 전파하지
# 않되, **삼킨 사실은 카운터로 드러낸다**(조용한 실패 금지).
def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        try:
            SINK.errors += 1
        except Exception:
            pass
        return None


def start(call_id, scenario=None, tenant=None, ts=None):
    return _safe(SINK.start, call_id, scenario=scenario, tenant=tenant, ts=ts)


def mark_turn(call_id):
    return _safe(SINK.mark_turn, call_id)


def mark_outcome(call_id, outcome):
    return _safe(SINK.mark_outcome, call_id, outcome)


def finish(call_id, outcome=None, ts=None):
    return _safe(SINK.finish, call_id, outcome=outcome, ts=ts)


def summary(period="today", now=None):
    """집계 조회. 조회 자체는 실패를 숨기지 않는다(호출부가 ops_stats 한 곳)."""
    return SINK.summary(period=period, now=now)


def reset():
    SINK.reset()


if __name__ == "__main__":
    import json as _json

    reset()
    s = summary()
    assert s["sample_size"] == 0 and s["auto_rate"] is None, s
    assert s["partial"] is True and s["data_source"] == "measured"
    assert s["calls"]["total"] == 0 and s["calls"]["in_progress"] == 0

    t0 = 1_000_000.0
    start("c1", scenario="refund", ts=t0)
    mark_turn("c1"); mark_turn("c1")
    finish("c1", outcome="bot_completed", ts=t0 + 40)
    start("c2", scenario="refund", ts=t0 + 1)
    mark_outcome("c2", "transferred")
    finish("c2", ts=t0 + 61)
    s = summary(now=t0 + 100)
    assert s["sample_size"] == 2, s
    assert s["calls"]["bot_completed"] == 1 and s["calls"]["transferred"] == 1
    assert s["auto_rate"] == 0.5 and s["transfer_rate"] == 0.5
    assert s["avg_duration_sec"] == 50.0 and s["avg_turns"] == 1.0

    # 사유 없는 종료: 응대 턴이 있으면 bot_completed, 없으면 abandoned
    start("c9", ts=t0 + 1); mark_turn("c9"); finish("c9", ts=t0 + 9)
    assert summary(now=t0 + 100)["calls"]["bot_completed"] == 2

    # 중복 answered 는 새 통화가 아니다
    start("c3", ts=t0 + 2); start("c3", ts=t0 + 3)
    assert summary(now=t0 + 100)["calls"]["in_progress"] == 1
    finish("c3", ts=t0 + 10)
    assert summary(now=t0 + 100)["sample_size"] == 4
    assert summary(now=t0 + 100)["calls"]["abandoned"] == 1

    # 시작을 못 본 종료는 세지 않고 카운터로 드러낸다
    assert finish("never-started") is None
    assert summary()["collector"]["orphan_finishes"] == 1

    # 미지 outcome 은 거부(조용히 바꾸지 않는다) — 래퍼는 삼키되 카운터가 오른다
    try:
        SINK.mark_outcome("c1", "weird")
        raise AssertionError("미지 outcome 이 통과했다")
    except ValueError:
        pass
    before = summary()["collector"]["errors"]
    mark_outcome("cX", "weird")
    assert summary()["collector"]["errors"] == before + 1

    # 통화ID 원문은 저장되지 않는다
    reset()
    start("01012345678", ts=t0)
    finish("01012345678", outcome="failed", ts=t0 + 5)
    blob = _json.dumps(SINK._done, ensure_ascii=False)
    assert "01012345678" not in blob and "1234" not in blob, blob
    assert digest("a") != digest("b") and len(digest("a")) == 12

    # 번호형 라벨은 라벨로 받지 않는다
    assert _label("01012345678") is None and _label("refund") == "refund"

    reset()
    print("call_metrics selftest OK:", _json.dumps(summary(), ensure_ascii=False)[:140], "...")
