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
#   4) 숫자 표기 우회(10진 `2130706433`·8진 `0177.0.0.1`·16진 `0x7f.0.0.1`) 해석 후 거부
#   5) 호스트 화이트리스트가 있으면 그 목록만 통과(가장 엄격한 운영 설정)
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
BLOCKED_SUFFIXES = (".internal", ".local")


def env_flag(name):
    """`"1"` 정확 일치만 ON — 저장소의 다른 게이트(`sip_adapter.is_live` 등)와 같은 규약."""
    return (os.environ.get(name) or "").strip() == "1"


def env_hosts(name):
    """콤마 구분 호스트 화이트리스트. 미설정이면 빈 목록(= 화이트리스트 미적용)."""
    v = (os.environ.get(name) or "").strip()
    return [x.strip().lower() for x in v.split(",") if x.strip()] if v else []


def _numeric_host(host):
    """숫자 표기 호스트를 IPv4 로 해석. 없으면 None(= 도메인 이름으로 본다).

    `http://2130706433/`·`http://0177.0.0.1/`·`http://0x7f.0.0.1/` 은 OS 리졸버가
    127.0.0.1 로 풀어주지만 `ipaddress.ip_address()` 는 ValueError 를 낸다 —
    그대로 두면 루프백·사설 검사가 통째로 비켜가는 고전적인 우회다.

    모든 라벨이 숫자 리터럴일 때만 IP 로 해석한다. `123.example.com` 처럼
    알파벳 라벨이 섞인 정상 도메인은 건드리지 않는다.
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
    try:
        if len(nums) == 1 and 0 <= nums[0] <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(nums[0])
        if len(nums) == 4 and all(0 <= n <= 255 for n in nums):
            return ipaddress.IPv4Address(
                (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3])
    except (ipaddress.AddressValueError, ValueError):
        return None
    return None


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
    except Exception:
        return False, "%s 형식이 올바르지 않습니다" % label
    if p.scheme not in ("http", "https"):
        return False, "http(s) 주소만 허용합니다"
    if p.scheme == "http" and not allow_insecure:
        return False, "https 주소만 허용합니다"
    try:
        host = (p.hostname or "").lower()
    except Exception:
        # 대괄호가 깨진 IPv6 표기 등 — 형식 오류로 돌린다(통과시키지 않는다)
        return False, "%s 형식이 올바르지 않습니다" % label
    if not host:
        return False, "호스트가 없습니다"
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
    print("_urlguard selftest OK")
