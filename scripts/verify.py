#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""릴리스 게이트 — 로컬과 CI가 **같은 기준**으로 검사한다.

게이트가 로컬과 CI에서 다르면 "내 컴퓨터에서는 됐는데"가 반복된다.
이 스크립트 하나만 통과하면 배포 가능한 상태라는 뜻이 되도록 유지한다.

검사:
  1. py_compile   — api/*.py, scripts/*.py 문법
  2. html_parse   — public/*.html 이 끝까지 파싱되는가(라이브로 나가는 파일)
  3. dup_id       — 같은 문서 안 중복 id (getElementById 가 조용히 틀린 요소를 잡는다)
  4. banned_words — 허위 도입사례·타사명 잔재 (§13-1)
  5. welfare_terms— 복지 사업 잔재 표기 (§13-5, B2B 브랜드와 충돌)

검사 범위: 게이트는 **고객에게 도달하는 것**만 막는다. 내부 운영 문서까지 막으면
사람이 게이트를 끄게 되고, 그러면 게이트가 없는 것과 같다.

사용: python3 scripts/verify.py [--json]
종료코드 0=통과, 1=실패.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 허위 도입사례·후기로 읽힐 수 있는 실제 기업/제품명. 발견되면 실패.
BANNED = ["농협", "라피치", "IBK", "날리지큐브", "보이스봇", "신세계", "하나은행"]
# 복지 사업 잔재 — B2B 브랜드(AICC Portal)와 맞지 않는다.
WELFARE = ["이음", "광산구", "3세대", "상생"]
# 문안상 정당한 사용까지 잡지 않도록, 검사 대상은 라이브 페이지로 한정한다.
HTML_DIR = os.path.join(REPO, "public")


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)


def _pys():
    out = []
    for d in ("api", "scripts"):
        p = os.path.join(REPO, d)
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.endswith(".py")]
    return out


def check_py_compile():
    files = _pys()
    if not files:
        return False, "검사할 .py 없음"
    p = subprocess.run([sys.executable, "-m", "py_compile"] + files,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = p.returncode == 0
    return ok, ("%d개 통과" % len(files) if ok
                else p.stdout.decode("utf-8", "replace")[-500:])


def _html_files():
    if not os.path.isdir(HTML_DIR):
        return []
    return [os.path.join(HTML_DIR, f) for f in sorted(os.listdir(HTML_DIR))
            if f.endswith(".html")]


def check_html():
    """파싱 + 중복 id 를 한 번의 읽기로 검사한다."""
    bad, dups, n = [], [], 0
    for path in _html_files():
        name = os.path.basename(path)
        try:
            text = io.open(path, encoding="utf-8").read()
            p = _Ids()
            p.feed(text)
            p.close()
            n += 1
        except Exception as e:                    # noqa: BLE001
            bad.append("%s(%s)" % (name, type(e).__name__))
            continue
        seen, dup = set(), set()
        for i in p.ids:
            (dup if i in seen else seen).add(i)
        if dup:
            dups.append("%s: %s" % (name, ", ".join(sorted(dup)[:5])))
    return (not bad, "%d개 파싱" % n if not bad else "실패: " + ", ".join(bad)), \
           (not dups, "중복 없음" if not dups else " / ".join(dups))


def _docs():
    """공개 문서 = 루트 .md 중 내부 로그(`_` 접두)를 뺀 것.
    `_night-auto-status.md` 같은 작업 로그는 배포물이 아니고, 오히려 금지어
    목록 자체를 인용하므로 검사하면 항상 실패한다."""
    return [os.path.join(REPO, f) for f in sorted(os.listdir(REPO))
            if f.endswith(".md") and not f.startswith("_")]


def _scan(words, label, targets):
    hits = []
    for path in targets:
        try:
            text = io.open(path, encoding="utf-8").read()
        except Exception:                          # noqa: BLE001
            continue
        found = [w for w in words if w in text]
        if found:
            hits.append("%s: %s" % (os.path.basename(path), ",".join(found)))
    return not hits, ("%s 없음(%d개 파일)" % (label, len(targets)) if not hits
                      else " / ".join(hits))


def main():
    ap = argparse.ArgumentParser(description="릴리스 게이트")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    (h_ok, h_msg), (d_ok, d_msg) = check_html()
    p_ok, p_msg = check_py_compile()
    # 허위 사례는 문서로 새어도 문제가 되므로 공개 문서까지 본다.
    b_ok, b_msg = _scan(BANNED, "금지어", _html_files() + _docs())
    # 복지 표기는 '제품 화면의 브랜드 일관성' 문제다. 내부 운영 문서가 형제
    # 프로젝트(이음)를 이름으로 언급하는 것은 정상이므로 화면만 검사한다.
    w_ok, w_msg = _scan(WELFARE, "복지 잔재", _html_files())

    steps = [
        {"step": "py_compile", "ok": p_ok, "detail": p_msg},
        {"step": "html_parse", "ok": h_ok, "detail": h_msg},
        {"step": "dup_id", "ok": d_ok, "detail": d_msg},
        {"step": "banned_words", "ok": b_ok, "detail": b_msg},
        {"step": "welfare_terms", "ok": w_ok, "detail": w_msg},
    ]
    ok = all(s["ok"] for s in steps)
    if a.json:
        print(json.dumps({"ok": ok, "steps": steps}, ensure_ascii=False, indent=2))
    else:
        for s in steps:
            print("%s %-14s %s" % ("PASS" if s["ok"] else "FAIL",
                                   s["step"], s["detail"]))
        print("---")
        print("통과" if ok else "실패")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
