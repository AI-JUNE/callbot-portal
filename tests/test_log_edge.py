# -*- coding: utf-8 -*-
"""api/_log.py 잔여 방어 분기 + 커버리지 표기 규약 회귀 — 의존성 0, 네트워크 미사용.

`tests/test_logging.py` 가 로그 1줄의 모양·PII 미기록·배선을 덮고,
`tests/test_request_log_wiring.py` 가 핸들러별 결선을 덮는다. 남은 것은
**로깅 자체가 고장났을 때만 도는 코드**다. 로깅은 장애 조사의 마지막 수단이라,
여기서 예외가 새면 요청이 죽거나(가용성) 흔적이 사라진다(추적 불가).

검증 대상
  1) 자립 — `api/` 가 sys.path 에 없어도 스스로 넣는다
  2) 모니터링 모듈 부재 — scrub 폴백으로도 로그가 나가고 값이 문자열로 정리된다
  3) 흡수 — 헤더 저장소·scrub·요청ID 생성이 터져도 호출자는 예외를 보지 않는다
  4) 커버리지 표기 규약 — `api/` 의 모든 `__main__` 셀프테스트 블록은
     `# pragma: no cover` 를 달아야 한다. 안 달면 커버리지 숫자가 '실행되지 않는
     방어 분기'와 '애초에 실행될 일 없는 셀프테스트'를 섞어 보여줘,
     남은 미커버가 어디인지 가리키지 못한다.

실행: python3 -m pytest tests/test_log_edge.py -q
"""
import glob
import importlib
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api")
sys.path.insert(0, API)

import _log  # noqa: E402


class Patcher(unittest.TestCase):
    def setUp(self):
        self._restore = []

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def capture(self):
        """stdout 을 가로채 emit 된 JSON 줄을 모은다."""
        buf = io.StringIO()
        self.patch(sys, "stdout", buf)
        return buf

    @staticmethod
    def lines(buf):
        return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


class LogOn(Patcher):
    """CALLBOT_LOG 를 켠 상태(테스트 환경은 보통 off)."""

    def setUp(self):
        super().setUp()
        self._saved_log = os.environ.get("CALLBOT_LOG")
        os.environ["CALLBOT_LOG"] = "on"

    def tearDown(self):
        if self._saved_log is None:
            os.environ.pop("CALLBOT_LOG", None)
        else:
            os.environ["CALLBOT_LOG"] = self._saved_log
        super().tearDown()


# ==========================================================================
# 1~2) 자립 + 모니터링 모듈 부재 폴백
# ==========================================================================
def reexec(mod):
    """모듈 본문을 같은 네임스페이스에서 다시 실행한다.

    importlib.reload 은 spec 을 sys.path 에서 다시 찾기 때문에, 바로 그 sys.path 를
    비워 두고 부트스트랩을 검증하려는 이 테스트에서는 쓸 수 없다.
    """
    spec = importlib.util.spec_from_file_location(
        mod.__name__, os.path.join(API, os.path.basename(mod.__file__)))
    spec.loader.exec_module(mod)


class TestBootstrapAndScrubFallback(LogOn):
    """`api/` 가 경로에 없고 _monitoring 도 못 불러오는 상태로 모듈을 다시 올린다."""

    def setUp(self):
        super().setUp()
        self._saved_path = list(sys.path)
        self._saved_mon = sys.modules.get("_monitoring")
        # 네임스페이스 통째로 보관한다. 다시 실행하면 함수 객체가 새로 만들어지는데,
        # 핸들러들은 import 시점에 `log_message = _log.suppress_access_log` 로
        # **그 객체를** 붙여 뒀다 — 이름만 맞춰 놓으면 동일성 회귀가 깨진다.
        self._saved_ns = dict(_log.__dict__)

        class Broken(object):
            def __getattr__(self, _n):
                raise RuntimeError("내부경로 /var/task/api 노출 금지")

        sys.modules["_monitoring"] = Broken()
        sys.path[:] = [p for p in sys.path if os.path.abspath(p) != API]
        reexec(_log)

    def tearDown(self):
        sys.path[:] = self._saved_path
        if self._saved_mon is None:
            sys.modules.pop("_monitoring", None)
        else:
            sys.modules["_monitoring"] = self._saved_mon
        _log.__dict__.clear()
        _log.__dict__.update(self._saved_ns)
        super().tearDown()
        # 복구 확인 — 이 파일이 다른 테스트의 로깅을 망가뜨리지 않는다
        import _monitoring
        self.assertIs(_log._scrub, _monitoring.scrub)
        self.assertIs(_log.suppress_access_log, self._saved_ns["suppress_access_log"])

    def test_module_puts_its_own_directory_on_sys_path(self):
        self.assertIn(API, [os.path.abspath(p) for p in sys.path])

    def test_scrub_fallback_is_installed(self):
        self.assertEqual(_log._scrub("abc"), "abc")
        self.assertEqual(_log._scrub(12), "12")      # 비문자열도 문자열로

    def test_logging_still_emits_one_line(self):
        buf = self.capture()
        _log.begin(None, "/api/chat", "POST", path="/api/chat?phone=010").finish(200)
        rec = self.lines(buf)[0]
        self.assertEqual(rec["status"], 200)
        self.assertEqual(rec["path"], "/api/chat")   # 쿼리는 폴백에서도 잘린다
        self.assertEqual(rec["level"], "info")


# ==========================================================================
# 3) 흡수 — 로깅이 터져도 호출자는 예외를 보지 않는다
# ==========================================================================
class TestAbsorbs(LogOn):

    def test_broken_header_store_still_yields_a_request_id(self):
        class Hostile(object):
            def get(self, *a, **kw):
                raise RuntimeError("헤더 저장소 고장")

        rid = _log.new_request_id(Hostile())
        self.assertRegex(rid, r"^[0-9a-f]{16}$")

    def test_broken_scrub_does_not_break_safe_path(self):
        self.patch(_log, "_scrub", lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(_log.safe_path("/api/tts?text=비밀"), "-")

    def test_broken_scrub_does_not_break_extra_fields(self):
        self.patch(_log, "_scrub", lambda v: (_ for _ in ()).throw(RuntimeError("boom")))
        buf = self.capture()
        rq = _log.begin(None, "/api/voice", "POST")
        rq.set(ev="answered")                 # 흡수 — 보조 필드는 비워 둔다
        rq.finish(200)
        rec = self.lines(buf)[0]
        self.assertEqual(rec["status"], 200)
        self.assertNotIn("extra", rec)

    def test_error_code_of_unprintable_object_is_generic(self):
        class Unprintable(object):
            def __str__(self):
                raise RuntimeError("str() 실패")

        self.assertEqual(_log.error_code(Unprintable()), "ERROR")

    def test_error_code_normalises_acronyms(self):
        self.assertEqual(_log.error_code(OSError()), "OS_ERROR")
        self.assertEqual(_log.error_code(ValueError()), "VALUE_ERROR")

    def test_begin_survives_broken_request_id_generator(self):
        self.patch(_log, "new_request_id",
                   lambda h=None: (_ for _ in ()).throw(RuntimeError("uuid 고장")))
        rq = _log.begin(None, "/api/health", "GET")
        self.assertRegex(rq.request_id, r"^[0-9a-f]{16}$")

    def test_fail_records_message_free_code_at_matching_level(self):
        buf = self.capture()
        _log.begin(None, "/api/chat", "POST").fail(ValueError("010-1234-5678 형식"), status=400)
        rec = self.lines(buf)[0]
        self.assertEqual((rec["level"], rec["status"], rec["error_code"]),
                         ("warn", 400, "VALUE_ERROR"))
        self.assertNotIn("010", json.dumps(rec, ensure_ascii=False))

    def test_emit_never_raises_on_unserialisable_record(self):
        buf = self.capture()
        _log.emit({"ts": object()})           # 직렬화 불가 — 조용히 포기
        self.assertEqual(buf.getvalue(), "")


# ==========================================================================
# 4) 커버리지 표기 규약
# ==========================================================================
class TestCoveragePragmaConvention(unittest.TestCase):

    def test_every_selftest_block_is_excluded_from_coverage(self):
        missing = []
        for path in sorted(glob.glob(os.path.join(API, "*.py"))):
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if line.startswith('if __name__ == "__main__":') \
                            and "pragma: no cover" not in line:
                        missing.append("%s:%d" % (os.path.basename(path), i))
        self.assertEqual(missing, [],
                         "셀프테스트/CLI 블록에 '# pragma: no cover' 누락 — "
                         "커버리지 숫자가 실제 미커버 분기를 가리키지 못한다: %s" % missing)


if __name__ == "__main__":     # pragma: no cover
    unittest.main(verbosity=2)
