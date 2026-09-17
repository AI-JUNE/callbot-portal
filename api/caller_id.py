# -*- coding: utf-8 -*-
"""발신번호 등록 상태 관리 (/api/caller_id).

배경 — 전기통신사업법상 **발신번호 사전등록제**
  아웃바운드 통화에 쓰는 번호는 사용 권한을 증빙해 사전 등록해야 하고, 증빙서류에는
  유효기간이 있다. 등록이 만료된 번호로 발신하면 차단·제재 대상이다. 지금까지 이
  대장(臺帳)이 없어 "어느 번호가 언제까지 유효한지"를 사람이 기억에 의존했다.

설계 원칙
  - **조회가 스위치를 켜지 않는다.** 이 엔드포인트는 CPAAS_LIVE 를 읽기만 하며
    어떤 경로로도 실발신을 활성화하지 않는다. `outbound_ready` 는 "등록 요건 충족"을
    뜻할 뿐이고, 실제 발신은 별도 승인 항목이다([승인 필요]).
  - **증빙 없이 승인 없다.** `verified` 로 가려면 증빙 종류·발급일이 있어야 하고,
    발급일이 오래된 서류(기본 90일)는 거부한다. 상태를 손으로 뒤집을 수 없다.
  - **만료는 계산한다.** 저장된 상태가 `verified` 여도 만료일이 지났으면 조회 결과는
    `expired` 다. 대장이 실제보다 좋아 보이는 일이 없다.
  - **번호 원문은 남기지 않는다.** 화면·목록·이력에는 마스킹 번호만 나간다.
    원문이 필요하면 `pii_vault` 로 봉인해 보관하고, 키가 없으면 아예 보관하지 않는다.
  - 저장은 인스턴스 메모리(휘발). 영속 저장소·관리자 인증 배선은 [승인 필요].

HTTP
  GET  /api/caller_id                     → 요약(상태별 집계·만료 임박·정책·게이트)
  GET  /api/caller_id?op=list&tenant=&status=  → 목록(마스킹 번호)
  GET  /api/caller_id?op=policy           → 번호 규칙·증빙 종류·상태 전이표
  GET  /api/caller_id?op=history          → 변경 이력(최근 100건)
  POST /api/caller_id {op:"register"|"verify"|"reject"|"renew"|"revoke", ...}

셀프테스트: python3 api/caller_id.py
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
import pii_vault    # noqa: E402

MAX_NUMBERS = 200
HISTORY_MAX = 100
EVIDENCE_MAX_AGE_DAYS = 90      # 증빙서류 발급 후 유효 기간(관행: 3개월). 확정은 [승인 필요]
DEFAULT_VALID_DAYS = 365        # 등록 유효기간(제안). 통신사 정책에 맞춰 조정
EXPIRY_WARN_DAYS = 30           # 만료 임박 경고 시작일
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# 상태 — 저장되는 값. `expired` 는 저장하지 않고 조회 시 계산한다.
STATUSES = ("pending", "verified", "rejected", "revoked")
VIEW_STATUSES = STATUSES + ("expired",)

EVIDENCE_TYPES = (
    "통신서비스 이용증명원",
    "사업자등록증",
    "위임장",
    "재직증명서",
)

# 상태 전이표 — 화면·검사가 같은 표를 쓴다(드리프트 방지)
TRANSITIONS = {
    "register": {"from": ("-", "rejected", "revoked"), "to": "pending",
                 "label": "등록 신청", "needs_evidence": False},
    "verify": {"from": ("pending",), "to": "verified",
               "label": "등록 완료(통신사 승인)", "needs_evidence": True},
    "reject": {"from": ("pending",), "to": "rejected",
               "label": "반려", "needs_evidence": False},
    "renew": {"from": ("verified", "pending", "expired"), "to": "verified",
              "label": "갱신(증빙 재제출)", "needs_evidence": True},
    # 만료된 등록도 중지할 수 있어야 한다 — 닫지 못하면 번호 원문이 대장에 남는다
    "revoke": {"from": ("pending", "verified", "expired"), "to": "revoked",
               "label": "사용 중지", "needs_evidence": False},
}

# 국내 발신 가능 번호만 받는다. 국제번호·문자 발신번호는 거부.
_NUM_PATTERNS = (
    re.compile(r"^010\d{8}$"),               # 이동전화(010 은 2004년 통합 이후 11자리 고정)
    re.compile(r"^01[16789]\d{7,8}$"),       # 구 식별번호(011·016~019, 10~11자리)
    re.compile(r"^02\d{7,8}$"),              # 서울
    re.compile(r"^0(3[1-3]|4[1-4]|5[1-5]|6[1-4])\d{7,8}$"),   # 지역
    re.compile(r"^070\d{8}$"),               # 인터넷전화
    re.compile(r"^(1[5678]\d{2})\d{4}$"),    # 대표번호 15xx/16xx/17xx/18xx
    re.compile(r"^080\d{6,8}$"),             # 수신자부담
)

_LOCK = threading.Lock()
_NUMBERS: dict = {}       # cid -> record
_HISTORY: list = []
_SEQ = [0]


def cpaas_live() -> bool:
    """실발신 게이트 — **읽기만** 한다. 이 모듈은 어떤 경우에도 켜지 않는다."""
    return (os.environ.get("CPAAS_LIVE") or "").strip() == "1"


def _now() -> float:
    return time.time()


def _iso(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def digits(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def mask_number(v) -> str:
    """앞 3자리 + 뒤 4자리만. voice._mask_phone 과 같은 규칙(회귀가 일치를 강제)."""
    s = digits(v)
    if not s:
        return ""
    if len(s) < 8:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def normalize_number(v) -> str:
    """국내 발신번호 정규화(숫자만). 형식에 맞지 않으면 ValueError."""
    raw = str(v or "").strip()
    if raw.startswith("+82"):
        raw = "0" + raw[3:]
    s = digits(raw)
    if not s:
        raise ValueError("번호를 입력하세요")
    if len(s) > 12:
        raise ValueError("번호가 너무 깁니다")
    for pat in _NUM_PATTERNS:
        if pat.match(s):
            return s
    raise ValueError("국내 발신 가능 번호 형식이 아닙니다(국제번호·특수번호 불가)")


def _validate_evidence(evidence_type, issued_at, now):
    """증빙 종류·발급일 검사. 오래된 서류는 거부한다."""
    if evidence_type not in EVIDENCE_TYPES:
        raise ValueError("증빙 종류는 %s 중 하나여야 합니다" % ", ".join(EVIDENCE_TYPES))
    try:
        issued = float(issued_at)
    except (TypeError, ValueError):
        raise ValueError("증빙 발급일(issued_at, epoch 초)이 필요합니다")
    if issued <= 0:
        raise ValueError("증빙 발급일이 올바르지 않습니다")
    if issued > now + 86400:
        raise ValueError("증빙 발급일이 미래입니다")
    if issued < now - EVIDENCE_MAX_AGE_DAYS * 86400:
        raise ValueError("증빙서류가 발급 후 %d일을 넘겼습니다 — 재발급이 필요합니다"
                         % EVIDENCE_MAX_AGE_DAYS)
    return issued


def effective_status(rec, now=None) -> str:
    """저장된 상태 + 만료 계산. 대장이 실제보다 좋아 보이지 않게 한다."""
    now = _now() if now is None else now
    if rec["status"] == "verified" and rec["expires_at"] and rec["expires_at"] <= now:
        return "expired"
    return rec["status"]


def days_left(rec, now=None):
    now = _now() if now is None else now
    if not rec["expires_at"]:
        return None
    return int((rec["expires_at"] - now) // 86400)


def view(rec, now=None) -> dict:
    """외부로 나가는 형태 — 번호 원문·봉투는 절대 실리지 않는다."""
    now = _now() if now is None else now
    st = effective_status(rec, now)
    left = days_left(rec, now)
    return {
        "id": rec["id"],
        "tenant_id": rec["tenant_id"],
        "number_masked": rec["number_masked"],
        "label": rec["label"],
        "status": st,
        "stored_status": rec["status"],
        "evidence_type": rec["evidence_type"],
        "evidence_issued_at": _iso(rec["evidence_issued_at"]),
        "evidence_stored": rec["evidence_protection"],
        "registered_at": _iso(rec["registered_at"]),
        "verified_at": _iso(rec["verified_at"]),
        "expires_at": _iso(rec["expires_at"]),
        "days_left": left,
        "expiring_soon": bool(st == "verified" and left is not None and left <= EXPIRY_WARN_DAYS),
        # 등록 요건 충족 여부일 뿐 — 실발신 허용이 아니다.
        "outbound_ready": st == "verified",
        "note": rec["note"],
    }


def _hist(action, cid, actor, detail=""):
    with _LOCK:
        _HISTORY.append({"ts": _iso(_now()), "action": action, "id": cid,
                         "actor": actor or "-", "detail": detail})
        del _HISTORY[:-HISTORY_MAX]


def validate_tenant_id(v) -> str:
    s = str(v or "").strip().lower()
    if not TENANT_RE.match(s):
        raise ValueError("소문자·숫자·-·_ 40자 이내여야 합니다")
    return s


def _find(tenant_id, number) -> str:
    for cid, rec in _NUMBERS.items():
        if rec["tenant_id"] == tenant_id and rec["number_digits_masked_key"] == number:
            return cid
    return ""


def register(tenant_id, number, label="", actor=None, now=None) -> dict:
    """등록 신청. 같은 테넌트·같은 번호가 이미 살아 있으면 거부(중복 대장 방지)."""
    now = _now() if now is None else now
    tid = validate_tenant_id(tenant_id)
    num = normalize_number(number)
    with _LOCK:
        existing = _find(tid, num)
        if existing:
            cur = effective_status(_NUMBERS[existing], now)
            if cur in ("pending", "verified"):
                raise ValueError("이미 등록된 번호입니다(%s)" % cur)
            cid = existing
        else:
            if len(_NUMBERS) >= MAX_NUMBERS:
                raise ValueError("등록 가능한 번호 수(%d)를 초과했습니다" % MAX_NUMBERS)
            _SEQ[0] += 1
            cid = "CID-%04d" % _SEQ[0]
        sealed, protection = _seal_number(cid, num)
        _NUMBERS[cid] = {
            "id": cid, "tenant_id": tid,
            "number_digits_masked_key": num,   # 중복 판정용 내부 키(외부로 나가지 않음)
            "number_sealed": sealed,
            "number_masked": mask_number(num),
            "label": str(label or "")[:60],
            "status": "pending",
            "evidence_type": "", "evidence_issued_at": 0.0,
            "evidence_sealed": None, "evidence_protection": "none",
            "registered_at": now, "verified_at": 0.0, "expires_at": 0.0,
            "note": "", "protection": protection,
        }
    _hist("register", cid, actor, mask_number(num))
    return view(_NUMBERS[cid], now)


def _seal_number(cid, num):
    """번호 원문 보관은 봉인 성공 시에만. 키가 없으면 마스킹만 남긴다(평문 폴백 없음)."""
    if not pii_vault.available():
        return None, "unavailable"
    return pii_vault.seal(num, "%s/number" % cid), "sealed"


def _apply(cid, op, actor=None, now=None, evidence_type=None, issued_at=None,
           valid_days=DEFAULT_VALID_DAYS, note=""):
    now = _now() if now is None else now
    rule = TRANSITIONS.get(op)
    if rule is None:
        raise ValueError("알 수 없는 동작입니다: %s" % op)
    rec = _NUMBERS.get(cid)
    if rec is None:
        raise KeyError(cid)
    cur = effective_status(rec, now)
    allowed = rule["from"]
    if cur not in allowed:
        raise ValueError("%s 상태에서는 '%s' 할 수 없습니다(가능: %s)"
                         % (cur, rule["label"], ", ".join(allowed)))
    if rule["needs_evidence"]:
        issued = _validate_evidence(evidence_type, issued_at, now)
        sealed, protection = ((pii_vault.seal("%s|%s" % (evidence_type, _iso(issued)),
                                              "%s/evidence" % cid), "sealed")
                              if pii_vault.available() else (None, "unavailable"))
        rec["evidence_type"] = evidence_type
        rec["evidence_issued_at"] = issued
        rec["evidence_sealed"] = sealed
        rec["evidence_protection"] = protection
    with _LOCK:
        rec["status"] = rule["to"]
        rec["note"] = str(note or "")[:200]
        if rule["to"] == "verified":
            try:
                vd = int(valid_days)
            except (TypeError, ValueError):
                raise ValueError("유효기간(valid_days)은 정수여야 합니다")
            if not (1 <= vd <= 1825):
                raise ValueError("유효기간은 1~1825일 사이여야 합니다")
            rec["verified_at"] = now
            rec["expires_at"] = now + vd * 86400
        elif rule["to"] in ("rejected", "revoked"):
            rec["expires_at"] = 0.0
            if rec["number_sealed"] and rule["to"] == "revoked":
                # 사용 중지된 번호는 원문을 암호 파기한다 — 대장에는 흔적만 남는다
                rec["number_sealed"] = pii_vault.shred(rec["number_sealed"])
                rec["protection"] = "shredded"
    _hist(op, cid, actor, rec["number_masked"])
    return view(rec, now)


def verify(cid, evidence_type, issued_at, valid_days=DEFAULT_VALID_DAYS, actor=None,
           now=None, note=""):
    return _apply(cid, "verify", actor, now, evidence_type, issued_at, valid_days, note)


def renew(cid, evidence_type, issued_at, valid_days=DEFAULT_VALID_DAYS, actor=None,
          now=None, note=""):
    return _apply(cid, "renew", actor, now, evidence_type, issued_at, valid_days, note)


def reject(cid, actor=None, now=None, note=""):
    return _apply(cid, "reject", actor, now, note=note)


def revoke(cid, actor=None, now=None, note=""):
    return _apply(cid, "revoke", actor, now, note=note)


def reveal_number(cid, actor=None):
    """번호 원문 복호 — 통신사 제출 등 실제 필요할 때만. 이력에 남는다."""
    rec = _NUMBERS.get(cid)
    if rec is None:
        raise KeyError(cid)
    if not rec["number_sealed"] or pii_vault.is_shredded(rec["number_sealed"]):
        _hist("reveal_empty", cid, actor, rec["protection"])
        return None
    out = pii_vault.unseal(rec["number_sealed"], "%s/number" % cid)
    _hist("reveal", cid, actor, rec["number_masked"])
    return out


def list_numbers(tenant=None, status=None, now=None) -> list:
    now = _now() if now is None else now
    out = [view(r, now) for r in _NUMBERS.values()]
    if tenant:
        out = [v for v in out if v["tenant_id"] == tenant]
    if status:
        out = [v for v in out if v["status"] == status]
    out.sort(key=lambda v: (v["status"] != "expired", v["days_left"] is None,
                            v["days_left"] if v["days_left"] is not None else 0))
    return out


def summary(now=None) -> dict:
    now = _now() if now is None else now
    rows = [view(r, now) for r in _NUMBERS.values()]
    counts = {s: 0 for s in VIEW_STATUSES}
    for v in rows:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    expiring = sorted([v for v in rows if v["expiring_soon"]],
                      key=lambda v: v["days_left"])
    return {
        "total": len(rows),
        "counts": counts,
        "expiring_soon": expiring,
        "expired": [v for v in rows if v["status"] == "expired"],
        "outbound_ready_count": sum(1 for v in rows if v["outbound_ready"]),
        "cpaas_live": cpaas_live(),
        "vault": {"available": pii_vault.available(), "kid": pii_vault.status()["kid"]},
        "persistence": "memory (영속 저장소 [승인 필요])",
        "activation_note": "등록 요건 충족과 실발신 허용은 다른 문제다 — 실발신은 [승인 필요]",
    }


def policy() -> dict:
    return {
        "evidence_types": list(EVIDENCE_TYPES),
        "evidence_max_age_days": EVIDENCE_MAX_AGE_DAYS,
        "default_valid_days": DEFAULT_VALID_DAYS,
        "expiry_warn_days": EXPIRY_WARN_DAYS,
        "statuses": list(VIEW_STATUSES),
        "transitions": [{"op": k, "label": v["label"], "from": list(v["from"]),
                         "to": v["to"], "needs_evidence": v["needs_evidence"]}
                        for k, v in TRANSITIONS.items()],
        "number_rules": "국내 이동전화·지역번호·070·대표번호(15xx~18xx)·080 만 등록 가능",
        "max_numbers": MAX_NUMBERS,
        "legal": "전기통신사업법 발신번호 사전등록 — 증빙 없이는 승인되지 않는다",
    }


def history(limit=50) -> list:
    n = max(1, min(int(limit or 50), HISTORY_MAX))
    with _LOCK:
        return [dict(h) for h in _HISTORY[-n:]]


def _clear_for_tests():
    with _LOCK:
        _NUMBERS.clear()
        del _HISTORY[:]
        _SEQ[0] = 0


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
        rq = _log.begin(self.headers, "/api/caller_id", "GET", self.path)
        if not self._gate(rq, "GET"):
            return
        try:
            q = parse_qs(urlparse(self.path).query)
            op = _errors.query_choice(q, "op", ("summary", "list", "policy", "history"),
                                      default="summary")
            tenant = _errors.query_str(q, "tenant", default="", max_len=40)
            if tenant:
                try:
                    tenant = validate_tenant_id(tenant)
                except ValueError as e:
                    raise _errors.ValidationError.field("tenant", str(e))
            status = _errors.query_str(q, "status", default="", max_len=20)
            if status and status not in VIEW_STATUSES:
                raise _errors.ValidationError.field(
                    "status", "가능한 값: %s" % ", ".join(VIEW_STATUSES))
            if op == "list":
                out = {"ok": True, "numbers": list_numbers(tenant or None, status or None),
                       "count": len(_NUMBERS), "persistence": summary()["persistence"]}
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
            _errors.handle(self, e, route="/api/caller_id", method="GET", rq=rq)

    def do_POST(self):
        rq = _log.begin(self.headers, "/api/caller_id", "POST", self.path)
        if not self._gate(rq, "POST"):
            return
        try:
            body = _errors.read_json(self, max_bytes=16 * 1024)
            op = _errors.as_choice(body, "op", tuple(TRANSITIONS), required=True)
            actor = self._actor()
            note = _errors.as_str(body, "note", default="", max_len=200)
            if op == "register":
                tid = _errors.as_str(body, "tenant_id", required=True, max_len=40)
                num = _errors.as_str(body, "number", required=True, max_len=20)
                label = _errors.as_str(body, "label", default="", max_len=60)
                try:
                    rec = register(tid, num, label, actor=actor)
                except ValueError as e:
                    field = "tenant_id" if "소문자" in str(e) else "number"
                    raise _errors.ValidationError.field(field, str(e))
                out = {"ok": True, "op": op, "number": rec}
            else:
                cid = _errors.as_str(body, "id", required=True, max_len=20)
                kw = {"actor": actor, "note": note}
                if TRANSITIONS[op]["needs_evidence"]:
                    kw["evidence_type"] = _errors.as_str(body, "evidence_type",
                                                         required=True, max_len=40)
                    kw["issued_at"] = body.get("issued_at")
                    kw["valid_days"] = body.get("valid_days", DEFAULT_VALID_DAYS)
                try:
                    rec = _apply(cid, op, **{k: v for k, v in kw.items()
                                             if k in ("actor", "note", "evidence_type",
                                                      "issued_at", "valid_days")})
                except KeyError:
                    raise _errors.ValidationError.field("id", "등록된 번호가 아닙니다")
                except ValueError as e:
                    msg = str(e)
                    field = "evidence_type" if "증빙" in msg else (
                        "valid_days" if "유효기간" in msg else "id")
                    raise _errors.ValidationError.field(field, msg)
                out = {"ok": True, "op": op, "number": rec}
            out["activation_note"] = summary()["activation_note"]
            rq.set(op=op)
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "POST", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "POST", "error", getattr(e, "status", 500),
                        rq.request_id)
            _errors.handle(self, e, route="/api/caller_id", method="POST", rq=rq)


if __name__ == "__main__":
    _clear_for_tests()
    now = time.time()
    r = register("demo", "010-1234-5678", "대표 상담", actor="tester", now=now)
    assert r["number_masked"] == "010****5678", r["number_masked"]
    assert r["status"] == "pending" and r["outbound_ready"] is False
    try:
        register("demo", "01012345678", now=now); print("FAIL: 중복 등록 허용됨")
    except ValueError:
        pass
    try:
        register("demo", "+1-202-555-0100", now=now); print("FAIL: 국제번호 등록됨")
    except ValueError:
        pass
    try:
        verify(r["id"], "통신서비스 이용증명원", now - 200 * 86400, now=now)
        print("FAIL: 만료된 증빙으로 승인됨")
    except ValueError:
        pass
    v = verify(r["id"], "통신서비스 이용증명원", now - 3 * 86400, valid_days=365, now=now)
    assert v["status"] == "verified" and v["outbound_ready"] is True
    later = now + 350 * 86400
    assert view(_NUMBERS[r["id"]], later)["expiring_soon"] is True
    expired = now + 400 * 86400
    assert view(_NUMBERS[r["id"]], expired)["status"] == "expired"
    rn = renew(r["id"], "사업자등록증", expired - 86400, now=expired)
    assert rn["status"] == "verified"
    rv = revoke(r["id"], actor="tester")
    assert rv["status"] == "revoked" and rv["outbound_ready"] is False
    blob = json.dumps([view(x) for x in _NUMBERS.values()], ensure_ascii=False)
    assert "1234" not in blob.replace("010****5678", ""), "번호 원문이 노출됐다"
    print("SELF-TEST OK:", json.dumps(summary()["counts"], ensure_ascii=False),
          "history=%d" % len(history()))
