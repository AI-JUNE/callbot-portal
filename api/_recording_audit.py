"""녹취·감사로그 — 백로그 P0-5 + 통화 개인정보 **암호화·파기 배선**.

원칙: build now, activate on approval.
- 순수 모듈(핸들러 없음) → Vercel 함수로 노출되지 않음.
- 실데이터 OFF: RECORDING_LIVE 플래그(기본 off)가 켜지기 전에는 어떤 오디오·전사
  원문도 저장하지 않는다. 켜는 것은 [승인 필요] — 통신비밀보호법·개인정보보호법상
  녹취 고지·동의 절차와 보관 체계를 사람이 승인한 뒤에만 가능.
- 여기서는 (1) 저장 스키마 정의 (2) sim 저장소(메타만) (3) 접근 감사로그
  (4) 보관/파기 정책 계산 (5) **저장되는 참조의 봉인·복호·암호 파기**까지 구현한다.

암호화 배선(2026-09-17 추가)
  저장 위치 참조(`audio_ref`·`transcript_ref`)는 그 자체로 개인정보에 이르는 열쇠다.
  RECORDING_LIVE + 동의가 모두 참일 때만 참조를 받고, 받는 즉시 `pii_vault.seal()` 로
  봉인해 보관한다. 레코드에는 **봉투 문자열만** 남는다.
  - 키(`PII_MASTER_KEY`)가 없으면 **평문으로 저장하지 않고 참조를 폐기**한다
    (`protection="unavailable"`, 감사기록 `register_refs_dropped`). 조용한 평문 폴백 없음.
  - 복호는 `reveal()` 한 곳뿐이며 목적 화이트리스트·감사기록을 거친다.
  - 파기는 둘로 나뉜다.
      · `purge_due()`   보관기간 만료 → 봉투 자체를 버린다(hard_delete/anonymize).
        **이것을 주기적으로 부르는 배선은 아직 없다**([승인 필요] — 되돌릴 수
        없는 동작이라 호출 시점·주체 설계가 먼저다). 그래서 보관기간이 지난
        레코드는 `active` 로 남는다. 2026-10-28(40차)부터 그 건수를
        `stats().expired_unpurged` 로 드러낸다 — 숫자로 말하지 않으면 `active`
        가 「보관기간 안에 있는 녹취」로 읽힌다.
      · `shred_record()` 만료 전 즉시 파기(정보주체 삭제요구 등) → 복호 재료만 지운
        **묘비**를 남긴다. 백업에 사본이 남아 있어도 복호 불가이고, 무엇이 있었다는
        사실은 감사에 남는다.

스키마(설계):
  RecordingMeta   — 통화 1건의 녹취 메타. 오디오/전사 '참조'만 갖고 원문은 없음.
  AccessAudit     — 누가(actor) 언제 무엇을(record_id) 왜(purpose) 접근했는지.
  RetentionPolicy — 보관 일수·파기 방식. 만료 산정과 파기 대상 조회 제공.

셀프테스트:
  python api/recording_audit.py
"""
import os
import sys
import time
import itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _pii_vault as pii_vault    # noqa: E402  (의존성 0 · 같은 폴더)

# ── 활성화 게이트 ────────────────────────────────────────────────────────────
def recording_live():
    """실녹취 저장 활성 여부. 기본 off. 켜는 것은 [승인 필요](자동 전환 금지)."""
    return (os.environ.get("RECORDING_LIVE") or "").strip().lower() in ("1", "true", "on")

# 보관 정책 기본값(설계 제안; 실제 일수는 도급 계약·법무 검토 후 확정 [승인 필요])
DEFAULT_RETENTION_DAYS = 90        # 통화 메타·전사 보관 일수(제안)
DEFAULT_PURGE_METHOD = "hard_delete"  # 파기 방식: hard_delete | anonymize
PURGE_METHODS = ("hard_delete", "anonymize")

ALLOWED_PURPOSES = ("qa", "dispute", "audit", "training_optout_check")  # 접근 목적 화이트리스트

# ── 보관 상한(인스턴스 메모리) ───────────────────────────────────────────────
# 영속 저장소는 [승인 필요]다. 그래서 이 장부는 프로세스 메모리에만 있고,
# 상한이 없으면 웜 인스턴스가 사는 동안 무한히 자란다 — 26차 `_escalation.QUEUE`,
# 30차 `voice._Session` 과 **같은 결함**이고 끝도 같다: 메모리가 자라 결국
# 진행 중인 통화가 죽는다. `purge_due()` 는 레코드를 **지우지 않고** 묘비로
# 바꾸기만 하므로(파기 증적) 파기해도 건수는 줄지 않는다 — 상한이 더 필요한 쪽이다.
# 눈금은 `_escalation.MAX_TICKETS`·`_call_metrics.MAX_RECORDS` 규약을 따른다.
MAX_RECORDS = 2000   # 녹취 메타 상한(초과 시 **파기된 것부터** 버린다)
MAX_AUDIT = 2000     # 접근 감사 기록 상한(오래된 것부터)

# 참조 보호 상태
PROTECTION_NONE = "none"                # 보관 중인 참조가 없음(기본)
PROTECTION_SEALED = "sealed"            # 봉인 보관 중
PROTECTION_UNAVAILABLE = "unavailable"  # 키 미설정 → 참조를 받지 않고 폐기함
PROTECTION_SHREDDED = "shredded"        # 암호 파기됨(복구 불가)


def _meta_schema():
    """RecordingMeta 스키마(문서화용). 오디오·전사 원문 필드는 의도적으로 없음."""
    return {
        "record_id": "str  REC-xxxx",
        "session_id": "str  sim 세션/콜 식별자(전화번호 아님)",
        "scenario": "str  refund/integrity/...",
        "started_at": "float epoch",
        "duration_sec": "int",
        "consent": "bool  녹취 고지·동의 여부(없으면 저장 금지)",
        "audio_ref": "str|None  오디오 저장 위치 **봉투**(평문 아님, sim=None)",
        "transcript_ref": "str|None  전사 저장 위치 **봉투**(평문 아님, sim=None)",
        "expires_at": "float  보관 만료(epoch)",
        "state": "active | purged",
        "protection": "none | sealed | unavailable | shredded",
    }


def _context(record_id, field):
    """봉투가 묶이는 문맥. 다른 레코드·다른 필드의 봉투를 옮겨 심을 수 없다."""
    return "%s/%s" % (record_id, field)


class RecordingStore:
    """sim 녹취 저장소 — 메타만 보관. 오디오·전사 원문은 어떤 경우에도 저장하지 않는다.

    RECORDING_LIVE(기본 off)가 꺼져 있으면 register()는 audio_ref/transcript_ref를
    항상 None으로 강제한다(이중 방어). 켜져 있고 동의가 있으면 참조를 **봉인**해
    보관하며, 봉인할 수 없으면(키 미설정) 평문 대신 참조를 폐기한다.
    실저장 연동은 [승인 필요].
    """

    def __init__(self, retention_days=DEFAULT_RETENTION_DAYS):
        self._seq = itertools.count(1)
        self._records = {}
        self._audit = []
        self.retention_days = retention_days
        # 버린 건수 — 조용한 삭제 금지. stats() 로 드러나고 ops_stats 가 내보낸다.
        self.dropped = 0          # 상한 초과로 버린 레코드
        self.dropped_active = 0   # 그중 **아직 보관기간이 남아 있던** 것(0 이어야 정상)
        self.audit_dropped = 0    # 상한 초과로 버린 감사 기록 줄 수

    # ── 보관 상한 ───────────────────────────────────────────────────────
    def _evict(self):
        """상한 초과 시 **파기된 레코드를 먼저** 버린다.

        순서가 중요하다. active 를 먼저 버리면 아직 보관기간이 남은 녹취가
        장부에서 사라져 `purge_due()` 가 영영 찾지 못한다 — 파기해야 할 것이
        파기되지 않은 채 '없는 일'이 되는 쪽이 가장 나쁘다. purged 는 봉투도
        참조도 없는 묘비라 먼저 버려도 잃는 것이 적다(파기 사실 자체는 감사
        기록에 남는다).

        묘비가 하나도 없으면 가장 오래된 것을 버리되 `dropped_active` 로 따로
        센다. 그 숫자가 0 이 아니면 용량이 실제로 모자란다는 뜻이고, 상한을
        올릴 것이 아니라 **영속 저장소**를 봐야 한다([승인 필요]).

        봉인된 참조는 버리기 전에 암호 파기한다 — 레코드 dict 사본이 어딘가
        남아 있어도 복호 재료가 함께 사라진다.
        """
        while len(self._records) >= MAX_RECORDS:
            # (파기 안 됨, 오래된 순) — False(=0) 인 purged 가 먼저 뽑힌다.
            victim = min(self._records.values(),
                         key=lambda r: (r["state"] == "active", r["started_at"]))
            if victim["protection"] == PROTECTION_SEALED:
                for field in ("audio_ref", "transcript_ref"):
                    if victim.get(field):
                        try:
                            victim[field] = pii_vault.shred(victim[field])
                        except Exception:   # pragma: no cover - 파기 실패가 축출을 막지 않는다
                            victim[field] = None
            del self._records[victim["record_id"]]
            self.dropped += 1
            if victim["state"] == "active":
                self.dropped_active += 1
            # 버린 사실은 감사에 남긴다(세션ID·참조는 적지 않는다 — 고정 문구만).
            self._log("system", victim["record_id"], "evict",
                      "보관 상한 %d건 초과 — state=%s" % (MAX_RECORDS, victim["state"]))

    # ── 등록 ────────────────────────────────────────────────────────────
    def register(self, session_id, scenario="", duration_sec=0, consent=False,
                 audio_ref=None, transcript_ref=None, now=None):
        """통화 1건의 녹취 메타 등록. 동의(consent) 없으면 참조도 받지 않는다."""
        now = time.time() if now is None else now
        live = recording_live()
        if not live or not consent:
            audio_ref = None       # 이중 방어: live 아니거나 미동의면 원문 참조 자체를 버림
            transcript_ref = None
        rid = "REC-%04d" % next(self._seq)

        protection = PROTECTION_NONE
        dropped = False
        if audio_ref is not None or transcript_ref is not None:
            if pii_vault.available():
                if audio_ref is not None:
                    audio_ref = pii_vault.seal(audio_ref, _context(rid, "audio"))
                if transcript_ref is not None:
                    transcript_ref = pii_vault.seal(transcript_ref, _context(rid, "transcript"))
                protection = PROTECTION_SEALED
            else:
                # 키가 없다 → 평문으로 남기지 않는다. 참조를 버리고 사실을 기록한다.
                audio_ref = transcript_ref = None
                protection = PROTECTION_UNAVAILABLE
                dropped = True

        rec = {
            "record_id": rid, "session_id": session_id, "scenario": scenario,
            "started_at": now, "duration_sec": int(duration_sec or 0),
            "consent": bool(consent),
            "audio_ref": audio_ref, "transcript_ref": transcript_ref,
            "expires_at": now + self.retention_days * 86400,
            "state": "active", "protection": protection,
        }
        self._evict()                   # 넣기 전에 자리를 비운다(상한을 넘기지 않는다)
        self._records[rid] = rec
        self._log("system", rid, "register",
                  "consent=%s live=%s protection=%s" % (bool(consent), live, protection))
        if dropped:
            # 삼키지 않는다 — 참조를 버렸다는 사실이 감사기록에 남는다.
            self._log("system", rid, "register_refs_dropped",
                      "암호화 키(%s) 미설정 — 평문 저장 대신 참조 폐기" % pii_vault.KEY_ENV)
        return dict(rec)

    # ── 접근(감사로그 필수) ─────────────────────────────────────────────
    def access(self, actor, record_id, purpose):
        """메타 열람. 목적이 화이트리스트에 없으면 거부하고 거부 사실도 감사기록.

        반환에 실린 참조는 **봉투**다(평문 아님). 평문이 필요하면 reveal() 을 쓴다.
        """
        if purpose not in ALLOWED_PURPOSES:
            self._log(actor, record_id, "access_denied", "목적 불허: %s" % purpose)
            raise PermissionError("access purpose not allowed: %s" % purpose)
        rec = self._records.get(record_id)
        if rec is None or rec["state"] != "active":
            self._log(actor, record_id, "access_miss", purpose)
            raise KeyError("record not found or purged: %s" % record_id)
        self._log(actor, record_id, "access", purpose)
        return dict(rec)

    def reveal(self, actor, record_id, purpose):
        """봉인된 참조를 **복호**한다. 접근 통제·감사는 access() 와 같은 규칙.

        반환 {"record_id", "audio_ref", "transcript_ref", "protection"} — 평문 참조.
        복호할 것이 없으면 값은 None 이고, 그 사실도 감사에 남는다.
        복호 실패(변조·키 교체 후 구키 미등록)는 삼키지 않고 예외로 올린다.
        """
        rec = self.access(actor, record_id, purpose)     # 목적·존재 검사 + access 기록
        out = {"record_id": record_id, "protection": rec["protection"],
               "audio_ref": None, "transcript_ref": None}
        if rec["protection"] != PROTECTION_SEALED:
            self._log(actor, record_id, "reveal_empty", rec["protection"])
            return out
        try:
            for field in ("audio_ref", "transcript_ref"):
                env = rec.get(field)
                if env:
                    out[field] = pii_vault.unseal(env, _context(record_id, field.split("_")[0]))
        except pii_vault.VaultError as e:
            self._log(actor, record_id, "reveal_failed", type(e).__name__)   # 사유 문구는 남기지 않음
            raise
        self._log(actor, record_id, "reveal", purpose)
        return out

    # ── 보관/파기 ───────────────────────────────────────────────────────
    def purge_due(self, now=None, method=DEFAULT_PURGE_METHOD):
        """보관 만료 레코드 파기(sim). 실스토리지 파기 연동은 [승인 필요].

        method 는 PURGE_METHODS 중 하나여야 한다. 오타(예: "anonymise")를 조용히
        hard_delete 로 처리하면 정책과 다른 파기가 감사기록에 남는다 — 즉시 거부.
        봉투는 묘비도 남기지 않고 버린다(만료 파기는 완전 삭제).
        """
        if method not in PURGE_METHODS:
            raise ValueError("unknown purge method: %s" % method)
        now = time.time() if now is None else now
        purged = []
        for rec in self._records.values():
            if rec["state"] == "active" and rec["expires_at"] <= now:
                was_sealed = rec["protection"] == PROTECTION_SEALED
                rec["state"] = "purged"
                rec["audio_ref"] = None
                rec["transcript_ref"] = None
                if was_sealed:
                    rec["protection"] = PROTECTION_SHREDDED
                if method == "anonymize":
                    rec["session_id"] = "anon"
                self._log("system", rec["record_id"], "purge", method)
                purged.append(rec["record_id"])
        return purged

    def shred_record(self, record_id, actor="system", reason=""):
        """만료 전 **즉시 암호 파기**(정보주체 삭제요구·오등록 등).

        봉투에서 복호 재료만 지운 묘비를 남긴다 — 백업에 사본이 남아도 복구 불가.
        레코드 자체는 살아 있어 "무엇이 언제 파기됐는지"를 감사로 증명할 수 있다.
        이미 파기된 레코드에 다시 요청해도 안전하다(멱등, 중복 기록 없음).
        """
        rec = self._records.get(record_id)
        if rec is None:
            self._log(actor, record_id, "shred_miss", reason or "-")
            raise KeyError("record not found: %s" % record_id)
        if rec["protection"] != PROTECTION_SEALED:
            return dict(rec)                     # 파기할 봉투가 없다 — 무기록·무변경
        for field in ("audio_ref", "transcript_ref"):
            if rec.get(field):
                rec[field] = pii_vault.shred(rec[field])
        rec["protection"] = PROTECTION_SHREDDED
        self._log(actor, record_id, "shred", reason or "-")
        return dict(rec)

    def overdue(self, now=None):
        """보관기간이 지났는데 **아직 파기되지 않은** 레코드 → (건수, 가장 오래 지난 초).

        **읽기만 한다 — 파기하지 않는다.** 파기는 되돌릴 수 없어서 호출 시점·주체
        설계가 먼저이고([승인 필요]), 그래서 `purge_due()` 를 부르는 배선이 아직
        없다. 그 사실을 숫자로 말하지 않으면 `active` 가 「보관기간 안에 있는
        녹취」로 읽힌다 — 레코드마다 `expires_at` 을 계산해 두지만 그 값을 읽는
        곳이 `purge_due()` 하나뿐이었으므로, **보관정책이 장식**이었다.
        시간이 지나면 이 숫자는 `active` 전체가 된다.

        모르는 것·못 한 것은 집계에 섞지 않고 카운터로 남긴다
        (`_call_metrics.stale_opens`·`orphan_finishes` 와 같은 판단).
        """
        now = time.time() if now is None else now
        n, oldest = 0, 0.0
        for r in self._records.values():
            if r.get("state") != "active":
                continue
            try:
                exp = float(r.get("expires_at") or 0)
            except Exception:          # pragma: no cover - 외부가 넣은 값 방어
                continue
            if exp <= 0 or exp > now:
                continue
            n += 1
            over = now - exp
            if over > oldest:
                oldest = over
        return n, int(oldest)

    def stats(self, now=None):
        """현황 + **이 장부의 성질**.

        숫자만 내보내면 소비자는 영속 장부로 읽는다. `scope`·`volatile` 로
        "인스턴스 메모리이고 재시작하면 사라진다"를 함께 말한다
        (`_escalation.QUEUE.stats()` 와 같은 규약).

        조회는 장부를 **바꾸지 않는다** — 보관 만료분도 세기만 한다(`overdue`).
        """
        s = {"active": 0, "purged": 0}
        for r in self._records.values():
            s[r["state"]] = s.get(r["state"], 0) + 1
        s["total"] = len(self._records)
        s["dropped"] = self.dropped
        s["dropped_active"] = self.dropped_active
        s["audit_dropped"] = self.audit_dropped
        # 보관기간이 지났는데 파기되지 않은 건수 + 가장 오래 지난 시간.
        # 0 이 아니면 `active` 를 「보관 중」으로 읽을 수 없다는 뜻이다.
        due, oldest = self.overdue(now)
        s["expired_unpurged"] = due
        s["oldest_expired_sec"] = oldest
        s["retention_days"] = int(self.retention_days)
        s["capacity"] = MAX_RECORDS
        s["scope"] = "instance"      # 클러스터 합산 아님
        s["volatile"] = True         # 재시작 시 소멸(영속 저장소 [승인 필요])
        return s

    def protection_stats(self):
        """참조 보호 상태 집계 — 키 미설정으로 버려진 참조가 있는지 드러낸다."""
        s = {PROTECTION_NONE: 0, PROTECTION_SEALED: 0,
             PROTECTION_UNAVAILABLE: 0, PROTECTION_SHREDDED: 0}
        for r in self._records.values():
            s[r["protection"]] = s.get(r["protection"], 0) + 1
        s["vault_available"] = pii_vault.available()
        return s

    def audit_log(self):
        """항목까지 복사 — 반환값을 고쳐도 원본은 변하지 않는다.

        **이 버퍼는 감사 원본이 아니다.** 인스턴스 메모리이고 최근
        `MAX_AUDIT` 줄만 들고 있다(재시작하면 사라진다). 법정 보존이 필요한
        감사 원본은 영속 저장소 몫이고 그건 [승인 필요] 다 — 잘려 나간 줄 수는
        `stats()["audit_dropped"]` 로 드러난다(모르는 채로 쓰지 않도록).
        """
        return [dict(a) for a in self._audit]

    def _log(self, actor, record_id, action, note=""):
        self._audit.append({"ts": time.time(), "actor": actor,
                            "record": record_id, "action": action, "note": note})
        if len(self._audit) > MAX_AUDIT:
            # append-only 성질(기록을 고치지 않는다)은 유지하되 상한은 둔다 —
            # 무한 성장은 진행 중인 통화를 죽인다. 버린 줄 수는 숨기지 않는다.
            cut = len(self._audit) - MAX_AUDIT
            del self._audit[:cut]
            self.audit_dropped += cut


STORE = RecordingStore()  # 모듈 전역(프로세스 단위 sim 저장소)


if __name__ == "__main__":     # pragma: no cover
    import base64
    assert not recording_live(), "테스트는 RECORDING_LIVE off 전제"
    st = RecordingStore(retention_days=90)

    # 1) live off → 참조를 넘겨도 저장되지 않아야 함(이중 방어)
    r = st.register("sim-refund", "refund", 120, consent=True,
                    audio_ref="s3://x", transcript_ref="s3://y")
    assert r["audio_ref"] is None and r["transcript_ref"] is None
    assert r["protection"] == PROTECTION_NONE

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

    # 4) live on + 동의 + 키 없음 → 평문 저장이 아니라 폐기
    os.environ["RECORDING_LIVE"] = "1"
    os.environ.pop(pii_vault.KEY_ENV, None)
    st2 = RecordingStore()
    d = st2.register("sim-live", "refund", 10, consent=True, audio_ref="s3://a")
    assert d["audio_ref"] is None and d["protection"] == PROTECTION_UNAVAILABLE
    assert any(a["action"] == "register_refs_dropped" for a in st2.audit_log())

    # 5) 키 등록 후 → 봉인 저장, 평문은 어디에도 없음, reveal 로만 복호
    os.environ[pii_vault.KEY_ENV] = base64.b64encode(b"K" * 32).decode()
    st3 = RecordingStore()
    s = st3.register("sim-live", "refund", 10, consent=True,
                     audio_ref="s3://a/rec.wav", transcript_ref="s3://a/rec.txt")
    assert s["protection"] == PROTECTION_SEALED
    assert "s3://" not in repr(s), "평문 참조가 레코드에 남았다"
    rv = st3.reveal("agent-01", s["record_id"], "dispute")
    assert rv["audio_ref"] == "s3://a/rec.wav" and rv["transcript_ref"] == "s3://a/rec.txt"

    # 6) 즉시 암호 파기 → 복호 불가, 멱등
    st3.shred_record(s["record_id"], actor="dpo", reason="삭제요구")
    assert st3._records[s["record_id"]]["protection"] == PROTECTION_SHREDDED
    try:
        st3.reveal("agent-01", s["record_id"], "dispute")
    except pii_vault.VaultError:
        print("FAIL: 파기 후 reveal 이 예외를 던졌다(빈 응답이어야 함)")
    before = len(st3.audit_log())
    st3.shred_record(s["record_id"], actor="dpo")
    assert len(st3.audit_log()) == before, "재파기가 중복 기록됨"

    os.environ.pop("RECORDING_LIVE"); os.environ.pop(pii_vault.KEY_ENV)

    # 7) 보관 만료 파기
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

    # 8) 보관 상한 — 넘겨서 확인한다(방어 코드는 초과 때만 돈다)
    st4 = RecordingStore()
    for i in range(MAX_RECORDS + 10):
        st4.register("sim-%d" % i, "refund", 1, consent=False)
    s4 = st4.stats()
    assert s4["total"] <= MAX_RECORDS and s4["dropped"] > 0, s4
    assert len(st4.audit_log()) <= MAX_AUDIT, len(st4.audit_log())

    print("SELF-TEST OK:", st.stats(), st3.protection_stats(),
          "audit=%d" % len(st.audit_log()), "caps=%s" % s4)
