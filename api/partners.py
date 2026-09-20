# -*- coding: utf-8 -*-
"""파트너(채널) 귀속 대장 (/api/partners).

배경 — 운영 대행 파트너와 수익을 배분한다. 지금은 계약·서비스 주체가 우리이고
파트너는 영업·운영을 맡지만, 향후 리셀러(파트너 명의 계약)로 전환될 수 있다.
그래서 지금 필요한 것은 화이트라벨이 아니라 **"어느 고객사가 어느 파트너를 통해
언제부터 언제까지 귀속됐는가"를 나중에 다투지 않게 남기는 장부**다.

설계 원칙
  - **`partner_id` 는 nullable.** 없으면 직접 계약이다. 파트너가 붙지 않은 고객사를
    억지로 어딘가에 소속시키지 않는다.
  - **귀속은 덮어쓰지 않고 기간으로 쌓는다.** 담당 파트너가 바뀌면 이전 귀속을
    지우는 게 아니라 그 시점에 닫고 새 기간을 연다. 정산 분쟁의 9할은 "그때 누구
    담당이었냐"이고, 마지막 값만 남기는 스키마는 그 질문에 답하지 못한다.
    기간은 **겹치지 않고 빈틈도 없다**(불변식을 검사한다).
  - **유입 경로와 파트너 유무가 어긋날 수 없다.** `direct` 인데 파트너가 붙거나
    `partner_*` 인데 파트너가 없으면 거부한다 — 모순된 장부가 분쟁을 만든다.
  - **담당자는 최소 수집.** 이름은 마스킹해 저장하고 연락처(전화·이메일)는 아예
    받지 않는다. 장부의 목적은 귀속 증명이지 사람 명부가 아니다.
  - **역할은 만들되 켜지 않는다.** `partner_admin` 권한 판정(`authorize`)은 완전히
    구현·검증되지만, 실제 접근 통제 배선(`enforce`)은 `PARTNER_RBAC_LIVE=1` 전까지
    **[승인 필요]** 로 막혀 있다. 반쯤 배선된 채로 배포되지 않도록 예외로 드러낸다.
  - **수수료·정산 금액은 이 모듈이 계산하지 않는다.** 근거(기간·경로·계약일)만
    남긴다. 요율과 청구는 계약서 확정 후 별도 항목이다.
  - 저장은 인스턴스 메모리(휘발). 영속 저장소·관리자 인증 배선은 [승인 필요].

HTTP
  GET  /api/partners                          → 요약(파트너·고객사 집계·게이트)
  GET  /api/partners?op=list&status=          → 파트너 목록
  GET  /api/partners?op=accounts&partner=&channel=  → 고객사 귀속 현황
  GET  /api/partners?op=attribution&tenant=   → 특정 고객사의 귀속 기간 전체
  GET  /api/partners?op=policy                → 유입 경로·역할·상태 전이표
  GET  /api/partners?op=history               → 변경 이력(최근 100건)
  POST /api/partners {op:"create"|"suspend"|"resume"|"attach"|"reassign"|"detach"}

셀프테스트: python3 api/partners.py
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import threading
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAX_PARTNERS = 200
MAX_ACCOUNTS = 2000
HISTORY_MAX = 100
PERIODS_MAX = 50            # 한 고객사의 귀속 변경 상한(장부 폭주 방지)

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PARTNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_DIGITS7 = re.compile(r"\d{7,}")

PARTNER_STATUSES = ("active", "suspended")

# 유입 경로 — 화면·검사·정산이 같은 표를 쓴다(드리프트 방지).
# needs_partner: 이 경로는 파트너가 반드시 있어야/없어야 한다.
CHANNELS = {
    "direct":           {"label": "직접 계약",      "needs_partner": False},
    "partner_referral": {"label": "파트너 소개",    "needs_partner": True},
    "partner_managed":  {"label": "파트너 운영대행", "needs_partner": True},
    "inbound":          {"label": "문의 유입",      "needs_partner": False},
    "event":            {"label": "행사·전시",      "needs_partner": False},
    "expansion":        {"label": "기존 고객 확장",  "needs_partner": False},
}

# 역할 — scope 가 판정의 전부다. partner_admin 만 자기 파트너로 좁혀진다.
ROLES = {
    "owner":         {"label": "서비스 운영자",   "scope": "all",     "activated": True},
    "ops":           {"label": "운영 담당",       "scope": "all",     "activated": True},
    "viewer":        {"label": "조회 전용",       "scope": "all",     "activated": True},
    "partner_admin": {"label": "파트너 담당자",   "scope": "partner", "activated": False},
}

_LOCK = threading.Lock()
_PARTNERS: dict = {}        # partner_id -> record
_ACCOUNTS: dict = {}        # tenant_id  -> {tenant_id, contracted_at, periods[]}
_HISTORY: list = []


# --------------------------------------------------------------------------
# 게이트 — 읽기만 한다. 이 모듈은 어떤 경로로도 권한을 켜지 않는다.
# --------------------------------------------------------------------------
def rbac_live() -> bool:
    return (os.environ.get("PARTNER_RBAC_LIVE") or "").strip() == "1"


def _now() -> float:
    return time.time()


def _iso(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


# --------------------------------------------------------------------------
# 검증 · 마스킹
# --------------------------------------------------------------------------
def validate_tenant_id(v) -> str:
    s = str(v or "").strip().lower()
    if not TENANT_RE.match(s):
        raise ValueError("소문자·숫자·_- 로 40자 이내여야 합니다")
    return s


def validate_partner_id(v) -> str:
    s = str(v or "").strip().lower()
    if not PARTNER_RE.match(s):
        raise ValueError("소문자·숫자·_- 로 40자 이내여야 합니다")
    return s


def mask_name(v) -> str:
    """담당자 이름 마스킹. 한글 3자 홍길동 → 홍*동, 2자 김철 → 김*.

    되돌릴 수 없는 단방향 처리다. 원문은 저장하지 않는다.
    """
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) == 1:
        return s
    if len(s) == 2:
        return s[0] + "*"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def reject_contact(field, v):
    """연락처는 받지 않는다 — 장부의 목적은 귀속 증명이지 사람 명부가 아니다."""
    s = str(v or "")
    if "@" in s or _DIGITS7.search(s.replace("-", "").replace(" ", "")):
        raise ValueError("연락처(전화·이메일)는 저장하지 않습니다. 이름만 입력하세요")


def _check_channel(channel, partner_id):
    """경로와 파트너 유무의 정합성. 모순된 장부를 애초에 만들지 않는다."""
    if channel not in CHANNELS:
        raise ValueError("허용값: %s" % ", ".join(sorted(CHANNELS)))
    needs = CHANNELS[channel]["needs_partner"]
    if needs and not partner_id:
        raise ValueError("'%s' 경로는 파트너 지정이 필요합니다" % CHANNELS[channel]["label"])
    if not needs and partner_id:
        raise ValueError("'%s' 경로에는 파트너를 붙일 수 없습니다" % CHANNELS[channel]["label"])


# --------------------------------------------------------------------------
# 파트너
# --------------------------------------------------------------------------
def create_partner(partner_id, name, owner="", note="", actor=None, now=None) -> dict:
    pid = validate_partner_id(partner_id)
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("파트너 이름은 필수입니다")
    if len(nm) > 60:
        raise ValueError("파트너 이름은 60자 이내")
    reject_contact("owner", owner)
    ts = _now() if now is None else float(now)
    with _LOCK:
        if pid in _PARTNERS:
            raise ValueError("이미 등록된 파트너 ID 입니다")
        if len(_PARTNERS) >= MAX_PARTNERS:
            raise ValueError("파트너 등록 한도(%d)를 초과했습니다" % MAX_PARTNERS)
        rec = {
            "id": pid,
            "name": nm,
            "status": "active",
            "owner_masked": mask_name(owner),
            "note": str(note or "").strip()[:200],
            "created_at": ts,
            "updated_at": ts,
        }
        _PARTNERS[pid] = rec
        _log_event("create", partner_id=pid, actor=actor, now=ts)
        return partner_view(rec)


def set_partner_status(partner_id, status, actor=None, note="", now=None) -> dict:
    if status not in PARTNER_STATUSES:
        raise ValueError("허용값: %s" % ", ".join(PARTNER_STATUSES))
    pid = validate_partner_id(partner_id)
    ts = _now() if now is None else float(now)
    with _LOCK:
        rec = _PARTNERS.get(pid)
        if rec is None:
            raise KeyError(pid)
        if rec["status"] == status:
            raise ValueError("이미 '%s' 상태입니다" % status)
        rec["status"] = status
        rec["updated_at"] = ts
        _log_event("suspend" if status == "suspended" else "resume",
                   partner_id=pid, actor=actor, note=note, now=ts)
        return partner_view(rec)


def partner_view(rec) -> dict:
    d = dict(rec)
    d["created_at"] = _iso(rec["created_at"])
    d["updated_at"] = _iso(rec["updated_at"])
    d["accounts"] = sum(1 for t in _ACCOUNTS.values()
                        if _current(t) and _current(t)["partner_id"] == rec["id"])
    return d


def list_partners(status=None) -> list:
    with _LOCK:
        out = [partner_view(r) for r in _PARTNERS.values()
               if not status or r["status"] == status]
    out.sort(key=lambda d: d["id"])
    return out


# --------------------------------------------------------------------------
# 귀속(attribution) — 기간으로 쌓는다
# --------------------------------------------------------------------------
def _current(acc):
    """열린(종료되지 않은) 귀속 기간. 없으면 None."""
    for p in reversed(acc["periods"]):
        if p["to_ts"] is None:
            return p
    return None


def _period(partner_id, channel, owner, reason, actor, ts):
    return {
        "partner_id": partner_id,
        "channel": channel,
        "owner_masked": mask_name(owner),
        "from_ts": ts,
        "to_ts": None,
        "reason": str(reason or "").strip()[:200],
        "actor": actor or "-",
        "recorded_at": ts,
    }


def attach(tenant_id, channel, partner_id=None, owner="", contracted_at=None,
           reason="", actor=None, now=None) -> dict:
    """고객사를 장부에 올린다(최초 귀속). `partner_id` 없으면 직접 계약."""
    tid = validate_tenant_id(tenant_id)
    pid = validate_partner_id(partner_id) if partner_id else None
    reject_contact("owner", owner)
    _check_channel(channel, pid)
    ts = _now() if now is None else float(now)
    contracted = float(contracted_at) if contracted_at else ts
    if contracted > ts + 86400:
        raise ValueError("계약일이 미래입니다")
    with _LOCK:
        if tid in _ACCOUNTS:
            raise ValueError("이미 등록된 고객사입니다. 담당 변경은 reassign 을 쓰세요")
        if len(_ACCOUNTS) >= MAX_ACCOUNTS:
            raise ValueError("고객사 등록 한도(%d)를 초과했습니다" % MAX_ACCOUNTS)
        if pid is not None and pid not in _PARTNERS:
            raise KeyError(pid)
        _ACCOUNTS[tid] = {
            "tenant_id": tid,
            "contracted_at": contracted,
            "periods": [_period(pid, channel, owner, reason, actor, contracted)],
        }
        _log_event("attach", tenant_id=tid, partner_id=pid, channel=channel,
                   actor=actor, note=reason, now=ts)
        return account_view(_ACCOUNTS[tid], ts)


def reassign(tenant_id, channel, partner_id=None, owner="", reason="",
             actor=None, now=None) -> dict:
    """담당 변경. 이전 기간을 같은 시점에 닫고 새 기간을 연다(빈틈·겹침 없음)."""
    tid = validate_tenant_id(tenant_id)
    pid = validate_partner_id(partner_id) if partner_id else None
    reject_contact("owner", owner)
    _check_channel(channel, pid)
    ts = _now() if now is None else float(now)
    with _LOCK:
        acc = _ACCOUNTS.get(tid)
        if acc is None:
            raise KeyError(tid)
        if pid is not None and pid not in _PARTNERS:
            raise KeyError(pid)
        cur = _current(acc)
        if cur is None:
            raise ValueError("귀속이 종료된 고객사입니다. attach 로 다시 올리세요")
        if cur["partner_id"] == pid and cur["channel"] == channel:
            raise ValueError("현재 귀속과 동일합니다")
        if ts < cur["from_ts"]:
            raise ValueError("변경 시점이 이전 귀속 시작일보다 빠릅니다")
        if len(acc["periods"]) >= PERIODS_MAX:
            raise ValueError("귀속 변경 한도(%d)를 초과했습니다" % PERIODS_MAX)
        cur["to_ts"] = ts
        acc["periods"].append(_period(pid, channel, owner, reason, actor, ts))
        _log_event("reassign", tenant_id=tid, partner_id=pid, channel=channel,
                   actor=actor, note=reason, now=ts)
        return account_view(acc, ts)


def detach(tenant_id, reason="", actor=None, now=None) -> dict:
    """해지. 기간을 닫기만 하고 지우지 않는다 — 지난 정산 근거는 남아야 한다."""
    tid = validate_tenant_id(tenant_id)
    ts = _now() if now is None else float(now)
    with _LOCK:
        acc = _ACCOUNTS.get(tid)
        if acc is None:
            raise KeyError(tid)
        cur = _current(acc)
        if cur is None:
            raise ValueError("이미 종료된 고객사입니다")
        if ts < cur["from_ts"]:
            raise ValueError("종료 시점이 귀속 시작일보다 빠릅니다")
        cur["to_ts"] = ts
        _log_event("detach", tenant_id=tid, partner_id=cur["partner_id"],
                   actor=actor, note=reason, now=ts)
        return account_view(acc, ts)


def attribution_at(tenant_id, at=None):
    """`at` 시점의 귀속 기간. 없으면 None. 정산이 기간별로 묻는 질문에 답한다."""
    tid = validate_tenant_id(tenant_id)
    ts = _now() if at is None else float(at)
    with _LOCK:
        acc = _ACCOUNTS.get(tid)
        if acc is None:
            return None
        for p in acc["periods"]:
            if p["from_ts"] <= ts and (p["to_ts"] is None or ts < p["to_ts"]):
                return _period_view(p)
    return None


def attribution_periods(tenant_id, frm=None, to=None) -> list:
    """`[frm, to)` 와 겹치는 귀속 기간들(원시 타임스탬프 포함 사본).

    `attribution_at` 은 한 시점만 답한다. 정산은 "이 달 안에서 담당이 언제
    바뀌었나"를 알아야 실적을 기간별로 쪼갤 수 있어서 경계가 필요하다.
    반환은 사본이라 호출자가 고쳐도 장부가 변하지 않는다(조회는 부작용 없음).
    미등록 고객사는 빈 목록 — 예외가 아니라 "근거 없음"으로 다룬다.
    """
    tid = validate_tenant_id(tenant_id)
    lo = None if frm is None else float(frm)
    hi = None if to is None else float(to)
    if lo is not None and hi is not None and hi < lo:
        raise ValueError("조회 종료가 시작보다 빠릅니다")
    out = []
    with _LOCK:
        acc = _ACCOUNTS.get(tid)
        if acc is None:
            return out
        for p in acc["periods"]:
            p_to = p["to_ts"]
            if lo is not None and p_to is not None and p_to <= lo:
                continue
            if hi is not None and p["from_ts"] >= hi:
                continue
            out.append({
                "partner_id": p["partner_id"],
                "channel": p["channel"],
                "owner_masked": p["owner_masked"],
                "from_ts": p["from_ts"],
                "to_ts": p_to,
                "attribution": "partner" if p["partner_id"] else "direct",
            })
    return out


def partner_name(partner_id) -> str:
    """파트너 표시명. 없으면 빈 문자열 — 없는 이름을 지어내지 않는다."""
    if not partner_id:
        return ""
    with _LOCK:
        rec = _PARTNERS.get(str(partner_id).strip().lower())
        return rec["name"] if rec else ""


def _period_view(p) -> dict:
    d = dict(p)
    d["from"] = _iso(p["from_ts"])
    d["to"] = _iso(p["to_ts"]) if p["to_ts"] else ""
    d["channel_label"] = CHANNELS.get(p["channel"], {}).get("label", p["channel"])
    d["attribution"] = "partner" if p["partner_id"] else "direct"
    d["open"] = p["to_ts"] is None
    d.pop("from_ts", None)
    d.pop("to_ts", None)
    d.pop("recorded_at", None)
    return d


def account_view(acc, at=None) -> dict:
    ts = _now() if at is None else float(at)
    cur = None
    for p in acc["periods"]:
        if p["from_ts"] <= ts and (p["to_ts"] is None or ts < p["to_ts"]):
            cur = p
            break
    return {
        "tenant_id": acc["tenant_id"],
        "contracted_at": _iso(acc["contracted_at"]),
        "partner_id": cur["partner_id"] if cur else None,
        "channel": cur["channel"] if cur else None,
        "attribution": ("partner" if cur["partner_id"] else "direct") if cur else "ended",
        "owner_masked": cur["owner_masked"] if cur else "",
        "active": cur is not None and cur["to_ts"] is None,
        "changes": len(acc["periods"]),
        "periods": [_period_view(p) for p in acc["periods"]],
    }


def list_accounts(partner_id=None, channel=None, at=None) -> list:
    ts = _now() if at is None else float(at)
    with _LOCK:
        items = [account_view(a, ts) for a in _ACCOUNTS.values()]
    if partner_id == "-":            # 직접 계약만
        items = [d for d in items if d["attribution"] == "direct"]
    elif partner_id:
        items = [d for d in items if d["partner_id"] == partner_id]
    if channel:
        items = [d for d in items if d["channel"] == channel]
    items.sort(key=lambda d: d["tenant_id"])
    return items


def check_invariants():
    """귀속 기간은 겹치지 않고 빈틈도 없다. 깨지면 정산 근거가 무너진다."""
    problems = []
    with _LOCK:
        for tid, acc in _ACCOUNTS.items():
            prev = None
            for p in acc["periods"]:
                if p["to_ts"] is not None and p["to_ts"] < p["from_ts"]:
                    problems.append("%s: 종료가 시작보다 빠름" % tid)
                if prev is not None:
                    if prev["to_ts"] is None:
                        problems.append("%s: 닫히지 않은 기간 뒤에 새 기간" % tid)
                    elif prev["to_ts"] != p["from_ts"]:
                        problems.append("%s: 기간 사이에 빈틈/겹침" % tid)
                prev = p
    return problems


# --------------------------------------------------------------------------
# 권한(RBAC) — 판정은 구현하되 배선은 승인 전까지 막는다
# --------------------------------------------------------------------------
def authorize(role, actor_partner_id, tenant_id, at=None) -> bool:
    """`role` 을 가진 주체가 `tenant_id` 를 볼 수 있는가. 순수 함수(부작용 없음).

    partner_admin 은 **그 시점에 자기 파트너로 귀속된** 고객사만 본다.
    직접 계약 고객사는 어떤 파트너에게도 보이지 않는다.
    """
    spec = ROLES.get(role)
    if spec is None:
        return False
    if spec["scope"] == "all":
        return True
    if not actor_partner_id:
        return False
    cur = attribution_at(tenant_id, at)
    if cur is None:
        return False
    return cur["partner_id"] == actor_partner_id


def visible_tenants(role, actor_partner_id=None, at=None) -> list:
    """조회 경로가 쓸 단일 진입점. 2계층(리셀러) 필터는 여기서만 끼어들면 된다."""
    ts = _now() if at is None else float(at)
    with _LOCK:
        tids = sorted(_ACCOUNTS)
    return [t for t in tids if authorize(role, actor_partner_id, t, ts)]


def enforce(role, actor_partner_id, tenant_id, at=None) -> bool:
    """실제 접근 통제 배선 지점. 승인 전에는 호출 자체가 실패한다.

    판정 로직이 조용히 '전부 허용'으로 동작해 반쯤 배선된 채 배포되는 일을 막는다.
    """
    if not rbac_live():
        raise RuntimeError(
            "[승인 필요] 파트너 역할 권한 통제는 아직 활성화되지 않았습니다 "
            "(PARTNER_RBAC_LIVE)")
    if not authorize(role, actor_partner_id, tenant_id, at):
        raise PermissionError("조회 권한이 없습니다")
    return True


# --------------------------------------------------------------------------
# 이력 · 요약
# --------------------------------------------------------------------------
def _log_event(action, tenant_id=None, partner_id=None, channel=None,
               actor=None, note="", now=None):
    """호출자가 _LOCK 을 잡은 상태에서 부른다."""
    _HISTORY.append({
        "at": _iso(_now() if now is None else now),
        "action": action,
        "tenant_id": tenant_id,
        "partner_id": partner_id,
        "channel": channel,
        "actor": actor or "-",
        "note": str(note or "").strip()[:200],
    })
    if len(_HISTORY) > HISTORY_MAX:
        del _HISTORY[:len(_HISTORY) - HISTORY_MAX]


def history(limit=50) -> list:
    n = max(1, min(int(limit or 50), HISTORY_MAX))
    with _LOCK:
        return [dict(h) for h in _HISTORY[-n:]]


def policy() -> dict:
    return {
        "channels": [{"value": k, **v} for k, v in sorted(CHANNELS.items())],
        "roles": [{"value": k, **v} for k, v in sorted(ROLES.items())],
        "partner_statuses": list(PARTNER_STATUSES),
        "limits": {"partners": MAX_PARTNERS, "accounts": MAX_ACCOUNTS,
                   "changes_per_account": PERIODS_MAX},
        "pii": "담당자 이름은 마스킹 저장, 연락처(전화·이메일)는 저장하지 않습니다",
        "settlement": "이 대장은 귀속 근거만 남깁니다. 수수료율·청구액은 계약 확정 후 별도 항목입니다",
    }


def summary(at=None) -> dict:
    ts = _now() if at is None else float(at)
    accounts = list_accounts(at=ts)
    by_partner = {}
    for a in accounts:
        if a["partner_id"]:
            by_partner[a["partner_id"]] = by_partner.get(a["partner_id"], 0) + 1
    return {
        "partners": {
            "total": len(_PARTNERS),
            "active": sum(1 for r in _PARTNERS.values() if r["status"] == "active"),
            "suspended": sum(1 for r in _PARTNERS.values() if r["status"] == "suspended"),
        },
        "accounts": {
            "total": len(accounts),
            "via_partner": sum(1 for a in accounts if a["attribution"] == "partner"),
            "direct": sum(1 for a in accounts if a["attribution"] == "direct"),
            "ended": sum(1 for a in accounts if a["attribution"] == "ended"),
        },
        "by_partner": by_partner,
        "rbac": {
            "active": rbac_live(),
            "role": "partner_admin",
            "note": ("활성" if rbac_live() else
                     "[승인 필요] 판정 로직은 구현·검증되었으나 접근 통제 배선은 꺼져 있습니다"),
        },
        "integrity": check_invariants(),
        "persistence": "instance-memory (휘발) — 영속 저장소 배선은 [승인 필요]",
        "generated_at": _iso(ts),
    }


def _clear_for_tests():
    with _LOCK:
        _PARTNERS.clear()
        _ACCOUNTS.clear()
        del _HISTORY[:]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
import _guard      # noqa: E402
import _log        # noqa: E402
import _errors     # noqa: E402
try:
    import _audit
except Exception:          # pragma: no cover
    _audit = None

OPS = ("create", "suspend", "resume", "attach", "reassign", "detach")


def _audit_safe(headers, path, method, result, status, rid):
    if not _audit:
        return
    try:
        _audit.record_request(headers, path, method, result, status, request_id=rid)
    except Exception:      # 감사 장애가 요청을 죽이지 않는다
        pass


class handler(BaseHTTPRequestHandler):
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

    def _gate(self, rq, method):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            _audit_safe(self.headers, self.path, method, "deny", _c, rq.request_id)
            rq.finish(_c, denied=True)
            _guard.deny(self, _c, _m, rq)
            return False
        return True

    def _actor(self):
        if not _audit:
            return None
        try:
            a = _audit.actor(self.headers)      # 키 원문 아님(지문·오리진만)
            return "%s:%s" % (a.get("type", "-"), a.get("id", "-"))
        except Exception:
            return None

    def do_GET(self):
        rq = _log.begin(self.headers, "/api/partners", "GET", self.path)
        if not self._gate(rq, "GET"):
            return
        try:
            q = parse_qs(urlparse(self.path).query)
            op = _errors.query_choice(
                q, "op", ("summary", "list", "accounts", "attribution", "policy", "history"),
                default="summary")
            status = _errors.query_str(q, "status", default="", max_len=20)
            if status and status not in PARTNER_STATUSES:
                raise _errors.ValidationError.field(
                    "status", "허용값: %s" % ", ".join(PARTNER_STATUSES))
            partner = _errors.query_str(q, "partner", default="", max_len=40)
            if partner and partner != "-":
                try:
                    partner = validate_partner_id(partner)
                except ValueError as e:
                    raise _errors.ValidationError.field("partner", str(e))
            channel = _errors.query_str(q, "channel", default="", max_len=30)
            if channel and channel not in CHANNELS:
                raise _errors.ValidationError.field(
                    "channel", "허용값: %s" % ", ".join(sorted(CHANNELS)))
            if op == "list":
                out = {"ok": True, "partners": list_partners(status or None)}
            elif op == "accounts":
                out = {"ok": True,
                       "accounts": list_accounts(partner or None, channel or None)}
            elif op == "attribution":
                tenant = _errors.query_str(q, "tenant", default="", max_len=40)
                if not tenant:
                    raise _errors.ValidationError.field("tenant", "필수 항목입니다")
                try:
                    tenant = validate_tenant_id(tenant)
                except ValueError as e:
                    raise _errors.ValidationError.field("tenant", str(e))
                acc = _ACCOUNTS.get(tenant)
                if acc is None:
                    raise _errors.ValidationError(
                        message="등록된 고객사가 아닙니다", code="NOT_FOUND", status=404)
                out = {"ok": True, "account": account_view(acc)}
            elif op == "policy":
                out = {"ok": True, **policy()}
            elif op == "history":
                out = {"ok": True, "history": history()}
            else:
                out = {"ok": True, **summary()}
            rq.set(op=op)
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "GET", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "GET", "error", getattr(e, "status", 500),
                        rq.request_id)
            _errors.handle(self, e, route="/api/partners", method="GET", rq=rq)

    def do_POST(self):
        rq = _log.begin(self.headers, "/api/partners", "POST", self.path)
        if not self._gate(rq, "POST"):
            return
        try:
            body = _errors.read_json(self, max_bytes=16 * 1024)
            op = _errors.as_choice(body, "op", OPS, required=True)
            actor = self._actor()
            note = _errors.as_str(body, "note", default="", max_len=200)
            owner = _errors.as_str(body, "owner", default="", max_len=40)
            try:
                if op == "create":
                    pid = _errors.as_str(body, "partner_id", required=True, max_len=40)
                    name = _errors.as_str(body, "name", required=True, max_len=60,
                                          allow_empty=False)
                    out = {"ok": True, "op": op,
                           "partner": create_partner(pid, name, owner, note, actor=actor)}
                elif op in ("suspend", "resume"):
                    pid = _errors.as_str(body, "partner_id", required=True, max_len=40)
                    st = "suspended" if op == "suspend" else "active"
                    out = {"ok": True, "op": op,
                           "partner": set_partner_status(pid, st, actor=actor, note=note)}
                else:
                    tid = _errors.as_str(body, "tenant_id", required=True, max_len=40)
                    if op == "detach":
                        out = {"ok": True, "op": op,
                               "account": detach(tid, reason=note, actor=actor)}
                    else:
                        channel = _errors.as_choice(body, "channel", tuple(CHANNELS),
                                                    required=True)
                        pid = _errors.as_str(body, "partner_id", default="", max_len=40)
                        fn = attach if op == "attach" else reassign
                        kw = {"partner_id": pid or None, "owner": owner,
                              "reason": note, "actor": actor}
                        if op == "attach" and body.get("contracted_at"):
                            kw["contracted_at"] = _errors.as_int(
                                body, "contracted_at", minimum=0)
                        out = {"ok": True, "op": op, "account": fn(tid, channel, **kw)}
            except KeyError as e:
                bad = "partner_id" if str(e).strip("'") in _kn(body, "partner_id") else "tenant_id"
                raise _errors.ValidationError.field(bad, "등록되지 않은 대상입니다")
            except ValueError as e:
                raise _errors.ValidationError.field(_field_for(op, str(e)), str(e))
            out["rbac"] = summary()["rbac"]
            rq.set(op=op)
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "POST", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "POST", "error", getattr(e, "status", 500),
                        rq.request_id)
            _errors.handle(self, e, route="/api/partners", method="POST", rq=rq)


def _kn(body, key):
    return str(body.get(key) or "").strip().lower()


def _field_for(op, msg):
    """오류 문구 → 어느 입력이 틀렸는지. 사용자가 고칠 곳을 지목한다."""
    if "연락처" in msg:
        return "owner"
    if "경로" in msg or "허용값" in msg:
        return "channel" if op in ("attach", "reassign") else "status"
    if "계약일" in msg:
        return "contracted_at"
    if "이름" in msg:
        return "name"
    if "파트너" in msg or op in ("create", "suspend", "resume"):
        return "partner_id"
    return "tenant_id"


if __name__ == "__main__":
    _clear_for_tests()
    t0 = 1_700_000_000.0
    p = create_partner("ch-alpha", "테스트 파트너", owner="홍길동", now=t0)
    assert p["status"] == "active" and p["owner_masked"] == "홍*동", p
    assert p["accounts"] == 0
    try:
        create_partner("ch-alpha", "중복", now=t0); print("FAIL: 중복 파트너 허용됨")
    except ValueError:
        pass
    try:
        create_partner("ch-beta", "연락처", owner="김철수 010-1234-5678", now=t0)
        print("FAIL: 연락처가 저장됐다")
    except ValueError:
        pass
    # 경로·파트너 정합성
    try:
        attach("acme", "direct", partner_id="ch-alpha", now=t0)
        print("FAIL: direct 에 파트너가 붙었다")
    except ValueError:
        pass
    try:
        attach("acme", "partner_referral", now=t0)
        print("FAIL: 파트너 없이 partner_referral 이 통과했다")
    except ValueError:
        pass
    a = attach("acme", "partner_referral", partner_id="ch-alpha", owner="이영희",
               contracted_at=t0 - 30 * 86400, now=t0)
    assert a["attribution"] == "partner" and a["changes"] == 1, a
    d = attach("selfco", "direct", now=t0)
    assert d["attribution"] == "direct" and d["partner_id"] is None
    # 기간 귀속 — 과거 시점은 옛 파트너로 남는다
    create_partner("ch-beta", "다른 파트너", now=t0)
    t1 = t0 + 100 * 86400
    r = reassign("acme", "partner_managed", partner_id="ch-beta", now=t1)
    assert r["partner_id"] == "ch-beta" and r["changes"] == 2, r
    assert attribution_at("acme", t0)["partner_id"] == "ch-alpha", "과거 귀속이 덮였다"
    assert attribution_at("acme", t1 + 10)["partner_id"] == "ch-beta"
    assert not check_invariants(), check_invariants()
    # 권한 — partner_admin 은 자기 고객사만, 직접계약은 아무에게도 안 보인다
    assert authorize("owner", None, "acme") is True
    assert authorize("partner_admin", "ch-beta", "acme") is True
    assert authorize("partner_admin", "ch-alpha", "acme") is False, "남의 고객사가 보인다"
    assert authorize("partner_admin", "ch-alpha", "acme", t0) is True, "과거 시점 판정 오류"
    assert authorize("partner_admin", "ch-beta", "selfco") is False, "직접계약이 새어나갔다"
    assert authorize("nope", None, "acme") is False
    assert visible_tenants("partner_admin", "ch-beta") == ["acme"]
    assert visible_tenants("owner") == ["acme", "selfco"]
    # 배선 게이트 — 승인 전에는 호출 자체가 실패한다
    os.environ.pop("PARTNER_RBAC_LIVE", None)
    try:
        enforce("owner", None, "acme"); print("FAIL: RBAC 게이트가 열려 있다")
    except RuntimeError:
        pass
    os.environ["PARTNER_RBAC_LIVE"] = "1"
    assert enforce("partner_admin", "ch-beta", "acme") is True
    try:
        enforce("partner_admin", "ch-alpha", "acme"); print("FAIL: 권한 없이 통과")
    except PermissionError:
        pass
    os.environ.pop("PARTNER_RBAC_LIVE", None)
    # 해지 — 지우지 않고 닫는다
    t2 = t1 + 10 * 86400
    e = detach("acme", now=t2)
    assert e["attribution"] == "ended" and e["active"] is False, e
    assert attribution_at("acme", t1 + 1)["partner_id"] == "ch-beta", "지난 근거가 사라졌다"
    assert not check_invariants()
    blob = json.dumps([account_view(x) for x in _ACCOUNTS.values()], ensure_ascii=False)
    for leak in ("홍길동", "이영희", "010", "1234"):
        assert leak not in blob, "개인정보가 노출됐다: %s" % leak
    s = summary(t2)
    assert s["accounts"]["direct"] == 1 and s["accounts"]["ended"] == 1, s["accounts"]
    assert s["rbac"]["active"] is False and "[승인 필요]" in s["rbac"]["note"]
    print("SELF-TEST OK:", json.dumps(s["accounts"], ensure_ascii=False),
          "history=%d" % len(history()))
