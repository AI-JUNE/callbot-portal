# -*- coding: utf-8 -*-
# ==========================================================================
# api/_urlguard.py — 외부가 준 URL 로 서버가 직접 통신하기 전의 주소 검증. 의존성 0.
# --------------------------------------------------------------------------
# 왜 한 곳에 모으나
#   서버가 "본문에 실려 온 URL" 로 나가는 경로가 둘이다 —
#     · 안부 결과 웹훅 발송(`wellbeing.check_callback_url`)
#     · 통화 녹음 다운로드(`voice.check_recording_url`)
#   둘 다 같은 공격(SSRF: 사설망·루프백·클라우드 메타데이터·file:// 로컬파일)을
#   받는다. 규칙을 두 군데 적어 두면 한쪽만 고쳐지는 일이 반드시 생기므로
#   (실제로 이 저장소에서 그 계열의 결함이 반복해 나왔다) 구현을 하나로 둔다.
#
# 규칙
#   1) http(s) 만 — `file://`·`ftp://`·`gopher://` 는 거부(로컬 파일 읽기 차단)
#   2) 기본은 https 만. 평문 http 는 개발 플래그가 켜져 있을 때만
#   3) 루프백·사설·링크로컬(169.254.169.254 = 메타데이터)·예약·멀티캐스트 거부
#   4) 숫자 표기 우회(10진 `2130706433`·8진 `0177.0.0.1`·16진 `0x7f.0.0.1`·
#      짧은 꼴 `127.1`·`169.254.43518`) 해석 후 거부
#   5) 호스트 화이트리스트가 있으면 그 목록만 통과(가장 엄격한 운영 설정)
#   6) 호스트 표기를 먼저 **정규화**한다(끝점 FQDN·대문자·빈 라벨) — 아래 참조
#
# 2026-10-29(41차) 결함 수정 — **호스트 표기 변형 두 가지가 가드를 통째로 비켜갔다.**
#   (a) 끝점(trailing dot) FQDN: `https://metadata.google.internal./latest` 는
#       `.internal` 접미 검사(`endswith`)에 걸리지 않고, `https://127.0.0.1./x` 는
#       `ipaddress` 가 ValueError 를 내고 `_numeric_host` 도 빈 라벨에서 포기해
#       **도메인 이름으로** 통과했다. 끝점은 DNS 에서 "절대 FQDN" 표기일 뿐이라
#       리졸버는 점 없는 것과 **같은 주소로 푼다** — 즉 검사만 비켜간다.
#   (b) 짧은 꼴 IPv4: `inet_aton` 은 `127.1`(=127.0.0.1)·`10.1`(=10.0.0.1)·
#       `169.254.43518`(=169.254.169.254, **메타데이터**)을 그대로 푼다. 그런데
#       `_numeric_host` 는 라벨이 1개·4개일 때만 IP 로 봤다("라벨 2~3개는
#       IP 표기가 아니다"라고 적혀 있었지만 그것이 틀렸다).
#   되돌려 말하면, `check()` 가 「사설·루프백으로는 보낼 수 없다」고 적어 둔 그
#   자리에서 **클라우드 메타데이터 주소가 통과**하고 있었다. 이 모듈은 안부 결과
#   웹훅·녹음 다운로드·Sentry DSN·주문 백엔드·보이스 엔진이 **공용으로** 쓰므로
#   한 곳의 구멍이 다섯 경로의 구멍이다.
#   재발 방지는 릴리스 게이트 20단계 `host_bypass` — 표기 우회 기록부를 두고
#   게이트가 **실제로 `check()` 를 돌려** 거부를 확인한다(과차단 방지 샘플 동반).
#
# 한계(코드로 닫지 못하는 부분 — 숨기지 않고 적어 둔다)
#   · DNS 를 조회하지 않으므로 공개 도메인이 사설 IP 로 해석되는 rebinding 은
#     막지 못한다. 완전한 방어는 아웃바운드 프록시 몫이며, 엄격히 잠그려면
#     호스트 화이트리스트를 쓴다.
#   · 리다이렉트는 이 모듈이 아니라 호출부가 막는다(검증된 주소가 302 로
#     사설망을 가리키면 검증이 무력화되므로, 호출부가 최종 URL 을 다시 검증한다).
# ==========================================================================
import ipaddress
import os
from urllib.parse import urlparse

MAX_URL = 2048

# 이름만으로 내부를 가리키는 호스트 — IP 검사로는 걸리지 않는다.
BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost",
    "metadata.google.internal", "metadata", "instance-data",
}
# `.localhost` 는 RFC 6761 예약 이름 — 리졸버가 루프백으로 보낸다(`x.localhost`).
BLOCKED_SUFFIXES = (".internal", ".local", ".localhost")


def env_flag(name):
    """`"1"` 정확 일치만 ON — 저장소의 다른 게이트(`sip_adapter.is_live` 등)와 같은 규약."""
    return (os.environ.get(name) or "").strip() == "1"


def env_hosts(name):
    """콤마 구분 호스트 화이트리스트. 미설정이면 빈 목록(= 화이트리스트 미적용)."""
    v = (os.environ.get(name) or "").strip()
    return [x.strip().lower() for x in v.split(",") if x.strip()] if v else []


def normalize_host(host):
    """호스트 표기 정규화 → (호스트, 올바른가).

    리졸버가 **같은 주소로 푸는** 표기를 검사 전에 한 꼴로 모은다. 여기서 모으지
    않으면 `endswith(".internal")`·`ipaddress` 같은 **문자열 판정이 전부** 표기
    변형 하나에 비켜간다(41차 결함).

    - 끝점(`metadata.google.internal.`)은 DNS 의 절대 FQDN 표기다 — 점을 떼도
      가리키는 곳이 같으므로 떼고 검사한다. 화이트리스트 대조도 같은 꼴이 된다.
    - 빈 라벨(`a..b`·`.`)은 어떤 리졸버도 풀지 않는다 — 통과가 아니라 형식 오류로
      돌린다(해석 실패를 통과로 바꾸지 않는다는 이 모듈의 규약).
    - IPv6 리터럴(`::1`·`::ffff:127.0.0.1`)에는 점 뒤 빈 라벨이 없으므로 그대로다.
    """
    h = (host or "").strip().lower()
    while h.endswith("."):
        h = h[:-1]
    if not h:
        return "", False
    if "." in h and any(not lbl for lbl in h.split(".")):
        return h, False
    return h, True


def _numeric_host(host):
    """숫자 표기 호스트를 IPv4 로 해석. 없으면 None(= 도메인 이름으로 본다).

    `http://2130706433/`·`http://0177.0.0.1/`·`http://0x7f.0.0.1/` 은 OS 리졸버가
    127.0.0.1 로 풀어주지만 `ipaddress.ip_address()` 는 ValueError 를 낸다 —
    그대로 두면 루프백·사설 검사가 통째로 비켜가는 고전적인 우회다.

    **`inet_aton` 과 같은 규칙으로 읽는다**(41차). 라벨이 1~4개면 마지막 라벨이
    남은 바이트를 통째로 받는다 — `127.1`=127.0.0.1, `10.1`=10.0.0.1,
    `169.254.43518`=169.254.169.254(메타데이터). 그 전까지 라벨 2~3개를
    "IP 표기가 아니다"라고 넘겼고, 그래서 짧은 꼴이 가드를 비켜갔다.

    모든 라벨이 숫자 리터럴일 때만 IP 로 해석한다. `123.example.com` 처럼
    알파벳 라벨이 섞인 정상 도메인은 건드리지 않는다(숫자 TLD 는 없으므로
    라벨 전부가 숫자인 호스트는 도메인 이름일 수 없다).
    """
    nums = []
    for part in host.split("."):
        if not part:
            return None
        try:
            low = part.lower()
            if low.startswith("0x"):
                nums.append(int(low, 16))
            elif part.startswith("0") and len(part) > 1:
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    if not 1 <= len(nums) <= 4:
        return None        # 라벨 5개 이상 = IP 표기가 아니다(inet_aton 도 거부한다)
    # 앞쪽 라벨은 1바이트, 마지막 라벨이 남은 바이트를 받는다(inet_aton 규칙).
    head, tail = nums[:-1], nums[-1]
    if any(not 0 <= n <= 255 for n in head):
        return None
    if not 0 <= tail < (1 << (8 * (4 - len(head)))):
        return None        # 범위 초과 = IP 표기가 아니다
    value = tail
    for i, n in enumerate(head):
        value |= n << (8 * (3 - i))
    # 범위를 먼저 확인하므로 IPv4Address 는 여기서 예외를 내지 않는다.
    return ipaddress.IPv4Address(value)


def as_ip(host):
    """호스트 문자열 -> IP 객체(해석 불가면 None)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    return _numeric_host(host)


def is_internal_ip(ip):
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def check(url, label="url", allow_insecure=False, allowlist=(), max_len=MAX_URL):
    """(ok, reason). 외부가 준 URL 로 나가도 되는지 판정한다.

    - `label`: 거부 사유에 쓰일 입력 필드명(응답의 `details[].reason` 에 실린다)
    - `allow_insecure`: 로컬 개발용. 평문 http·내부 주소를 통과시킨다
    - `allowlist`: 비어 있지 않으면 **이 호스트만** 통과(그 외 검사 생략 없이 종료)
    """
    u = (url or "").strip()
    if not u:
        return False, "%s 이 비어 있습니다" % label
    if len(u) > int(max_len):
        return False, "%s 이 너무 깁니다" % label
    try:
        p = urlparse(u)
        host = (p.hostname or "").lower()
    except Exception:
        # 대괄호가 깨진 IPv6 표기(`https://[::1`) 등 — urlsplit 이 ValueError 를
        # 낸다. 형식 오류로 돌린다(해석 실패를 통과로 바꾸지 않는다).
        return False, "%s 형식이 올바르지 않습니다" % label
    if p.scheme not in ("http", "https"):
        return False, "http(s) 주소만 허용합니다"
    if p.scheme == "http" and not allow_insecure:
        return False, "https 주소만 허용합니다"
    if not host:
        return False, "호스트가 없습니다"
    # 표기 변형을 먼저 모은다 — 아래 **모든** 판정이 문자열 비교이기 때문이다.
    host, host_ok = normalize_host(host)
    if not host:
        return False, "호스트가 없습니다"
    if not host_ok:
        return False, "%s 형식이 올바르지 않습니다" % label
    if allowlist:
        if host not in [h.lower() for h in allowlist]:
            return False, "허용되지 않은 호스트입니다"
        return True, ""
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        if not allow_insecure:
            return False, "내부 주소로는 보낼 수 없습니다"
    ip = as_ip(host)
    if ip is not None and is_internal_ip(ip):
        if not allow_insecure:
            return False, "사설·루프백 주소로는 보낼 수 없습니다"
    return True, ""


if __name__ == "__main__":  # pragma: no cover
    assert check("https://eum.example.org/hook")[0]
    assert not check("http://eum.example.org/hook")[0]
    assert not check("file:///etc/passwd")[0]
    assert not check("https://127.0.0.1/x")[0]
    assert not check("https://2130706433/x")[0]          # 10진 표기 루프백
    assert not check("https://0177.0.0.1/x")[0]          # 8진 표기 루프백
    assert not check("https://169.254.169.254/latest")[0]  # 메타데이터
    assert check("https://123.example.com/x")[0]         # 숫자 라벨 정상 도메인
    assert check("https://only.me/x", allowlist=["only.me"])[0]
    assert not check("https://other.me/x", allowlist=["only.me"])[0]
    # 41차 — 표기 변형(같은 곳으로 풀리는 다른 꼴)
    assert not check("https://127.0.0.1./x")[0]            # 끝점 FQDN
    assert not check("https://metadata.google.internal./latest")[0]
    assert not check("https://127.1/x")[0]                 # 짧은 꼴 IPv4
    assert not check("https://169.254.43518/latest")[0]    # 3칸 꼴 = 메타데이터
    assert not check("https://x.localhost/x")[0]           # RFC 6761 루프백
    assert not check("https://a..b/x")[0]                  # 빈 라벨 = 형식 오류
    assert check("https://only.me./x", allowlist=["only.me"])[0]  # 끝점도 같은 호스트
    assert check("https://8.8.8.8/x")[0] and check("https://999.1/x")[0]
    print("_urlguard selftest OK")
