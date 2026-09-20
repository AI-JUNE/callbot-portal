# -*- coding: utf-8 -*-
"""파트너 정산 리포트 (/api/settlement).

무엇을 하는가 — 파트너별 **계약·이용 실적·수수료 산출 근거**를 한 달 단위로
모아 보여주고 CSV 로 내보낸다. 정산 회의에서 다투는 질문은 셋뿐이다:
"이 고객사가 그때 누구 담당이었나", "실적이 얼마였나", "요율이 얼마였나".
이 모듈은 그 셋의 근거를 나란히 놓는 일만 한다.

하지 않는 것 (의도적으로)
  - **청구·지급을 실행하지 않는다.** 산출물은 항상 `status="draft"` 다.
    실제 정산·송금은 계약서와 사람의 확인을 거친다 **[승인 필요]**.
  - **요율을 하드코딩하지 않는다.** 요율표는 설정값(`PARTNER_RATE_CARD` 환경변수
    또는 `set_rate_card()`)이고 **기본은 미설정**이다. 요율이 없으면 금액을
    0 원으로 계산하지 않고 `commission_krw=null` + `status="rate_missing"` 로
    드러낸다 — 0 원도 주장이고, 틀린 주장이 청구서로 가면 분쟁이 된다.
  - **실적을 지어내지 않는다.** 집계는 `record_usage()` 로 들어온 관측치뿐이다.
    표본이 없으면 리포트는 빈 표다(데모 수치로 채우지 않는다).
  - **요율을 HTTP 로 바꾸지 않는다.** 이 엔드포인트는 읽기 전용(GET)이다.
    요율 변경이 API 한 번으로 되면 그건 정산 장부가 아니라 사고다.

설계에서 신경 쓴 것
  - **귀속 기간으로 쪼갠다.** 정산월 중간에 담당 파트너가 바뀌면 그 날의 실적을
    기간 점유 시간 비율로 나눈다. 나눌 때 **최대잉여법**을 써서 쪼갠 건수·금액의
    합이 원본과 정확히 같다 — 반올림으로 통화가 사라지거나 생기지 않는다.
  - **한국 월 정산 기준.** 월 경계·일 버킷은 KST(UTC+9)로 끊는다.
    UTC 로 끊으면 매달 말일 밤 9시간치가 옆 달로 새어 분쟁이 된다.
  - **돈은 Decimal 로 계산**하고 원 단위 반올림(ROUND_HALF_UP)한다. 부가세는
    계산하지 않고 요율표의 표기(`vat`)를 그대로 옮긴다(세무는 사람이 확정).
  - **빠진 것을 빠진 채로 보여준다.** 장부에 없는 고객사의 실적은 조용히 버리지
    않고 `unattributed` 로 남겨 합계 위에 띄운다.
  - **개인정보 없음.** 실적은 고객사(테넌트) 단위 집계이고 담당자는 장부의
    마스킹 이름만 쓴다. 통화 식별자·번호는 이 모듈에 들어오지 않는다.

HTTP (읽기 전용)
  GET /api/settlement                      → 요약(요율표 상태·집계 커버리지)
  GET /api/settlement?op=report&month=YYYY-MM   → 정산 리포트(JSON)
  GET /api/settlement?op=export&month=YYYY-MM   → CSV 내보내기
  GET /api/settlement?op=ratecard          → 요율표(검증 결과 포함, 값은 설정값)
  GET /api/settlement?op=usage&month=       → 실적 집계 커버리지
  POST                                     → 405 (요율·실적은 API 로 바꾸지 않는다)

셀프테스트: python3 api/settlement.py
"""
from __future__ import annotations

import os
import io
import re
import sys
import csv
import json
import time
import calendar
import threading
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import partners   # noqa: E402  (귀속 장부 — 정산의 단일 진실원)

KST_OFFSET = 9 * 3600          # 월·일 경계는 한국 시각으로 끊는다
DAY = 86400

MAX_USAGE_BUCKETS = 5000       # (고객사×일) 버킷 상한 — 오래된 것부터 버린다
USAGE_RETENTION_SEC = 400 * DAY
MAX_CALLS_PER_RECORD = 100000
MAX_MINUTES_PER_RECORD = 1000000.0
MAX_REVENUE_PER_RECORD = 10 ** 11     # 1,000억 — 오타 방어(정상 값 아님)

MONTH_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")
RATE_KEY_RE = re.compile(r"^(\*|[a-z0-9][a-z0-9_-]{0,39})/(\*|[a-z_]{1,30})$")

LINE_STATUSES = ("ok", "rate_missing", "revenue_missing", "direct", "unattributed")

_LOCK = threading.Lock()
_USAGE: dict = {}              # (tenant_id, day_index) -> bucket
_RATE_CARD_OVERRIDE = None     # set_rate_card() 로 주입된 런타임 요율표
_DROPPED = 0                   # 상한·보관기간으로 버린 버킷 수(숨기지 않는다)
_ERRORS = 0                    # 수집 중 삼킨 예외 수


# --------------------------------------------------------------------------
# 시각 — KST 월/일 경계
# --------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def _iso(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def day_index(ts) -> int:
    """KST 기준 일련 일자. 같은 한국 날짜면 같은 값."""
    return int((float(ts) + KST_OFFSET) // DAY)


def day_start(idx) -> float:
    return float(idx) * DAY - KST_OFFSET


def day_label(idx) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day_start(idx) + KST_OFFSET))


def month_range(month):
    """'YYYY-MM' → (시작ts, 종료ts) KST 경계. 잘못된 형식은 ValueError."""
    m = MONTH_RE.match(str(month or "").strip())
    if not m:
        raise ValueError("YYYY-MM 형식이어야 합니다")
    y, mo = int(m.group(1)), int(m.group(2))
    last = calendar.monthrange(y, mo)[1]
    start = calendar.timegm((y, mo, 1, 0, 0, 0, 0, 0, 0)) - KST_OFFSET
    end = calendar.timegm((y, mo, last, 23, 59, 59, 0, 0, 0)) + 1 - KST_OFFSET
    return (float(start), float(end))


def current_month(now=None) -> str:
    t = _now() if now is None else float(now)
    return time.strftime("%Y-%m", time.gmtime(t + KST_OFFSET))


def previous_month(now=None) -> str:
    """기본 정산 대상은 지난달 — 아직 안 끝난 달을 기본으로 보여주지 않는다."""
    cur = current_month(now)
    y, mo = int(cur[:4]), int(cur[5:7])
    return "%04d-%02d" % ((y - 1, 12) if mo == 1 else (y, mo - 1))


# --------------------------------------------------------------------------
# 요율표 — 설정값. 하드코딩된 숫자가 없다.
# --------------------------------------------------------------------------
EMPTY_CARD = {
    "version": "",
    "currency": "KRW",
    "vat": "excluded",
    "rates": {},
}

RATE_CARD_SCHEMA = {
    "version": "요율표 판번호(필수, 비어 있으면 미설정으로 본다)",
    "currency": "KRW 만 지원",
    "vat": "excluded | included — 표기만 옮긴다. 세액을 계산하지 않는다",
    "rates": "키 '<partner_id>/<channel>' (와일드카드 '*' 허용) → "
             "{commission_pct, unit_fee_krw} 중 하나 이상",
}


def _as_decimal(v):
    if isinstance(v, bool):
        raise ValueError("숫자여야 합니다")
    if isinstance(v, (int, float, str)):
        return Decimal(str(v).strip())
    raise ValueError("숫자여야 합니다")


def validate_rate_card(card):
    """요율표 정규화 + 문제 목록. 문제가 있으면 **쓰지 않는다**(조용히 고치지 않음)."""
    problems = []
    if not isinstance(card, dict):
        return (dict(EMPTY_CARD), ["요율표는 JSON 객체여야 합니다"])
    out = {
        "version": str(card.get("version") or "").strip()[:40],
        "currency": str(card.get("currency") or "KRW").strip().upper()[:8],
        "vat": str(card.get("vat") or "excluded").strip().lower()[:16],
        "rates": {},
    }
    if not out["version"]:
        problems.append("version 이 비어 있습니다")
    if out["currency"] != "KRW":
        problems.append("currency 는 KRW 만 지원합니다(받은 값: %s)" % out["currency"])
    if out["vat"] not in ("excluded", "included"):
        problems.append("vat 는 excluded|included 여야 합니다")
        out["vat"] = "excluded"
    rates = card.get("rates")
    if not isinstance(rates, dict):
        problems.append("rates 는 JSON 객체여야 합니다")
        rates = {}
    if len(rates) > 500:
        problems.append("요율 항목이 너무 많습니다(500 초과)")
        rates = {}
    for key, rule in sorted(rates.items()):
        k = str(key).strip().lower()
        if not RATE_KEY_RE.match(k):
            problems.append("요율 키 형식 오류: %s" % str(key)[:40])
            continue
        if not isinstance(rule, dict):
            problems.append("%s: 규칙은 객체여야 합니다" % k)
            continue
        norm = {}
        try:
            if rule.get("commission_pct") is not None:
                pct = _as_decimal(rule["commission_pct"])
                if pct < 0 or pct > 100:
                    raise ValueError("commission_pct 는 0~100 이어야 합니다")
                norm["commission_pct"] = pct
            if rule.get("unit_fee_krw") is not None:
                fee = _as_decimal(rule["unit_fee_krw"])
                if fee != fee.to_integral_value() or fee < 0 or fee > 1000000:
                    raise ValueError("unit_fee_krw 는 0~1000000 정수여야 합니다")
                norm["unit_fee_krw"] = fee
        except ValueError as e:
            problems.append("%s: %s" % (k, e))
            continue
        except Exception:
            problems.append("%s: 숫자 형식 오류" % k)
            continue
        if not norm:
            problems.append("%s: commission_pct 또는 unit_fee_krw 가 필요합니다" % k)
            continue
        out["rates"][k] = norm
    return (out, problems)


def _card_source():
    if _RATE_CARD_OVERRIDE is not None:
        return "runtime"
    return "env" if (os.environ.get("PARTNER_RATE_CARD") or "").strip() else "none"


def rate_card():
    """현재 요율표 → (정규화 카드, 문제목록, 출처).

    호출 시점에 읽는다 — 배포 후 환경변수를 바꾸면 다음 조회부터 반영된다.
    파싱 실패를 빈 카드로 위장하지 않고 문제로 드러낸다.
    """
    with _LOCK:
        override = _RATE_CARD_OVERRIDE
    if override is not None:
        card, probs = validate_rate_card(override)
        return (card, probs, "runtime")
    raw = (os.environ.get("PARTNER_RATE_CARD") or "").strip()
    if not raw:
        return (dict(EMPTY_CARD), ["요율표가 설정되지 않았습니다 [승인 필요: 계약 요율 등록]"], "none")
    try:
        parsed = json.loads(raw)
    except Exception:
        return (dict(EMPTY_CARD), ["PARTNER_RATE_CARD 가 JSON 이 아닙니다"], "env")
    card, probs = validate_rate_card(parsed)
    return (card, probs, "env")


def set_rate_card(card):
    """런타임 주입(테스트·계약 확정 전 검토용). HTTP 로는 도달할 수 없다."""
    global _RATE_CARD_OVERRIDE
    if card is not None:
        _, probs = validate_rate_card(card)
        if probs:
            raise ValueError("; ".join(probs[:3]))
    with _LOCK:
        _RATE_CARD_OVERRIDE = None if card is None else json.loads(json.dumps(card, default=str))


def lookup_rate(card, partner_id, channel):
    """요율 조회 → (규칙, 적용키). 없으면 (None, "")."""
    rates = (card or {}).get("rates") or {}
    pid = str(partner_id or "").strip().lower()
    ch = str(channel or "").strip().lower()
    for key in ("%s/%s" % (pid, ch), "%s/*" % pid, "*/%s" % ch, "*/*"):
        if pid == "" and key.startswith("/"):
            continue
        if key in rates:
            return (rates[key], key)
    return (None, "")


# --------------------------------------------------------------------------
# 이용 실적 수집 — 주입된 관측치만 센다
# --------------------------------------------------------------------------
def record_usage(tenant_id, ts=None, calls=1, minutes=0.0, revenue_krw=None):
    """이용 실적을 (고객사, 한국 날짜) 버킷에 더한다.

    통화 처리 경로에서 불릴 수 있으므로 **잘못된 입력은 예외로 거부**하되,
    호출부가 감싸 쓰도록 `record_usage_safe()` 를 따로 둔다(통화가 정산 때문에
    끊기면 안 된다).
    """
    tid = partners.validate_tenant_id(tenant_id)
    t = _now() if ts is None else float(ts)
    if t <= 0:
        raise ValueError("ts 가 올바르지 않습니다")
    n = int(calls)
    if n < 0 or n > MAX_CALLS_PER_RECORD:
        raise ValueError("calls 는 0~%d 이어야 합니다" % MAX_CALLS_PER_RECORD)
    mins = float(minutes or 0.0)
    if mins < 0 or mins > MAX_MINUTES_PER_RECORD or mins != mins:
        raise ValueError("minutes 가 올바르지 않습니다")
    rev = None
    if revenue_krw is not None:
        if isinstance(revenue_krw, bool):
            raise ValueError("revenue_krw 는 정수(원)여야 합니다")
        rev = int(revenue_krw)
        if rev < 0 or rev > MAX_REVENUE_PER_RECORD:
            raise ValueError("revenue_krw 는 0~%d 이어야 합니다" % MAX_REVENUE_PER_RECORD)
    key = (tid, day_index(t))
    with _LOCK:
        b = _USAGE.get(key)
        if b is None:
            _prune_locked(t)
            if len(_USAGE) >= MAX_USAGE_BUCKETS:
                _evict_oldest_locked()
            b = _USAGE[key] = {"tenant_id": tid, "day": key[1], "calls": 0,
                               "minutes": 0.0, "revenue_krw": None, "records": 0}
        b["calls"] += n
        b["minutes"] += mins
        b["records"] += 1
        if rev is not None:
            b["revenue_krw"] = (b["revenue_krw"] or 0) + rev
        return dict(b)


def record_usage_safe(*a, **kw):
    """통화 경로용 래퍼 — 실패해도 예외를 올리지 않되 숫자로 드러낸다."""
    global _ERRORS
    try:
        return record_usage(*a, **kw)
    except Exception:
        with _LOCK:
            _ERRORS += 1
        return None


def _prune_locked(now):
    global _DROPPED
    cutoff = day_index(float(now) - USAGE_RETENTION_SEC)
    old = [k for k in _USAGE if k[1] < cutoff]
    for k in old:
        del _USAGE[k]
    _DROPPED += len(old)


def _evict_oldest_locked():
    global _DROPPED
    k = min(_USAGE, key=lambda x: x[1])
    del _USAGE[k]
    _DROPPED += 1


def usage_coverage(month=None, now=None):
    """집계 커버리지 — 리포트가 무엇을 근거로 계산했는지 소비자가 확인할 수 있게."""
    t = _now() if now is None else float(now)
    mon = month or previous_month(t)
    start, end = month_range(mon)
    with _LOCK:
        rows = [dict(b) for b in _USAGE.values()
                if start <= day_start(b["day"]) < end]
        total_buckets, dropped, errs = len(_USAGE), _DROPPED, _ERRORS
    tenants = sorted({r["tenant_id"] for r in rows})
    return {
        "month": mon,
        "data_source": "measured" if rows else "none",
        "buckets": len(rows),
        "records": sum(r["records"] for r in rows),
        "tenants": len(tenants),
        "calls": sum(r["calls"] for r in rows),
        "minutes": round(sum(r["minutes"] for r in rows), 1),
        "revenue_reported_buckets": sum(1 for r in rows if r["revenue_krw"] is not None),
        "collector": {
            "buckets_total": total_buckets,
            "dropped": dropped,
            "errors": errs,
            "retention_days": USAGE_RETENTION_SEC // DAY,
            "scope": "instance",   # 인스턴스 메모리 — 클러스터 합산이 아니다
        },
    }


def reset_usage():
    global _DROPPED, _ERRORS
    with _LOCK:
        _USAGE.clear()
        _DROPPED = 0
        _ERRORS = 0


def _clear_for_tests():
    reset_usage()
    set_rate_card(None)


# --------------------------------------------------------------------------
# 배분 — 쪼개도 합이 변하지 않는다
# --------------------------------------------------------------------------
def allocate_int(total, weights):
    """정수 `total` 을 가중치대로 나눈다(최대잉여법). **합은 항상 total.**

    단순 반올림으로 나누면 100 건이 99 건이나 101 건이 된다. 정산에서 그건
    "없는 통화를 청구했다"는 말과 같아서 합 보존을 규칙으로 못 박는다.
    """
    n = int(total)
    ws = [Decimal(str(w)) for w in weights]
    s = sum(ws)
    if not ws:
        return []
    if s <= 0:
        out = [0] * len(ws)
        if ws:
            out[0] = n
        return out
    raw = [Decimal(n) * w / s for w in ws]
    base = [int(r.to_integral_value(rounding="ROUND_FLOOR")) for r in raw]
    rest = n - sum(base)
    order = sorted(range(len(ws)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in range(int(rest)):
        base[order[i % len(order)]] += 1
    return base


def _overlap(a0, a1, b0, b1):
    lo = max(float(a0), float(b0))
    hi = min(float(a1), b1 if b1 is not None else float(a1))
    return max(0.0, hi - lo)


def split_bucket(bucket, periods, start, end):
    """하루치 버킷을 그 날 겹치는 귀속 기간들로 쪼갠다.

    담당이 그 날 안에서 바뀌었으면 점유 시간 비율로 나눈다. 장부가 덮지 못하는
    구간(등록 전·해지 후)은 버리지 않고 `unattributed` 조각으로 남긴다 —
    조용히 사라진 실적이 나중에 "왜 빠졌냐"가 된다.
    """
    d0 = max(day_start(bucket["day"]), float(start))
    d1 = min(day_start(bucket["day"]) + DAY, float(end))
    span = max(0.0, d1 - d0)
    slices = []
    covered = 0.0
    if span > 0:
        for p in periods:
            ov = _overlap(d0, d1, p["from_ts"], p["to_ts"])
            if ov > 0:
                covered += ov
                slices.append({"partner_id": p["partner_id"], "channel": p["channel"],
                               "owner_masked": p.get("owner_masked", ""),
                               "attribution": p["attribution"],
                               "from_ts": max(d0, p["from_ts"]),
                               "to_ts": min(d1, p["to_ts"]) if p["to_ts"] else d1,
                               "weight": ov})
    gap = span - covered
    if span <= 0 or gap > 1.0:      # 1초 미만 차이는 경계 계산 오차로 본다
        slices.append({"partner_id": None, "channel": None, "owner_masked": "",
                       "attribution": "unattributed",
                       "from_ts": d0, "to_ts": d1,
                       "weight": gap if span > 0 else 1.0})
    weights = [s["weight"] for s in slices]
    calls = allocate_int(bucket["calls"], weights)
    rev = (allocate_int(bucket["revenue_krw"], weights)
           if bucket.get("revenue_krw") is not None else [None] * len(slices))
    tw = sum(Decimal(str(w)) for w in weights) or Decimal(1)
    for i, s in enumerate(slices):
        s["calls"] = calls[i]
        s["minutes"] = float(Decimal(str(bucket["minutes"])) * Decimal(str(weights[i])) / tw)
        s["revenue_krw"] = rev[i]
        s["split"] = len(slices) > 1
    return slices


# --------------------------------------------------------------------------
# 리포트
# --------------------------------------------------------------------------
def _won(d) -> int:
    return int(Decimal(d).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(n) -> str:
    return "{:,}".format(int(n))


def _commission(line, card):
    """한 줄의 수수료와 산출 근거. 계산할 수 없으면 **금액을 만들지 않는다**."""
    if line["attribution"] == "unattributed":
        return (None, "", "unattributed", "귀속 장부에 없는 고객사 — 정산 대상에서 제외")
    if line["attribution"] == "direct":
        return (None, "", "direct", "직접 계약 — 파트너 수수료 없음")
    rule, key = lookup_rate(card, line["partner_id"], line["channel"])
    if not rule:
        return (None, "", "rate_missing",
                "요율 미설정(%s/%s) — 계약 요율 등록 후 산출 [승인 필요]"
                % (line["partner_id"], line["channel"]))
    parts, total = [], Decimal(0)
    pct = rule.get("commission_pct")
    if pct is not None:
        if line["revenue_krw"] is None:
            return (None, key, "revenue_missing",
                    "매출액 미보고 — 요율 %s%% 를 적용할 근거가 없습니다" % _pct_str(pct))
        total += Decimal(line["revenue_krw"]) * pct / Decimal(100)
        parts.append("매출 %s원 × %s%%" % (_money(line["revenue_krw"]), _pct_str(pct)))
    fee = rule.get("unit_fee_krw")
    if fee is not None:
        total += Decimal(line["calls"]) * fee
        parts.append("%s건 × %s원" % (_money(line["calls"]), _money(fee)))
    return (_won(total), key, "ok", " + ".join(parts))


def _pct_str(pct) -> str:
    s = str(Decimal(pct).normalize())
    return s if "E" not in s.upper() else str(Decimal(pct))


def report(month=None, partner_id=None, now=None):
    """월 정산 리포트(초안). 계산할 수 없는 줄은 금액을 비워 드러낸다."""
    t = _now() if now is None else float(now)
    mon = month or previous_month(t)
    start, end = month_range(mon)
    card, problems, source = rate_card()
    with _LOCK:
        buckets = [dict(b) for b in _USAGE.values()
                   if start <= day_start(b["day"]) < end]
    periods_cache = {}
    groups = {}
    for b in sorted(buckets, key=lambda x: (x["tenant_id"], x["day"])):
        tid = b["tenant_id"]
        if tid not in periods_cache:
            periods_cache[tid] = partners.attribution_periods(tid, start, end)
        for s in split_bucket(b, periods_cache[tid], start, end):
            key = (tid, s["partner_id"] or "", s["channel"] or "", s["attribution"])
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "tenant_id": tid, "partner_id": s["partner_id"],
                    "channel": s["channel"], "attribution": s["attribution"],
                    "owner_masked": s["owner_masked"],
                    "calls": 0, "minutes": 0.0, "revenue_krw": None,
                    "revenue_buckets": 0, "buckets": 0, "split": False,
                    "from_ts": s["from_ts"], "to_ts": s["to_ts"],
                }
            g["calls"] += s["calls"]
            g["minutes"] += s["minutes"]
            g["buckets"] += 1
            g["split"] = g["split"] or s["split"]
            g["from_ts"] = min(g["from_ts"], s["from_ts"])
            g["to_ts"] = max(g["to_ts"], s["to_ts"])
            if s["revenue_krw"] is not None:
                g["revenue_krw"] = (g["revenue_krw"] or 0) + s["revenue_krw"]
                g["revenue_buckets"] += 1

    lines = []
    for g in groups.values():
        amount, rate_key, status, basis = _commission(g, card)
        line = {
            "tenant_id": g["tenant_id"],
            "partner_id": g["partner_id"],
            "partner_name": partners.partner_name(g["partner_id"]),
            "channel": g["channel"],
            "channel_label": partners.CHANNELS.get(g["channel"], {}).get("label", "") ,
            "attribution": g["attribution"],
            "owner_masked": g["owner_masked"],
            "from": _iso(g["from_ts"]),
            "to": _iso(g["to_ts"]),
            "calls": g["calls"],
            "minutes": round(g["minutes"], 1),
            "revenue_krw": g["revenue_krw"],
            "revenue_partial": bool(g["revenue_krw"] is not None
                                    and g["revenue_buckets"] < g["buckets"]),
            "rate_key": rate_key,
            "commission_krw": amount,
            "basis": basis,
            "status": status,
            "split": g["split"],
        }
        lines.append(line)
    lines.sort(key=lambda d: (d["partner_id"] or "~", d["tenant_id"], d["from"]))
    if partner_id:
        # "-" = 파트너 없는 줄(직접·미귀속)만. 그 외는 해당 파트너 줄만.
        if str(partner_id).strip() == "-":
            lines = [d for d in lines if d["partner_id"] is None]
        else:
            want = str(partner_id).strip().lower()
            lines = [d for d in lines if d["partner_id"] == want]

    by_partner = {}
    for d in lines:
        k = d["partner_id"] or "-"
        p = by_partner.setdefault(k, {
            "partner_id": d["partner_id"], "partner_name": d["partner_name"],
            "tenants": 0, "calls": 0, "minutes": 0.0,
            "revenue_krw": None, "commission_krw": None, "needs_attention": 0,
        })
        p["tenants"] += 1
        p["calls"] += d["calls"]
        p["minutes"] = round(p["minutes"] + d["minutes"], 1)
        if d["revenue_krw"] is not None:
            p["revenue_krw"] = (p["revenue_krw"] or 0) + d["revenue_krw"]
        if d["commission_krw"] is not None:
            p["commission_krw"] = (p["commission_krw"] or 0) + d["commission_krw"]
        if d["status"] in ("rate_missing", "revenue_missing", "unattributed"):
            p["needs_attention"] += 1

    attention = [d for d in lines
                 if d["status"] in ("rate_missing", "revenue_missing", "unattributed")]
    computed = [d for d in lines if d["commission_krw"] is not None]
    incomplete = bool(attention) or bool(problems)
    return {
        "ok": True,
        "status": "draft",           # 확정·청구·지급은 사람이 한다 [승인 필요]
        "month": mon,
        "period": {"from": _iso(start), "to": _iso(end), "timezone": "KST(UTC+9)"},
        "in_progress": bool(end > t),     # 아직 끝나지 않은 달은 확정 대상이 아니다
        "currency": card["currency"],
        "vat": card["vat"],
        "rounding": "원 단위 반올림(ROUND_HALF_UP)",
        "rate_card": {"version": card["version"], "source": source,
                      "configured": bool(card["version"] and card["rates"]),
                      "rules": len(card["rates"]), "problems": problems},
        "lines": lines,
        "by_partner": [by_partner[k] for k in sorted(by_partner)],
        "totals": {
            "lines": len(lines),
            "tenants": len({d["tenant_id"] for d in lines}),
            "calls": sum(d["calls"] for d in lines),
            "minutes": round(sum(d["minutes"] for d in lines), 1),
            # 매출·수수료는 **계산된 줄만** 더한다. 빈 줄을 0 으로 메우지 않는다.
            "revenue_krw": (sum(d["revenue_krw"] for d in lines
                                if d["revenue_krw"] is not None)
                            if any(d["revenue_krw"] is not None for d in lines) else None),
            "commission_krw": (sum(d["commission_krw"] for d in computed)
                               if computed else None),
            "commission_lines": len(computed),
            "complete": not incomplete,
        },
        "attention": [{"tenant_id": d["tenant_id"], "partner_id": d["partner_id"],
                       "status": d["status"], "note": d["basis"]} for d in attention],
        "usage": usage_coverage(mon, t),
        "note": ("초안입니다. 확정·청구·지급은 계약서와 사람의 확인을 거칩니다 [승인 필요]"),
        "generated_at": _iso(t),
    }


# --------------------------------------------------------------------------
# CSV 내보내기 — 엑셀에서 바로 열리되, 셀이 수식이 되지 않게
# --------------------------------------------------------------------------
CSV_COLUMNS = ("정산월", "파트너ID", "파트너명", "고객사", "유입경로", "귀속",
               "실적기간시작", "실적기간종료", "통화건수", "통화분", "매출(원)",
               "적용요율키", "수수료(원)", "산출근거", "상태", "담당자")

STATUS_LABEL = {
    "ok": "산출완료",
    "rate_missing": "요율미설정",
    "revenue_missing": "매출미보고",
    "direct": "직접계약",
    "unattributed": "귀속없음",
}


def csv_cell(v) -> str:
    """수식 주입 방어. `=`·`+`·`-`·`@` 로 시작하는 셀은 엑셀이 수식으로 실행한다.

    정산 파일은 회계 담당자가 연다. 고객사 ID 하나가 `=cmd|...` 이면 그 사람의
    PC 에서 수식이 돈다. 값을 버리지 않고 앞에 작은따옴표를 붙여 무력화한다.
    """
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ").replace("\x00", "")
    if s[:1] in ("=", "+", "-", "@", "\t"):
        s = "'" + s
    return s


def to_csv(rep) -> str:
    """리포트 → CSV 본문. 값이 없는 칸은 0 이 아니라 빈 칸이다."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(list(CSV_COLUMNS))
    for d in rep.get("lines") or []:
        w.writerow([csv_cell(x) for x in (
            rep.get("month", ""),
            d.get("partner_id") or "",
            d.get("partner_name") or "",
            d.get("tenant_id", ""),
            d.get("channel_label") or d.get("channel") or "",
            {"partner": "파트너", "direct": "직접", "unattributed": "미귀속"}.get(
                d.get("attribution"), d.get("attribution") or ""),
            d.get("from", ""),
            d.get("to", ""),
            d.get("calls", 0),
            d.get("minutes", 0),
            "" if d.get("revenue_krw") is None else d["revenue_krw"],
            d.get("rate_key") or "",
            "" if d.get("commission_krw") is None else d["commission_krw"],
            d.get("basis") or "",
            STATUS_LABEL.get(d.get("status"), d.get("status") or ""),
            d.get("owner_masked") or "",
        )])
    return buf.getvalue()


def csv_filename(rep) -> str:
    mon = str(rep.get("month") or "").replace("/", "-")[:7]
    return "settlement_%s_draft.csv" % (mon or "unknown")


# --------------------------------------------------------------------------
# 요약
# --------------------------------------------------------------------------
def summary(now=None) -> dict:
    t = _now() if now is None else float(now)
    card, problems, source = rate_card()
    mon = previous_month(t)
    return {
        "ok": True,
        "status": "draft-only",
        "default_month": mon,
        "months": {"previous": mon, "current": current_month(t)},
        "rate_card": {
            "version": card["version"], "source": source,
            "configured": bool(card["version"] and card["rates"]),
            "rules": len(card["rates"]), "currency": card["currency"],
            "vat": card["vat"], "problems": problems,
            "schema": RATE_CARD_SCHEMA,
            "note": "요율은 설정값입니다. 이 API 로는 바꿀 수 없습니다",
        },
        "usage": usage_coverage(mon, t),
        "ledger": {"accounts": partners.summary(t)["accounts"],
                   "integrity": partners.check_invariants()},
        "limits": {"usage_buckets": MAX_USAGE_BUCKETS,
                   "retention_days": USAGE_RETENTION_SEC // DAY},
        "persistence": "instance-memory (휘발) — 영속 저장소·청구 연동은 [승인 필요]",
        "generated_at": _iso(t),
    }


# --------------------------------------------------------------------------
# HTTP (읽기 전용)
# --------------------------------------------------------------------------
import _guard      # noqa: E402
import _log        # noqa: E402
import _errors     # noqa: E402
try:
    import _audit
except Exception:          # pragma: no cover
    _audit = None

GET_OPS = ("summary", "report", "export", "ratecard", "usage")


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

    def _send_csv(self, text, filename, rq=None):
        # BOM: 엑셀이 UTF-8 CSV 를 한글 깨짐 없이 연다(국내 현업 필수).
        d = ("﻿" + text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        if rq is not None:
            _log.attach(self, rq)
        self.send_header("Cache-Control", "no-store")
        safe = re.sub(r"[^A-Za-z0-9._-]", "", str(filename))[:60] or "settlement.csv"
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("X-Settlement-Status", "draft")
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

    def _month(self, q):
        mon = _errors.query_str(q, "month", default="", max_len=7)
        if not mon:
            return previous_month()
        if not MONTH_RE.match(mon):
            raise _errors.ValidationError.field("month", "YYYY-MM 형식이어야 합니다")
        if mon > current_month():
            raise _errors.ValidationError.field("month", "미래 월은 정산 대상이 아닙니다")
        return mon

    def _partner(self, q):
        pid = _errors.query_str(q, "partner", default="", max_len=40)
        if not pid or pid == "-":
            return pid
        try:
            return partners.validate_partner_id(pid)
        except ValueError as e:
            raise _errors.ValidationError.field("partner", str(e))

    def do_GET(self):
        rq = _log.begin(self.headers, "/api/settlement", "GET", self.path)
        if not self._gate(rq, "GET"):
            return
        try:
            q = parse_qs(urlparse(self.path).query)
            op = _errors.query_choice(q, "op", GET_OPS, default="summary")
            if op == "report":
                out = report(self._month(q), self._partner(q) or None)
            elif op == "export":
                rep = report(self._month(q), self._partner(q) or None)
                rq.set(op=op, lines=len(rep["lines"]))
                self._send_csv(to_csv(rep), csv_filename(rep), rq)
                _audit_safe(self.headers, self.path, "GET", "allow", 200, rq.request_id)
                rq.finish(200)
                return
            elif op == "ratecard":
                card, problems, source = rate_card()
                out = {"ok": True, "source": source, "version": card["version"],
                       "currency": card["currency"], "vat": card["vat"],
                       # 요율 값은 계약 정보다 — 키와 규칙만 내보내고 비밀은 없다.
                       "rates": {k: {kk: str(vv) for kk, vv in v.items()}
                                 for k, v in sorted(card["rates"].items())},
                       "problems": problems, "schema": RATE_CARD_SCHEMA,
                       "editable_via_api": False}
            elif op == "usage":
                out = {"ok": True, **usage_coverage(self._month(q))}
            else:
                out = summary()
            rq.set(op=op)
            self._send(200, out, rq)
            _audit_safe(self.headers, self.path, "GET", "allow", 200, rq.request_id)
            rq.finish(200)
        except Exception as e:
            _audit_safe(self.headers, self.path, "GET", "error", getattr(e, "status", 500),
                        rq.request_id)
            _errors.handle(self, e, route="/api/settlement", method="GET", rq=rq)

    def do_POST(self):
        """요율·실적은 API 로 바꾸지 않는다 — 405 로 분명히 거절한다."""
        rq = _log.begin(self.headers, "/api/settlement", "POST", self.path)
        if not self._gate(rq, "POST"):
            return
        _audit_safe(self.headers, self.path, "POST", "deny", 405, rq.request_id)
        rq.finish(405, denied=True)
        _errors.send(self, status=405, code="METHOD_NOT_ALLOWED",
                     message="정산 리포트는 읽기 전용입니다. 요율은 설정값으로 등록합니다",
                     rq=rq, extra_headers=[("Allow", "GET, OPTIONS")])

    do_PUT = do_POST
    do_DELETE = do_POST


if __name__ == "__main__":      # pragma: no cover
    partners._clear_for_tests()
    _clear_for_tests()
    base = calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0))
    partners.create_partner("ch-alpha", "알파채널")
    partners.attach("acme", "partner_managed", "ch-alpha", contracted_at=base - 30 * DAY)
    record_usage("acme", ts=base, calls=120, minutes=310.5, revenue_krw=1200000)
    record_usage("ghost", ts=base, calls=7, minutes=9.0)
    set_rate_card({"version": "selftest", "rates": {"ch-alpha/*": {"commission_pct": 15}}})
    rep = report("2026-08", now=base + 20 * DAY)
    print(json.dumps({"totals": rep["totals"], "attention": rep["attention"],
                      "lines": [(l["tenant_id"], l["status"], l["commission_krw"],
                                 l["basis"]) for l in rep["lines"]]},
                     ensure_ascii=False, indent=2))
    print(to_csv(rep))
    _clear_for_tests()
    partners._clear_for_tests()
