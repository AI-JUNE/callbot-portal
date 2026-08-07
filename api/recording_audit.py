"""녹취·감사로그 설계·스텁 — 백로그 P0-5.

원칙: build now, activate on approval.
- 순수 모듈(핸들러 없음) → Vercel 함수로 노출되지 않음.
- 실데이터 OFF: RECORDING_LIVE 플래그(기본 off)가 켜지기 전에는 어떤 오디오·전사
  원문도 저장하지 않는다. 켜는 것은 [승인 필요] — 통신비밀보호법·개인정보보호법상
  녹취 고지·동의 절차와 보관 체계를 사람이 승인한 뒤에만 가능.
- 여기서는 (1) 저장 스키마 정의 (2) sim 저장소(메타만) (3) 접근 감사로그
  (4) 보관/파기 정책 계산까지만 구현한다.

스키마(설계):
  RecordingMeta   — 통화 1건의 녹취 메타. 오디오/전사 '참조'만 갖고 원문은 없음.
  TranscriptRef   — 전사 저장 위치 참조(sim에서는 항상 저장 안 함 표시).
  AccessAudit     — 누가(actor) 언제 무엇을(record_id) 왜(purpose) 접근했는지.
  RetentionPolicy — 보관 일수·파기 방식. 만료 산정과 파기 대상 조회 제공.

셀프테스트:
  python api/recording_audit.py
"""
import os
import time
import itertools

# ── 활성화 게이트 ────────────────────────────────────────────────────────────
def recording_live():
    """실녹취 저장 활성 여부. 기본 off. 켜는 것은 [승인 필요](자동 전환 금지)."""
    return (os.environ.get("RECORDING_LIVE") or "").strip().lower() in ("1", "true", "on")

# 보관 정책 기본값(설계 제안; 실제 일수는 도급 계약·법무 검토 후 확정 [승인 필요])
DEFAULT_RETENTION_DAYS = 90        # 통화 메타·전사 보관 일수(제안)
DEFAULT_PURGE_METHOD = "hard_delete"  # 파기 방식: hard_delete | anonymize

ALLOWED_PURPOSES = ("qa", "dispute", "audit", "training_optout_check")  # 접근 목적 화이트리스트


def _meta_schema():
    """RecordingMeta 스키마(문서화용). 오디오·전사 원문 필드는 의도적으로 없음."""
    return {
        "record_id": "str  REC-xxxx",
        "session_id": "str  sim 세션/콜 식별자(전화번호 아님)",
        "scenario": "str  refund/integrity/...",
        "started_at": "float epoch",
        "duration_sec": "int",
        "consent": "bool  녹취 고지·동의 여부(없으면 저장 금지)",
        "audio_ref": "str|None  오디오 저장 위치 참조(sim=None)",
        "transcript_ref": "str|None  전사 저장 위치 참조(sim=None)",
        "expires_at": "float  보관 만료(epoch)",
        "state": "active | purged",
    }


class RecordingStore:
    """sim 녹취 저장소 — 메타만 보관. 오디오·전사 원문은 어떤 경우에도 저장하지 않는다.

    RECORDING_LIVE(기본 off)가 꺼져 있으면 register()는 audio_ref/transcript_ref를
    항상 None으로 강제한다(이중 방어). 실저장 연동은 [승인 필요].
    """

    def __init__(self, retention_days=DEFAULT_RETENTION_DAYS):
        self._seq = itertools.count(1)
        self._records = {}
        self._audit = []
        self.retention_days = retention_days

    # ── 등록 ────────────────────────────────────────────────────────────
    def register(self, session_id, scenario="", duration_sec=0, consent=False,
                 audio_ref=None, transcript_ref=None, now=None):
        """통화 1건의 녹취 메타 등록. 동의(consent) 없으면 참조도 받지 않는다."""
        now = time.time() if now is None else now
        if not recording_live() or not consent:
            audio_ref = None       # 이중 방어: live 아니거나 미동의면 원문 참조 자체를 버림
            transcript_ref = None
        rid = "REC-%04d" % next(self._seq)
        rec = {
            "record_id": rid, "session_id": session_id, "scenario": scenario,
            "started_at": now, "duration_sec": int(duration_sec or 0),
            "consent": bool(consent),
            "audio_ref": audio_ref, "transcript_ref": transcript_ref,
            "expires_at": now + self.retention_days * 86400,
            "state": "active",
        }
        self._records[rid] = rec
        self._log("system", rid, "register", "consent=%s live=%s" % (bool(consent), recording_live()))
        return dict(rec)

    # ── 접근(감사로그 필수) ─────────────────────────────────────────────
    def access(self, actor, record_id, purpose):
        """메타 열람. 목적이 화이트리스트에 없으면 거부하고 거부 사실도 감사기록."""
        if purpose not in ALLOWED_PURPOSES:
            self._log(actor, record_id, "access_denied", "목적 불허: %s" % purpose)
            raise PermissionError("access purpose not allowed: %s" % purpose)
        rec = self._records.get(record_id)
        if rec is None or rec["state"] != "active":
            self._log(actor, record_id, "access_miss", purpose)
            raise KeyError("record not found or purged: %s" % record_id)
        self._log(actor, record_id, "access", purpose)
        return dict(rec)

    # ── 보관/파기 ───────────────────────────────────────────────────────
    def purge_due(self, now=None, method=DEFAULT_PURGE_METHOD):
        """보관 만료 레코드 파기(sim). 실스토리지 파기 연동은 [승인 필요]."""
        now = time.time() if now is None else now
        purged = []
        for rec in self._records.values():
            if rec["state"] == "active" and rec["expires_at"] <= now:
                rec["state"] = "purged"
                rec["audio_ref"] = None
                rec["transcript_ref"] = None
                if method == "anonymize":
                    rec["session_id"] = "anon"
                self._log("system", rec["record_id"], "purge", method)
                purged.append(rec["record_id"])
        return purged

    def stats(self):
        s = {"active": 0, "purged": 0}
        for r in self._records.values():
            s[r["state"]] = s.get(r["state"], 0) + 1
        s["total"] = len(self._records)
        return s

    def audit_log(self):
        return list(self._audit)

    def _log(self, actor, record_id, action, note=""):
        self._audit.append({"ts": time.time(), "actor": actor,
                            "record": record_id, "action": action, "note": note})


STORE = RecordingStore()  # 모듈 전역(프로세스 단위 sim 저장소)


if __name__ == "__main__":
    assert not recording_live(), "테스트는 RECORDING_LIVE off 전제"
    st = RecordingStore(retention_days=90)

    # 1) live off → 참조를 넘겨도 저장되지 않아야 함(이중 방어)
    r = st.register("sim-refund", "refund", 120, consent=True,
                    audio_ref="s3://x", transcript_ref="s3://y")
    assert r["audio_ref"] is None and r["transcript_ref"] is None

    # 2) 미동의 등록도 메타만
    r2 = st.register("sim-refund", "refund", 60, consent=False)
    assert r2["consent"] is False and r2["audio_ref"] is None

    # 3) 접근: 허용 목적 OK, 불허 목적 거부+감사기록
    got = st.access("agent-01", r["record_id"], "qa")
    assert got["record_id"] == r["record_id"]
    try:
        st.access("agent-01", r["record_id"], "marketing")
        print("FAIL: 불허 목적 접근 허용됨")
    except PermissionError:
        pass

    # 4) 보관 만료 파기
    future = time.time() + 91 * 86400
    purged = st.purge_due(now=future)
    assert r["record_id"] in purged and r2["record_id"] in purged
    try:
        st.access("agent-01", r["record_id"], "qa")
        print("FAIL: 파기 레코드 접근 허용됨")
    except KeyError:
        pass

    acts = [a["action"] for a in st.audit_log()]
    assert "access_denied" in acts and "purge" in acts and "access_miss" in acts
    print("SELF-TEST OK:", st.stats(), "audit=%d" % len(st.audit_log()))
