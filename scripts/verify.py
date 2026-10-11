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
  6. outbound     — 외부로 나가는 호출이 등록부에 있는가 + 요청 유래 URL 은 가드 경유
  7. request_log  — 모든 라우트가 요청 1건당 구조화 로그 1줄을 남기는가
  8. log_fields   — 로그 보조 필드(extra) 이름이 `_log.FIELDS` 등록부에 있는가

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
API_DIR = os.path.join(REPO, "api")

# --------------------------------------------------------------------------
# 아웃바운드 호출 등록부 — "이 서버가 어디로 나가는가"를 목록으로 관리한다.
#
# 20차에서 통화 웹훅이 넘겨준 녹음 URL 을 검증 없이 그대로 열고 있었다(사설망·
# 클라우드 메타데이터 열람 가능). 그런 코드는 리뷰에서 눈에 띄지 않는다 —
# 한 줄 추가로 끝나기 때문이다. 그래서 게이트가 센다: api 의 어떤 파일이든
# 새로 외부 호출을 추가하면, 사유를 여기에 적기 전에는 통과하지 못한다.
# 값은 "어디로·URL 이 어디서 오는가"를 한 줄로 적는다.
# --------------------------------------------------------------------------
OUTBOUND = {
    "_engine.py":        "Gemini 생성 API — URL 고정 상수(모델명만 환경변수)",
    "_stt.py":           "Gemini 전사 API — URL 고정 상수",
    "_vstudio.py":       "보이스 스튜디오 엔진 — VOICE_ENGINE_URL(환경변수, _urlguard 필수)",
    "_monitoring.py":    "Sentry envelope — SENTRY_DSN(환경변수, _urlguard 필수)",
    "_order_backend.py": "주문 백엔드 — ORDER_API_BASE(환경변수, _urlguard 필수)",
    "health.py":         "deep 점검 TCP 도달성 — HEALTH_DEEP=1 일 때만",
    "voice.py":          "녹음 다운로드 — 요청 본문의 URL(_urlguard 필수)",
    "wellbeing.py":      "안부 결과 웹훅 — 요청 본문의 URL(_urlguard 필수)",
}
# 주소가 **고정 상수가 아닌** 파일. 공용 가드(api/_urlguard.py)를 반드시 거친다.
# 요청 본문에서 오는 것(voice·wellbeing)과 설정에서 오는 것(_order_backend·_vstudio·
# _monitoring)을 같이 둔다 — 환경변수라고 검증을 빼면 오타·잘못 복사한 값으로
# 주문 정보와 Bearer 키, 합성할 발화와 HMAC 서명, 오류 봉투와 DSN 공개키가
# 평문 http·사설망·클라우드 메타데이터로 나간다(쓰기 승인 뒤에는 환불 접수가 엉뚱한 주소로 간다).
URLGUARD_REQUIRED = {"voice.py", "wellbeing.py", "_order_backend.py",
                     "_vstudio.py", "_monitoring.py"}
OUTBOUND_CALLS = ("urlopen(", "socket.create_connection(")


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


def api_sources():
    """{파일명: 소스} — api/*.py 만. 읽기 실패는 조용히 넘기지 않고 비운다."""
    out = {}
    if not os.path.isdir(API_DIR):
        return out
    for f in sorted(os.listdir(API_DIR)):
        if not f.endswith(".py"):
            continue
        try:
            out[f] = io.open(os.path.join(API_DIR, f), encoding="utf-8").read()
        except Exception as e:                     # noqa: BLE001
            out[f] = "# UNREADABLE %s" % type(e).__name__
    return out


def _calls_out(text):
    """줄 주석을 뺀 코드에서 외부 호출이 보이는가."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue                               # 주석 속 언급은 호출이 아니다
        if any(c in s for c in OUTBOUND_CALLS):
            return True
    return False


def check_outbound(sources):
    """등록부와 실제가 일치하는가 + 요청 유래 URL 은 가드를 거치는가."""
    unregistered, no_guard, stale = [], [], []
    for name, text in sorted(sources.items()):
        out = _calls_out(text)
        if out and name not in OUTBOUND:
            unregistered.append(name)
        elif (not out) and name in OUTBOUND:
            stale.append(name)                     # 등록부가 현실과 다르면 등록부가 아니다
        if out and name in URLGUARD_REQUIRED and "_urlguard" not in text:
            no_guard.append(name)
    msgs = []
    if unregistered:
        msgs.append("미등록 외부 호출: %s (scripts/verify.py OUTBOUND 에 사유를 적을 것)"
                    % ", ".join(unregistered))
    if no_guard:
        msgs.append("가드 미경유: %s (_urlguard 로 검증할 것)" % ", ".join(no_guard))
    if stale:
        msgs.append("등록부 잔재(호출 없음): %s" % ", ".join(stale))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "등록 %d개 · 요청 유래 %d개 가드 경유"
                             % (len(OUTBOUND), len(URLGUARD_REQUIRED)))


def check_request_log(sources):
    """라우트(`_` 없는 api/*.py)는 요청 1건당 구조화 로그 1줄을 남긴다.

    로그가 없는 라우트는 장애가 나도 되짚을 기록이 없다 — 거부·오류가 흔적 없이
    사라진다. `_` 로 시작하는 파일은 공용 모듈(함수로 배포되지 않는다)이라 제외.
    """
    bad, n = [], 0
    for name, text in sorted(sources.items()):
        if name.startswith("_") or "class handler" not in text:
            continue
        n += 1
        if "_log.begin(" not in text:
            bad.append(name)
    if not n:
        return False, "검사할 라우트 없음"
    return (not bad), ("%d개 라우트 배선" % n if not bad
                       else "로그 미배선: %s" % ", ".join(bad))


# --------------------------------------------------------------------------
# log_fields 게이트
#
# 구조화 로그의 보조 필드(`rq.set(op=...)`·`rq.finish(code, kind=...)`)는 이름
# 규약이 없으면 라우트마다 제각각이 된다(같은 '건수'를 lines·turns·msg_count 로
# 적던 상태). 더 위험한 쪽은 **임의 칸이 한 줄로 늘어나는 것**이다 —
# 집계 카디널리티가 터지고 PII 유입 경로가 생긴다. 등록부는 api/_log.py 의
# FIELDS 고, 여기서는 그 밖의 칸을 쓰는 코드를 실패시킨다.
#
# 로그 객체를 정적으로 특정할 수는 없으므로 호출 모양으로 좁힌다:
#   rq.set(...) / rq.finish(...) / rq.fail(...) / self._rq.* / _close(rq, ...)
# `code`·`deep` 은 함수 자신의 인자라 보조 필드가 아니다.
# --------------------------------------------------------------------------
LOG_METHODS = ("set", "finish", "fail")
LOG_RECEIVERS = ("rq", "_rq")
LOG_CONTROL_KW = {"code", "deep"}


def _log_fields_registry():
    """api/_log.py 의 FIELDS 를 **실행 없이** 읽는다(ast 리터럴)."""
    import ast
    path = os.path.join(API_DIR, "_log.py")
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except Exception:                          # noqa: BLE001
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "FIELDS":
                    try:
                        return set(ast.literal_eval(node.value))
                    except Exception:          # noqa: BLE001
                        return None
    return None


def _log_kwargs(text):
    """소스에서 (보조 필드 이름, 줄번호) 목록을 뽑는다."""
    import ast
    out = []
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return out                             # 문법 오류는 py_compile 게이트가 잡는다
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        hit = False
        if isinstance(f, ast.Attribute) and f.attr in LOG_METHODS:
            base = f.value
            name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else "")
            hit = name in LOG_RECEIVERS
        elif isinstance(f, ast.Name) and f.id == "_close":
            hit = True
        if not hit:
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in LOG_CONTROL_KW:
                out.append((kw.arg, node.lineno))
    return out


def check_log_fields(sources):
    reg = _log_fields_registry()
    if not reg:
        return False, "api/_log.py 의 FIELDS 등록부를 읽을 수 없음"
    bad, n = [], 0
    for name, text in sorted(sources.items()):
        if name == "_log.py":
            continue                           # 등록부 본인
        for field, line in _log_kwargs(text):
            n += 1
            if field not in reg:
                bad.append("%s:%d %s" % (name, line, field))
    if bad:
        return False, ("미등록 로그 보조 필드: %s (api/_log.py FIELDS 에 사유를 "
                       "적을 것)" % ", ".join(bad[:8]))
    return True, "등록 %d개 · 사용 %d곳" % (len(reg), n)


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
    srcs = api_sources()
    o_ok, o_msg = check_outbound(srcs)
    l_ok, l_msg = check_request_log(srcs)
    f_ok, f_msg = check_log_fields(srcs)

    steps = [
        {"step": "py_compile", "ok": p_ok, "detail": p_msg},
        {"step": "html_parse", "ok": h_ok, "detail": h_msg},
        {"step": "dup_id", "ok": d_ok, "detail": d_msg},
        {"step": "banned_words", "ok": b_ok, "detail": b_msg},
        {"step": "welfare_terms", "ok": w_ok, "detail": w_msg},
        {"step": "outbound", "ok": o_ok, "detail": o_msg},
        {"step": "request_log", "ok": l_ok, "detail": l_msg},
        {"step": "log_fields", "ok": f_ok, "detail": f_msg},
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
