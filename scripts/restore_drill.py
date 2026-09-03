#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""복구 리허설(restore drill) — 백업본에서 실제로 되살아나는지 검증한다.

문서만 있는 백업 절차는 검증된 절차가 아니다. 이 스크립트는 저장소를 번들로
백업하고, 그 번들만으로 빈 디렉터리에 복원한 뒤, 복원본이 배포 가능한
상태인지(핵심 파일 존재·파이썬 컴파일·HTML 파싱)까지 확인한다.

안전: 읽기 전용. 원본 저장소를 수정하지 않고, 네트워크를 쓰지 않으며,
작업 산출물은 임시 디렉터리에만 만들고 종료 시 지운다. --keep 로 보존 가능.

사용:
    python3 scripts/restore_drill.py            # 리허설 실행
    python3 scripts/restore_drill.py --json     # 기계 판독용 결과
    python3 scripts/restore_drill.py --keep     # 복원본 남기기(수동 확인용)

종료코드 0=성공, 1=실패.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 복원본에 반드시 있어야 하는 파일 — 없으면 배포가 불가능한 것들.
CRITICAL = [
    "public/index.html",   # 라이브 /
    "public/admin.html",   # 라이브 /admin
    "api/health.py",
    "vercel.json",
    "COMMERCIAL_READINESS.md",
]


def _run(args, cwd=None):
    """서브프로세스 실행. (returncode, stdout+stderr) 반환."""
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


class _Sink(HTMLParser):
    """파싱만 하고 버리는 싱크 — 예외 없이 끝까지 읽히는지가 관심사."""
    def error(self, message):  # py3.9 이하 호환
        raise AssertionError(message)


class Drill:
    def __init__(self):
        self.steps = []
        self.t0 = time.time()

    def step(self, name, ok, detail=""):
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        return ok

    @property
    def ok(self):
        return all(s["ok"] for s in self.steps)

    @property
    def elapsed(self):
        return round(time.time() - self.t0, 1)


def drill(keep=False):
    d = Drill()
    work = tempfile.mkdtemp(prefix="callbot-restore-drill-")
    bundle = os.path.join(work, "callbot-portal.bundle")
    restored = os.path.join(work, "restored")

    try:
        # 1) 백업 — 저장소 전체를 단일 번들 파일로. 원본은 건드리지 않는다.
        rc, out = _run(["git", "bundle", "create", bundle, "--all"], cwd=REPO)
        size = os.path.getsize(bundle) if os.path.exists(bundle) else 0
        if not d.step("backup", rc == 0 and size > 0,
                      "번들 %.1f MiB" % (size / 1048576.0) if size else out[-400:]):
            return d, work

        # 2) 번들 무결성 — 복원 전에 백업본 자체가 성한지 먼저 본다.
        # (git bundle verify 는 저장소 안에서만 동작하므로 원본을 cwd 로 쓴다 — 읽기 전용)
        rc, out = _run(["git", "bundle", "verify", bundle], cwd=REPO)
        if not d.step("verify_bundle", rc == 0, out.splitlines()[-1] if out else ""):
            return d, work

        # 3) 복원 — 번들만으로 빈 디렉터리에 클론(원본 경로 참조 없음).
        rc, out = _run(["git", "clone", "--branch", "main", bundle, restored], cwd=work)
        if not d.step("restore", rc == 0, out[-400:] if rc else "clone 완료"):
            return d, work

        # 4) 커밋 일치 — 복원본 HEAD 가 원본 HEAD 와 같은가.
        _, src_head = _run(["git", "rev-parse", "HEAD"], cwd=REPO)
        _, dst_head = _run(["git", "rev-parse", "HEAD"], cwd=restored)
        d.step("head_match", src_head == dst_head and len(dst_head) == 40,
               "%s == %s" % (src_head[:8], dst_head[:8]))

        # 5) 핵심 파일 존재 — 배포에 필요한 것이 실제로 딸려왔는가.
        missing = [p for p in CRITICAL
                   if not os.path.isfile(os.path.join(restored, p))]
        d.step("critical_files", not missing,
               "누락 없음(%d개 확인)" % len(CRITICAL) if not missing
               else "누락: " + ", ".join(missing))

        # 6) 파이썬 컴파일 — 복원본 API 가 문법적으로 온전한가.
        api = os.path.join(restored, "api")
        pys = sorted(f for f in os.listdir(api) if f.endswith(".py")) \
            if os.path.isdir(api) else []
        rc, out = _run([sys.executable, "-m", "py_compile"]
                       + [os.path.join(api, f) for f in pys]) if pys else (1, "api/*.py 없음")
        d.step("py_compile", rc == 0, "%d개 컴파일" % len(pys) if rc == 0 else out[-400:])

        # 7) HTML 파싱 — 라이브로 나가는 public/*.html 이 끝까지 읽히는가.
        pub = os.path.join(restored, "public")
        htmls = sorted(f for f in os.listdir(pub) if f.endswith(".html")) \
            if os.path.isdir(pub) else []
        bad = []
        for f in htmls:
            try:
                with open(os.path.join(pub, f), encoding="utf-8") as fh:
                    p = _Sink(convert_charrefs=True)
                    p.feed(fh.read())
                    p.close()
            except Exception as e:               # noqa: BLE001 — 어떤 파싱 실패든 기록
                bad.append("%s(%s)" % (f, type(e).__name__))
        d.step("html_parse", htmls and not bad,
               "%d개 파싱" % len(htmls) if not bad else "실패: " + ", ".join(bad))

        return d, work
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="백업·복구 리허설")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--keep", action="store_true", help="복원본 보존")
    a = ap.parse_args()

    d, work = drill(keep=a.keep)
    result = {
        "ok": d.ok,
        "elapsed_sec": d.elapsed,
        "steps": d.steps,
        "workdir": work if a.keep else None,
    }
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for s in d.steps:
            print("%s %-16s %s" % ("PASS" if s["ok"] else "FAIL", s["step"], s["detail"]))
        print("---")
        print("%s / %.1fs / %d단계" % ("성공" if d.ok else "실패", d.elapsed, len(d.steps)))
        if a.keep:
            print("복원본: %s" % work)
    return 0 if d.ok else 1


if __name__ == "__main__":
    sys.exit(main())
