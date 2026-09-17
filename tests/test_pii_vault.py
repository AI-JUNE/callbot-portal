# -*- coding: utf-8 -*-
"""api/pii_vault.py 봉인(암호화) + api/recording_audit.py 암호화·파기 배선 회귀 테스트.

검증 대상 (COMMERCIAL_READINESS 'Callbot 전용 — 통화 개인정보 암호화·파기 배선')
  1) 키 관리    — 미설정/짧은 키 거부, 지문(kid)만 노출, 상태에 키 원문 없음
  2) 봉인·복호  — 왕복, 유니콘 문자열, 빈 문자열, 길이 상한, nonce 재사용 없음
  3) 무결성     — 암호문·태그·nonce·kid 변조, 문맥 불일치, 형식 오류 전부 거부
  4) 회전       — 구키 복호, rewrap 후 현행 키, 구키 미등록 시 실패
  5) 암호 파기  — 묘비 복호 불가, 멱등, 미리보기 문자열
  6) 배선       — 게이트/동의/키 조합별 저장 동작, reveal 감사, 즉시 파기, 만료 파기
  7) 유출 금지  — 레코드·감사기록·예외 어디에도 평문 참조가 없다

실행: python3 -m pytest tests/test_pii_vault.py -q
"""
import os
import sys
import time
import base64
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))

import pii_vault           # noqa: E402
import recording_audit     # noqa: E402

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
ENVS = ("PII_MASTER_KEY", "PII_MASTER_KEY_OLD", "RECORDING_LIVE")


class NetworkTouched(AssertionError):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._boom

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _boom(self, *a, **k):
        raise NetworkTouched("봉인 모듈이 네트워크를 건드렸다")

    def use(self, key=KEY_A, old=None):
        os.environ["PII_MASTER_KEY"] = key
        if old:
            os.environ["PII_MASTER_KEY_OLD"] = old


# ==========================================================================
# 1) 키 관리
# ==========================================================================
class TestKeys(Base):
    def test_unavailable_without_key(self):
        self.assertFalse(pii_vault.available())
        self.assertIsNone(pii_vault.status()["kid"])

    def test_seal_refuses_without_key(self):
        """평문 폴백 금지 — 키가 없으면 봉인 대신 예외."""
        with self.assertRaises(pii_vault.VaultUnavailable):
            pii_vault.seal("s3://a", "REC-0001/audio")

    def test_key_formats_accepted(self):
        for raw in (KEY_A, ("a" * 64), ("x" * 40)):     # base64 · hex · 충분히 긴 문자열
            os.environ["PII_MASTER_KEY"] = raw
            self.assertTrue(pii_vault.available(), raw[:8])

    def test_short_key_rejected(self):
        """약한 키를 조용히 받아주지 않는다."""
        for raw in ("", "   ", "short", "abc123", "x" * 31):
            os.environ["PII_MASTER_KEY"] = raw
            self.assertFalse(pii_vault.available(), repr(raw))

    def test_status_never_leaks_key_material(self):
        self.use(KEY_A, old=KEY_B)
        st = pii_vault.status()
        blob = repr(st)
        self.assertNotIn(KEY_A, blob)
        self.assertNotIn(KEY_B, blob)
        self.assertNotIn("A" * 32, blob)
        self.assertEqual(len(st["kid"]), 8)
        self.assertEqual(len(st["retired_kids"]), 1)
        self.assertIn("[승인 필요]", st["note"])

    def test_kid_is_stable_and_distinct(self):
        self.assertEqual(pii_vault.key_id(b"A" * 32), pii_vault.key_id(b"A" * 32))
        self.assertNotEqual(pii_vault.key_id(b"A" * 32), pii_vault.key_id(b"B" * 32))

    def test_old_keys_parse_list_and_ignore_blanks(self):
        self.use(KEY_A, old="%s, ,%s" % (KEY_B, "short"))
        self.assertEqual(len(pii_vault.status()["retired_kids"]), 1)


# ==========================================================================
# 2) 봉인·복호
# ==========================================================================
class TestSealOpen(Base):
    def setUp(self):
        Base.setUp(self)
        self.use()

    def test_roundtrip(self):
        env = pii_vault.seal("s3://bucket/rec.wav", "REC-0001/audio")
        self.assertEqual(pii_vault.unseal(env, "REC-0001/audio"), "s3://bucket/rec.wav")

    def test_ciphertext_hides_plaintext(self):
        env = pii_vault.seal("010-1234-5678 김민수", "REC-0001/audio")
        for leak in ("010", "1234", "5678", "김민수"):
            self.assertNotIn(leak, env, leak)

    def test_unicode_and_empty_and_bytes(self):
        for src in ("", "가나다 ABC 123", "😀 emoji", "a" * 2000):
            env = pii_vault.seal(src, "c")
            self.assertEqual(pii_vault.unseal(env, "c"), src)
        env = pii_vault.seal(b"\x00\x01binary", "c")
        self.assertTrue(pii_vault.is_envelope(env))

    def test_nonce_is_fresh_each_time(self):
        """같은 평문·같은 문맥이라도 봉투가 매번 달라야 한다(패턴 노출 방지)."""
        seen = {pii_vault.seal("same", "c") for _ in range(20)}
        self.assertEqual(len(seen), 20)

    def test_envelope_shape(self):
        env = pii_vault.seal("x", "c")
        parts = env.split(".")
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[0], "pv1")
        self.assertEqual(parts[1], pii_vault.status()["kid"])
        self.assertTrue(pii_vault.is_envelope(env))
        self.assertFalse(pii_vault.is_shredded(env))

    def test_oversize_rejected(self):
        with self.assertRaises(ValueError):
            pii_vault.seal("a" * (pii_vault.MAX_PLAINTEXT + 1), "c")

    def test_none_rejected(self):
        with self.assertRaises(ValueError):
            pii_vault.seal(None, "c")

    def test_envelope_kid_does_not_decrypt(self):
        env = pii_vault.seal("secret-ref", "c")
        os.environ.pop("PII_MASTER_KEY")
        self.assertEqual(len(pii_vault.envelope_kid(env)), 8)   # 키 없이도 지문은 읽힌다
        with self.assertRaises(pii_vault.VaultUnavailable):
            pii_vault.unseal(env, "c")


# ==========================================================================
# 3) 무결성 — 변조·문맥·형식
# ==========================================================================
class TestIntegrity(Base):
    def setUp(self):
        Base.setUp(self)
        self.use()
        self.env = pii_vault.seal("s3://bucket/rec.wav", "REC-0001/audio")

    def _flip(self, index):
        """첫 글자를 바꾼다 — 마지막 글자는 패딩 없는 base64 에서 남는 비트가 있어
        표기만 달라지고 바이트는 같을 수 있다(그 경우는 아래 비정규 표기 시험이 맡는다)."""
        parts = self.env.split(".")
        s = parts[index]
        parts[index] = ("A" if s[0] != "A" else "B") + s[1:]
        return ".".join(parts)

    def test_ciphertext_tamper_rejected(self):
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(self._flip(3), "REC-0001/audio")

    def test_tag_tamper_rejected(self):
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(self._flip(4), "REC-0001/audio")

    def test_nonce_tamper_rejected(self):
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(self._flip(2), "REC-0001/audio")

    def test_kid_tamper_rejected(self):
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.unseal(self._flip(1), "REC-0001/audio")

    def test_context_mismatch_rejected(self):
        """다른 레코드의 봉투를 옮겨 심어도 열리지 않는다."""
        for ctx in ("REC-0002/audio", "REC-0001/transcript", "", "rec-0001/audio"):
            with self.assertRaises(pii_vault.VaultTamper, msg=ctx):
                pii_vault.unseal(self.env, ctx)

    def test_wrong_key_rejected(self):
        os.environ["PII_MASTER_KEY"] = KEY_B
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.unseal(self.env, "REC-0001/audio")

    def test_non_envelope_rejected(self):
        for bad in ("", "s3://plain", "pv1.only.three", "pv2.a.b.c.d", None, 123):
            with self.assertRaises(pii_vault.VaultError, msg=repr(bad)):
                pii_vault.unseal(bad, "c")

    def test_malformed_base64_rejected(self):
        parts = self.env.split(".")
        parts[2] = "!!!!"
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(".".join(parts), "REC-0001/audio")

    def test_non_canonical_base64_rejected(self):
        """마지막 글자만 바꿔 바이트는 같고 표기만 다른 봉투 — 같은 값으로 받아주지 않는다."""
        parts = self.env.split(".")
        tail = parts[3]
        alt = None
        alphabet = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for ch in alphabet:
            cand = tail[:-1] + ch
            if cand == tail:
                continue
            try:
                if base64.urlsafe_b64decode(cand + "=" * (-len(cand) % 4)) == \
                   base64.urlsafe_b64decode(tail + "=" * (-len(tail) % 4)):
                    alt = cand
                    break
            except Exception:
                continue
        self.assertIsNotNone(alt, "이 길이라면 비정규 표기가 반드시 존재한다")
        parts[3] = alt
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(".".join(parts), "REC-0001/audio")

    def test_truncated_nonce_rejected(self):
        parts = self.env.split(".")
        parts[2] = parts[2][:4]
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(".".join(parts), "REC-0001/audio")

    def test_failure_message_has_no_plaintext_or_key(self):
        try:
            pii_vault.unseal(self._flip(3), "REC-0001/audio")
        except pii_vault.VaultError as e:
            msg = str(e)
            self.assertNotIn("s3://", msg)
            self.assertNotIn(KEY_A, msg)


# ==========================================================================
# 4) 키 회전
# ==========================================================================
class TestRotation(Base):
    def test_old_key_still_decrypts(self):
        self.use(KEY_A)
        env = pii_vault.seal("s3://a", "c")
        self.use(KEY_B, old=KEY_A)
        self.assertEqual(pii_vault.unseal(env, "c"), "s3://a")

    def test_rewrap_moves_to_primary(self):
        self.use(KEY_A)
        env = pii_vault.seal("s3://a", "c")
        self.use(KEY_B, old=KEY_A)
        fresh = pii_vault.rewrap(env, "c")
        self.assertNotEqual(pii_vault.envelope_kid(fresh), pii_vault.envelope_kid(env))
        self.assertEqual(pii_vault.envelope_kid(fresh), pii_vault.status()["kid"])
        os.environ.pop("PII_MASTER_KEY_OLD")            # 구키 폐기 후에도 새 봉투는 열린다
        self.assertEqual(pii_vault.unseal(fresh, "c"), "s3://a")

    def test_retired_key_removed_makes_old_envelope_unreadable(self):
        self.use(KEY_A)
        env = pii_vault.seal("s3://a", "c")
        self.use(KEY_B)
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.unseal(env, "c")


# ==========================================================================
# 5) 암호 파기
# ==========================================================================
class TestShred(Base):
    def setUp(self):
        Base.setUp(self)
        self.use()
        self.env = pii_vault.seal("s3://bucket/rec.wav", "REC-0001/audio")

    def test_shredded_cannot_be_opened(self):
        tomb = pii_vault.shred(self.env)
        self.assertTrue(pii_vault.is_shredded(tomb))
        with self.assertRaises(pii_vault.VaultShredded):
            pii_vault.unseal(tomb, "REC-0001/audio")

    def test_shred_is_idempotent(self):
        tomb = pii_vault.shred(self.env)
        self.assertEqual(pii_vault.shred(tomb), tomb)

    def test_shred_keeps_kid_for_audit(self):
        tomb = pii_vault.shred(self.env)
        self.assertEqual(pii_vault.envelope_kid(tomb), pii_vault.envelope_kid(self.env))

    def test_shred_rejects_non_envelope(self):
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.shred("s3://plain")

    def test_safe_preview(self):
        self.assertEqual(pii_vault.safe_preview("s3://a"), "(미봉인)")
        self.assertIn("봉인됨", pii_vault.safe_preview(self.env))
        self.assertIn("파기", pii_vault.safe_preview(pii_vault.shred(self.env)))
        self.assertNotIn("s3://", pii_vault.safe_preview(self.env))


# ==========================================================================
# 6) recording_audit 배선
# ==========================================================================
class TestWiring(Base):
    def live(self):
        os.environ["RECORDING_LIVE"] = "1"

    def test_gate_off_stores_nothing_even_with_key(self):
        self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a")
        self.assertIsNone(r["audio_ref"])
        self.assertEqual(r["protection"], "none")

    def test_no_consent_stores_nothing(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=False, audio_ref="s3://a")
        self.assertIsNone(r["audio_ref"])
        self.assertEqual(r["protection"], "none")

    def test_live_without_key_drops_refs_and_records_it(self):
        self.live()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a", transcript_ref="s3://t")
        self.assertIsNone(r["audio_ref"])
        self.assertIsNone(r["transcript_ref"])
        self.assertEqual(r["protection"], "unavailable")
        notes = [a for a in st.audit_log() if a["action"] == "register_refs_dropped"]
        self.assertEqual(len(notes), 1)
        self.assertIn("PII_MASTER_KEY", notes[0]["note"])

    def test_live_with_key_seals(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a", transcript_ref="s3://t")
        self.assertEqual(r["protection"], "sealed")
        self.assertTrue(pii_vault.is_envelope(r["audio_ref"]))
        self.assertTrue(pii_vault.is_envelope(r["transcript_ref"]))
        self.assertNotIn("s3://", repr(r))

    def test_each_field_has_its_own_context(self):
        """전사 봉투를 오디오 자리에 옮겨 심어도 열리지 않는다."""
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a", transcript_ref="s3://t")
        rec = st._records[r["record_id"]]
        rec["audio_ref"] = rec["transcript_ref"]
        with self.assertRaises(pii_vault.VaultError):
            st.reveal("agent", r["record_id"], "qa")

    def test_reveal_returns_plaintext_and_audits(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a", transcript_ref="s3://t")
        got = st.reveal("dpo", r["record_id"], "dispute")
        self.assertEqual(got["audio_ref"], "s3://a")
        self.assertEqual(got["transcript_ref"], "s3://t")
        acts = [(a["action"], a["actor"]) for a in st.audit_log()]
        self.assertIn(("reveal", "dpo"), acts)
        self.assertIn(("access", "dpo"), acts)     # 열람 기록도 함께 남는다

    def test_reveal_respects_purpose_whitelist(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a")
        with self.assertRaises(PermissionError):
            st.reveal("mallory", r["record_id"], "marketing")
        self.assertEqual(st.audit_log()[-1]["action"], "access_denied")
        self.assertFalse([a for a in st.audit_log() if a["action"] == "reveal"])

    def test_reveal_on_unsealed_record_is_empty_not_error(self):
        self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True)
        got = st.reveal("dpo", r["record_id"], "qa")
        self.assertIsNone(got["audio_ref"])
        self.assertEqual(got["protection"], "none")
        self.assertEqual(st.audit_log()[-1]["action"], "reveal_empty")

    def test_reveal_failure_is_not_swallowed(self):
        """키를 잃으면 '빈 값'이 아니라 오류로 드러난다."""
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a")
        os.environ["PII_MASTER_KEY"] = KEY_B
        with self.assertRaises(pii_vault.VaultError):
            st.reveal("dpo", r["record_id"], "qa")
        last = st.audit_log()[-1]
        self.assertEqual(last["action"], "reveal_failed")
        self.assertNotIn("s3://", last["note"])

    def test_shred_record_blocks_reveal(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a", transcript_ref="s3://t")
        out = st.shred_record(r["record_id"], actor="dpo", reason="삭제요구")
        self.assertEqual(out["protection"], "shredded")
        self.assertTrue(pii_vault.is_shredded(out["audio_ref"]))
        got = st.reveal("dpo", r["record_id"], "qa")
        self.assertIsNone(got["audio_ref"])
        self.assertEqual(got["protection"], "shredded")

    def test_shred_record_is_idempotent_without_duplicate_audit(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a")
        st.shred_record(r["record_id"], actor="dpo")
        n = len(st.audit_log())
        st.shred_record(r["record_id"], actor="dpo")
        self.assertEqual(len(st.audit_log()), n)

    def test_shred_unknown_record_raises_and_logs(self):
        st = recording_audit.RecordingStore()
        with self.assertRaises(KeyError):
            st.shred_record("REC-9999", actor="dpo")
        self.assertEqual(st.audit_log()[-1]["action"], "shred_miss")

    def test_purge_drops_envelope_entirely(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore(retention_days=1)
        r = st.register("s", consent=True, audio_ref="s3://a", now=0.0)
        st.purge_due(now=10 ** 9)
        rec = st._records[r["record_id"]]
        self.assertIsNone(rec["audio_ref"])
        self.assertEqual(rec["protection"], "shredded")
        self.assertEqual(rec["state"], "purged")

    def test_protection_stats(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        st.register("s", consent=True, audio_ref="s3://a")
        st.register("s2", consent=True)
        s = st.protection_stats()
        self.assertEqual(s["sealed"], 1)
        self.assertEqual(s["none"], 1)
        self.assertTrue(s["vault_available"])

    def test_schema_matches_record_keys(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://a")
        self.assertEqual(set(r), set(recording_audit._meta_schema()))

    def test_register_does_not_create_or_flip_keys(self):
        """저장 동작이 게이트·키를 스스로 켜지 않는다."""
        st = recording_audit.RecordingStore()
        st.register("s", consent=True, audio_ref="s3://a")
        self.assertIsNone(os.environ.get("PII_MASTER_KEY"))
        self.assertIsNone(os.environ.get("RECORDING_LIVE"))

    def test_audit_never_contains_plaintext_ref(self):
        self.live(); self.use()
        st = recording_audit.RecordingStore()
        r = st.register("s", consent=True, audio_ref="s3://secret/rec.wav")
        st.reveal("dpo", r["record_id"], "qa")
        st.shred_record(r["record_id"], actor="dpo")
        blob = repr(st.audit_log())
        self.assertNotIn("s3://", blob)
        self.assertNotIn("secret", blob)


if __name__ == "__main__":
    unittest.main()
