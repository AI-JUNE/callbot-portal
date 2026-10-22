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
  6) mem_caps      — 인스턴스 메모리 저장소에 보관 상한이 있는가(등록부 대조)
  7) 현재 저장소는 위 검사를 모두 통과한다(게이트가 늘 빨간불이면 사람이 끈다)
  8) 등록부가 현실과 맞다 — 등록된 파일이 실제로 존재하고 실제로 밖으로 나간다

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


class TestMemCaps(unittest.TestCase):
    """30차 게이트 — 인스턴스 메모리 저장소에 보관 상한이 있는가.

    영속 저장소가 없는 동안 통화 세션·감사·안부 결과는 전부 프로세스 메모리에
    쌓인다. 상한이 없다는 것은 **코드에 없는 것**이라 리뷰에서 보이지 않는다.
    """

    def _repo(self):
        return verify.api_sources()

    def test_new_unregistered_store_fails(self):
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": "CACHE = {}\n"}))
        self.assertFalse(ok)
        self.assertIn("newthing.py", msg)
        self.assertIn("MEM_STORES", msg)

    def test_empty_list_store_also_counts(self):
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": "LOG = []\n"}))
        self.assertFalse(ok)
        self.assertIn("LOG", msg)

    def test_dict_call_does_not_slip_past_the_literal_check(self):
        """`{}` 만 보면 `dict()` 로 우회된다."""
        ok, _msg = verify.check_mem_caps(self._all({"newthing.py": "CACHE = dict()\n"}))
        self.assertFalse(ok)

    def test_store_inside_a_class_body_is_checked(self):
        src = "class _S:\n    _cache = {}\n"
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("_cache", msg)

    def test_constant_registry_is_not_a_store(self):
        """값이 든 리터럴(상수·요율표)은 자라지 않는다 — 과차단하면 게이트가 꺼진다."""
        src = "LABELS = {'a': 1}\nORDER = ['x']\n"
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_instance_attribute_of_a_per_request_class_is_out_of_scope(self):
        """검사 범위를 좁힌 사실을 고정한다(한계를 숨기지 않는다).

        모듈 전역 인스턴스가 없는 클래스는 호출부가 매번 새로 만든다 =
        요청당 수명이다. 여기까지 잡으면 과차단이고, 과차단되면 사람이
        게이트를 끈다.
        """
        src = "class _S:\n    def __init__(self):\n        self._x = {}\n"
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_instance_attribute_of_a_module_singleton_is_checked(self):
        """31차 확장 — `STORE = C()` 로 박히는 인스턴스는 프로세스 수명을 산다."""
        src = ("class C:\n    def __init__(self):\n        self._recs = {}\n"
               "STORE = C()\n")
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("C._recs", msg)

    def test_singleton_attribute_assigned_outside_init_is_also_checked(self):
        """reset() 에서 비우는 저장소도 같은 인스턴스다 — 생성자만 보면 샌다."""
        src = ("class C:\n    def reset(self):\n        self._recs = []\n"
               "STORE = C()\n")
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("C._recs", msg)

    def test_same_store_emptied_in_two_places_is_reported_once(self):
        src = ("class C:\n"
               "    def __init__(self):\n        self._recs = {}\n"
               "    def reset(self):\n        self._recs = {}\n"
               "STORE = C()\n")
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertEqual(msg.count("C._recs"), 1)

    def test_a_local_variable_is_not_a_store(self):
        """`out = []` 같은 지역 변수까지 잡으면 게이트가 쓸모없어진다."""
        src = ("class C:\n    def f(self):\n        out = []\n        return out\n"
               "STORE = C()\n")
        ok, msg = verify.check_mem_caps(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_registered_singleton_store_passes_with_its_cap(self):
        src = ("MAX_RECS = 10\n"
               "class C:\n    def __init__(self):\n        self._recs = {}\n"
               "STORE = C()\n")
        srcs = self._all({"newthing.py": src})
        verify.MEM_STORES[("newthing.py", "C._recs")] = ("MAX_RECS", "테스트")
        try:
            ok, msg = verify.check_mem_caps(srcs)
        finally:
            del verify.MEM_STORES[("newthing.py", "C._recs")]
        self.assertTrue(ok, msg)

    def test_removing_a_singleton_attribute_cap_fails(self):
        """30차가 범위 밖으로 두고 보고서에 적어 둔 구멍 — 이제 게이트가 본다."""
        srcs = self._repo()
        srcs["_recording_audit.py"] = (
            "class RecordingStore:\n    def __init__(self):\n"
            "        self._records = {}\n        self._audit = []\n"
            "STORE = RecordingStore()\n")
        ok, msg = verify.check_mem_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("MAX_RECORDS", msg)

    def test_removing_the_cap_symbol_fails(self):
        """등록부만 남고 상한이 사라지는 드리프트를 잡는다."""
        srcs = self._repo()
        srcs["voice.py"] = "class _Session:\n    _mem = {}\n"
        ok, msg = verify.check_mem_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("SESSION_MAX", msg)

    def test_stale_registry_entry_fails(self):
        srcs = self._repo()
        srcs["wellbeing.py"] = "X = 1\n"
        ok, msg = verify.check_mem_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._repo()
        srcs["newthing.py"] = "raise SystemExit('실행되면 안 된다')\nLABELS = {'a': 1}\n"
        ok, msg = verify.check_mem_caps(srcs)
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_mem_caps(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.MEM_STORES), msg)

    def test_registry_matches_reality(self):
        """등록된 저장소가 실제로 그 파일에 있고, 상한 기호도 있다."""
        srcs = self._repo()
        for (fname, store), (symbol, note) in verify.MEM_STORES.items():
            self.assertIn(fname, srcs, "등록부가 없는 파일을 가리킨다: %s" % fname)
            self.assertIn((store, ), [(n, ) for n, _l in verify._mem_targets(srcs[fname])],
                          "%s 에 %s 저장소가 없다" % (fname, store))
            self.assertIn(symbol, srcs[fname], "%s 에 상한 기호 %s 가 없다" % (fname, symbol))
            self.assertTrue(note.strip(), "사유가 비어 있다: %s" % fname)

    def _all(self, extra):
        srcs = self._repo()
        srcs.update(extra)
        return srcs


class TestHistoryCaps(unittest.TestCase):
    """32차 게이트 — `run_turn` 호출부가 대화 이력 상한을 거치는가.

    `mem_caps` 는 저장소 **선언**(`X = {}`)을 본다. 대화 이력은 그 눈으로
    보이지 않는다 — 선언은 `{"messages": []}` 한 칸이고 자라는 것은 그 안쪽이다.
    31차가 모델 창을, 32차가 저장 이력을 막았지만 둘 다 **호출부가 거쳐야**
    효력이 있다(지금까지 `voice.py` 가 거치지 않았다).
    """

    def _repo(self):
        return verify.api_sources()

    def _all(self, extra):
        srcs = self._repo()
        srcs.update(extra)
        return srcs

    def test_new_unregistered_caller_fails(self):
        src = "from _engine import run_turn\nr = run_turn(msgs)\n"
        ok, msg = verify.check_history_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("newthing.py", msg)
        self.assertIn("HISTORY_CALLERS", msg)

    def test_attribute_call_is_also_a_call(self):
        """`engine.run_turn(...)` 모양으로 불러도 같은 호출부다."""
        src = "import _engine\nr = _engine.run_turn(msgs)\n"
        ok, msg = verify.check_history_caps(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("newthing.py", msg)

    def test_importing_without_calling_is_not_a_caller(self):
        """과차단하지 않는다 — import·주석만 있는 파일은 이력을 쌓지 않는다."""
        src = "from _engine import run_turn  # 아직 안 쓴다\n"
        ok, msg = verify.check_history_caps(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_mentioning_run_turn_in_a_comment_is_not_a_call(self):
        src = "# run_turn(msgs) 로 바꿀 예정\nX = 1\n"
        ok, msg = verify.check_history_caps(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_removing_the_cap_symbol_fails(self):
        """등록부만 남고 상한이 사라지는 드리프트를 잡는다(voice 의 trim_history)."""
        srcs = self._repo()
        srcs["voice.py"] = "r = run_turn(sess['messages'])\n"
        ok, msg = verify.check_history_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("trim_history", msg)

    def test_a_comment_does_not_count_as_the_cap(self):
        """주석에 적어 두는 것은 배선이 아니다(audit_actions 와 같은 규약)."""
        srcs = self._repo()
        srcs["voice.py"] = "# trim_history 를 붙일 예정\nr = run_turn(m)\n"
        ok, msg = verify.check_history_caps(srcs)
        self.assertFalse(ok)

    def test_stale_registry_entry_fails(self):
        srcs = self._repo()
        srcs["sim_call.py"] = "X = 1\n"
        ok, msg = verify.check_history_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_engine_losing_its_own_caps_fails(self):
        """호출부 배선만 보면, 엔진이 상한을 지워도 전부 통과해 버린다."""
        for symbol in ("HISTORY_MAX_MSGS", "HISTORY_MAX_CHARS", "def trim_history",
                       "mem_prior"):
            srcs = self._repo()
            srcs["_engine.py"] = srcs["_engine.py"].replace(symbol, "GONE")
            ok, msg = verify.check_history_caps(srcs)
            self.assertFalse(ok, symbol)
            self.assertIn(symbol, msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._repo()
        srcs["newthing.py"] = "raise SystemExit('실행되면 안 된다')\nX = 1\n"
        ok, msg = verify.check_history_caps(srcs)
        self.assertTrue(ok, msg)

    def test_syntax_error_is_left_to_py_compile(self):
        srcs = self._repo()
        srcs["newthing.py"] = "def broken(:\n"
        ok, msg = verify.check_history_caps(srcs)
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_history_caps(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.HISTORY_CALLERS), msg)

    def test_registry_matches_reality(self):
        srcs = self._repo()
        for fname, (symbol, note) in verify.HISTORY_CALLERS.items():
            self.assertIn(fname, srcs, "등록부가 없는 파일을 가리킨다: %s" % fname)
            self.assertTrue(verify._calls_run_turn(srcs[fname]),
                            "%s 는 run_turn 을 호출하지 않는다" % fname)
            self.assertIn(symbol, srcs[fname],
                          "%s 에 상한 기호 %s 가 없다" % (fname, symbol))
            self.assertTrue(note.strip(), "사유가 비어 있다: %s" % fname)


class TestSpeechRules(unittest.TestCase):
    """33차 게이트 — 고객 발화를 **긍정으로 읽는** 규칙에 부정 처리가 있는가.

    `_engine._AFFIRM` 은 `(네|예|응|좋아|…|할게|동의)` 한 줄이었고, 한글 1글자
    대안은 다른 낱말 안에서 걸린다(「취소**할게**요」·「**예**전에」). 코드에
    **없는 것**(부정 처리)이 문제라 리뷰에서 보이지 않는다.
    """

    def _repo(self):
        return verify.api_sources()

    def _all(self, extra):
        srcs = self._repo()
        srcs.update(extra)
        return srcs

    def test_unregistered_hangul_rule_fails(self):
        src = "import re\n_YES = re.compile('좋아요|맞아요')\n"
        ok, msg = verify.check_speech_rules(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("newthing.py:_YES", msg)
        self.assertIn("SPEECH_RULES", msg)

    def test_the_old_loose_shape_is_caught(self):
        """이번 결함의 모양 그대로 — 1글자 한글 대안이 돌아오면 실패한다."""
        srcs = self._repo()
        srcs["_engine.py"] = srcs["_engine.py"].replace(
            "_AFFIRM_PHRASE=re.compile(",
            "_AFFIRM_PHRASE=re.compile(\"(네|예|응|좋아|할게|동의)\")\n_UNUSED=re.compile(")
        ok, msg = verify.check_speech_rules(srcs)
        self.assertFalse(ok)
        self.assertIn("1글자", msg)

    def test_a_prefixed_group_is_not_a_single_syllable_alternative(self):
        """과차단하지 않는다 — `진행(?:해|하)` 의 '해' 는 홀로 걸리지 않는다."""
        self.assertEqual(verify._single_hangul_alts("진행(?:해|하)|접수해"), [])
        self.assertEqual(verify._single_hangul_alts("좋아요|해\\s*주세요"), [])

    def test_a_bare_group_of_single_syllables_is_flagged(self):
        self.assertEqual(sorted(set(verify._single_hangul_alts("(네|예|응|좋아)"))),
                         ["네", "예", "응"])
        self.assertIn("네", verify._single_hangul_alts("네|좋아요"))

    def test_a_boundary_guarded_single_syllable_is_allowed(self):
        """경계를 보는 대안은 낱말 안에서 걸리지 않는다 — 이것이 올바른 모양이다."""
        self.assertEqual(verify._single_hangul_alts("(?<![가-힣])네(?![가-힣])"), [])
        self.assertEqual(verify._single_hangul_alts("(?<![가-힣])안\\s+[가-힣]|싫어요"),
                         [])

    def test_a_character_class_is_not_a_required_syllable(self):
        self.assertEqual(verify._syllables("[네예응]\\s*\\?"), "")
        self.assertEqual(verify._syllables("좋아요"), "좋아요")

    def test_latin_alternatives_are_out_of_scope(self):
        self.assertEqual(verify._single_hangul_alts("yes|ok|좋아요"), [])

    def test_deny_rules_may_stay_loose(self):
        """거절 쪽은 넓게 걸려도 된다 — 동의가 아니라고 끝나고 봇이 다시 묻는다."""
        self.assertIn("싫", verify._single_hangul_alts(
            verify._hangul_regexes(self._repo()["_engine.py"])["_DENY"]))
        ok, msg = verify.check_speech_rules(self._repo())
        self.assertTrue(ok, msg)

    def test_a_regex_built_inside_a_function_is_out_of_scope(self):
        """범위를 좁힌 사실을 고정한다(과차단되면 사람이 게이트를 끈다)."""
        src = ("import re\ndef f(x):\n    pat = re.compile('네|예')\n"
               "    return pat.search(x)\n")
        ok, msg = verify.check_speech_rules(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_a_regex_without_hangul_is_out_of_scope(self):
        src = "import re\nX = re.compile(r'a|b')\n"
        ok, msg = verify.check_speech_rules(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_stale_registry_entry_fails(self):
        srcs = self._repo()
        srcs["wellbeing.py"] = "X = 1\n"
        ok, msg = verify.check_speech_rules(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_losing_the_negation_symbol_fails(self):
        """등록부만 남고 부정 처리가 사라지는 드리프트를 잡는다."""
        for fname, symbols in verify.SPEECH_REQUIRED.items():
            for symbol in symbols:
                srcs = self._repo()
                srcs[fname] = srcs[fname].replace(symbol, "GONE")
                ok, msg = verify.check_speech_rules(srcs)
                self.assertFalse(ok, "%s %s" % (fname, symbol))
                self.assertIn(symbol, msg)

    def test_a_comment_does_not_count_as_the_wiring(self):
        srcs = self._repo()
        srcs["wellbeing.py"] = ("# def _negated 를 붙일 예정\n"
                                "_NEG = re.compile('지\\\\s*않|지\\\\s*못')\n"
                                "_POS_IDIOM = re.compile('아무렇지|나쁘지 않')\n")
        ok, msg = verify.check_speech_rules(srcs)
        self.assertFalse(ok)
        self.assertIn("_negated", msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._repo()
        srcs["newthing.py"] = "raise SystemExit('실행되면 안 된다')\nX = 1\n"
        ok, msg = verify.check_speech_rules(srcs)
        self.assertTrue(ok, msg)

    def test_syntax_error_is_left_to_py_compile(self):
        srcs = self._repo()
        srcs["newthing.py"] = "def broken(:\n"
        ok, msg = verify.check_speech_rules(srcs)
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_speech_rules(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.SPEECH_RULES), msg)

    def test_registry_matches_reality(self):
        srcs = self._repo()
        for (fname, symbol), (direction, note) in verify.SPEECH_RULES.items():
            self.assertIn(fname, srcs, "등록부가 없는 파일을 가리킨다: %s" % fname)
            self.assertIn(symbol, verify._hangul_regexes(srcs[fname]),
                          "%s 에 %s 가 없다" % (fname, symbol))
            self.assertIn(direction, ("affirm", "deny"), symbol)
            self.assertTrue(note.strip(), "사유가 비어 있다: %s" % symbol)


class TestKeywordTables(unittest.TestCase):
    """34차 게이트 — 문자열 낱말표도 발화를 판정한다.

    33차가 정규식만 세고 「문자열 리스트 키워드표는 범위 밖」이라고 적어 뒀고,
    34차 결함이 그 범위 밖에서 나왔다(`_eum_request._RULES` 의 `피가`·`숨을` 이
    「커**피를**」·「한**숨을**」 에서 걸려 긴급으로 올라갔다).
    """

    def _repo(self):
        return verify.api_sources()

    def _all(self, extra):
        srcs = self._repo()
        srcs.update(extra)
        return srcs

    def test_unregistered_keyword_table_fails(self):
        src = "WORDS = ('쓰러', '의식', '숨이')\n"
        ok, msg = verify.check_keyword_tables(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("newthing.py:WORDS", msg)
        self.assertIn("KEYWORD_TABLES", msg)

    def test_an_unregistered_single_syllable_keyword_fails(self):
        """이번 결함의 이웃 — 1글자 낱말을 몰래 끼워 넣으면 통과하지 못한다."""
        srcs = self._repo()
        srcs["_eum_request.py"] = srcs["_eum_request.py"].replace(
            '"다툼", "불편"', '"다툼", "불편", "말"')
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("1글자", msg)
        self.assertIn("말", msg)

    def test_registered_single_syllable_keywords_pass(self):
        """금지가 아니라 검토 강제다 — 사유가 적힌 1글자는 통과한다."""
        ok, msg = verify.check_keyword_tables(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("1글자 등록 %d건" % len(verify.KEYWORD_SHORT), msg)

    def test_a_removed_single_syllable_registration_is_stale(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = srcs["_eum_request.py"].replace('"돈", ', "")
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("1글자 등록부 잔재", msg)

    def test_label_tables_are_not_checked_for_single_syllables(self):
        """표시용 표는 판정에 쓰이지 않으므로 느슨해도 된다(과차단 방지)."""
        srcs = self._all({"newthing.py": "T = ('가', '나', '다')\n"})
        verify.KEYWORD_TABLES[("newthing.py", "T")] = ("label", "시험용")
        try:
            ok, msg = verify.check_keyword_tables(srcs)
            self.assertTrue(ok, msg)
        finally:
            del verify.KEYWORD_TABLES[("newthing.py", "T")]

    def test_sentences_and_prompts_are_out_of_scope(self):
        """범위를 좁힌 사실을 고정한다 — 질문지·안내 문구는 낱말표가 아니다."""
        for src in ("Q = ('요즘 기분은 어떠세요?', '식사는 하셨어요?', '잠은 잘 주무세요?')\n",
                    "T = ('환불 금액 산정을 도와드립니다', '재배달 접수를 도와드립니다',"
                    " '주문 조회를 도와드립니다')\n"):
            ok, msg = verify.check_keyword_tables(self._all({"newthing.py": src}))
            self.assertTrue(ok, msg)

    def test_a_table_built_inside_a_function_is_out_of_scope(self):
        src = ("def f():\n    words = ('쓰러', '의식', '숨이')\n    return words\n")
        ok, msg = verify.check_keyword_tables(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_a_table_without_hangul_is_out_of_scope(self):
        src = "X = ('a', 'bb', 'ccc')\n"
        ok, msg = verify.check_keyword_tables(self._all({"newthing.py": src}))
        self.assertTrue(ok, msg)

    def test_a_dict_table_is_in_scope(self):
        """`wellbeing._RULES` 는 사전이다 — 모양이 달라도 같은 위험이다."""
        src = "X = {'a': ['쓰러', '의식'], 'b': ['숨이']}\n"
        ok, msg = verify.check_keyword_tables(self._all({"newthing.py": src}))
        self.assertFalse(ok)
        self.assertIn("newthing.py:X", msg)

    def test_stale_registry_entry_fails(self):
        """표가 사라지고 등록부만 남는 드리프트. 다른 검사가 대신 울어 주지
        않도록 1글자·가림 등록이 없는 파일로 본다(검사 하나가 하나를 지킨다)."""
        srcs = self._repo()
        srcs["settlement.py"] = "X = 1\n"
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("등록부 잔재", msg)
        self.assertIn("settlement.py:CSV_COLUMNS", msg)

    def test_losing_the_decoy_mask_fails(self):
        """등록부만 남고 오탐 가림이 사라지는 드리프트를 잡는다(= 34차 결함 복귀)."""
        for fname, symbols in verify.KEYWORD_MASKS.items():
            for symbol in symbols:
                srcs = self._repo()
                srcs[fname] = srcs[fname].replace(symbol, "GONE")
                ok, msg = verify.check_keyword_tables(srcs)
                self.assertFalse(ok, "%s %s" % (fname, symbol))
                self.assertIn(symbol, msg)

    def test_a_comment_does_not_count_as_the_mask(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = ("# _DECOY 와 def _mask_decoys 를 붙일 예정\n"
                                   "_RULES = {'a': ['쓰러', '의식', '숨이']}\n")
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertFalse(ok)
        self.assertIn("_mask_decoys", msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._repo()
        srcs["newthing.py"] = "raise SystemExit('실행되면 안 된다')\nX = 1\n"
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertTrue(ok, msg)

    def test_syntax_error_is_left_to_py_compile(self):
        srcs = self._repo()
        srcs["newthing.py"] = "def broken(:\n"
        ok, msg = verify.check_keyword_tables(srcs)
        self.assertTrue(ok, msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_keyword_tables(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.KEYWORD_TABLES), msg)

    def test_registry_matches_reality(self):
        srcs = self._repo()
        for (fname, symbol), (purpose, note) in verify.KEYWORD_TABLES.items():
            self.assertIn(fname, srcs, "등록부가 없는 파일을 가리킨다: %s" % fname)
            self.assertIn(symbol, verify._keyword_tables(srcs[fname]),
                          "%s 에 %s 가 없다" % (fname, symbol))
            self.assertIn(purpose, ("match", "mask", "label"), symbol)
            self.assertTrue(note.strip(), "사유가 비어 있다: %s" % symbol)
        for (fname, symbol, word), note in verify.KEYWORD_SHORT.items():
            self.assertEqual(verify.KEYWORD_TABLES[(fname, symbol)][0], "match")
            self.assertIn(word, verify._keyword_tables(srcs[fname])[symbol])
            self.assertTrue(note.strip(), "1글자 사유가 비어 있다: %s" % word)


class TestJudgeInputs(unittest.TestCase):
    """35차 게이트 — 판정 함수에 **잘린 글**을 넘기지 않는가.

    15단계까지는 판정 *규칙*을 봤다. 35차 결함은 규칙이 아니라 규칙에 넣는
    *글*이 틀려서 났다 — `_eum_request.build_payload` 가 500자로 자른 요약으로
    분류해 뒤쪽 긴급 신호를 통째로 놓쳤다. 두 줄을 같이 읽어야만 보인다.
    """

    def _repo(self):
        return verify.api_sources()

    DEFECT = ("SUMMARY_MAX = 500\n"
              "def clean_summary(t):\n"
              "    return scrub(t)[:SUMMARY_MAX]\n"
              "def build_payload(t):\n"
              "    summary = clean_summary(t)\n"
              "    label, ev = classify(summary)\n"
              "    return label\n"
              "def classify(t):\n    return t\n")

    def test_the_35th_defect_is_caught(self):
        """절단이 **호출된 함수 안**에 있어도 잡는다 — 그게 실제 결함의 모양이다."""
        srcs = self._repo()
        srcs["_eum_request.py"] = self.DEFECT
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("classify", msg)
        self.assertIn("summary", msg)

    def test_inline_call_is_caught(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = self.DEFECT.replace(
            "    summary = clean_summary(t)\n    label, ev = classify(summary)\n",
            "    label, ev = classify(clean_summary(t))\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("classify", msg)

    def test_untruncated_helper_result_passes(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = self.DEFECT.replace(
            "    summary = clean_summary(t)\n    label, ev = classify(summary)\n",
            "    label, ev = classify(clean_text(t))\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_defect_inside_a_truncating_helper_is_also_caught(self):
        """자르는 함수 **안**에서 판정해도 같은 결함이다 — 검사에서 빼지 않는다."""
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "SUMMARY_MAX = 500\n"
            "def clean_summary(t):\n"
            "    s = scrub(t)[:SUMMARY_MAX]\n"
            "    classify(s)\n"
            "    return scrub(t)[:SUMMARY_MAX]\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("classify", msg)

    def test_main_reports_this_check_not_a_stub(self):
        """배선이 끊겨도(상수 통과) 단계 이름은 찍힌다 — 메시지까지 맞춰 둔다."""
        import io as _io
        import contextlib
        _ok, msg = verify.check_judge_inputs(verify.api_sources())
        buf = _io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py"]
        try:
            with contextlib.redirect_stdout(buf):
                verify.main()
        finally:
            sys.argv = argv
        self.assertIn(msg, buf.getvalue())

    def test_direct_slice_is_caught(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "SUMMARY_MAX = 500\n"
            "def build_payload(t):\n"
            "    s = clean_text(t)[:SUMMARY_MAX]\n"
            "    return classify(s)\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("`s`", msg)

    def test_keyword_argument_is_caught(self):
        srcs = self._repo()
        srcs["_escalation.py"] = (
            "def go(t):\n"
            "    cut = t[:200]\n"
            "    return policy.evaluate(text=cut)\n"
            "def evaluate(text=''):\n    return text\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("evaluate", msg)

    def test_untruncated_input_passes(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "SUMMARY_MAX = 500\n"
            "def build_payload(t):\n"
            "    full = clean_text(t)\n"
            "    summary = full[:SUMMARY_MAX]\n"
            "    return classify(full), summary\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_lower_bound_slice_is_not_truncation(self):
        """`x[2:]`·`x[0]` 은 윗쪽을 자르지 않는다 — 과차단하면 게이트가 꺼진다."""
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "def build_payload(t):\n"
            "    tail = t[2:]\n"
            "    first = t[0]\n"
            "    return classify(tail), first\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_unregistered_function_is_out_of_scope(self):
        """등록부에 적은 판정 함수만 본다 — 자동 수집하면 과차단된다."""
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "def build_payload(t):\n"
            "    s = t[:500]\n"
            "    return shorten(s)\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_other_file_with_same_function_name_is_out_of_scope(self):
        srcs = self._repo()
        srcs["newthing.py"] = (
            "def go(t):\n"
            "    s = t[:500]\n"
            "    return classify(s)\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_truncation_in_another_function_is_not_borrowed(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = (
            "def cut(t):\n"
            "    s = t[:500]\n"
            "    return s\n"
            "def build_payload(t):\n"
            "    s = clean_text(t)\n"
            "    return classify(s)\n"
            "def classify(t):\n    return t\n")
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_missing_judge_function_is_registry_drift(self):
        srcs = self._repo()
        srcs["_eum_request.py"] = "X = 1\n"
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)

    def test_gate_reads_sources_without_executing_them(self):
        srcs = self._repo()
        srcs["newthing.py"] = "raise SystemExit('실행되면 안 된다')\nX = 1\n"
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertTrue(ok, msg)

    def test_syntax_error_is_left_to_py_compile(self):
        srcs = self._repo()
        srcs["wellbeing.py"] = "def broken(:\n"
        ok, msg = verify.check_judge_inputs(srcs)
        self.assertFalse(ok)          # 파싱 못 한 파일은 등록부 잔재로 드러난다
        self.assertIn("잔재", msg)

    def test_repository_passes_today(self):
        ok, msg = verify.check_judge_inputs(self._repo())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.JUDGE_INPUTS), msg)

    def test_registry_matches_reality(self):
        srcs = self._repo()
        for (fname, func), note in verify.JUDGE_INPUTS.items():
            self.assertIn(fname, srcs, "등록부가 없는 파일을 가리킨다: %s" % fname)
            self.assertIn("def %s(" % func, srcs[fname], "%s 에 %s 가 없다" % (fname, func))
            self.assertTrue(note.strip(), "사유가 비어 있다: %s" % func)


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
                     "spoken_rules", "mem_caps", "history_caps", "speech_rules",
                     "keyword_tables", "judge_inputs"):
            self.assertIn(step, out)
        self.assertNotIn("FAIL", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
