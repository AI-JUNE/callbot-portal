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

개인정보:
  이벤트·dry-run 기록에는 원문 번호를 싣지 않는다(voice._mask_phone 과 같은 규칙,
  앞 3자리 + 뒤 4자리). 어댑터 로그는 운영 화면·모니터링으로 흘러갈 수 있는 값이다.

셀프테스트:
  python api/sip_adapter.py
"""
import os
import time
import uuid

EVENT_TYPES = ("ring", "answered", "transcript", "dtmf", "hangup")
DIRECTIONS = ("inbound", "outbound")


def is_live():
    """실회선 게이트. 기본 OFF. 호출 시점에 읽는다(배포 후 환경변수 변경 반영)."""
    return os.environ.get("CPAAS_LIVE", "0") == "1"


# 하위호환: 모듈 로드 시점 스냅샷. 판정에는 is_live() 를 쓴다.
CPAAS_LIVE = is_live()


def mask_phone(v):
    """번호 마스킹 — voice._mask_phone 과 동일 규칙(의존 없이 복제)."""
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    if not s:
        return ""
    if len(s) < 8:
        return "*" * len(s)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def _clean_number(number):
    """발신 대상 번호 검증. 문자열·숫자 4자리 이상만 허용. 실패는 ValueError(조용히 기록 금지)."""
    if not isinstance(number, str):
        raise ValueError("number must be a string")
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 4 or len(digits) > 20:
        raise ValueError("number must contain 4~20 digits")
    return digits


def _event(call_id, etype, direction, **extra):
    if etype not in EVENT_TYPES:
        raise ValueError("unknown event type: %r" % (etype,))
    if direction not in DIRECTIONS:
        raise ValueError("unknown direction: %r" % (direction,))
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
        self.listener_errors = 0   # 리스너 예외 건수 — 삼키지 않고 health 로 드러낸다

    def on_event(self, cb):
        """콜 이벤트 콜백 등록. cb(event_dict)"""
        if not callable(cb):
            raise TypeError("listener must be callable")
        self._listeners.append(cb)

    def _emit(self, ev):
        """리스너 하나가 죽어도 나머지 리스너·콜 진행(hangup 정리)은 계속된다.

        예전엔 첫 리스너 예외가 simulate_inbound 를 중간에 끊어 통화가 _active 에
        영원히 남았다(누수). 예외는 개수로 남기고 각 리스너에 사본을 준다(변조 격리).
        """
        for cb in list(self._listeners):
            try:
                cb(dict(ev))
            except Exception:
                self.listener_errors += 1

    def dial(self, number, meta=None):
        """아웃바운드 발신. 실발신은 전 어댑터 공통 금지(승인 게이트)."""
        raise NotImplementedError

    def hangup(self, call_id):
        raise NotImplementedError

    def health(self):
        return {
            "adapter": self.name,
            "live": False,
            "ok": True,
            "gate": "on" if is_live() else "off",
            "listener_errors": self.listener_errors,
        }


# ── sim 구현 (통신비 0) ───────────────────────────────────────
class SimSIPAdapter(SIPAdapter):
    name = "sim"
    MAX_DRY_RUN_LOG = 200

    def __init__(self):
        super().__init__()
        self._active = {}
        self.dry_run_log = []  # dial 시도 기록 (실발신 없음 · 번호는 마스킹)

    def simulate_inbound(self, caller, utterances=None):
        """인바운드 콜 1건을 스크립트로 재생. 반환: call_id

        utterances 는 문자열 목록만. 빈 목록이면 발화 없이 응답→종료.
        """
        if utterances is None:
            utterances = ["여보세요", "상담 문의드려요"]
        if isinstance(utterances, str) or not all(isinstance(u, str) for u in utterances):
            raise ValueError("utterances must be a list of strings")
        call_id = "sim-" + uuid.uuid4().hex[:12]
        masked = mask_phone(caller)
        self._active[call_id] = masked
        self._emit(_event(call_id, "ring", "inbound", caller=masked))
        self._emit(_event(call_id, "answered", "inbound"))
        for u in utterances:
            self._emit(_event(call_id, "transcript", "inbound", text=u))
        self.hangup(call_id)
        return call_id

    def dial(self, number, meta=None):
        if is_live():
            # 게이트가 켜져 있어도 sim 어댑터는 실발신 불가 — 이중 방어
            raise PermissionError("[승인 필요] sim 어댑터는 실발신을 지원하지 않음")
        digits = _clean_number(number)
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("meta must be a dict")
        rec = {
            "dry_run": True,
            "would_dial": mask_phone(digits),
            "meta": dict(meta or {}),
            "ts": round(time.time(), 3),
            "billed": 0,
        }
        self.dry_run_log.append(rec)
        del self.dry_run_log[:-self.MAX_DRY_RUN_LOG]
        return dict(rec)

    def hangup(self, call_id):
        if call_id in self._active:
            self._emit(_event(call_id, "hangup", "inbound"))
            del self._active[call_id]
            return True
        return False

    def health(self):
        h = super().health()
        h["active_calls"] = len(self._active)
        h["dry_runs"] = len(self.dry_run_log)
        return h


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

    def health(self):
        h = super().health()
        h["ok"] = False
        h["detail"] = "[승인 필요] 인터페이스 골격만 존재"
        return h


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
    """게이트 OFF → 무조건 sim. ON 이어도 미지 어댑터명은 sim 으로 폴백(실발신 쪽으로 넘어지지 않는다)."""
    want = (os.environ.get("CALLBOT_SIP_ADAPTER") or "sim").strip().lower()
    if want != "sim" and not is_live():
        return SimSIPAdapter()
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
    assert got[0]["caller"] == "010****0000"
    print("SELF-TEST OK")
