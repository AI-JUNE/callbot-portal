# -*- coding: utf-8 -*-
"""정산 리포트 · 테넌트 과금 라인 (/api/settlement?op=lines) — AICC-Core 정산 리포트의 포털 화면 쪽.

무엇을 하는가 — 기간(정산월)·테넌트·건수·과금 단위·금액을 한 줄씩 놓는다. AICC-Core 의
과금 단위(`src/billing/usage.ts: BillableUnit`)와 같은 이름을 쓰고, 금액은 **아는 값만** 적는다.
없는 값은 0 이 아니라 `null` 이다(0 원도 주장이고, 틀린 주장이 청구서로 가면 분쟁이 된다).

출처 스위치 `SETTLEMENT_SOURCE` (기본 미설정)
  - 미설정 · `demo`  : **데모 데이터**. 모든 줄에 `demo=true`·비고 「데모 데이터」가 붙고 응답 머리에도
                       `demo=true` 가 선다. 화면은 이 표식을 배지로 그린다. 실측처럼 보이게 하지 않는다.
  - `ledger`         : `settlement.record_usage()` 로 들어온 이 인스턴스의 관측치만 쓴다. 표본이 없으면
                       빈 표다(데모 수치로 채우지 않는다). 영속 집계·청구 시스템 연동은 **[승인 필요]**.
  - 그 밖의 값       : 알 수 없는 값은 데모로 떨어뜨리되 `source_note` 로 그 사실을 드러낸다.

진입점은 `settlement.py`(기존 함수) — 밑줄 파일이라 Vercel 함수 수(Hobby 12개)를 늘리지 않는다.
이 모듈은 다른 저장소 모듈을 import 하지 않는다(순환 import 방지). 장부 접근은 호출자가 넘긴다.
CSV 는 화면이 조립해 내려받되, 같은 규칙(`csv_cell`: 수식 주입 방어·따옴표 감싸기)을 서버도 갖는다
— `format=csv` 로 받을 수 있고, 회귀 테스트가 규칙을 고정한다.
"""
from __future__ import annotations

import math

STATUS = "draft"
SOURCES = ("demo", "ledger")

# AICC-Core `BillableUnit` 과 같은 키. 라벨은 화면용(현업 용어).
UNITS = {
    "voice_seconds": "통화 초(반올림 전)",
    "voice_units": "통화 과금 단위(계약 반올림 적용)",
    "sessions": "세션 수",
    "llm_prompt_tokens": "LLM 입력 토큰",
    "llm_completion_tokens": "LLM 출력 토큰",
    "stt_seconds": "STT 초",
    "tts_seconds": "TTS 초",
}

COLUMNS = ("period", "tenant_id", "count", "unit", "amount_krw", "status", "note")
CSV_HEADER = ("기간", "테넌트", "건수", "과금 단위", "금액(원)", "상태", "비고")
DEMO_NOTE = "데모 데이터"

# 데모 줄 — 고정값·가짜 회사명 없음. 이 수치는 실측이 아니며 모든 줄에 표식이 붙는다.
_DEMO_ROWS = (
    ("demo-tenant-a", "voice_units", 1240, 372000),
    ("demo-tenant-a", "sessions", 310, None),
    ("demo-tenant-b", "voice_units", 865, 259500),
    ("demo-tenant-b", "sessions", 198, None),
    ("demo-tenant-c", "sessions", 42, None),
)


def source(env):
    """(출처, 안내 문구). 알 수 없는 값은 데모로 떨어뜨리되 조용히 넘기지 않는다."""
    v = (env.get("SETTLEMENT_SOURCE") or "").strip().lower()
    if not v:
        return "demo", ""
    if v in SOURCES:
        return v, ""
    return "demo", "SETTLEMENT_SOURCE 값(%s)을 알 수 없어 데모 데이터로 표시합니다." % v[:20]


def _line(period, tenant_id, unit, count, amount_krw, demo, note=""):
    if amount_krw is None:
        status = "demo" if demo else "amount_missing"
    else:
        status = "demo" if demo else "ok"
    return {"period": period, "tenant_id": tenant_id, "count": int(count), "unit": unit,
            "unit_label": UNITS.get(unit, unit), "amount_krw": amount_krw, "status": status,
            "demo": bool(demo), "note": note}


def demo_lines(period, tenant_id=None):
    return [_line(period, t, u, c, a, True, DEMO_NOTE) for t, u, c, a in _DEMO_ROWS
            if not tenant_id or t == tenant_id]


def ledger_lines(period, buckets, tenant_id=None):
    """`settlement` 의 (고객사×일) 버킷 목록 → 테넌트별 줄. 매출은 통화 과금 단위 줄에만 붙는다.

    세션 줄은 건수만 적고 금액은 비운다(세션 과금 요율이 장부에 없다 — 모른다고 적는 것이 맞다).
    """
    agg = {}
    for b in buckets or []:
        tid = b.get("tenant_id")
        if not tid or (tenant_id and tid != tenant_id):
            continue
        a = agg.setdefault(tid, {"calls": 0, "minutes": 0.0, "revenue": None})
        a["calls"] += int(b.get("calls") or 0)
        a["minutes"] += float(b.get("minutes") or 0.0)
        if b.get("revenue_krw") is not None:
            a["revenue"] = (a["revenue"] or 0) + int(b["revenue_krw"])
    out = []
    for tid in sorted(agg):
        a = agg[tid]
        out.append(_line(period, tid, "voice_units", int(math.ceil(a["minutes"])), a["revenue"], False,
                         "" if a["revenue"] is not None else "매출 미보고 — 금액을 비웁니다"))
        out.append(_line(period, tid, "sessions", a["calls"], None, False, "세션 요율 미설정 — 건수만"))
    return out


def totals(lines):
    known = [l["amount_krw"] for l in lines if l["amount_krw"] is not None]
    return {"lines": len(lines), "tenants": len({l["tenant_id"] for l in lines}),
            "count": sum(l["count"] for l in lines),
            "amount_krw": sum(known) if known else None,
            "amount_complete": bool(lines) and len(known) == len(lines)}


def build(period, env, buckets=None, tenant_id=None):
    """응답 본문. `buckets` 는 ledger 출처일 때 호출자(settlement)가 넘긴 그 달의 버킷."""
    src, note = source(env)
    lines = demo_lines(period, tenant_id) if src == "demo" else ledger_lines(period, buckets, tenant_id)
    return {
        "ok": True,
        "op": "lines",
        "month": period,
        "tenant_id": tenant_id or "",
        "source": src,
        "demo": src == "demo",
        "source_note": note,
        "status": STATUS,
        "columns": list(COLUMNS),
        "units": dict(UNITS),
        "lines": lines,
        "totals": totals(lines),
        "note": ("데모 데이터 — 실측이 아닙니다. SETTLEMENT_SOURCE=ledger 로 바꾸면 이 인스턴스의 관측치만 씁니다."
                 if src == "demo" else
                 "이 인스턴스가 관측한 실적만 집계했습니다(인스턴스 메모리). 영속 집계·청구 연동은 [승인 필요]."),
    }


def csv_cell(v):
    """CSV 한 칸 — 수식 주입 방어(`=`·`+`·`-`·`@`·탭·CR 로 시작하면 작은따옴표) + 따옴표 감싸기."""
    s = "" if v is None else str(v)
    s = s.replace("\x00", "")
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


def to_csv(resp):
    """응답 → CSV 본문(CRLF). 값이 없는 칸은 0 이 아니라 빈 칸. 데모면 비고에 표식이 남는다."""
    rows = [",".join(CSV_HEADER)]
    for l in resp.get("lines") or []:
        rows.append(",".join(csv_cell(x) for x in (
            l.get("period", ""), l.get("tenant_id", ""), l.get("count", ""),
            l.get("unit_label") or l.get("unit", ""),
            "" if l.get("amount_krw") is None else l["amount_krw"],
            l.get("status", ""), l.get("note", ""))))
    return "\r\n".join(rows) + "\r\n"


def csv_filename(resp):
    mon = str(resp.get("month") or "").replace("/", "-")[:7] or "unknown"
    return "settlement_lines_%s_%s.csv" % (mon, "demo" if resp.get("demo") else "draft")
