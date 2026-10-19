# -*- coding: utf-8 -*-
"""이음 3R 「현장 음성 요청」 다리 — STT 로 받은 참여자 발화를 분류해 이음 소통창으로 넘긴다.

흐름(이음 발표 13쪽): 음성 요청 → STT 변환 → **요청 유형 분류(여기)** → 이음 담당자 알림 → 접수·조치 → 참여자 회신 → 처리 이력.
STT 는 기존 `/api/stt`(speech.py) 가 맡고, 이 모듈은 받은 글을 분류·정리해 이음 웹훅으로 보낸다.

규약 (이음 `docs/CHANNEL_API.md` 6절 · `src/app/api/channel/aicc/request/route.ts`)
  POST https://eum.gowon.co.kr/api/channel/aicc/request
  헤더  X-Channel-Id: phone · X-Channel-Timestamp: <유닉스 초> · X-Channel-Signature: sha256=<hex>
        서명 = HMAC-SHA256(공유 비밀, "<ts>.<원문 본문>") — 이음 `CHANNEL_SECRET` 과 같은 값
  본문  {participant_id, kind_label, summary, ref}

설계 원칙
 - **분류는 규칙**: 같은 말이면 같은 유형이 나온다(안부 판정과 같은 이유). 근거 낱말을 함께 돌려준다.
   긴급 낱말이 하나라도 있으면 다른 유형보다 앞선다 — 놓치는 쪽이 더 위험하다.
 - **개인정보**: 요약은 `monitoring.scrub()` 을 지나 연락처·주민번호 꼴을 가린 뒤 500자로 자른다.
   참여자 키는 이음이 준 익명 키만 받는다.
 - **실전송은 스위치로만**: 공유 비밀(`EUM_CHANNEL_SECRET`)이 없거나 `send` 가 거짓이면 보내지 않고
   페이로드만 돌려준다(dry-run). 전송 실패는 숨기지 않는다(호출부가 502 로 드러낸다).
 - **SSRF**: 보낼 주소는 `_urlguard` 를 지나고, 허용 호스트 기본값은 이음 운영 도메인 하나다.

환경변수
  EUM_CHANNEL_SECRET   이음 CHANNEL_SECRET 과 같은 값(16자 이상). 없으면 dry-run 만
  EUM_REQUEST_URL      기본 https://eum.gowon.co.kr/api/channel/aicc/request
  EUM_REQUEST_HOSTS    허용 호스트(콤마). 기본 eum.gowon.co.kr
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

try:
    from _monitoring import scrub as _scrub
except Exception:  # 모니터링 모듈이 없어도 동작해야 한다
    def _scrub(v):
        return v if isinstance(v, str) else str(v)

import _urlguard  # noqa: E402  — 보안 통제라 폴백을 두지 않는다(fail-closed)

DEFAULT_URL = "https://eum.gowon.co.kr/api/channel/aicc/request"
DEFAULT_HOSTS = ("eum.gowon.co.kr",)
CHANNEL_ID = "phone"            # 이음 CHANNEL_KEY_CHANNELS 는 chat·phone 두 가지 — 음성은 phone
SUMMARY_MAX = 500
TIMEOUT = 8.0

# 분류 라벨 — 이음 `kindFromVoice` 가 읽는 이름과 같다(이음 쪽 유형: schedule·onsite·senior·urgent·other)
LABELS = ("emergency", "schedule_change", "onsite_issue", "senior_concern", "other")
LABEL_KO = {
    "emergency": "긴급 연결",
    "schedule_change": "일정 변경",
    "onsite_issue": "활동 중 문제",
    "senior_concern": "어르신 상태 걱정",
    "other": "기타 요청",
}
# 앞에 있는 유형이 먼저다 — 긴급은 다른 낱말과 섞여 있어도 긴급이다.
_RULES = (
    ("emergency", ("119", "쓰러", "의식", "숨을", "숨이", "피가", "피를", "응급", "위급", "다치셨", "다쳤", "넘어지셨", "사고")),
    ("schedule_change", ("일정", "시간 변경", "시간을 바", "날짜", "못 가", "못가", "늦을", "늦어", "취소", "미루", "바꾸")),
    ("senior_concern", ("어르신", "할머니", "할아버지", "기운", "식사를 안", "드시지", "우울", "아프시", "아파하", "걱정")),
    ("onsite_issue", ("현장", "활동 중", "문이 잠", "연락이 안", "열쇠", "물건", "돈", "다툼", "불편", "문제")),
)
_PID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def classify(text):
    """(label, 근거 낱말 목록). 같은 글이면 항상 같은 답이다."""
    t = re.sub(r"\s+", " ", str(text or ""))
    for label, words in _RULES:
        hit = [w for w in words if w in t]
        if hit:
            return label, hit
    return "other", []


def clean_summary(text):
    """연락처·주민번호 꼴을 가리고 500자로 자른다(원문 발화를 그대로 보내지 않는다)."""
    s = _scrub(re.sub(r"\s+", " ", str(text or "")).strip())
    return s[:SUMMARY_MAX]


def build_payload(participant_id, transcript, ref=None):
    pid = str(participant_id or "").strip()
    if not _PID.match(pid):
        raise ValueError("participant_id 형식이 올바르지 않습니다")
    summary = clean_summary(transcript)
    if len(summary) < 2:
        raise ValueError("요청 내용(transcript)이 비어 있습니다")
    label, evidence = classify(summary)
    payload = {
        "participant_id": pid,
        "kind_label": label,
        "summary": summary,
        "ref": str(ref or "") or ("vr_" + uuid.uuid4().hex[:12]),
    }
    return payload, {"label": label, "label_ko": LABEL_KO[label], "evidence": evidence}


def _secret():
    return (os.environ.get("EUM_CHANNEL_SECRET") or "").strip()


def target_url():
    return (os.environ.get("EUM_REQUEST_URL") or "").strip() or DEFAULT_URL


def _hosts():
    hosts = _urlguard.env_hosts("EUM_REQUEST_HOSTS")
    return hosts or set(DEFAULT_HOSTS)


def sign(body, secret, ts=None):
    """이음 `verifyChannelIntake` 와 같은 서명 — HMAC-SHA256("<ts>.<body>")."""
    ts = str(ts if ts is not None else int(time.time()))
    if isinstance(body, str):
        body = body.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), ts.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    return ts, "sha256=" + mac


def deliver(url, payload, secret, timeout=TIMEOUT):
    """한 번 보낸다. 실패를 삼키지 않고 사유를 돌려준다(URL·비밀은 사유에 담지 않는다)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts, sig = sign(body, secret)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "aicc-eum-request/1",
        "X-Channel-Id": CHANNEL_ID,
        "X-Channel-Timestamp": ts,
        "X-Channel-Signature": sig,
    }
    out = {"delivered": False, "signed": True, "status": None, "error": None}
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            out["delivered"] = 200 <= out["status"] < 300
            if not out["delivered"]:
                out["error"] = "이음이 %d 로 응답했습니다" % out["status"]
    except urllib.error.HTTPError as e:
        out["status"] = int(getattr(e, "code", 0) or 0)
        out["error"] = "이음이 %s 로 응답했습니다" % (out["status"] or "오류")
    except Exception as e:
        out["error"] = "이음 전송 실패(%s)" % type(e).__name__
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


def run(participant_id, transcript, ref=None, send=False, deliver_fn=None, retry_fn=None):
    """분류·정리 → (스위치가 켜져 있으면) 이음으로 전송.

    반환: {ok, payload, classification, mode: "dry-run"|"sent", delivery?, note?}
    `retry_fn` 은 wellbeing.deliver_with_retry 를 넘겨받는다(재시도·실패 보관 규약을 한 벌로 쓴다).
    """
    payload, cls = build_payload(participant_id, transcript, ref)
    secret = _secret()
    if not send or len(secret) < 16:
        note = ("전송하지 않았습니다(dry-run). 공유 비밀(EUM_CHANNEL_SECRET)을 등록하고 send=true 로 보내면 이음 소통창에 접수됩니다."
                if len(secret) < 16 else "send=true 로 보내면 이음 소통창에 접수됩니다.")
        return {"ok": True, "mode": "dry-run", "payload": payload, "classification": cls, "note": note}
    url = target_url()
    ok, reason = _urlguard.check(url, label="eum_request_url", allow_insecure=False, allowlist=_hosts())
    if not ok:
        return {"ok": False, "mode": "sent", "payload": payload, "classification": cls,
                "delivery": {"delivered": False, "error": "보낼 주소가 허용 범위 밖입니다: " + reason}}
    fn = deliver_fn or (lambda u, pl: deliver(u, pl, secret))
    delivery = retry_fn(url, payload, deliver_fn=fn) if retry_fn else fn(url, payload)
    return {"ok": bool(delivery.get("delivered")), "mode": "sent", "payload": payload,
            "classification": cls, "delivery": delivery}


if __name__ == "__main__":     # pragma: no cover — 네트워크 미사용 셀프테스트
    assert classify("어르신이 쓰러지셨어요 일정도 바꿔야")[0] == "emergency"
    assert classify("내일 활동 시간을 바꾸고 싶어요")[0] == "schedule_change"
    assert classify("할머니가 식사를 안 하세요")[0] == "senior_concern"
    assert classify("현관 문이 잠겨 있어요")[0] == "onsite_issue"
    assert classify("그냥 여쭤볼게요")[0] == "other"
    p, c = build_payload("u-par-1", "연락처 010-1234-5678 로 전화 주세요 일정 변경", "r1")
    assert "1234-5678" not in p["summary"], p
    r = run("u-par-1", "내일 늦을 것 같아요")
    assert r["mode"] == "dry-run" and r["payload"]["kind_label"] == "schedule_change"
    ts, sg = sign(b'{"a":1}', "x" * 16, 1000)
    assert sg.startswith("sha256=") and ts == "1000"
    print("SELF-TEST OK", json.dumps(r["payload"], ensure_ascii=False))
