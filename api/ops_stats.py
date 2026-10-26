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
- B151: 회귀(tests/test_ops_stats.py)로 3건 수정.
  (a) 감사 기록을 **결과가 확정된 뒤**에 남긴다 — 이전에는 응답 생성 전에 allow/200 을
      먼저 적어, 500 으로 끝난 요청도 감사에는 성공으로 남고 400 은 아예 기록되지 않았다.
  (b) 감사 호출 실패가 요청을 죽이지 않는다(가용성 우선). 이전에는 try 안이라 500 이 됐다.
  (c) period 가 문자열이 아니면(list·dict 등) `in` 이 TypeError 를 내던 것을 today 로 폴백.
- `data_source="demo"` 를 응답에 함께 실어 **수치가 실측이 아님을 소비자가 알 수 있게** 한다.
  (새 수치를 추가하는 것이 아니라, 기존 데모 수치에 정직한 꼬리표를 붙이는 것)
- B160: **실측 블록(`measured`)** 추가 — `call_metrics` 가 이 인스턴스에서 실제로 처리한
  통화만 세어 둔 값. 데모 수치와 **섞지 않고 별도 블록**으로 내보낸다. 표본 0건이면
  비율은 `null`(0.0 으로 내리지 않는다). 헤드라인 수치는 여전히 `data_source="demo"`.
- 36차: `?op=escalations` — **전환 티켓 목록**(읽기 전용). 집계 숫자만으로는
  "누가 무엇 때문에 기다리는지" 알 수 없어 32~35차가 네 번 미룬 자리다.
  내보내는 칸은 화이트리스트(`TICKET_KEYS`)뿐이고 전부 저장소가 마스킹한 값이다.
  같은 회차에 `ESCALATION_KEYS` 결함도 고쳤다 — 큐가 세던 `audit_dropped`·
  `summary_dropped` 가 어디에도 나오지 않았다(세어 두고 보여주지 않으면 센 것이
  아니다). 재발 방지는 릴리스 게이트 17단계 `counter_exposure`.
- 실 CTI·실통계 연동은 [승인 필요] — 여기서는 sim 집계만.

사용:
  from ops_stats import get_ops_summary
  get_ops_summary()                    # dict (오늘)
  get_ops_summary(period="week")      # 주간(데모)
  get_escalations(state="open")       # 전환 티켓(실측 · 읽기 전용)

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
# 26차: 상한으로 버린 티켓(`dropped`·그중 처리 전이던 `dropped_waiting`)과
# 기록 실패(`record_errors`)를 함께 내보낸다 — 세어 두고 보여주지 않으면
# 센 것이 아니다(큐는 인스턴스 메모리라 유실이 실제로 일어난다).
# 36차 결함 수정: `audit_dropped`·`summary_dropped` 가 **세어지기만 하고**
# 어디에도 나오지 않았다. 큐는 둘 다 센다(`_escalation.stats()`) — 전이 기록을
# 상한으로 버린 수, 마스킹이 불가능해 싣지 않은 요약 수. 세어 두고 보여주지
# 않으면 센 것이 아니다(26·31·35차가 같은 자리에서 반복한 규약이고, 바로 옆
# `RECORDING_KEYS` 는 `audit_dropped` 를 이미 내보내고 있었다 — 절반만 지켜진
# 규칙이다). 재발 방지는 릴리스 게이트 17단계 `counter_exposure`.
# 39차: `expired`(만료로 이탈 처리한 미처리 티켓)·`open_ttl_sec`(그 기준) 추가.
# 전이 배선이 없는 동안 **모든** 티켓이 `queued` 로 남아 대기 숫자가 단조 증가했다 —
# 이제 만료되므로, 어디로 갔는지 말하지 않으면 숫자가 조용히 줄어든 것으로 읽힌다.
ESCALATION_KEYS = ("queued", "assigned", "resolved", "abandoned", "total",
                   "dropped", "dropped_waiting", "audit_dropped",
                   "summary_dropped", "record_errors", "expired",
                   "open_ttl_sec")
# 숫자가 아닌 '성질' 칸 — 소스를 못 읽어도 구현의 성질은 변하지 않으므로 기본값이 있다.
ESCALATION_META = {"scope": "instance", "volatile": True}
# 31차: 녹취 장부도 상한이 생겼다(인스턴스 메모리). 버린 건수를 내보내지 않으면
# 조용한 삭제가 된다 — `dropped_active` 가 0 이 아니면 **아직 보관기간이 남은**
# 녹취 메타가 장부에서 사라졌다는 뜻이라 운영자가 알아야 한다.
RECORDING_KEYS = ("active", "purged", "total",
                  "dropped", "dropped_active", "audit_dropped")
RECORDING_META = {"scope": "instance", "volatile": True}
GATE_FLAGS = ("RECORDING_LIVE", "CPAAS_LIVE", "SPEECH_LIVE")


def _safe_stats(mod_name, obj_name):
    """sim 스토어 stats() 를 best-effort 로 수집(실패해도 집계는 계속)."""
    try:
        mod = __import__(mod_name)
        return getattr(mod, obj_name).stats()
    except Exception:
        return None


def _norm_stats(raw, keys, meta=None):
    """B146: sim stats 를 고정 스키마·정수로 정규화.

    소스를 못 읽었으면 전부 0 + source="unavailable". 예외를 던지지 않는다.
    `meta` 는 숫자가 아닌 성질 칸({키: 기본값}) — 소스가 같은 키를 주면 그 값을,
    없거나 못 읽으면 기본값을 쓴다(성질은 소스 장애로 바뀌지 않는다).
    """
    ok = isinstance(raw, dict)
    out = {}
    for k in keys:
        v = raw.get(k) if ok else None
        try:
            out[k] = int(v)
        except Exception:
            out[k] = 0
    for k, default in (meta or {}).items():
        v = raw.get(k) if ok else None
        out[k] = default if v is None else v
    out["source"] = "sim" if ok else "unavailable"
    return out


# B160: 실측 블록 고정 스키마 — 수집기가 없거나 죽어도 콘솔이 깨지지 않는다.
MEASURED_CALL_KEYS = ("total", "bot_completed", "transferred", "abandoned",
                      "failed", "in_progress")
MEASURED_RATE_KEYS = ("auto_rate", "transfer_rate", "avg_duration_sec", "avg_turns")
# 39차: 수집기 카운터 노출 목록. `_call_metrics.stats()` 가 만드는 칸과 **같아야**
# 하고, 어긋나면 릴리스 게이트 `counter_exposure` 가 실패한다. 이 자리가 그
# 게이트 밖에 있던 동안 `stale_opens`(종료 이벤트 유실로 만료시킨 관측치)처럼
# 새로 세기 시작한 손실이 응답에서 빠질 수 있었다 — 세어 두고 보여주지 않으면
# 센 것이 아니다(26·31·35·36차와 같은 규약).
MEASURED_COLLECTOR_KEYS = ("errors", "orphan_finishes", "dropped", "stale_opens",
                           "retention_sec", "open_ttl_sec", "scope")
# 숫자가 아닌 '성질' 칸 — 수집기를 못 읽어도 구현의 성질은 변하지 않는다.
MEASURED_COLLECTOR_DEFAULTS = {"scope": "instance"}

# 소비자가 '봇 자동완결'을 과대해석하지 않도록 판정 정의를 함께 싣는다.
MEASURED_DEFINITION = {
    "bot_completed": "상담사 전환 없이 봇 응대 턴이 1회 이상 있었고 통화가 종료됨",
    "transferred": "엔진이 상담사 전환을 결정함",
    "abandoned": "봇 응대 턴 없이 통화가 종료됨",
    "failed": "통화가 연결되지 못하거나 오류로 종료됨",
}


def _measured(period):
    """실측 관측치(call_metrics). 수집기 장애가 대시보드를 죽이지 않는다.

    비어 있어도 **데모 값으로 채우지 않는다** — 없는 것은 없는 대로 0/null 이다.
    """
    raw = None
    try:
        import _call_metrics as call_metrics
        raw = call_metrics.summary(period=period)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {"data_source": "unavailable", "period": period, "sample_size": 0,
                "window_sec": 0, "partial": True,
                "calls": {k: 0 for k in MEASURED_CALL_KEYS},
                **{k: None for k in MEASURED_RATE_KEYS},
                "collector": {k: MEASURED_COLLECTOR_DEFAULTS.get(k, 0)
                              for k in MEASURED_COLLECTOR_KEYS},
                "definition": dict(MEASURED_DEFINITION)}
    raw = dict(raw)
    raw["definition"] = dict(MEASURED_DEFINITION)
    return raw


# --------------------------------------------------------------------------
# 활성화 게이트 현황 (읽기 전용) — 37차 결함 수정
#
# 이 자리가 게이트 값을 **자기 규칙으로 다시 해석**하고 있었다
# (`("1","true","on","yes")`). 그런데 그 게이트로 **실제로 동작하는** 모듈은
# 다른 규칙을 쓴다 — `CPAAS_LIVE`·`SPEECH_LIVE` 는 `"1"` 정확 일치이고
# `RECORDING_LIVE` 만 `("1","true","on")` 이다. 어느 쪽과도 같지 않았다
# (`yes` 는 아무도 받지 않는다).
#
# 그래서 `CPAAS_LIVE=true` 를 넣으면 **콘솔 모니터는 「CPAAS_LIVE ON」,
# 코드는 전부 OFF** 였다. 같은 화면의 발신번호 패널은 `caller_id.cpaas_live()`
# 를 읽어 「실발신 게이트 OFF」라고 적는다 — 한 화면이 같은 환경변수를 두고
# 서로 다른 말을 했다. 게이트 현황은 운영자가 "지금 실발신·과금이 일어나는가"를
# 판단하는 자리다. 거기서 틀리면 켰다고 믿고 안 켜졌거나, 껐다고 믿고 안 꺼진다.
#
# 고친 방법: 판정을 **복제하지 않는다**. 그 게이트로 동작하는 모듈의 술어를
# 부른다(36차 `OPEN_STATES` 와 같은 규약 — 정의는 소유자가 갖는다).
# 재발 방지는 릴리스 게이트 18단계 `live_gates`.
# --------------------------------------------------------------------------
_GATE_OWNERS = {
    # 게이트: (소유 모듈, 판정 술어, 그 술어의 규칙 낱말 — 폴백도 같은 규칙을 쓴다)
    "RECORDING_LIVE": ("_recording_audit", "recording_live", ("1", "true", "on")),
    "CPAAS_LIVE": ("_sip_adapter", "is_live", ("1",)),
    "SPEECH_LIVE": ("_speech_providers", "is_live", ("1",)),
}


def _owner_flag(name):
    """소유 모듈의 술어를 **호출 시점에** 읽는다. 못 읽으면 None(모름).

    읽기 전용이다 — 술어는 어느 것도 환경변수를 쓰지 않는다(회귀로 고정).
    """
    mod, attr, _ = _GATE_OWNERS[name]
    try:
        return bool(getattr(__import__(mod), attr)())
    except Exception:
        return None


def _flag(name):
    """소유 모듈을 못 불러왔을 때의 폴백 — **그 게이트의 규칙 그대로** 읽는다.

    여기서 규칙을 넓히면(예전의 `"yes"`) 화면이 코드와 다른 말을 한다.
    환경 조회 자체가 터지면 OFF 로 답한다 — 장애가 스위치를 켜지 않는다.
    """
    words = _GATE_OWNERS[name][2]
    try:
        return str(os.environ.get(name, "")).strip().lower() in words
    except Exception:
        return False


def gate_flags():
    """B146: 활성화 게이트 현황(읽기 전용). 값 변경 없음."""
    out = {}
    for n in GATE_FLAGS:
        v = _owner_flag(n)
        out[n.lower()] = _flag(n) if v is None else v
    return out


# HTTP 계층 입력검증용 화이트리스트 — 데모 기간 정의와 같은 출처를 쓴다
PERIODS = tuple(DEMO_PERIODS.keys())

# --------------------------------------------------------------------------
# 전환 티켓 조회(36차) — `GET /api/ops_stats?op=escalations`
#
# 왜 여기인가: 큐(`_escalation.QUEUE`)는 "상담사에게 연결해 드리겠습니다"라고
# 고객에게 **말한 약속**의 유일한 기록인데 **사람이 볼 수 있는 곳이 없었다**
# (32~35차가 네 번 미룬 자리다). 집계 숫자(`escalation.queued=3`)만으로는
# 누가 무엇 때문에 기다리는지 알 수 없다. 새 함수 파일은 만들지 않는다 —
# Vercel Hobby 12개 한도가 이미 꽉 찼으므로 기존 라우트의 하위 동작이다.
#
# 내보내는 것은 **저장소가 이미 마스킹해 둔 칸**뿐이고, 그중에서도 아래
# 화이트리스트에 적은 칸만 나간다(저장소에 칸이 하나 늘어도 자동으로 새지
# 않는다). 이 경로는 읽기 전용이다 — 상태 전이·배정은 여기서 하지 않는다
# (실 CTI 연동은 [승인 필요]).
# --------------------------------------------------------------------------
OPS = ("summary", "escalations")          # ?op= 화이트리스트(기본 summary)
TICKET_STATES = ("queued", "assigned", "resolved", "abandoned")
# `open` = 아직 사람이 집어가지 않은 것(queued+assigned). 운영자가 가장 자주
# 묻는 질문이라 이름을 따로 둔다 — 정의는 `_escalation.OPEN_STATES` 가 갖는다.
TICKET_FILTER_STATES = TICKET_STATES + ("open",)
TICKET_ORDERS = ("oldest", "newest")      # 기본은 큐 순서(오래 기다린 사람 먼저)
TICKET_LIMIT_DEFAULT = 50
TICKET_LIMIT_MAX = 200
# 티켓 1건에서 내보내는 칸(화이트리스트). `session_id` 는 통화 참조라
# 바깥 이름을 `call_ref` 로 바꿔 둔다(세션 쿠키·로그인 세션으로 오해 방지).
TICKET_KEYS = ("id", "state", "reason", "scenario", "call_ref",
               "summary", "summary_dropped", "ts", "age_sec")
_TICKET_FIELD_MAX = 200                   # 저장소 상한과 별개의 2차 방어선
_FALLBACK_OPEN_STATES = ("queued", "assigned")


def _esc_module():
    """`_escalation` 모듈(없으면 None). 조회 하나가 대시보드를 죽이지 않는다."""
    try:
        return __import__("_escalation")
    except Exception:          # pragma: no cover - 모듈 부재 폴백
        return None


def _cell(v):
    """티켓 한 칸 — 문자열로 접고 길이를 다시 막는다.

    저장소가 이미 `_label`/`_summary` 로 마스킹·절단했다. 여기서 또 막는 것은
    그 규약이 깨졌을 때 **HTTP 로 새지 않게** 하려는 2차 방어선이다.
    """
    if v is None:
        return ""
    try:
        s = v if isinstance(v, str) else str(v)
    except Exception:
        return "-"
    return s[:_TICKET_FIELD_MAX]


def _ticket_view(tk, now):
    """티켓 dict → 화이트리스트 칸만. 어떤 입력에도 예외를 내지 않는다."""
    tk = tk if isinstance(tk, dict) else {}
    try:
        ts = float(tk.get("ts") or 0)
    except Exception:
        ts = 0.0
    age = None
    if ts > 0:
        age = int(now - ts)
        if age < 0:                      # 시계 역행 — 음수 경과시간은 뜻이 없다
            age = 0
    return {
        "id": _cell(tk.get("id")),
        "state": _cell(tk.get("state")),
        "reason": _cell(tk.get("reason")),
        "scenario": _cell(tk.get("scenario")),
        "call_ref": _cell(tk.get("session_id")),
        "summary": _cell(tk.get("summary")),
        # 빈 요약의 뜻을 말한다 — True 면 "가릴 수 없어 버렸다"(모델이 요약을
        # 주지 않은 것과 다르다). 옛 티켓에 칸이 없으면 False.
        "summary_dropped": bool(tk.get("summary_dropped")),
        "ts": int(ts),
        "age_sec": age,
    }


def get_escalations(state=None, limit=TICKET_LIMIT_DEFAULT, order="oldest"):
    """전환 티켓 목록 — 읽기 전용. **실측이다**(데모 수치가 아니다).

    큐를 못 읽으면 빈 목록 + `data_source="unavailable"` 로 사실대로 말한다 —
    데모 티켓을 지어내지 않는다.
    """
    mod = _esc_module()
    try:
        limit = max(1, min(int(limit), TICKET_LIMIT_MAX))
    except Exception:
        limit = TICKET_LIMIT_DEFAULT
    if order not in TICKET_ORDERS:
        order = "oldest"
    open_states = tuple(getattr(mod, "OPEN_STATES", _FALLBACK_OPEN_STATES) or
                        _FALLBACK_OPEN_STATES)
    raw = None
    if mod is not None:
        try:
            raw = mod.QUEUE.list()         # ts 오름차순(큐 순서)
        except Exception:
            raw = None
    now = time.time()
    if raw is None:
        tickets, matched, source = [], 0, "unavailable"
    else:
        source = "sim"
        if state == "open":
            wanted = open_states
        elif state:
            wanted = (state,)
        else:
            wanted = None
        sel = [t for t in raw
               if wanted is None or (t or {}).get("state") in wanted]
        matched = len(sel)
        if order == "newest":
            sel = list(reversed(sel))
        tickets = [_ticket_view(t, now) for t in sel[:limit]]
    return {
        "ok": True,
        "ts": int(now),
        "op": "escalations",
        "mode": "sim",                     # 실 CTI 배정 없음(상태 전이만)
        # 데모 블록과 달리 **이 인스턴스가 실제로 적은 티켓**이다.
        "data_source": "measured" if source == "sim" else "unavailable",
        "source": source,
        "scope": "instance",               # 클러스터 합산 아님
        "volatile": True,                  # 재시작하면 대기 티켓이 사라진다
        "open_states": list(open_states),
        "filter": {"state": state, "limit": limit, "order": order},
        "matched": matched,                # 필터에 걸린 전체 건수(실측)
        "count": len(tickets),             # 이번 응답에 실린 건수
        # 잘랐으면 **잘랐다고 말한다** — 목록이 조용히 짧아지면 운영자는
        # 큐가 작아진 것으로 읽는다(`?op=failures` 의 `dropped` 와 같은 규약).
        "truncated": matched > len(tickets),
        "stats": _norm_stats(_safe_stats("_escalation", "QUEUE"),
                             ESCALATION_KEYS, ESCALATION_META),
        "tickets": tickets,
    }


def get_ops_summary(baseline=None, period="today"):
    # 화이트리스트 밖은 today. 문자열이 아닌 값(list/dict)은 `in` 이 TypeError 를
    # 낼 수 있으므로 타입부터 확인한다 — 조회 하나가 500 으로 번지지 않게.
    if not isinstance(period, str) or period not in DEMO_PERIODS:
        period = "today"
    b = dict(DEMO_BASELINE)
    if baseline:
        b.update(baseline)
    p = DEMO_PERIODS[period]
    calls_today = int(b["calls_today"])
    total = int(p.get("calls", calls_today))     # 기간 합계(오늘=일 값)
    auto_rate = float(p.get("auto_rate", b["auto_rate"]))
    auto_done = int(round(total * auto_rate))
    esc = _norm_stats(_safe_stats("_escalation", "QUEUE"), ESCALATION_KEYS,
                      ESCALATION_META)
    rec = _norm_stats(_safe_stats("_recording_audit", "STORE"), RECORDING_KEYS,
                      RECORDING_META)
    return {
        "ok": True,
        "ts": int(time.time()),
        "mode": "sim",  # 실통계 연동 전까지 항상 sim
        # 소비자가 수치의 출처를 오해하지 않도록 명시한다. 실집계 배선 시 "live".
        "data_source": "demo",
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
        # 프로세스 내 sim 큐/저장소 현황(서버리스에선 인스턴스 단위 참고치).
        # escalation 은 `scope`/`volatile` 로 **재시작 시 대기 티켓이 사라진다**는
        # 사실을 함께 말한다 — 숫자만 보면 영속 큐로 읽힌다.
        "escalation": esc,
        "recording": rec,
        # B146: 활성화 게이트 현황(읽기 전용 · 여기서 켜지 않음)
        "gates": gate_flags(),
        # B160: 이 인스턴스가 실제로 처리한 통화의 관측치(데모와 분리).
        # 위쪽 calls/wait/sla 는 데모다 — 둘을 합치지 않는다.
        "measured": _measured(period),
    }


from http.server import BaseHTTPRequestHandler
import _guard
import _log
import _errors
import _monitoring as monitoring
try:
    import _audit          # 관리 기능 접근 감사 (부재해도 서비스는 뜬다)
except Exception:          # pragma: no cover
    _audit = None


class handler(BaseHTTPRequestHandler):
    # 기본 접근로그는 쿼리스트링(PII 가능)을 그대로 찍으므로 침묵 — 구조화 로그가 대체
    log_message = _log.suppress_access_log

    def _send(self, code, obj, rq=None):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if rq is not None:
            _log.attach(self, rq)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.send_header("Access-Control-Expose-Headers", "X-Request-Id")
        self.end_headers()

    def do_GET(self):
        rq = _log.begin(self.headers, "/api/ops_stats", "GET", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            # 감사: 실패한 관리 기능 접근 시도가 오히려 추적 가치가 높다
            if _audit:
                try:
                    _audit.record_request(self.headers, self.path, "GET", "deny", _c,
                                          request_id=rq.request_id)
                except Exception:      # 감사 장애가 요청을 죽이지 않는다
                    pass
            # 로그는 _guard.deny → _errors.send 가 봉투의 code 와 함께 닫는다.
            return _guard.deny(self, _c, _m, rq)
        period = None
        op = None
        try:
            # 입력검증: 전부 화이트리스트. 미지정은 기본값, 그 외 값은 400.
            q = parse_qs(urlparse(self.path).query)
            op = _errors.query_choice(q, "op", OPS, default="summary")
            rq.set(op=op)
            if op == "escalations":
                # 전환 티켓 목록(36차). 읽기 전용 — 상태 전이는 하지 않는다.
                state = _errors.query_choice(q, "state", TICKET_FILTER_STATES)
                order = _errors.query_choice(q, "order", TICKET_ORDERS,
                                             default="oldest")
                limit = _errors.query_int(q, "limit",
                                          default=TICKET_LIMIT_DEFAULT,
                                          minimum=1, maximum=TICKET_LIMIT_MAX)
                summary = get_escalations(state=state, limit=limit, order=order)
            else:
                period = _errors.query_choice(q, "period", PERIODS,
                                              default="today")
                rq.set(period=period)
                summary = get_ops_summary(period=period)
        except Exception as e:
            # 표준 에러 봉투 + 모니터링(5xx만) + 구조화 로그를 한 번에 처리
            env = _errors.handle(self, e, route="/api/ops_stats", method="GET", rq=rq)
            st = 500
            try:
                st = int((env or {}).get("status") or 500)
            except Exception:
                pass
            # 실패도 사실대로 남긴다 — 감사가 실패를 성공으로 적으면 조사 근거가 무너진다.
            self._audit("GET", "error" if st >= 500 else "deny", st, rq,
                        period, op)
            return
        self._send(200, summary, rq)
        rq.finish(200)
        # 결과가 확정된 뒤에 기록한다(성공을 미리 적지 않는다).
        self._audit("GET", "allow", 200, rq, period, op)

    def _audit(self, method, result, status, rq, period=None, op=None):
        """관리 경로 접근 감사. 실패해도 요청에 전파하지 않는다(가용성 우선).

        `op` 를 함께 적는다 — 같은 경로가 집계 조회와 **전환 티켓 목록 조회**
        둘을 맡으므로, 액션명만으로는 "누가 대기 중인 고객 목록을 열람했는지"를
        감사에서 가려낼 수 없다(개인정보 열람 조사에서 그 구분이 핵심이다).
        """
        if not _audit:
            return
        try:
            extra = {}
            if period is not None:
                extra["period"] = period
            if op is not None:
                extra["op"] = op
            _audit.record_request(self.headers, self.path, method, result, status,
                                  request_id=getattr(rq, "request_id", None), **extra)
        except Exception:
            pass


if __name__ == "__main__":     # pragma: no cover
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
    for weird in (None, 3, ["week"], {"a": 1}):
        assert get_ops_summary(period=weird)["period"] == "today", weird
    assert s["data_source"] == "demo"

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
    # --- B160: 실측 블록 ---
    m = s["measured"]
    assert m["data_source"] in ("measured", "unavailable")
    for k in MEASURED_CALL_KEYS:
        assert isinstance(m["calls"][k], int), k
    # 표본이 없으면 비율은 null — 0.0 으로 내리거나 데모 값으로 채우지 않는다
    if m["sample_size"] == 0:
        assert all(m[k] is None for k in MEASURED_RATE_KEYS), m
    # 헤드라인은 여전히 데모 — 실측이 데모 자리에 끼어들지 않는다
    assert s["data_source"] == "demo" and s["calls"]["today"] == 214
    assert set(m["definition"]) == {"bot_completed", "transferred", "abandoned", "failed"}
    # 수집기가 죽어도 스키마는 그대로
    import builtins as _b
    _real = _b.__import__

    def _boom(name, *a, **kw):
        if name == "_call_metrics":
            raise RuntimeError("down")
        return _real(name, *a, **kw)
    _b.__import__ = _boom
    try:
        u = _measured("today")
    finally:
        _b.__import__ = _real
    assert u["data_source"] == "unavailable" and u["calls"]["total"] == 0
    assert all(u[k] is None for k in MEASURED_RATE_KEYS)
    # 수집기가 죽은 폴백도 **같은 칸 구성**이다(콘솔이 없는 칸을 읽지 않는다)
    assert set(u["collector"]) == set(MEASURED_COLLECTOR_KEYS), u["collector"]

    # --- 36차: 손실·오류 카운터 노출 ---
    for k in ("audit_dropped", "summary_dropped", "dropped", "dropped_waiting",
              "record_errors", "expired"):
        assert k in ESCALATION_KEYS, k
    # 39차: 실측 수집기 카운터도 같은 규약(게이트 `counter_exposure` 가 강제한다)
    import _call_metrics as _cm
    assert set(_cm.SINK.stats()) == set(MEASURED_COLLECTOR_KEYS), _cm.SINK.stats()

    # --- 36차: 전환 티켓 조회 ---
    import _escalation as _esc
    _esc.QUEUE.__init__()
    e0 = get_escalations()
    assert e0["ok"] and e0["op"] == "escalations" and e0["tickets"] == []
    assert e0["matched"] == 0 and e0["truncated"] is False
    assert e0["scope"] == "instance" and e0["volatile"] is True
    t1 = _esc.QUEUE.enqueue("call-aaa", "request", "고객 010-1234-5678 환불 문의")
    _esc.QUEUE.enqueue("call-bbb", "sensitive", "소송 언급")
    _esc.QUEUE.transition(t1["id"], "abandoned")
    e1 = get_escalations()
    assert e1["count"] == 2 and e1["data_source"] == "measured"
    assert set(e1["tickets"][0]) == set(TICKET_KEYS), e1["tickets"][0]
    assert "010-1234-5678" not in json.dumps(e1, ensure_ascii=False)
    assert "session_id" not in json.dumps(e1, ensure_ascii=False)
    assert e1["tickets"][0]["call_ref"] == "call-aaa"
    assert [t["id"] for t in get_escalations(order="newest")["tickets"]] == \
        [t["id"] for t in reversed(e1["tickets"])]
    assert get_escalations(state="open")["count"] == 1
    assert get_escalations(state="abandoned")["tickets"][0]["id"] == t1["id"]
    cut = get_escalations(limit=1)
    assert cut["count"] == 1 and cut["matched"] == 2 and cut["truncated"] is True
    for bad in (0, -5, "x", None, 9999):
        assert 1 <= get_escalations(limit=bad)["filter"]["limit"] <= TICKET_LIMIT_MAX, bad
    _esc.QUEUE.__init__()

    # 직렬화 가능
    json.dumps(s, ensure_ascii=False)
    print("ops_stats selftest OK:", json.dumps(w, ensure_ascii=False)[:160], "...")
