"""SIP/회선 연동 어댑터 스텁 (백로그 P0-2).

원칙: build now, activate on approval.
- 실발신·실수신·과금 코드 없음. 기본 어댑터는 sim (스크립트 이벤트 재생).
- 실회선(twilio/kt/lg 등)은 인터페이스 골격만. 활성화는 CPAAS_LIVE=1 [승인 필요].
- 이벤트 모델: ring → answered → (transcript|dtmf)* → hangup

사용:
  from sip_adapter import get_adapter
  a = get_adapter()                      # 기본 sim
  a.on_event(lambda ev: print(ev))
  a.simulate_inbound("01000000000")      # sim 전용: 인바운드 콜 1건 재생
  a.dial("01000000000")                  # dry-run 기록만 반환 (실발신 안 함)

셀프테스트:
  python api/sip_adapter.py
"""
import os
import time
import uuid

CPAAS_LIVE = os.environ.get("CPAAS_LIVE", "0") == "1"  # 실회선 게이트. 기본 OFF

EVENT_TYPES = ("ring", "answered", "transcript", "dtmf", "hangup")


def _event(call_id, etype, direction, **extra):
    assert etype in EVENT_TYPES, etype
    ev = {
        "call_id": call_id,
        "type": etype,
        "direction": direction,        # inbound | outbound
        "ts": round(time.time(), 3),
        "sim": True,
    }
    ev.update(extra)
    return ev


# ── 인터페이스 ────────────────────────────────────────────────
class SIPAdapter:
    """회선 어댑터 공통 인터페이스."""
    name = "base"

    def __init__(self):
        self._listeners = []

    def on_event(self, cb):
        """콜 이벤트 콜백 등록. cb(event_dict)"""
        self._listeners.append(cb)

    def _emit(self, ev):
        for cb in self._listeners:
            cb(ev)

    def dial(self, number, meta=None):
        """아웃바운드 발신. 실발신은 전 어댑터 공통 금지(승인 게이트)."""
        raise NotImplementedError

    def hangup(self, call_id):
        raise NotImplementedError

    def health(self):
        return {"adapter": self.name, "live": False, "ok": True}


# ── sim 구현 (통신비 0) ───────────────────────────────────────
class SimSIPAdapter(SIPAdapter):
    name = "sim"

    def __init__(self):
        super().__init__()
        self._active = {}
        self.dry_run_log = []  # dial 시도 기록 (실발신 없음)

    def simulate_inbound(self, caller, utterances=None):
        """인바운드 콜 1건을 스크립트로 재생. 반환: call_id"""
        call_id = "sim-" + uuid.uuid4().hex[:12]
        self._active[call_id] = caller
        self._emit(_event(call_id, "ring", "inbound", caller=caller))
        self._emit(_event(call_id, "answered", "inbound"))
        for u in (utterances or ["여보세요", "상담 문의드려요"]):
            self._emit(_event(call_id, "transcript", "inbound", text=u))
        self.hangup(call_id)
        return call_id

    def dial(self, number, meta=None):
        if CPAAS_LIVE:
            # 게이트가 켜져 있어도 sim 어댑터는 실발신 불가 — 이중 방어
            raise PermissionError("[승인 필요] sim 어댑터는 실발신을 지원하지 않음")
        rec = {
            "dry_run": True,
            "would_dial": number,
            "meta": meta or {},
            "ts": round(time.time(), 3),
            "billed": 0,
        }
        self.dry_run_log.append(rec)
        return rec

    def hangup(self, call_id):
        if call_id in self._active:
            direction = "inbound"
            self._emit(_event(call_id, "hangup", direction))
            del self._active[call_id]
            return True
        return False


# ── 실회선 골격 ([승인 필요] · 구현 금지 상태) ────────────────
class _LivePending(SIPAdapter):
    def _deny(self):
        raise PermissionError(
            "[승인 필요] %s 실회선 연동은 계약·승인 후 활성화(CPAAS_LIVE=1). "
            "현재는 인터페이스 골격만 존재." % self.name
        )

    def dial(self, number, meta=None):
        self._deny()

    def hangup(self, call_id):
        self._deny()


class TwilioAdapter(_LivePending):
    name = "twilio"
    # 필요 env(예정): TWILIO_ACCOUNT_SID/AUTH_TOKEN, 웹훅은 _guard CPAAS_WEBHOOK_TOKEN 재사용


class KtAdapter(_LivePending):
    name = "kt"


class LgAdapter(_LivePending):
    name = "lg"


# ── 팩토리 ────────────────────────────────────────────────────
_ADAPTERS = {"sim": SimSIPAdapter, "twilio": TwilioAdapter, "kt": KtAdapter, "lg": LgAdapter}


def get_adapter():
    want = (os.environ.get("CALLBOT_SIP_ADAPTER") or "sim").strip().lower()
    if want != "sim" and not CPAAS_LIVE:
        return SimSIPAdapter()  # 게이트 OFF → 무조건 sim
    return _ADAPTERS.get(want, SimSIPAdapter)()


if __name__ == "__main__":
    a = get_adapter()
    got = []
    a.on_event(got.append)
    cid = a.simulate_inbound("01000000000", ["여보세요", "환불 문의요", "네 감사합니다"])
    print("adapter:", a.name, "call:", cid, "events:", [e["type"] for e in got])
    print("dial dry-run:", a.dial("01011112222", {"campaign": "care"}))
    try:
        TwilioAdapter().dial("0100000")
        print("FAIL: deny 미작동")
    except PermissionError as e:
        print("DENY OK:", e)
    assert [e["type"] for e in got] == ["ring", "answered", "transcript", "transcript", "transcript", "hangup"]
    print("SELF-TEST OK")
