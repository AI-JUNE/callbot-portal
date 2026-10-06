# -*- coding: utf-8 -*-
"""통화 개인정보 봉인(암호화) — /api 공용 모듈. HTTP 핸들러 없음.

배경
  `recording_audit.py` 가 정의한 보관·파기 정책은 "어디에 저장하는가"까지만 다뤘고,
  **저장되는 값 자체는 평문**이었다(설계 완료·미배선). 이 모듈이 그 빈칸을 채운다.

원칙
  1. **평문 폴백 금지.** 키가 없으면 `seal()` 은 `VaultUnavailable` 로 거부한다.
     "키가 없으니 일단 평문으로 저장"은 사고의 표준 경로다 — 저장을 포기할지언정 평문으로
     남기지 않는다. 호출부(recording_audit)는 이 예외를 삼키지 않고 참조를 버린다.
  2. **의존성 0.** Vercel 파이썬 런타임에 추가 패키지를 넣지 않는다. 표준 라이브러리
     `hmac`/`hashlib`/`secrets` 만으로 구성한다. 새 암호 원시함수를 만들지 않고,
     검증된 조립법(HKDF 유도 → HMAC-SHA256 카운터 키스트림 → encrypt-then-MAC)만 쓴다.
     AES-GCM 으로 바꿀 자리는 `_encrypt`/`_decrypt` 두 함수뿐이다(교체점 집중).
  3. **문맥 결속(AAD).** 봉투는 `context`(예: "REC-0007/audio")에 묶인다. 다른 레코드의
     봉투를 복사해 넣어도 복호되지 않는다 — 봉투 교체 공격과 기록 뒤섞임을 함께 막는다.
  4. **키 회전.** `PII_MASTER_KEY` 가 현행 키, `PII_MASTER_KEY_OLD`(쉼표 구분)는 복호 전용.
     `rewrap()` 으로 현행 키로 다시 봉인한다. 봉투의 `kid` 는 키 지문 앞 8자리(원문 아님).
     지문은 **느리게**(PBKDF2) 유도한다 — 밖으로 나가는 값이 키 후보 검증기가 되지
     않도록. 구 지문(sha256 1회)으로 봉인된 봉투는 계속 복호된다(`legacy_key_id`).
  6. **봉인할 수 없으면 지문만.** 평문을 들고 있을 이유가 "같은 값인지 비교"뿐이라면
     `fingerprint()` 로 비가역 지문만 남긴다 — 평문은 어디에도 두지 않는다.
  5. **암호 파기(crypto-shredding).** 백업까지 지우는 것은 불가능에 가깝다. `shred()` 는
     봉투에서 복호에 필요한 조각(nonce)을 지운 묘비(tombstone)를 돌려준다. 백업에 남은
     사본이 있어도 되돌릴 수 없고, "파기했다"는 사실은 기록으로 남는다.

환경변수
  PII_MASTER_KEY       base64(권장, 32바이트) · hex(64자) · 32자 이상 문자열 중 하나
  PII_MASTER_KEY_OLD   복호 전용 구키(쉼표로 여러 개). 회전 유예기간용
  키 등록은 [승인 필요] — 사람이 Vercel 환경변수에 넣는다. 코드에 키를 두지 않는다.

봉투 형식(문자열 1개, 로그·JSON 어디에 실려도 안전)
  pv1.<kid>.<b64u nonce>.<b64u ciphertext>.<b64u tag>
  파기 묘비: pv1.<kid>.-.<b64u ciphertext>.-   (nonce·tag 소거)

셀프테스트: python3 api/pii_vault.py
"""
from __future__ import annotations

import os
import hmac
import math
import base64
import hashlib
import secrets

VERSION = "pv1"
KEY_ENV = "PII_MASTER_KEY"
OLD_KEY_ENV = "PII_MASTER_KEY_OLD"
NONCE_LEN = 16
TAG_LEN = 32
MAX_PLAINTEXT = 8 * 1024        # 통화 참조·짧은 메모용. 오디오 원문을 넣는 자리가 아니다
_SHRED_MARK = "-"
KID_ITERS = 200_000             # 지문 유도 비용 — key_id() 주석 참조(약 50ms, 프로세스당 1회)
MIN_ENTROPY_BITS = 128          # 재료 품질 경고선. 이 아래면 status() 가 weak 로 알린다


class VaultError(Exception):
    """복호 실패의 상위 타입. 세부 사유를 외부에 흘리지 않는다."""


class VaultUnavailable(VaultError):
    """키 미설정. 평문 저장으로 대체하지 말 것."""


class VaultTamper(VaultError):
    """인증 실패 — 변조·문맥 불일치·키 불일치. 셋을 구분해 알려주지 않는다(오라클 방지)."""


class VaultShredded(VaultError):
    """암호 파기된 봉투. 되돌릴 수 없다."""


# ── 키 관리 ────────────────────────────────────────────────────────────────
def _decode_key(raw: str):
    """base64 / hex / 원문 문자열을 32바이트 키 재료로 만든다. 실패하면 None."""
    s = (raw or "").strip()
    if not s:
        return None
    for dec in (lambda v: base64.b64decode(v + "=" * (-len(v) % 4), validate=True),
                lambda v: bytes.fromhex(v)):
        try:
            b = dec(s)
            if len(b) >= 32:
                return b
        except Exception:
            pass
    b = s.encode("utf-8")
    if len(b) >= 32:                 # 충분히 긴 임의 문자열은 그대로 재료로 쓴다
        return b
    return None                      # 짧은 키는 거부 — 약한 키를 조용히 받아주지 않는다


def _primary():
    return _decode_key(os.environ.get(KEY_ENV) or "")


def _old_keys():
    raw = os.environ.get(OLD_KEY_ENV) or ""
    out = []
    for part in raw.split(","):
        k = _decode_key(part)
        if k:
            out.append(k)
    return out


_KID_CACHE: dict = {}


def legacy_key_id(key: bytes) -> str:
    """pv1 초기 지문(sha256 1회). **복호 호환**을 위해서만 남는다 — 새 봉투에 쓰지 않는다."""
    return hashlib.sha256(b"pii-vault/kid/v1" + key).hexdigest()[:8]


def key_id(key: bytes) -> str:
    """키 지문 앞 8자리. 키 원문이 복원되지 않는다(로그·봉투에 실어도 안전).

    **느리게 유도한다.** 지문은 봉투·로그·미리보기(`safe_preview`)에 실려 모듈 밖으로
    나간다. sha256 한 번으로 만들면 그 8자리가 "이 후보가 진짜 키인가"를 암호문 없이
    확인해 주는 **공짜 검증기**가 된다 — 사전 공격이 초당 수백만 후보로 돌아간다.
    `_decode_key` 가 긴 문자열 암호도 재료로 받아주므로(아래) 그 위험은 이론이 아니다.
    PBKDF2 로 후보 1개당 비용을 20만 배 올린다.

    같은 키의 지문은 프로세스 안에서 한 번만 계산한다(캐시). 캐시는 키 바이트를
    들고 있지만 키는 이미 `os.environ` 에 있으므로 노출면이 늘지 않는다.
    """
    cached = _KID_CACHE.get(key)
    if cached:
        return cached
    kid = hashlib.pbkdf2_hmac("sha256", key, b"pii-vault/kid/v2", KID_ITERS).hex()[:8]
    if len(_KID_CACHE) >= 32:        # 회전 유예로도 32개를 넘지 않는다 — 넘으면 비운다
        _KID_CACHE.clear()
    _KID_CACHE[key] = kid
    return kid


def available() -> bool:
    """현행 키가 설정되어 있는가. False 면 seal() 은 거부한다."""
    return _primary() is not None


# ── 비가역 지문(같음 판정 전용) ────────────────────────────────────────────
_FP_SALT = secrets.token_bytes(32)      # 프로세스 1회 생성. fingerprint() 주석 참조


def fingerprint(value, context: str = "") -> str:
    """같음·다름만 판정하는 비가역 지문 — **평문을 들고 있지 않기 위한** 장치.

    전화번호처럼 후보가 10^9 수준인 값은 평범한 해시로 가려도 초 단위에 역산된다.
    그래서 프로세스마다 새로 만든 비밀 소금으로 HMAC 한다. 지문이 밖으로 새도 원문을
    되돌릴 수 없고, 소금은 어디에도 저장되지 않는다.

    **프로세스 안에서만 안정적이다.** 재시작하면 같은 값의 지문이 달라진다. 지금
    이 지문을 쓰는 대장(`caller_id`)이 인스턴스 메모리에만 있으므로 수명이 같다 —
    영속 저장소를 붙이는 날(별도 [승인 필요]) 소금도 함께 보관해야 한다.
    봉투의 `kid` 와는 무관하다(키가 없어도 동작한다).
    """
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value or b"")
    msg = (context or "").encode("utf-8") + b"\x00" + data
    return hmac.new(_FP_SALT, msg, hashlib.sha256).hexdigest()[:32]


# ── 키 재료 품질 ───────────────────────────────────────────────────────────
# 32바이트만 넘으면 무엇이든 받던 자리. HKDF 는 재료를 늘려 주지만 **엔트로피를
# 만들어 주지는 않는다** — 반복 문자열·흔한 암구호는 길어도 약하다. 거부하지는
# 않는다(거부는 키 미설정과 같아져 보관을 포기하게 만든다). 대신 드러낸다.
_PLACEHOLDERS = ("changeme", "change-me", "password", "passwd", "secret",
                 "example", "test", "dummy", "placeholder", "yourkey")


def _entropy_bits_upper(b: bytes) -> int:
    """관측 바이트 분포의 섀넌 엔트로피 × 길이. **상한**이다.

    문장·반복 같은 구조는 실제를 이 값보다 낮춘다. 그래서 낮으면 확실히 약하지만
    높다고 안전이 증명되지는 않는다 — 이 함수는 '강함'을 말하지 않는다.
    """
    if not b:
        return 0
    n = len(b)
    h = 0.0
    for c in set(b):
        p = b.count(c) / n
        h -= p * math.log2(p)
    return int(h * n)


def material_quality(raw: str) -> dict:
    """키 재료가 **약한지** 판정. 원문·전체 길이는 담지 않는다."""
    b = _decode_key(raw)
    if b is None:
        return {"present": False, "weak": None, "reasons": []}
    distinct = len(set(b))
    bits = _entropy_bits_upper(b)
    reasons = []
    if distinct < 8:
        reasons.append("서로 다른 바이트가 %d개뿐 — 반복 재료" % distinct)
    if bits < MIN_ENTROPY_BITS:
        reasons.append("엔트로피 상한 %d비트 < 권장 %d비트" % (bits, MIN_ENTROPY_BITS))
    low = (raw or "").strip().lower()
    if any(w in low for w in _PLACEHOLDERS):
        reasons.append("흔한 자리표시자 낱말 포함")
    return {"present": True, "weak": bool(reasons), "entropy_bits_upper": bits,
            "distinct_bytes": distinct, "reasons": reasons,
            "advice": "" if not reasons
                      else "`openssl rand -base64 32` 로 만든 값으로 교체 권장"}


def status() -> dict:
    """운영 점검용 요약. 키 원문·길이는 노출하지 않는다.

    **미인증 공개 경로에 그대로 싣지 말 것.** `kid` 와 재료 품질은 키 후보를 좁혀
    주는 단서다 — 발신번호 요약(`caller_id.summary()`)이 `kid` 를 공개 응답에
    싣고 있던 것이 이번 회차에 제거한 결함이다. 공개면에는 `available()` 만 쓴다.
    """
    p = _primary()
    olds = _old_keys()
    return {
        "available": p is not None,
        "kid": key_id(p) if p else None,
        "retired_kids": [key_id(k) for k in olds],
        "material": material_quality(os.environ.get(KEY_ENV) or ""),
        "alg": "HKDF-SHA256 + HMAC-SHA256-CTR + HMAC-SHA256 (encrypt-then-MAC)",
        "kid_alg": "PBKDF2-HMAC-SHA256 %d회 (구 봉투의 sha256 지문도 복호 허용)" % KID_ITERS,
        "env": KEY_ENV,
        "note": "키 등록은 [승인 필요] — 미설정 시 봉인 대상은 저장되지 않는다(평문 폴백 없음)",
    }


# ── 내부: 키 유도·키스트림 ─────────────────────────────────────────────────
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    """엄격 복호 — **정규 표기만** 허용한다.

    패딩 없는 base64 는 마지막 글자의 남는 비트가 버려진다. 즉 서로 다른 문자열이
    같은 바이트로 풀린다(가단성). 그대로 두면 봉투 하나가 여러 표기를 갖게 되어
    중복 판정·멱등 처리가 어긋나고, 변조 탐지 시험도 헛돈다. 되감아 비교해 걸러낸다.
    """
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if _b64e(raw) != s:
        raise ValueError("non-canonical base64")
    return raw


def _hkdf(master: bytes, nonce: bytes, context: str, label: bytes) -> bytes:
    """HKDF-SHA256(RFC 5869) 추출→확장. 32바이트 1블록이면 충분하다."""
    prk = hmac.new(b"pii-vault/v1", master, hashlib.sha256).digest()
    info = label + b"|" + nonce + b"|" + (context or "").encode("utf-8")
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _keystream(ek: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 을 PRF 로 쓰는 카운터 모드 키스트림. (ek, nonce) 쌍은 재사용되지 않는다."""
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hmac.new(ek, nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def _xor(data: bytes, ks: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, ks))


def _encrypt(master: bytes, nonce: bytes, context: str, plain: bytes):
    ek = _hkdf(master, nonce, context, b"enc")
    mk = _hkdf(master, nonce, context, b"mac")
    ct = _xor(plain, _keystream(ek, nonce, len(plain)))
    tag = hmac.new(mk, b"%s|%s|" % (VERSION.encode(), nonce) + ct, hashlib.sha256).digest()
    return ct, tag


def _decrypt(master: bytes, nonce: bytes, context: str, ct: bytes, tag: bytes):
    ek = _hkdf(master, nonce, context, b"enc")
    mk = _hkdf(master, nonce, context, b"mac")
    want = hmac.new(mk, b"%s|%s|" % (VERSION.encode(), nonce) + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(want, tag):      # 태그 먼저 — 복호 전에 검증(encrypt-then-MAC)
        return None
    return _xor(ct, _keystream(ek, nonce, len(ct)))


# ── 공개 API ───────────────────────────────────────────────────────────────
def seal(plaintext, context: str = "") -> str:
    """평문을 봉투 문자열로. 키가 없으면 VaultUnavailable — 평문을 돌려주지 않는다."""
    master = _primary()
    if master is None:
        raise VaultUnavailable("암호화 키(%s) 미설정 — 봉인 없이 저장하지 않는다" % KEY_ENV)
    if plaintext is None:
        raise ValueError("plaintext is required")
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
    if len(data) > MAX_PLAINTEXT:
        raise ValueError("plaintext too large: %d > %d" % (len(data), MAX_PLAINTEXT))
    nonce = secrets.token_bytes(NONCE_LEN)
    ct, tag = _encrypt(master, nonce, context, data)
    return ".".join((VERSION, key_id(master), _b64e(nonce), _b64e(ct), _b64e(tag)))


def is_envelope(value) -> bool:
    return isinstance(value, str) and value.startswith(VERSION + ".") and value.count(".") == 4


def is_shredded(value) -> bool:
    if not is_envelope(value):
        return False
    parts = value.split(".")
    return parts[2] == _SHRED_MARK or parts[4] == _SHRED_MARK


def envelope_kid(value) -> str:
    """봉투가 어느 키로 봉인됐는지(지문). 복호하지 않는다."""
    if not is_envelope(value):
        raise VaultError("not an envelope")
    return value.split(".")[1]


def unseal(envelope: str, context: str = "") -> str:
    """봉투를 평문으로. 실패 사유는 타입으로만 구분한다(어느 키/어느 바이트가 틀렸는지 함구)."""
    if not is_envelope(envelope):
        raise VaultError("not an envelope")
    if is_shredded(envelope):
        raise VaultShredded("암호 파기된 기록 — 복구 불가")
    _v, kid, n_s, c_s, t_s = envelope.split(".")
    try:
        nonce, ct, tag = _b64d(n_s), _b64d(c_s), _b64d(t_s)
    except Exception:
        raise VaultTamper("malformed envelope")
    if len(nonce) != NONCE_LEN or len(tag) != TAG_LEN:
        raise VaultTamper("malformed envelope")
    candidates = [k for k in ([_primary()] + _old_keys()) if k is not None]
    if not candidates:
        raise VaultUnavailable("복호 키(%s) 미설정" % KEY_ENV)
    for key in candidates:
        # 지문이 다른 키로는 시도하지 않는다(무의미한 연산 회피).
        # 구 지문(sha256 1회)으로 봉인된 봉투도 계속 복호한다 — 지문 유도를 느리게
        # 바꾼 것이 이미 보관된 기록을 못 읽게 만들면 안 된다.
        if kid not in (key_id(key), legacy_key_id(key)):
            continue
        plain = _decrypt(key, nonce, context, ct, tag)
        if plain is not None:
            return plain.decode("utf-8", "replace")
        break                            # 지문이 같은데 태그가 틀렸다 = 변조 또는 문맥 불일치
    raise VaultTamper("복호 실패 — 변조·문맥 불일치·키 불일치")


def rewrap(envelope: str, context: str = "") -> str:
    """구키 봉투를 현행 키로 다시 봉인(회전). 이미 현행 키면 새 nonce 로 재봉인한다."""
    return seal(unseal(envelope, context), context)


def shred(envelope: str) -> str:
    """암호 파기. 복호 재료를 지운 묘비를 돌려준다 — 백업에 사본이 남아도 되돌릴 수 없다.

    암호문 자체는 남겨 "무엇이 있었다"는 사실(길이·존재)만 감사에 남기고, 내용은 영구히
    닫는다. 완전 삭제가 필요하면 호출부가 묘비마저 버리면 된다.
    """
    if not is_envelope(envelope):
        raise VaultError("not an envelope")
    if is_shredded(envelope):
        return envelope                  # 재파기는 무해·무기록(idempotent)
    _v, kid, _n, ct, _t = envelope.split(".")
    return ".".join((VERSION, kid, _SHRED_MARK, ct, _SHRED_MARK))


def safe_preview(envelope) -> str:
    """화면·로그용 표시값. 절대 복호하지 않는다."""
    if not is_envelope(envelope):
        return "(미봉인)"
    if is_shredded(envelope):
        return "(암호 파기됨)"
    return "(봉인됨 · kid=%s)" % envelope_kid(envelope)


if __name__ == "__main__":     # pragma: no cover
    os.environ[KEY_ENV] = base64.b64encode(b"K" * 32).decode()
    os.environ.pop(OLD_KEY_ENV, None)
    assert available()
    env = seal("s3://bucket/rec-0001.wav", "REC-0001/audio")
    assert "s3://" not in env and "bucket" not in env
    assert unseal(env, "REC-0001/audio") == "s3://bucket/rec-0001.wav"
    try:
        unseal(env, "REC-0002/audio"); print("FAIL: 문맥 불일치 복호됨")
    except VaultTamper:
        pass
    bad = env[:-1] + ("A" if env[-1] != "A" else "B")
    try:
        unseal(bad, "REC-0001/audio"); print("FAIL: 변조 봉투 복호됨")
    except VaultError:
        pass
    tomb = shred(env)
    assert is_shredded(tomb) and shred(tomb) == tomb
    try:
        unseal(tomb, "REC-0001/audio"); print("FAIL: 파기 봉투 복호됨")
    except VaultShredded:
        pass
    old = os.environ[KEY_ENV]
    os.environ[OLD_KEY_ENV] = old
    os.environ[KEY_ENV] = base64.b64encode(b"N" * 32).decode()
    assert unseal(env, "REC-0001/audio") == "s3://bucket/rec-0001.wav"     # 구키 복호
    fresh = rewrap(env, "REC-0001/audio")
    assert envelope_kid(fresh) != envelope_kid(env)
    os.environ.pop(KEY_ENV); os.environ.pop(OLD_KEY_ENV)
    assert not available()
    try:
        seal("x", "c"); print("FAIL: 키 없이 봉인됨")
    except VaultUnavailable:
        pass
    print("SELF-TEST OK:", safe_preview(env), safe_preview(tomb))
