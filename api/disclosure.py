# -*- coding: utf-8 -*-
"""AI 고지 문구 — 테넌트별 설정 (/api/disclosure).

법정 요구: 사람이 아닌 AI 가 응대하는 통화임을 **통화 시작 시** 고지한다.
(인공지능 기본법상 생성형 AI 산출물·응대 고지 의무 — 세부 문안은 사람이 확정.)

설계 원칙
  - **기본 문구는 항상 요건을 충족**한다. 테넌트가 아무것도 설정하지 않아도 고지가 빠지지 않는다.
  - 테넌트 문구는 저장 전 **규칙 검사**를 통과해야 한다(요건 누락·허위 녹음 고지·개인정보·금지어).
    사람이 문구를 바꿔도 고지 의무가 조용히 사라질 수 없다.
  - 녹음 고지는 `RECORDING_LIVE` 게이트와 **일치**해야 한다. 녹음하지 않으면서 "녹음됩니다"라고
    말하는 것도, 녹음하면서 말하지 않는 것도 허위 고지다.
  - 저장은 인스턴스 메모리(휘발). 영속 저장소·관리자 인증 확정 후 배선 **[승인 필요]**.
    그 전까지 이 화면은 "설정 가능한 구조와 검증 규칙"을 심사·시연하는 용도다.
  - 변경은 append-only 이력(최근 100건)에 남는다. 문구 전문은 이력에도 남는다(개인정보가 아니고
    검증을 통과한 문구만 저장되므로).

HTTP
  GET  /api/disclosure                → 기본 문구·요건 목록·테넌트 수·검증 규칙
  GET  /api/disclosure?tenant=<id>    → 해당 테넌트의 **실효 문구**(설정 없으면 기본)와 검사 결과
  GET  /api/disclosure?op=list        → 설정된 테넌트 목록(문구 포함)
  GET  /api/disclosure?op=history     → 변경 이력
  POST /api/disclosure  {tenant_id, text, brand?, dry_run?}   → 검사 후 저장(dry_run=true 면 검사만)
  POST /api/disclosure  {tenant_id, op:"reset"}               → 기본 문구로 복귀

voice.py 는 `greeting(tenant_id)` 로 통화 첫 발화를 가져간다(설정 없으면 기본 문구).
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

sys.path.insert(0, os.path.dirname(__file__))

DEFAULT_BRAND = "AICC Portal"
# {brand} 는 저장 시점이 아니라 재생 시점에 치환된다(브랜드만 바꿔도 문구가 따라온다).
DEFAULT_TEXT = ("안녕하세요, {brand} AI 상담원입니다. 이 통화는 인공지능이 응대하며, "
                "언제든 상담사 연결을 요청하실 수 있습니다. 무엇을 도와드릴까요?")
RECORDING_SENTENCE = "상담 품질 향상을 위해 통화 내용이 녹음됩니다."

MIN_LEN = 10
MAX_LEN = 300           # TTS 약 20초. 고지가 길면 고객이 끊는다
MAX_TENANTS = 200
HISTORY_MAX = 100
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_PHONE_RE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
_RRN_RE = re.compile(r"\d{6}-?[1-4]\d{6}")
# scripts/verify.py BANNED 와 같은 목록(테스트가 일치를 강제) — 허위 도입사례 표기 차단
BANNED = ("농협", "라피치", "IBK", "날리지큐브", "보이스봇", "신세계", "하나은행")

# 요건 정의 — 화면·검사가 같은 표를 쓴다.
#   level=error  : 미충족 시 저장 거부
#   level=warn   : 저장은 되지만 경고를 남긴다(권고)
REQUIREMENTS = (
    {"key": "ai_identity", "level": "error",
     "label": "AI 응대 명시",
     "hint": "'AI'·'인공지능'·'자동응답'·'콜봇' 중 하나가 들어가야 합니다",
     "keywords": ("AI", "인공지능", "자동응답", "자동 응답", "콜봇", "챗봇", "가상 상담원")},
    {"key": "operator", "level": "error",
     "label": "운영 주체(브랜드) 표기",
     "hint": "{brand} 자리표시자 또는 브랜드명이 들어가야 합니다",
     "keywords": ()},          # brand 로 동적 검사
    {"key": "human_option", "level": "warn",
     "label": "상담사 연결 가능 안내(권고)",
     "hint": "'상담사'·'상담원'·'직원' 연결이 가능함을 알리는 것을 권고합니다",
     "keywords": ("상담사", "상담원", "직원")},
    {"key": "recording_consistency", "level": "error",
     "label": "녹음 고지와 실제 녹음 설정 일치",
     "hint": "RECORDING_LIVE 가 켜져 있으면 '녹음' 고지가 필요하고, 꺼져 있으면 녹음을 언급하면 안 됩니다",
     "keywords": ("녹음", "녹취")},
)

_LOCK = threading.Lock()
_TENANTS: dict = {}       # tenant_id -> record
_HISTORY: list = []       # append-only, 최근 HISTORY_MAX


def recording_live() -> bool:
    return (os.environ.get("RECORDING_LIVE") or "").strip().lower() in ("1", "true", "on")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _has(text: str, word: str) -> bool:
    """키워드 포함 판정. 영문은 단어 경계로 본다 — 'AICC' 의 'AI' 가 고지로 인정되면 안 된다."""
    if re.fullmatch(r"[A-Za-z]+", word):
        return re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(word), text) is not None
    return word in text


def render(text: str, brand: str) -> str:
    return (text or "").replace("{brand}", brand or DEFAULT_BRAND)


def default_text(brand: str = DEFAULT_BRAND, with_recording: bool | None = None) -> str:
    """기본 문구. 녹음 게이트가 켜져 있으면 녹음 고지 문장을 덧붙인다."""
    rec = recording_live() if with_recording is None else bool(with_recording)
    t = DEFAULT_TEXT
    if rec:
        t = t.replace(" 무엇을 도와드릴까요?", " " + RECORDING_SENTENCE + " 무엇을 도와드릴까요?")
    return render(t, brand)


# --------------------------------------------------------------------------
# 검사
# --------------------------------------------------------------------------
def check(text: str, brand: str = DEFAULT_BRAND, recording: bool | None = None) -> dict:
    """문구가 요건을 충족하는지 규칙으로 판정한다.

    반환 {"ok": bool, "checks": [{key,label,level,passed,hint}], "errors": [...], "warnings": [...]}
    ok 는 level=error 항목이 전부 통과했을 때만 True.
    """
    rec = recording_live() if recording is None else bool(recording)
    raw = text or ""
    rendered = render(raw, brand)
    checks, errors, warnings = [], [], []

    def add(key, label, level, passed, hint):
        checks.append({"key": key, "label": label, "level": level, "passed": bool(passed), "hint": hint})
        if not passed:
            (errors if level == "error" else warnings).append({"field": "text", "key": key, "reason": hint})

    # 형식
    n = len(raw.strip())
    add("length", "길이 %d~%d자" % (MIN_LEN, MAX_LEN), "error", MIN_LEN <= n <= MAX_LEN,
        "%d~%d자여야 합니다(현재 %d자)" % (MIN_LEN, MAX_LEN, n))
    add("no_pii", "개인정보(전화번호·주민번호) 없음", "error",
        not (_PHONE_RE.search(raw) or _RRN_RE.search(raw)), "전화번호·주민번호 형태의 문자열은 넣을 수 없습니다")
    hit = [b for b in BANNED if b in raw]
    add("no_banned", "타사·허위 도입사례 표기 없음", "error", not hit,
        "허용되지 않는 표기: %s" % ", ".join(hit) if hit else "타사명·도입사례를 넣을 수 없습니다")
    add("no_markup", "마크업 없음", "error", not re.search(r"[<>{}]", rendered),
        "꺾쇠·중괄호는 넣을 수 없습니다({brand} 자리표시자만 허용)")

    for r in REQUIREMENTS:
        k = r["key"]
        if k == "operator":
            passed = ("{brand}" in raw) or (bool(brand) and brand in raw)
        elif k == "recording_consistency":
            mentions = any(_has(rendered, w) for w in r["keywords"])
            passed = (mentions == rec)
            hint = (r["hint"] if passed else
                    ("녹음이 켜져 있습니다(RECORDING_LIVE). 녹음 고지 문장을 넣어야 합니다" if rec
                     else "녹음이 꺼져 있습니다. '녹음'·'녹취' 언급은 허위 고지가 됩니다"))
            add(k, r["label"], r["level"], passed, hint)
            continue
        else:
            passed = any(_has(rendered, w) for w in r["keywords"])
        add(k, r["label"], r["level"], passed, r["hint"])

    return {"ok": not errors, "checks": checks, "errors": errors, "warnings": warnings,
            "rendered": rendered, "recording_live": rec}


# --------------------------------------------------------------------------
# 저장소 (인스턴스 메모리 · 영속화 [승인 필요])
# --------------------------------------------------------------------------
def validate_tenant_id(tid) -> str:
    if not isinstance(tid, str) or not TENANT_RE.match(tid.strip().lower()):
        raise ValueError("tenant_id 는 영문 소문자·숫자·'-'·'_' 1~40자여야 합니다")
    return tid.strip().lower()


def _hist(entry: dict):
    _HISTORY.append(entry)
    del _HISTORY[:-HISTORY_MAX]


def set_text(tenant_id, text, brand=None, actor=None) -> dict:
    """검사 통과 시 저장. 실패는 ValueError(details) — 조용히 저장하지 않는다."""
    tid = validate_tenant_id(tenant_id)
    if not isinstance(text, str):
        raise ValueError("text 는 문자열이어야 합니다")
    brand = (brand if isinstance(brand, str) and brand.strip() else DEFAULT_BRAND).strip()[:60]
    if re.search(r"[<>{}]", brand):
        raise ValueError("brand 에 꺾쇠·중괄호를 넣을 수 없습니다")
    res = check(text, brand)
    if not res["ok"]:
        e = ValueError("고지 문구가 요건을 충족하지 않습니다")
        e.details = res["errors"]      # type: ignore[attr-defined]
        raise e
    with _LOCK:
        prev = _TENANTS.get(tid)
        if prev is None and len(_TENANTS) >= MAX_TENANTS:
            raise ValueError("테넌트 설정 상한(%d)에 도달했습니다" % MAX_TENANTS)
        rec = {"tenant_id": tid, "brand": brand, "text": text.strip(),
               "version": (prev["version"] + 1) if prev else 1,
               "updated_at": _now(), "warnings": [w["key"] for w in res["warnings"]]}
        _TENANTS[tid] = rec
        _hist({"ts": rec["updated_at"], "tenant_id": tid, "op": "set", "version": rec["version"],
               "from_version": prev["version"] if prev else 0, "actor": (actor or "")[:24],
               "text": rec["text"], "brand": brand})
        return dict(rec)


def reset(tenant_id, actor=None) -> bool:
    tid = validate_tenant_id(tenant_id)
    with _LOCK:
        prev = _TENANTS.pop(tid, None)
        if prev is None:
            return False
        _hist({"ts": _now(), "tenant_id": tid, "op": "reset", "version": prev["version"] + 1,
               "from_version": prev["version"], "actor": (actor or "")[:24]})
        return True


def effective(tenant_id=None) -> dict:
    """실효 문구 — 테넌트 설정이 있으면 그것, 없으면 기본. 항상 검사 결과를 함께 돌려준다."""
    tid = validate_tenant_id(tenant_id) if tenant_id else None
    with _LOCK:
        rec = dict(_TENANTS[tid]) if tid and tid in _TENANTS else None
    if rec:
        brand, text, source, version = rec["brand"], rec["text"], "tenant", rec["version"]
    else:
        brand, text, source, version = DEFAULT_BRAND, default_text(), "default", 0
    res = check(text, brand)
    return {"tenant_id": tid, "source": source, "version": version, "brand": brand,
            "text": render(text, brand), "template": text,
            "recording_live": res["recording_live"], "ok": res["ok"], "checks": res["checks"],
            "warnings": res["warnings"]}


def greeting(tenant_id=None) -> str:
    """voice.py 용 — 어떤 경우에도 문자열을 돌려준다(고지가 빠지는 쪽으로 실패하지 않는다)."""
    try:
        return effective(tenant_id)["text"]
    except Exception:
        return default_text()


def env_override() -> dict | None:
    """CALLBOT_GREETING 환경변수(운영자 명시 설정)가 있으면 그 문구의 요건 검사 결과.

    voice.py 는 테넌트 설정이 없을 때 이 값을 기본 문구보다 우선한다. 요건 미충족이면
    화면이 경고한다 — 조용히 덮어쓰지도, 조용히 통과시키지도 않는다.
    """
    env = (os.environ.get("CALLBOT_GREETING") or "").strip()
    if not env:
        return None
    res = check(env, DEFAULT_BRAND)
    return {"set": True, "text": env, "ok": res["ok"], "checks": res["checks"], "errors": res["errors"]}


def list_tenants() -> list:
    with _LOCK:
        return [dict(v) for _, v in sorted(_TENANTS.items())]


def history(limit=50) -> list:
    n = max(1, min(int(limit or 50), HISTORY_MAX))
    with _LOCK:
        return [dict(h) for h in _HISTORY[-n:]]


def _clear_for_tests():
    with _LOCK:
        _TENANTS.clear()
        del _HISTORY[:]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
import _guard
import _log
import _errors
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

    def do_GET(self):
        rq = _log.begin(self.headers, "/api/disclosure", "GET", self.path)
        if not self._gate(rq, "GET"):
            return
        try:
            q = parse_qs(urlparse(self.path).query)
            op = _errors.query_choice(q, "op", ("info", "list", "history"), default="info")
            tenant = _errors.query_str(q, "tenant", default="", max_len=40)
            if tenant:
                try:
                    validate_tenant_id(tenant)
                except ValueError as e:
                    raise _errors.ValidationError.field("tenant", str(e))
                out = {"ok": True, **effective(tenant)}
            elif op == "list":
                out = {"ok": True, "tenants": list_tenants(), "count": len(_TENANTS),
                       "persistence": "memory (영속 저장소 [승인 필요])"}
            elif op == "history":
                out = {"ok": True, "history": history()}
            else:
                out = {"ok": True, "default": effective(None),
                       "requirements": [{k: r[k] for k in ("key", "level", "label", "hint")} for r in REQUIREMENTS],
                       "limits": {"min_len": MIN_LEN, "max_len": MAX_LEN, "max_tenants": MAX_TENANTS},
                       "tenant_count": len(_TENANTS), "recording_live": recording_live(),
                       "env_override": env_override(),
                       "persistence": "memory (영속 저장소 [승인 필요])"}
            rq.set(op=op, tenant_set=bool(tenant))
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "GET", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "GET", "error", getattr(e, "status", 500), rq.request_id)
            _errors.handle(self, e, route="/api/disclosure", method="GET", rq=rq)

    def do_POST(self):
        rq = _log.begin(self.headers, "/api/disclosure", "POST", self.path)
        if not self._gate(rq, "POST"):
            return
        status = 500
        try:
            body = _errors.read_json(self, max_bytes=16 * 1024)
            tid_raw = _errors.as_str(body, "tenant_id", required=True, max_len=40)
            try:
                tid = validate_tenant_id(tid_raw)
            except ValueError as e:
                raise _errors.ValidationError.field("tenant_id", str(e))
            op = _errors.as_choice(body, "op", ("set", "reset"), default="set")
            actor = None
            if _audit:
                try:
                    a = _audit.actor(self.headers)          # 키 원문 아님(지문·오리진만)
                    actor = "%s:%s" % (a.get("type", "-"), a.get("id", "-"))
                except Exception:
                    actor = None
            if op == "reset":
                existed = reset(tid, actor=actor)
                out = {"ok": True, "tenant_id": tid, "reset": existed, **{"effective": effective(tid)}}
            else:
                text = _errors.as_str(body, "text", required=True, max_len=MAX_LEN * 2)
                brand = _errors.as_str(body, "brand", default=DEFAULT_BRAND, max_len=60)
                dry = bool(body.get("dry_run"))
                res = check(text, brand)
                if not res["ok"]:
                    raise _errors.ValidationError(details=res["errors"],
                                                  message="고지 문구가 요건을 충족하지 않습니다")
                if dry:
                    out = {"ok": True, "dry_run": True, "tenant_id": tid, "checks": res["checks"],
                           "warnings": res["warnings"], "rendered": res["rendered"]}
                else:
                    try:
                        rec = set_text(tid, text, brand, actor=actor)
                    except ValueError as e:
                        raise _errors.ValidationError.field("tenant_id", str(e))
                    out = {"ok": True, "dry_run": False, "saved": rec, "effective": effective(tid),
                           "persistence": "memory (영속 저장소 [승인 필요])"}
            rq.set(op=op, dry_run=bool(body.get("dry_run")))
            status = 200
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "POST", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "POST", "error", getattr(e, "status", status), rq.request_id)
            _errors.handle(self, e, route="/api/disclosure", method="POST", rq=rq)


if __name__ == "__main__":
    print(json.dumps(effective(None), ensure_ascii=False, indent=2))
    print(json.dumps(check("안녕하세요 온라인몰입니다. 무엇을 도와드릴까요?", "온라인몰"), ensure_ascii=False, indent=2))
