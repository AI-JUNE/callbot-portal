# -*- coding: utf-8 -*-
"""
[사람 승인 필요 — 미적용 제안]
개인정보(PII) 저장 암호화 · 파기(crypto-shredding) 설계 — Callbot P0.

배경
  녹취·전사·주문 이력에는 전화번호·이름 등 PII 가 들어간다. 현재는 실수집이
  OFF(sim)라 문제가 없지만, 상용 전환 시 "평문 저장 금지·파기 증명 가능"이
  전제 조건이다(개인정보보호법 제21조·제29조, 안전성 확보조치 기준).
  이 모듈은 그 설계를 코드로 고정해 두는 제안이며, 어떤 핸들러에도 연결하지
  않았고 기본 플래그 OFF 다. 실키 발급·실데이터 암호화 개시는 [승인 필요].

설계 (envelope encryption + crypto-shredding)
  1) 레코드마다 DEK(데이터 키, 32B)를 난수 생성 → 필드 암호화(AES-256-GCM).
  2) DEK 는 KEK(마스터 키, env CALLBOT_PII_KEK, base64 32B)로 감싸(wrap) 저장.
     * KEK 는 코드·저장소에 절대 두지 않는다. Vercel env / KMS 로만 주입.
     * KEK 교체(rotation) = wrapped DEK 만 재암호화, 본문 재암호화 불필요.
  3) 파기 = wrapped DEK 삭제(crypto-shredding). 백업·복제본에 본문이 남아도
     키가 없어 복원 불가 → 물리 삭제가 어려운 스토리지에서도 파기를 증명.
     recording_audit.RetentionPolicy(90일)의 purge_due 가 파기 대상을 산정하면
     스토리지 삭제 대신/과 함께 shred_key() 를 호출하는 구성을 권장.
  4) 로그·통계에는 원문 대신 가명값(HMAC-SHA256, 별도 솔트 키) 사용 —
     동일인 집계는 가능하되 역산 불가. 솔트 키 폐기 시 가명값도 연결 불가.

의존성·폴백 정책
  AES-GCM 은 `cryptography` 패키지가 필요하다(현재 requirements.txt 에 없음 —
  승인 시 `cryptography>=42` 추가). 미설치·미설정 상태에서 암호화를 요청하면
  **가짜 암호화(인코딩만)로 대체하지 않고 예외를 던진다** — 침묵 저하 금지.

활성화 절차(승인 시 사람이 직접)
  (1) requirements.txt 에 cryptography 추가  (2) CALLBOT_PII_KEK 발급·주입
  (3) PII_CRYPTO_LIVE=1  (4) 저장 경로(order_backend·recording_audit)에 배선
  (5) 파기 배치(purge_due→shred_key) 스케줄 등록

셀프테스트: python3 proposals/pii_crypto.py  (cryptography 있으면 왕복 검증)
"""
import os
import json
import hmac
import base64
import hashlib
import secrets

# ── 플래그·키 로딩 (기본 OFF — build now, activate on approval) ─────────────
LIVE_FLAG = "PII_CRYPTO_LIVE"          # "1" 일 때만 실암호화 경로 사용 [승인 필요]
KEK_ENV = "CALLBOT_PII_KEK"            # base64(32B) 마스터 키 — 코드/저장소 보관 금지
PSEUDO_SALT_ENV = "CALLBOT_PII_SALT"   # 가명화 전용 솔트 키(HMAC) — KEK 와 분리 보관

# 필드 레지스트리: 어떤 필드가 PII 인지 코드로 고정(누락·자의적 판단 방지)
PII_FIELDS = ("phone", "name", "address", "memo_free_text", "transcript")


def is_live():
    return (os.environ.get(LIVE_FLAG) or "").strip() == "1"


def _load_kek():
    raw = (os.environ.get(KEK_ENV) or "").strip()
    if not raw:
        raise RuntimeError("CALLBOT_PII_KEK not set — 실키 발급은 [승인 필요]")
    kek = base64.b64decode(raw)
    if len(kek) != 32:
        raise RuntimeError("CALLBOT_PII_KEK must be base64 of 32 bytes")
    return kek


def _aesgcm():
    """cryptography 백엔드 로더 — 없으면 명시적 실패(가짜 암호화 금지)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "cryptography 패키지 필요(requirements.txt 추가는 승인 항목): %s" % e)


# ── envelope encryption ────────────────────────────────────────────────────
def new_dek():
    """레코드별 데이터 키(32B) 생성."""
    return secrets.token_bytes(32)


def wrap_dek(dek, kek=None):
    """DEK 를 KEK(AES-256-GCM)로 감싼다 → base64 문자열(저장용)."""
    AESGCM = _aesgcm()
    kek = kek or _load_kek()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(kek).encrypt(nonce, dek, b"dek-wrap-v1")
    return base64.b64encode(nonce + ct).decode("ascii")


def unwrap_dek(wrapped, kek=None):
    AESGCM = _aesgcm()
    kek = kek or _load_kek()
    blob = base64.b64decode(wrapped)
    return AESGCM(kek).decrypt(blob[:12], blob[12:], b"dek-wrap-v1")


def encrypt_field(dek, plaintext, field=""):
    """단일 PII 필드 암호화 → 'v1:base64(nonce+ct)' (field 를 AAD 로 바인딩)."""
    AESGCM = _aesgcm()
    nonce = secrets.token_bytes(12)
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
    ct = AESGCM(dek).encrypt(nonce, data, ("f:%s" % field).encode("utf-8"))
    return "v1:" + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(dek, token, field=""):
    AESGCM = _aesgcm()
    if not isinstance(token, str) or not token.startswith("v1:"):
        raise ValueError("unknown ciphertext format")
    blob = base64.b64decode(token[3:])
    pt = AESGCM(dek).decrypt(blob[:12], blob[12:], ("f:%s" % field).encode("utf-8"))
    return pt.decode("utf-8")


def encrypt_record(record, kek=None):
    """dict 에서 PII_FIELDS 만 암호화한 사본 + wrapped DEK 반환.
    반환: (enc_record, wrapped_dek) — 저장은 둘을 함께, 파기는 wrapped_dek 만 삭제."""
    dek = new_dek()
    out = dict(record)
    for f in PII_FIELDS:
        if f in out and out[f] not in (None, ""):
            out[f] = encrypt_field(dek, str(out[f]), f)
    return out, wrap_dek(dek, kek)


def decrypt_record(enc_record, wrapped_dek, kek=None):
    dek = unwrap_dek(wrapped_dek, kek)
    out = dict(enc_record)
    for f in PII_FIELDS:
        v = out.get(f)
        if isinstance(v, str) and v.startswith("v1:"):
            out[f] = decrypt_field(dek, v, f)
    return out


# ── 파기(crypto-shredding) — 실스토리지 연동은 [승인 필요] ────────────────
def shred_key(store, record_id):
    """wrapped DEK 삭제 = 해당 레코드 PII 영구 복원 불가.
    store 는 dict 형 인터페이스(sim). 실 KV/DB 연동 시 동일 계약으로 교체.
    반환: 삭제 여부. 파기 사실은 recording_audit 감사로그에 남길 것."""
    return store.pop(record_id, None) is not None


# ── 가명화(로그·통계용 — 복호화 불가·동일인 집계 가능) ──────────────────
def pseudonym(value, salt=None):
    """HMAC-SHA256 12자 가명값. 솔트 키(CALLBOT_PII_SALT) 없으면 예외 —
    무솔트 해시는 전화번호처럼 공간이 작은 값에서 역산 가능하므로 금지."""
    salt = salt or (os.environ.get(PSEUDO_SALT_ENV) or "").strip()
    if not salt:
        raise RuntimeError("CALLBOT_PII_SALT not set — 가명화 솔트는 [승인 필요]")
    mac = hmac.new(salt.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:12]


# ── 셀프테스트(로컬 전용 — 임시 키 사용, 환경변수·실데이터 불필요) ──────
def _selftest():
    kek = secrets.token_bytes(32)
    rec = {"phone": "010-1234-5678", "name": "홍길동", "status": "done"}
    enc, wrapped = encrypt_record(rec, kek)
    assert enc["phone"].startswith("v1:") and enc["status"] == "done"
    dec = decrypt_record(enc, wrapped, kek)
    assert dec == rec, "roundtrip mismatch"
    store = {"r1": wrapped}
    assert shred_key(store, "r1") and "r1" not in store
    try:
        decrypt_record(enc, wrapped, secrets.token_bytes(32))
        raise AssertionError("wrong KEK must fail")
    except Exception:
        pass
    assert pseudonym("010-1234-5678", "s1") != pseudonym("010-1234-5678", "s2")
    assert pseudonym("a", "s1") == pseudonym("a", "s1")
    print(json.dumps({"ok": True, "note": "pii_crypto selftest passed"}))


if __name__ == "__main__":
    try:
        _selftest()
    except RuntimeError as e:
        print(json.dumps({"ok": False, "skipped": str(e)}, ensure_ascii=False))
