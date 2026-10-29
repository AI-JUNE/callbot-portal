# -*- coding: utf-8 -*-
"""SSRF 가드의 **호스트 표기 정규화** 회귀 — 2026-10-29(41차) 결함.

무엇이 틀렸었나
  `_urlguard.check()` 는 「루프백·사설·메타데이터 주소로는 보낼 수 없다」고
  적어 두고 **같은 곳으로 풀리는 다른 표기**를 통과시켰다.

    · 끝점(trailing dot) FQDN — `https://metadata.google.internal./latest`
      `.internal` 접미 검사(`endswith`)가 성립하지 않고, `127.0.0.1.` 은
      `ipaddress` 가 ValueError 를 내고 `_numeric_host` 도 빈 라벨에서 포기해
      **도메인 이름으로** 통과했다. 끝점은 DNS 의 절대 FQDN 표기일 뿐이라
      리졸버는 점 없는 것과 같은 주소로 푼다 — 검사만 비켜간다.
    · 짧은 꼴 IPv4 — `https://127.1/x`·`https://10.1/x`·
      `https://169.254.43518/latest`(= 169.254.169.254, **클라우드 메타데이터**).
      `inet_aton` 은 라벨 1~4개를 모두 읽지만 가드는 1개·4개만 봤다.
    · `.localhost` — RFC 6761 예약 이름이라 리졸버가 루프백으로 보낸다.

이 모듈은 안부 결과 웹훅·녹음 다운로드·Sentry DSN·주문 백엔드·보이스 엔진이
**공용으로** 쓴다. 한 곳의 구멍이 다섯 경로의 구멍이므로, 아래는 모듈 함수와
실제 호출부(`wellbeing.check_callback_url`·`voice.check_recording_url`) 양쪽을
같은 표로 고정한다.

과차단도 함께 고정한다 — 가드가 정상 주소를 막기 시작하면 사람이 가드를 끈다.

네트워크 미사용(DNS 조회 없음 · 판정 함수만 호출한다).
실행: python3 -m pytest tests/test_urlguard_hostnorm.py -q
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)

import _urlguard  # noqa: E402

# 리졸버가 내부로 푸는 표기들 — 전부 거부여야 한다.
BYPASS = (
    ("https://127.0.0.1./x", "끝점 루프백"),
    ("https://127.0.0.1../x", "끝점 두 개"),
    ("https://LOCALHOST./x", "끝점 + 대문자"),
    ("https://metadata.google.internal./latest", "끝점 + 메타데이터 이름"),
    ("https://foo.internal./x", "끝점 + .internal 접미"),
    ("https://printer.local./x", "끝점 + .local 접미"),
    ("https://169.254.169.254./x", "끝점 + 메타데이터 IP"),
    ("https://127.1/x", "짧은 꼴 = 127.0.0.1"),
    ("https://127.0.1/x", "짧은 꼴 3칸 = 127.0.1 → 127.0.0.1"),
    ("https://10.1/x", "짧은 꼴 = 10.0.0.1(사설)"),
    ("https://192.168.1/x", "짧은 꼴 = 192.168.0.1(사설)"),
    ("https://169.254.43518/latest", "3칸 꼴 = 169.254.169.254(메타데이터)"),
    ("https://2130706433./x", "10진 32비트 + 끝점"),
    ("https://0x7f.1/x", "16진 + 짧은 꼴"),
    ("https://0177.1/x", "8진 + 짧은 꼴"),
    ("https://x.localhost/x", "RFC 6761 — 루프백으로 보낸다"),
    ("https://deep.sub.localhost/x", "`.localhost` 하위 이름"),
)

# IP 표기가 아니거나 공개 주소인 것들 — 통과여야 한다(과차단 방지).
ALLOWED = (
    ("https://eum.gowon.co.kr/api/channel/aicc/request", "실제 연동 대상"),
    ("https://123.example.com/x", "숫자 라벨이 섞인 정상 도메인"),
    ("https://hooks.example.org:8443/cb", "비표준 포트"),
    ("https://8.8.8.8/x", "공개 IP 리터럴"),
    ("https://999.1/x", "앞 라벨 범위 초과 — IP 표기가 아니다"),
    ("https://1.16777216/x", "뒤 라벨 범위 초과 — IP 표기가 아니다"),
    ("https://1.2.3.4.5/x", "라벨 5개 — inet_aton 도 IP 로 읽지 않는다"),
    ("https://08.0.0.1/x", "8진 리터럴이 아니다(8 은 8진수에 없다)"),
    ("https://hook.example.org./cb", "끝점이지만 공개 도메인 — 막지 않는다"),
    ("https://localhostel.example.org/x", "`localhost` 를 품은 정상 이름"),
    ("https://mylocal.example.org/x", "`.local` 접미가 아니다"),
)


class HostNormalize(unittest.TestCase):
    def test_정규화_규약(self):
        self.assertEqual(_urlguard.normalize_host("Example.ORG."), ("example.org", True))
        self.assertEqual(_urlguard.normalize_host("example.org..."), ("example.org", True))
        self.assertEqual(_urlguard.normalize_host("  EXAMPLE.org "), ("example.org", True))
        # 빈 라벨은 통과로 바꾸지 않는다(해석 실패 = 거부)
        self.assertFalse(_urlguard.normalize_host("a..b")[1])
        self.assertFalse(_urlguard.normalize_host(".a")[1])
        # 점만 남는 호스트는 호스트가 없는 것이다
        self.assertEqual(_urlguard.normalize_host("...")[0], "")
        self.assertEqual(_urlguard.normalize_host(None)[0], "")
        # IPv6 리터럴은 건드리지 않는다
        self.assertEqual(_urlguard.normalize_host("::ffff:127.0.0.1"),
                         ("::ffff:127.0.0.1", True))

    def test_빈_라벨은_형식오류로_닫는다(self):
        for u in ("https://a..b/x", "https://.a/x"):
            ok, reason = _urlguard.check(u, label="callback_url")
            self.assertFalse(ok, u)
            self.assertIn("형식", reason)
        ok, reason = _urlguard.check("https://./x")
        self.assertFalse(ok)
        self.assertIn("호스트", reason)


class NumericShortForm(unittest.TestCase):
    """`inet_aton` 과 같은 규칙으로 읽는가."""

    def test_짧은_꼴을_IP_로_읽는다(self):
        for host, want in (("127.1", "127.0.0.1"), ("1.2.3", "1.2.0.3"),
                           ("10.1", "10.0.0.1"), ("192.168.1", "192.168.0.1"),
                           ("169.254.43518", "169.254.169.254"),
                           ("0x7f.1", "127.0.0.1"), ("0177.1", "127.0.0.1"),
                           ("2130706433", "127.0.0.1")):
            self.assertEqual(str(_urlguard.as_ip(host)), want, host)

    def test_IP_표기가_아닌_것은_단정하지_않는다(self):
        for host in ("1.2.3.4.5", "999.1", "1.16777216", "256.1", "08.0.0.1",
                     "99999999999", "cdn.example.io", "1..2", ""):
            self.assertIsNone(_urlguard.as_ip(host), host)

    def test_공개_IP_는_내부가_아니다(self):
        for host in ("8.8.8.8", "1.1.1.1", "9.9.9.9"):
            ip = _urlguard.as_ip(host)
            self.assertIsNotNone(ip, host)
            self.assertFalse(_urlguard.is_internal_ip(ip), host)


class GuardDecisions(unittest.TestCase):
    def test_표기_우회는_전부_거부(self):
        for url, why in BYPASS:
            with self.subTest(url=url):
                ok, reason = _urlguard.check(url, label="callback_url")
                self.assertFalse(ok, "%s 가 통과했다 — %s" % (url, why))
                self.assertTrue(reason, url)
                # 거부 사유에 URL·경로가 되비치지 않는다(기존 규약 유지)
                self.assertNotIn("/x", reason)

    def test_정상_주소는_통과(self):
        for url, why in ALLOWED:
            with self.subTest(url=url):
                ok, reason = _urlguard.check(url, label="callback_url")
                self.assertTrue(ok, "과차단: %s (%s) — %s" % (url, reason, why))

    def test_개발_플래그는_여전히_탈출구다(self):
        """`allow_insecure` 는 로컬 개발용 — 정규화가 그 탈출구를 막지 않는다."""
        self.assertTrue(_urlguard.check("http://localhost./x", allow_insecure=True)[0])
        self.assertTrue(_urlguard.check("http://127.1/x", allow_insecure=True)[0])

    def test_화이트리스트는_같은_호스트로_대조한다(self):
        """끝점은 같은 호스트다 — 화이트리스트 운영에서 표기 때문에 막히지 않는다."""
        self.assertTrue(_urlguard.check("https://only.me./x", allowlist=["only.me"])[0])
        self.assertTrue(_urlguard.check("https://ONLY.ME/x", allowlist=["only.me"])[0])
        self.assertFalse(_urlguard.check("https://other.me./x", allowlist=["only.me"])[0])
        # 화이트리스트가 켜져 있어도 빈 라벨은 형식 오류로 닫힌다
        self.assertFalse(_urlguard.check("https://a..b/x", allowlist=["a..b"])[0])


class SharedByCallers(unittest.TestCase):
    """규칙이 한 곳이라는 사실을 **호출부에서** 확인한다(한쪽만 고쳐지지 않게)."""

    def setUp(self):
        for k in ("CPAAS_RECORDING_HOSTS", "WELLBEING_CALLBACK_HOSTS",
                  "WELLBEING_ALLOW_INSECURE", "CPAAS_RECORDING_ALLOW_INSECURE"):
            os.environ.pop(k, None)

    def test_안부_콜백과_녹음_다운로드_양쪽에서_막힌다(self):
        import voice
        import wellbeing
        self.assertIs(wellbeing._urlguard, _urlguard)
        self.assertIs(voice._urlguard, _urlguard)
        for url, why in BYPASS:
            with self.subTest(url=url):
                self.assertFalse(wellbeing.check_callback_url(url)[0],
                                 "안부 콜백 통과: %s — %s" % (url, why))
                self.assertFalse(voice.check_recording_url(url)[0],
                                 "녹음 다운로드 통과: %s — %s" % (url, why))

    def test_이음_전송도_같은_판정을_쓴다(self):
        import _eum_request
        self.assertIs(_eum_request._urlguard, _urlguard)


# --------------------------------------------------------------------------
# 라우트 — 소켓 E2E
#
# 계약은 함수 반환값이 아니라 **라우트가 돌려주는 응답**이다. 가드가 거부해도
# 라우트가 그 판정을 흘려 버리면 아무것도 막히지 않는다. 그래서 라우팅·오리진
# 가드·직렬화를 모두 거친 뒤의 본문을 본다.
#
# 네트워크: **루프백 + 임의 포트만**. 거부된 주소로는 요청 자체를 보내지
# 않으므로 외부로 나가는 패킷이 없다(과금 0).
# --------------------------------------------------------------------------
E2E_ENVS = ("CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_ALLOWED_ORIGINS",
            "CPAAS_LIVE", "WELLBEING_ALLOW_INSECURE", "WELLBEING_CALLBACK_HOSTS")


class _Server(object):
    def __init__(self, handler):
        import http.server
        import threading
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def call(self, path, body=None):
        import json
        import urllib.error
        import urllib.request
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.origin + path, data=data,
                                     method="POST" if data else "GET")
        req.add_header("Origin", self.origin)
        req.add_header("Sec-Fetch-Site", "same-origin")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class WellbeingRouteOverHttp(unittest.TestCase):
    """안부 결과 웹훅 — 표기 우회 콜백은 **실행 전에** 400 으로 닫힌다."""

    @classmethod
    def setUpClass(cls):
        import wellbeing
        cls._saved = {k: os.environ.get(k) for k in E2E_ENVS}
        for k in E2E_ENVS:
            os.environ.pop(k, None)
        cls.srv = _Server(wellbeing.handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_표기_우회_콜백은_400_이고_이력도_남지_않는다(self):
        import wellbeing
        for url, why in BYPASS:
            with self.subTest(url=url):
                before = len(wellbeing.RECENT)
                status, d = self.srv.call(
                    "/api/wellbeing/call",
                    {"senior_id": "s-1", "profile": "ok", "callback_url": url})
                self.assertEqual(status, 400, "%s — %s / %s" % (url, why, d))
                self.assertEqual(d.get("code"), "INVALID_REQUEST", d)
                reasons = [x.get("reason", "") for x in (d.get("details") or [])]
                self.assertTrue(any(r for r in reasons), d)
                # 막힌 요청은 전송도 보관도 하지 않는다(이력이 생기지 않는다)
                self.assertEqual(len(wellbeing.RECENT), before, url)
                # 사유에 콜백 URL·경로가 되비치지 않는다(기존 규약 유지)
                blob = json.dumps(d, ensure_ascii=False)
                self.assertNotIn("/x", blob)
                self.assertNotIn("169.254", blob)

    def test_콜백_없는_dry_run_은_그대로_200(self):
        """과차단 확인 — 정상 경로는 손대지 않았다."""
        status, d = self.srv.call("/api/wellbeing/call",
                                  {"senior_id": "s-2", "profile": "ok"})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["ok"])
        self.assertTrue(d["delivery"]["skipped"], d["delivery"])
        self.assertFalse(d["delivery"]["delivered"], d["delivery"])
        self.assertIn(d["payload"]["risk_level"], ("low", "mid", "high", "unknown"))

    def test_개발_탈출구는_살아_있다(self):
        """`WELLBEING_ALLOW_INSECURE=1` 로 루프백 콜백이 **실제로** 들어간다.

        정규화가 탈출구를 막았으면 이 경로가 400 으로 닫힌다. 외부망은 타지
        않는다(콜백 받는 쪽도 루프백 임의 포트다).
        """
        import http.server

        got = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got.append(self.rfile.read(n))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        sink = _Server(Sink)
        os.environ["WELLBEING_ALLOW_INSECURE"] = "1"
        try:
            status, d = self.srv.call(
                "/api/wellbeing/call",
                {"senior_id": "s-3", "profile": "ok",
                 "callback_url": sink.origin + "/cb"})
        finally:
            os.environ.pop("WELLBEING_ALLOW_INSECURE", None)
            sink.close()
        self.assertEqual(status, 200, d)
        self.assertTrue(d["delivery"]["delivered"], d["delivery"])
        self.assertEqual(len(got), 1)
        body = json.loads(got[0].decode("utf-8"))
        self.assertEqual(body["schema"], "wellbeing.result.v1")
        # 원문 발화·성명이 웹훅 본문에 실리지 않는다(기존 규약 유지)
        self.assertNotIn("senior_name", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
