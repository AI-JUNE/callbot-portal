# -*- coding: utf-8 -*-
"""scripts/verify.py 릴리스 게이트 — 새 검사 2종의 회귀. 네트워크 미사용.

왜 이 파일이 필요한가
  게이트는 "통과했다"는 말만 남기고 조용히 아무것도 검사하지 않을 수 있다.
  실제로 무엇을 잡는지 확인하지 않으면, 게이트가 있다는 사실이 오히려 안심시킨다.
  그래서 **일부러 틀린 소스를 넣어 잡히는지**까지 본다.

검증 대상
  1) outbound    — 미등록 외부 호출·가드 미경유·등록부 잔재를 잡는다
  2) request_log — 라우트에 구조화 로그 배선이 빠지면 잡는다
  3) 현재 저장소는 두 검사를 모두 통과한다(게이트가 늘 빨간불이면 사람이 끈다)
  4) 등록부가 현실과 맞다 — 등록된 파일이 실제로 존재하고 실제로 밖으로 나간다

실행: python3 -m pytest tests/test_release_gate.py -q
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify  # noqa: E402


class TestOutbound(unittest.TestCase):
    def test_unregistered_outbound_call_fails(self):
        ok, msg = verify.check_outbound({"newthing.py": "urllib.request.urlopen(url)"})
        self.assertFalse(ok)
        self.assertIn("newthing.py", msg)

    def test_socket_reachability_counts_as_outbound(self):
        ok, _msg = verify.check_outbound({"probe.py": "socket.create_connection((h, p))"})
        self.assertFalse(ok)

    def test_comment_mention_is_not_a_call(self):
        """주석의 설명까지 호출로 세면 게이트가 거짓말을 한다."""
        ok, _msg = verify.check_outbound({"doc.py": "# urlopen( 을 쓰면 안 된다"})
        self.assertTrue(ok)

    def test_request_derived_url_must_pass_urlguard(self):
        src = "import urllib.request\nurllib.request.urlopen(req)\n"
        ok, msg = verify.check_outbound({"voice.py": src})
        self.assertFalse(ok)
        self.assertIn("가드 미경유", msg)
        ok, _msg = verify.check_outbound({"voice.py": "import _urlguard\n" + src})
        self.assertTrue(ok)

    def test_stale_registry_entry_fails(self):
        ok, msg = verify.check_outbound({"_monitoring.py": "pass"})
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_registry_matches_reality(self):
        srcs = verify.api_sources()
        for name in verify.OUTBOUND:
            self.assertIn(name, srcs, "등록부에만 있는 파일: %s" % name)
        for name in verify.URLGUARD_REQUIRED:
            self.assertIn(name, verify.OUTBOUND)

    def test_repository_passes_today(self):
        ok, msg = verify.check_outbound(verify.api_sources())
        self.assertTrue(ok, msg)


class TestRequestLog(unittest.TestCase):
    def test_route_without_log_fails(self):
        ok, msg = verify.check_request_log({"thing.py": "class handler(X):\n    pass\n"})
        self.assertFalse(ok)
        self.assertIn("thing.py", msg)

    def test_route_with_log_passes(self):
        ok, _msg = verify.check_request_log(
            {"thing.py": "class handler(X):\n    rq = _log.begin(self.headers, '/x', 'GET')\n"})
        self.assertTrue(ok)

    def test_shared_modules_are_exempt(self):
        """`_` 로 시작하는 파일은 함수로 배포되지 않는 공용 모듈이다."""
        ok, msg = verify.check_request_log({
            "_stt.py": "class handler(X):\n    pass\n",
            "speech.py": "class handler(_stt.handler):\n    rq = _log.begin(h, '/x', 'GET')\n",
        })
        self.assertTrue(ok, msg)
        self.assertIn("1개 라우트", msg)

    def test_empty_api_dir_is_a_failure_not_a_pass(self):
        """검사 대상이 0개인데 통과로 보고하면 게이트가 사라진 것을 모른다."""
        ok, msg = verify.check_request_log({})
        self.assertFalse(ok)
        self.assertIn("없음", msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_request_log(verify.api_sources())
        self.assertTrue(ok, msg)


class TestGateRuns(unittest.TestCase):
    def test_main_returns_zero_on_clean_tree(self):
        import io as _io
        import contextlib
        buf = _io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py"]
        try:
            with contextlib.redirect_stdout(buf):
                rc = verify.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 0, buf.getvalue())
        out = buf.getvalue()
        for step in ("outbound", "request_log"):
            self.assertIn(step, out)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
