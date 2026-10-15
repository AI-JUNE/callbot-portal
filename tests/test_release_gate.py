# -*- coding: utf-8 -*-
"""scripts/verify.py 릴리스 게이트 — 검사 4종의 회귀. 네트워크 미사용.

왜 이 파일이 필요한가
  게이트는 "통과했다"는 말만 남기고 조용히 아무것도 검사하지 않을 수 있다.
  실제로 무엇을 잡는지 확인하지 않으면, 게이트가 있다는 사실이 오히려 안심시킨다.
  그래서 **일부러 틀린 소스를 넣어 잡히는지**까지 본다.

검증 대상
  1) outbound     — 미등록 외부 호출·가드 미경유·등록부 잔재를 잡는다
  2) request_log  — 라우트에 구조화 로그 배선이 빠지면 잡는다
  3) audit_actions— 감사를 배선했는데 `_audit.ADMIN_ACTIONS` 에 없는 라우트를 잡는다
  4) spoken_numbers— 프롬프트·데모 툴 결과에 박힌 수치(고객이 듣는 값)를 잡는다
  5) spoken_rules  — 시나리오 프롬프트가 "조회하지 않은 값은 말하지 않는다"를 지시하는가
  5) 현재 저장소는 네 검사를 모두 통과한다(게이트가 늘 빨간불이면 사람이 끈다)
  5) 등록부가 현실과 맞다 — 등록된 파일이 실제로 존재하고 실제로 밖으로 나간다

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


class TestAuditActions(unittest.TestCase):
    """27차: 감사를 배선했는데 등록부에 없으면 **아무것도 남지 않는다**.
    그 상태는 코드만 봐서는 보이지 않는다(호출은 있다) — 게이트가 센다."""

    WIRED = ("class handler(X):\n"
             "    def do_POST(self):\n"
             "        _audit.record_request(h, p, 'POST', 'allow', 200)\n")

    def test_wired_route_missing_from_registry_fails(self):
        ok, msg = verify.check_audit_actions({"newledger.py": self.WIRED})
        self.assertFalse(ok)
        self.assertIn("/api/newledger", msg)

    def test_record_without_request_helper_also_counts(self):
        src = "class handler(X):\n    _audit.record(h, 'x.y', 'allow', status=200)\n"
        ok, msg = verify.check_audit_actions({"probe.py": src})
        self.assertFalse(ok)
        self.assertIn("/api/probe", msg)

    def test_comment_mention_is_not_wiring(self):
        """주석의 설명까지 배선으로 세면 게이트가 거짓말을 한다."""
        src = "class handler(X):\n    # _audit.record_request( 를 붙일 예정\n    pass\n"
        ok, msg = verify.check_audit_actions(dict(verify.api_sources(), **{"doc.py": src}))
        self.assertTrue(ok, msg)

    def test_shared_modules_are_exempt(self):
        """`_` 로 시작하는 파일은 함수로 배포되지 않는 공용 모듈이다."""
        srcs = dict(verify.api_sources())
        srcs["_helper.py"] = self.WIRED
        ok, msg = verify.check_audit_actions(srcs)
        self.assertTrue(ok, msg)

    def test_stale_registry_entry_fails(self):
        """배선이 사라졌는데 등록부에 남아 있으면 등록부가 아니다."""
        srcs = {k: v for k, v in verify.api_sources().items() if k != "settlement.py"}
        ok, msg = verify.check_audit_actions(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)
        self.assertIn("/api/settlement", msg)

    def test_gate_reads_the_registry_from_source(self):
        """게이트는 api 코드를 **실행하지 않고** 등록부를 읽는다."""
        sys.path.insert(0, os.path.join(ROOT, "api"))
        import _audit                            # noqa: E402
        self.assertEqual(verify._audit_actions_registry(), dict(_audit.ADMIN_ACTIONS))

    def test_gate_reports_missing_registry(self):
        saved = verify.API_DIR
        verify.API_DIR = os.path.join(ROOT, "scripts")   # _audit.py 가 없는 폴더
        try:
            ok, msg = verify.check_audit_actions(verify.api_sources())
        finally:
            verify.API_DIR = saved
        self.assertFalse(ok)
        self.assertIn("ADMIN_ACTIONS", msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_audit_actions(verify.api_sources())
        self.assertTrue(ok, msg)


class TestSpokenNumbers(unittest.TestCase):
    """28차: 고객이 **귀로 듣는 수치**가 리터럴로 박혀 있으면 잡는다.

    27차 `queue_position: 2`·28차 `eta_days: 3`·대본의 「37,000원」이 모두 한 줄
    짜리 리터럴이었다 — 리뷰에서는 보이지 않고, 모든 통화에서 같은 거짓이 반복된다.
    """

    PROMPT = "PROMPT_X = '연체 안내입니다. 확인 후 안내드리겠습니다.'\n"

    def _srcs(self, demo):
        return {"_engine.py": self.PROMPT, "_order_backend.py": demo}

    DEMO_OK = ("class DemoOrderBackend(OrderBackend):\n"
               "    def confirm_refund(self, inp):\n"
               "        return {'status': 'accepted', 'eta_days': None,\n"
               "                'eta_days_known': False, 'note': '알 수 없음'}\n")

    def test_prompt_with_an_amount_fails(self):
        srcs = self._srcs(self.DEMO_OK)
        srcs["_engine.py"] = "PROMPT_OVERDUE = '현재 미납금은 37,000원입니다.'\n"
        ok, msg = verify.check_spoken_numbers(srcs)
        self.assertFalse(ok)
        self.assertIn("PROMPT_OVERDUE", msg)
        self.assertIn("37,000원", msg)

    def test_amount_hidden_in_a_concatenated_prompt_is_found(self):
        """프롬프트는 여러 줄을 이어 붙인다 — 중간 줄도, 어떤 이어붙임이든 본다.

        나란히 놓은 문자열은 파서가 한 덩어리로 합치지만 `+`·f-string 은
        그렇지 않다 — 표현식 안쪽까지 내려가지 않으면 그 모양으로 숨는다.
        """
        for label, src in (
            ("나란히", "PROMPT_A = ('첫 줄\\n'\n '미납금은 37,000원입니다\\n'\n '끝 줄')\n"),
            ("더하기", "PROMPT_A = '첫 줄' + '미납금은 37,000원입니다' + '끝 줄'\n"),
            ("f-string", "PROMPT_A = f'{x} 미납금은 37,000원입니다'\n"),
        ):
            with self.subTest(shape=label):
                srcs = self._srcs(self.DEMO_OK)
                srcs["_engine.py"] = src
                ok, msg = verify.check_spoken_numbers(srcs)
                self.assertFalse(ok)
                self.assertIn("PROMPT_A", msg)

    def test_call_duration_is_not_an_amount(self):
        """'약 2분'·'생년월일 6자리'까지 잡으면 사람이 게이트를 끈다."""
        srcs = self._srcs(self.DEMO_OK)
        srcs["_engine.py"] = ("PROMPT_B = '약 2분 걸립니다. 생년월일 6자리를 "
                              "말씀해 주세요. 3회 틀리면 종료합니다.'\n")
        ok, msg = verify.check_spoken_numbers(srcs)
        self.assertTrue(ok, msg)

    def test_missing_prompts_is_a_failure_not_a_pass(self):
        """검사 대상이 0개인데 통과로 보고하면 게이트가 사라진 것을 모른다."""
        ok, msg = verify.check_spoken_numbers({"_order_backend.py": self.DEMO_OK})
        self.assertFalse(ok)
        self.assertIn("PROMPT_", msg)

    def test_demo_tool_result_with_a_number_fails(self):
        demo = ("class DemoOrderBackend(OrderBackend):\n"
                "    def confirm_refund(self, inp):\n"
                "        return {'status': 'accepted', 'eta_days': 3}\n")
        ok, msg = verify.check_spoken_numbers(self._srcs(demo))
        self.assertFalse(ok)
        self.assertIn("eta_days=3", msg)

    def test_number_nested_in_the_result_is_found(self):
        demo = ("class DemoOrderBackend(OrderBackend):\n"
                "    def escalate_to_agent(self, inp):\n"
                "        return {'queue': {'position': 2}}\n")
        ok, msg = verify.check_spoken_numbers(self._srcs(demo))
        self.assertFalse(ok)
        self.assertIn("position=2", msg)

    def test_unknown_markers_are_not_numbers(self):
        """null + `*_known: false` 로 '모른다'를 말하는 모양은 통과해야 한다."""
        ok, msg = verify.check_spoken_numbers(self._srcs(self.DEMO_OK))
        self.assertTrue(ok, msg)

    def test_demo_order_ledger_is_not_the_target(self):
        """데모 주문(DEMO_ORDER)의 금액은 시연용 '장부'다 — 한도·견적이 그 값에서
        계산돼 나오므로 서로 어긋나지 않는다(과차단하면 데모가 죽는다)."""
        demo = ("DEMO_ORDER = {'items': [{'name': 'x', 'qty': 1, 'price': 159000}]}\n"
                + self.DEMO_OK)
        ok, msg = verify.check_spoken_numbers(self._srcs(demo))
        self.assertTrue(ok, msg)

    def test_missing_demo_class_fails(self):
        ok, msg = verify.check_spoken_numbers(self._srcs("# 데모 백엔드가 사라졌다\n"))
        self.assertFalse(ok)
        self.assertIn("DemoOrderBackend", msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._srcs(self.DEMO_OK)
        srcs["_engine.py"] = "raise SystemExit('실행되면 안 된다')\nPROMPT_Z = 'ok'\n"
        ok, msg = verify.check_spoken_numbers(srcs)
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_spoken_numbers(verify.api_sources())
        self.assertTrue(ok, msg)


class TestSpokenRules(unittest.TestCase):
    """29차: 숫자가 박혀 있지 않아도 **답하라고 지시하면** 모델이 지어낸다.

    연체 대본의 일반 질의 자리(「인지세? 만기 전 상환 비용?」)가 그랬다 —
    「간결·정확하게 답한다」 한 줄뿐이었고, 그 값을 조회하는 경로는 없다.
    그래서 게이트는 반대쪽을 본다: **규칙이 프롬프트 안에 있는가**.
    """

    SYS = ("def _sys(phone):\n"
           "    return ('너는 상담원이다.\\n'\n"
           "            f'발신번호:{phone}\\n'\n"
           "            '툴이 주지 않은 수치는 말하지 않는다.')\n")

    def _srcs(self, prompt, sysfn=None):
        return {"_engine.py": prompt + (self.SYS if sysfn is None else sysfn)}

    def test_prompt_without_the_rule_fails(self):
        src = "PROMPT_OVERDUE = '인지세는 간결·정확하게 답한다.'\n"
        ok, msg = verify.check_spoken_rules(self._srcs(src))
        self.assertFalse(ok)
        self.assertIn("PROMPT_OVERDUE", msg)

    def test_either_marker_passes(self):
        for rule in ("조회하지 않은 값은 말하지 않는다", "모르는 금액은 지어내지 않는다"):
            with self.subTest(rule=rule):
                ok, msg = verify.check_spoken_rules(
                    self._srcs("PROMPT_A = '안내입니다. %s'\n" % rule))
                self.assertTrue(ok, msg)

    def test_rule_hidden_in_a_concatenated_prompt_is_found(self):
        for label, src in (
            ("나란히", "PROMPT_A = ('첫 줄\\n'\n '조회하지 않은 값은 말하지 않는다\\n'\n '끝')\n"),
            ("더하기", "PROMPT_A = '첫 줄' + '조회하지 않은 값은 말하지 않는다'\n"),
            ("f-string", "PROMPT_A = f'{x} 조회하지 않은 값은 말하지 않는다'\n"),
        ):
            with self.subTest(shape=label):
                ok, msg = verify.check_spoken_rules(self._srcs(src))
                self.assertTrue(ok, msg)

    def test_the_cs_system_prompt_is_checked_too(self):
        """상수가 아니라 함수로 조립되는 프롬프트도 같은 규칙을 받는다."""
        sysfn = ("def _sys(phone):\n"
                 "    return '너는 상담원이다. 주문은 툴 결과를 인용한다.'\n")
        ok, msg = verify.check_spoken_rules(
            self._srcs("PROMPT_A = '조회하지 않은 값은 말하지 않는다'\n", sysfn))
        self.assertFalse(ok)
        self.assertIn("_sys()", msg)

    def test_a_rule_in_the_docstring_does_not_count(self):
        """주석·독스트링은 봇이 말하는 문장이 아니다(모델에게 가지 않는다)."""
        sysfn = ("def _sys(phone):\n"
                 "    '''모르는 수치는 말하지 않는다 — 라고 적어만 뒀다'''\n"
                 "    return '너는 상담원이다.'\n")
        ok, msg = verify.check_spoken_rules(
            self._srcs("PROMPT_A = '지어내지 않는다'\n", sysfn))
        self.assertFalse(ok)
        self.assertIn("_sys()", msg)

    def test_missing_prompt_function_is_registry_drift(self):
        """`_sys` 가 사라지면 검사 대상이 조용히 줄어든다 — 등록부 잔재로 잡는다."""
        ok, msg = verify.check_spoken_rules(
            {"_engine.py": "PROMPT_A = '지어내지 않는다'\n"})
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_missing_prompts_is_a_failure_not_a_pass(self):
        ok, msg = verify.check_spoken_rules({"_order_backend.py": "X = 1\n"})
        self.assertFalse(ok)
        self.assertIn("PROMPT_", msg)

    def test_gate_reads_sources_without_executing_them(self):
        src = "raise SystemExit('실행되면 안 된다')\nPROMPT_Z = '지어내지 않는다'\n"
        ok, msg = verify.check_spoken_rules(self._srcs(src))
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_spoken_rules(verify.api_sources())
        self.assertTrue(ok, msg)

    def test_every_live_scenario_prompt_is_covered(self):
        """통과 메시지가 '0개 검사'를 숨기지 않는지 — 실제 개수를 본다."""
        ok, msg = verify.check_spoken_rules(verify.api_sources())
        self.assertTrue(ok, msg)
        self.assertIn("6개", msg)              # PROMPT_* 5개 + _sys()


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
        for step in ("outbound", "request_log", "audit_actions", "spoken_numbers",
                     "spoken_rules"):
            self.assertIn(step, out)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
