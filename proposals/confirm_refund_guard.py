# -*- coding: utf-8 -*-
# ==========================================================================
# [사람 승인 필요] confirm_refund 환불 가드 강화 — 제안(PROPOSAL) 전용
# --------------------------------------------------------------------------
# 목적: api/engine.py 의 환불 접수(confirm_refund) 경로에 상용화 안전장치를
#       추가하기 위한 "제안" 코드다. 이 파일은 어디에서도 import 되지 않으며,
#       라이브 동작을 바꾸지 않는다. 실제 활성화(engine.py 반영/배포)는
#       반드시 사람이 검토·승인한 뒤 직접 수행한다.
#
# 추가 안전장치 3종:
#   (1) 2단계 확인 : 금액 안내 → 사용자 1차 동의(quote 후) → 최종 재확인 동의
#                    두 번의 명시 동의가 모두 있어야만 접수. 단일 "네"로 접수 불가.
#   (2) 금액 재확인 : 접수 요청 금액이 직전 quote_refund 견적과 정확히 일치해야 함.
#                    (한도 이하라도 견적과 불일치 시 차단 → 오상환/과다환불 방지)
#   (3) 감사로그    : 접수/차단 결정마다 audit 이벤트를 남긴다(비식별·append-only).
#                    개인정보(발신번호 등)는 마스킹하여 기록.
#
# CPaaS·과금·실발신과 무관(sim/dry-run 유지). 실제 환불 API 호출은 없다.
# ==========================================================================
from __future__ import annotations
import json, hashlib, datetime

# --- (3) 감사로그: append-only, 비식별 -----------------------------------
def _mask_phone(phone: str) -> str:
    if not phone:
        return "unknown"
    h = hashlib.sha256(phone.encode()).hexdigest()[:10]
    return f"tel:{h}"

def audit_log(event: str, order_id, amount, decision: str, reason: str = "", phone: str = ""):
    """환불 결정 감사 이벤트를 dict로 반환(제안: 실제로는 append-only 저장소로)."""
    return {
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "event": event,                 # confirm_refund
        "order_id": order_id,
        "amount": int(amount or 0),
        "currency": "KRW",
        "decision": decision,           # accepted / blocked
        "reason": reason,
        "actor": _mask_phone(phone),    # 개인정보 마스킹
    }

# --- (1)+(2) 강화 가드 ----------------------------------------------------
# mem 예시(engine._mem 확장 제안):
#   quoted_amount : 직전 quote_refund 견적 금액(int)
#   affirm_count  : quote 이후 누적된 명시 동의 횟수(int)  ← 2단계 확인용
#   max_refund    : 정책 한도(int)
def guard_confirm_refund(inp: dict, mem: dict):
    """
    반환: (allow: bool, reason: str, escalate: bool, audit: dict)
    """
    order_id = inp.get("order_id")
    amount = int(inp.get("refund_amount", 0))
    phone = mem.get("phone", "")

    def _deny(reason, esc=False):
        return (False, reason, esc,
                audit_log("confirm_refund", order_id, amount, "blocked", reason, phone))

    if not inp.get("user_confirmed"):
        return _deny("사용자 확정 플래그 없음")
    # (1) 2단계 확인: quote 후 명시 동의가 2회 이상이어야 접수
    if int(mem.get("affirm_count", 0)) < 2:
        return _deny("2단계 확인 미충족(금액 안내 후 재확인 동의 필요)")
    if amount <= 0:
        return _deny("금액 0 이하")
    # (2) 금액 재확인: 견적과 정확히 일치해야 함
    quoted = mem.get("quoted_amount")
    if quoted is None:
        return _deny("견적(quote_refund) 없이 접수 불가")
    if amount != int(quoted):
        return _deny(f"견적 불일치(req={amount} vs quote={quoted})")
    # 정책 한도 초과 → 상담사 에스컬레이션
    mx = mem.get("max_refund")
    if mx is not None and amount > int(mx):
        return (False, f"한도 초과({amount}>{mx})", True,
                audit_log("confirm_refund", order_id, amount, "blocked", "한도 초과→상담사", phone))
    return (True, "", False,
            audit_log("confirm_refund", order_id, amount, "accepted", "2단계·금액일치", phone))


if __name__ == "__main__":
    # 자체 스모크 테스트(외부 호출 없음)
    base = {"phone": "01012345678", "quoted_amount": 159000, "max_refund": 159000}
    cases = [
        ({"order_id": "X", "refund_amount": 159000, "user_confirmed": True},
         {**base, "affirm_count": 2}, True),   # 정상
        ({"order_id": "X", "refund_amount": 159000, "user_confirmed": True},
         {**base, "affirm_count": 1}, False),  # 2단계 미충족
        ({"order_id": "X", "refund_amount": 200000, "user_confirmed": True},
         {**base, "affirm_count": 2}, False),  # 견적 불일치+한도초과
        ({"order_id": "X", "refund_amount": 159000, "user_confirmed": False},
         {**base, "affirm_count": 2}, False),  # 확정 플래그 없음
    ]
    for inp, mem, expect in cases:
        allow, reason, esc, aud = guard_confirm_refund(inp, mem)
        assert allow == expect, (inp, mem, allow, reason)
        print(f"allow={allow!s:5} esc={esc!s:5} reason={reason or '-':28} audit.actor={aud['actor']}")
    print("OK: proposal smoke test passed")
