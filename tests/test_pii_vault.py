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
import hmac
import time
import base64
import hashlib
import subprocess
import unittest
import urllib.request

API_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api")
sys.path.insert(0, API_DIR)

import _pii_vault as pii_vault           # noqa: E402
import _recording_audit as recording_audit     # noqa: E402

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


# ==========================================================================
# 8) 지문 유도 비용 (2026-10-06 결함 회귀)
#
# 봉투의 `kid` 는 로그·미리보기·응답으로 모듈 밖에 나간다. 그 8자리가 sha256
# 한 번이면 "이 후보가 진짜 키인가"를 암호문 없이 확인해 주는 공짜 검증기가
# 된다 — `_decode_key` 가 문자열 암호도 받아주므로 사전 공격이 성립한다.
# ==========================================================================
class TestKidDerivation(Base):
    def test_kid_is_not_a_single_sha256(self):
        k = b"A" * 32
        cheap = hashlib.sha256(b"pii-vault/kid/v1" + k).hexdigest()[:8]
        self.assertEqual(pii_vault.legacy_key_id(k), cheap)
        self.assertNotEqual(pii_vault.key_id(k), cheap)

    def test_kid_cost_floor(self):
        """비용이 보안성이다 — 반복 횟수를 낮추면 이 테스트가 막는다."""
        self.assertGreaterEqual(pii_vault.KID_ITERS, 100_000)

    def test_kid_still_stable_and_distinct(self):
        self.assertEqual(pii_vault.key_id(b"A" * 32), pii_vault.key_id(b"A" * 32))
        self.assertNotEqual(pii_vault.key_id(b"A" * 32), pii_vault.key_id(b"B" * 32))
        self.assertEqual(len(pii_vault.key_id(b"A" * 32)), 8)

    def test_new_envelopes_use_slow_kid(self):
        self.use(KEY_A)
        master = pii_vault._decode_key(KEY_A)
        env = pii_vault.seal("x", "c")
        self.assertEqual(pii_vault.envelope_kid(env), pii_vault.key_id(master))
        self.assertNotEqual(pii_vault.envelope_kid(env), pii_vault.legacy_key_id(master))

    def test_legacy_envelope_still_unseals(self):
        """유도 방식을 바꿨다고 이미 보관된 봉투를 못 읽게 되면 안 된다."""
        self.use(KEY_A)
        master = pii_vault._decode_key(KEY_A)
        nonce = b"\x07" * pii_vault.NONCE_LEN
        ct, tag = pii_vault._encrypt(master, nonce, "REC-1/audio", b"s3://old/rec.wav")
        legacy = ".".join(("pv1", pii_vault.legacy_key_id(master),
                           pii_vault._b64e(nonce), pii_vault._b64e(ct),
                           pii_vault._b64e(tag)))
        self.assertEqual(pii_vault.envelope_kid(legacy), pii_vault.legacy_key_id(master))
        self.assertEqual(pii_vault.unseal(legacy, "REC-1/audio"), "s3://old/rec.wav")
        with self.assertRaises(pii_vault.VaultTamper):
            pii_vault.unseal(legacy, "REC-2/audio")        # 문맥 결속은 그대로
        fresh = pii_vault.rewrap(legacy, "REC-1/audio")     # 재봉인은 새 지문으로
        self.assertEqual(pii_vault.envelope_kid(fresh), pii_vault.key_id(master))

    def test_wrong_key_still_rejected(self):
        """구 지문 허용이 '아무 키나 통과'로 번지지 않는다."""
        self.use(KEY_A)
        env = pii_vault.seal("x", "c")
        os.environ["PII_MASTER_KEY"] = KEY_B
        os.environ.pop("PII_MASTER_KEY_OLD", None)
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.unseal(env, "c")


# ==========================================================================
# 9) 비가역 지문 — 평문을 들고 있지 않기 위한 장치
# ==========================================================================
class TestFingerprint(Base):
    def test_stable_within_process(self):
        a = pii_vault.fingerprint("01012345678", "caller_id/number")
        self.assertEqual(a, pii_vault.fingerprint("01012345678", "caller_id/number"))
        self.assertEqual(len(a), 32)

    def test_distinct_values_and_contexts(self):
        n = "01012345678"
        self.assertNotEqual(pii_vault.fingerprint(n, "a"), pii_vault.fingerprint(n, "b"))
        self.assertNotEqual(pii_vault.fingerprint(n, "a"),
                            pii_vault.fingerprint("01012345679", "a"))

    def test_not_a_bare_hash(self):
        """번호 후보는 10^9 개뿐 — 공개 입력만으로 계산되는 해시는 초 단위에 역산된다."""
        n, ctx = "01012345678", "caller_id/number"
        fp = pii_vault.fingerprint(n, ctx)
        self.assertNotIn(n, fp)
        self.assertEqual(len(pii_vault._FP_SALT), 32)
        public = (n.encode(), ctx.encode() + b"\x00" + n.encode(),
                  ctx.encode() + n.encode(), n.encode() + ctx.encode())
        for msg in public:                       # 소금 없이 만들 수 있는 조립법들
            self.assertNotEqual(fp, hashlib.sha256(msg).hexdigest()[:32])
            self.assertNotEqual(fp, hmac.new(b"", msg, hashlib.sha256).hexdigest()[:32])

    def test_salt_is_secret_not_derivable(self):
        """결정적 판정: 소금이 비밀이면 **다른 프로세스에서 지문이 달라진다**.

        같게 나온다면 공개 입력만으로 계산된다는 뜻이고(조립법을 하나하나 맞혀 볼
        필요도 없다), 그러면 번호 전수 대입으로 원문이 복원된다.
        영속 저장소를 붙여 소금을 보관하게 되는 날 이 테스트는 **의도적으로**
        고쳐야 한다(fingerprint() 주석 참조) — 조용히 깨지는 것이 아니다.
        """
        code = ("import sys; sys.path.insert(0, %r); import _pii_vault as v; "
                "print(v.fingerprint('01012345678', 'caller_id/number'))"
                % os.path.abspath(API_DIR))
        other = subprocess.check_output([sys.executable, "-c", code]).decode().strip()
        self.assertEqual(len(other), 32, other)
        self.assertNotEqual(other, pii_vault.fingerprint("01012345678", "caller_id/number"),
                            "프로세스 간 지문이 같다 — 비밀 소금이 없다")

    def test_works_without_key(self):
        """봉인 키가 없어도 지문은 만들어진다 — 평문 보관의 대체 수단이니까."""
        self.assertFalse(pii_vault.available())
        self.assertEqual(len(pii_vault.fingerprint("x", "c")), 32)

    def test_accepts_bytes_and_empty(self):
        self.assertEqual(pii_vault.fingerprint(b"ab", "c"),
                         pii_vault.fingerprint("ab", "c"))
        self.assertEqual(len(pii_vault.fingerprint("", "")), 32)
        self.assertEqual(len(pii_vault.fingerprint(None, "c")), 32)


# ==========================================================================
# 10) 키 재료 품질 — 32바이트만 넘으면 무엇이든 받던 자리
# ==========================================================================
STRONG = base64.b64encode(bytes(range(32))).decode()    # 서로 다른 32바이트 = 160비트


class TestKeyMaterial(Base):
    def test_repeated_material_is_flagged(self):
        q = pii_vault.material_quality(base64.b64encode(b"K" * 32).decode())
        self.assertTrue(q["present"])
        self.assertTrue(q["weak"])
        self.assertTrue(any("반복" in r for r in q["reasons"]), q["reasons"])
        self.assertEqual(q["distinct_bytes"], 1)
        self.assertEqual(q["entropy_bits_upper"], 0)

    def test_strong_material_is_not_flagged(self):
        q = pii_vault.material_quality(STRONG)
        self.assertFalse(q["weak"], q["reasons"])
        self.assertGreaterEqual(q["entropy_bits_upper"], pii_vault.MIN_ENTROPY_BITS)
        self.assertEqual(q["advice"], "")

    def test_placeholder_passphrase_is_flagged(self):
        q = pii_vault.material_quality("changeme-changeme-changeme-changeme")
        self.assertTrue(q["weak"])
        self.assertTrue(any("자리표시자" in r for r in q["reasons"]), q["reasons"])
        self.assertIn("openssl", q["advice"])

    def test_low_entropy_passphrase_is_flagged(self):
        q = pii_vault.material_quality("aaaabbbbccccddddaaaabbbbccccdddd")
        self.assertTrue(q["weak"])
        self.assertLess(q["entropy_bits_upper"], pii_vault.MIN_ENTROPY_BITS)

    def test_absent_material(self):
        for raw in ("", "   ", "short"):
            q = pii_vault.material_quality(raw)
            self.assertFalse(q["present"], repr(raw))
            self.assertIsNone(q["weak"])
            self.assertEqual(q["reasons"], [])

    def test_status_reports_material_without_leaking_it(self):
        self.use(KEY_A)                       # base64("A"*32) — 반복 재료
        st = pii_vault.status()
        self.assertTrue(st["material"]["weak"])
        self.assertNotIn(KEY_A, repr(st))
        self.assertNotIn("A" * 32, repr(st))
        self.assertIn("PBKDF2", st["kid_alg"])
        os.environ["PII_MASTER_KEY"] = STRONG
        self.assertFalse(pii_vault.status()["material"]["weak"])

    def test_entropy_of_empty_material(self):
        self.assertEqual(pii_vault._entropy_bits_upper(b""), 0)

    def test_kid_cache_is_bounded(self):
        """캐시가 무한히 자라지 않는다 — 회전 유예로도 32개를 넘지 않는다."""
        pii_vault._KID_CACHE.clear()
        for i in range(32):
            pii_vault._KID_CACHE[b"stub-%02d" % i] = "deadbeef"
        kid = pii_vault.key_id(b"Z" * 32)
        self.assertEqual(len(pii_vault._KID_CACHE), 1)      # 비운 뒤 새로 담았다
        self.assertEqual(pii_vault.key_id(b"Z" * 32), kid)  # 캐시 적중도 같은 값

    def test_envelope_helpers_reject_non_envelopes(self):
        self.assertFalse(pii_vault.is_shredded("010-1234-5678"))
        with self.assertRaises(pii_vault.VaultError):
            pii_vault.envelope_kid("010-1234-5678")

    def test_weak_key_is_reported_not_refused(self):
        """약한 키를 거부하면 '키 미설정'과 같아져 보관을 포기하게 된다 —
        그쪽이 더 나쁘다. 그래서 막지 않고 드러낸다."""
        self.use(KEY_A)
        self.assertTrue(pii_vault.available())
        env = pii_vault.seal("s3://a", "c")
        self.assertEqual(pii_vault.unseal(env, "c"), "s3://a")


if __name__ == "__main__":
    unittest.main()
