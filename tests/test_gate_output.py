# -*- coding: utf-8 -*-
"""릴리스 게이트 출력 인코딩 회귀 (38차) — 네트워크 미사용.

왜 이 파일이 필요한가
  `scripts/verify.py --json` 은 **기계가 읽는 출구**다 — CI 가 게이트 결과를
  파싱하고, 37차가 추가한 `tests/test_live_gates.py` 도 이 경로로 단계 목록을
  확인한다. 그런데 출력을 `print()` 로 내보내고 있었다 — Windows 콘솔
  인코딩(cp949)으로 나간다. 한글 `detail` 이 cp949 바이트로 나가고, UTF-8 로
  읽는 소비자는 `UnicodeDecodeError` 로 **게이트 결과를 아예 읽지 못했다**
  (37차가 남긴 회귀 1건이 그래서 빨간 상태로 있었다).

  게이트가 틀린 답을 내는 것보다 **아무 답도 못 내는 것**이 나쁘다 — 통과했는지
  실패했는지 모르면 사람은 게이트를 건너뛴다. JSON 은 RFC 8259 가 UTF-8 로
  못박았으므로 콘솔 코드페이지와 무관하게 UTF-8 로 쓴다.

  사람용 출력은 콘솔 인코딩을 그대로 쓴다(cp949 콘솔에서 한글이 보여야 한다).
  다만 표현 못 하는 글자가 섞여도 트레이스백으로 죽지 않는다.

실행: python3 -m pytest tests/test_gate_output.py -q
"""
import io
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verify  # noqa: E402


class _FakeConsole:
    """콘솔을 흉내낸 stdout — `.buffer`(바이트)와 `.encoding` 을 갖는다."""

    def __init__(self, encoding):
        self.buffer = io.BytesIO()
        self.encoding = encoding

    def write(self, s):                    # 사람 손으로 쓰는 경로(폴백 검사용)
        self.buffer.write(s.encode(self.encoding, "backslashreplace"))

    def flush(self):
        pass


def _fake_stdout(encoding):
    con = _FakeConsole(encoding)
    return con.buffer, con


class TestJsonIsAlwaysUtf8(unittest.TestCase):
    def test_subprocess_json_parses_as_utf8(self):
        """이번 결함 그대로 — cp949 콘솔에서 돌려도 UTF-8 로 읽힌다."""
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "verify.py"),
                              "--json"], capture_output=True, cwd=ROOT)
        self.assertIn(out.returncode, (0, 1), out.stderr[:400])
        data = json.loads(out.stdout.decode("utf-8"))   # 바이트를 직접 UTF-8 로
        self.assertIn("ok", data)
        self.assertTrue(data["steps"])
        detail = " ".join(s["detail"] for s in data["steps"])
        self.assertRegex(detail, "[가-힣]")              # 한글이 살아 있다
        self.assertNotIn("�", detail)               # 깨진 글자가 없다

    def test_emit_json_ignores_console_codepage(self):
        raw, txt = _fake_stdout("cp949")
        old = sys.stdout
        sys.stdout = txt
        try:
            verify._emit('{"detail": "통과"}', as_json=True)
        finally:
            sys.stdout = old
        self.assertEqual(raw.getvalue().decode("utf-8"), '{"detail": "통과"}\n')

    def test_emit_human_uses_console_codepage(self):
        """사람용 출력은 콘솔 인코딩 그대로 — cp949 콘솔에서 한글이 보인다."""
        raw, txt = _fake_stdout("cp949")
        old = sys.stdout
        sys.stdout = txt
        try:
            verify._emit("PASS 통과")
        finally:
            sys.stdout = old
        self.assertEqual(raw.getvalue().decode("cp949"), "PASS 통과\n")

    def test_emit_survives_unencodable_character(self):
        """표현 못 하는 글자에도 게이트가 죽지 않는다(침묵보다 깨진 글자)."""
        raw, txt = _fake_stdout("ascii")
        old = sys.stdout
        sys.stdout = txt
        try:
            verify._emit("PASS 통과")           # 예외가 나면 실패
        finally:
            sys.stdout = old
        self.assertIn(b"PASS", raw.getvalue())


class TestCapturedStdoutStillWorks(unittest.TestCase):
    """`redirect_stdout(StringIO)` 로 가로채는 기존 회귀가 깨지지 않는다."""

    def test_emit_falls_back_without_buffer(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            verify._emit("통과")
            verify._emit('{"ok": true}', as_json=True)
        finally:
            sys.stdout = old
        self.assertEqual(buf.getvalue(), '통과\n{"ok": true}\n')

    def test_main_human_output_is_capturable(self):
        import contextlib
        buf = io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py"]
        try:
            with contextlib.redirect_stdout(buf):
                rc = verify.main()
        finally:
            sys.argv = argv
        text = buf.getvalue()
        self.assertIn(rc, (0, 1))
        self.assertIn("---", text)
        self.assertIn("py_compile", text)

    def test_main_json_output_is_capturable(self):
        import contextlib
        buf = io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py", "--json"]
        try:
            with contextlib.redirect_stdout(buf):
                verify.main()
        finally:
            sys.argv = argv
        data = json.loads(buf.getvalue())
        self.assertEqual([s["step"] for s in data["steps"]][0], "py_compile")


if __name__ == "__main__":
    unittest.main()
