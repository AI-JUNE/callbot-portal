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
  9. audit_actions— 감사를 배선한 라우트가 `_audit.ADMIN_ACTIONS` 등록부에 있는가
 10. spoken_numbers— 고객이 듣게 되는 수치(프롬프트 금액·데모 툴 결과)가 박혀 있지 않은가

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
import re
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


# --------------------------------------------------------------------------
# audit_actions 게이트
#
# 27차에서 드러난 결함: `caller_id`·`partners`·`settlement`·`wellbeing` 은 감사
# 기록을 allow/deny/error 세 자리에 모두 배선했는데 `_audit.ADMIN_ACTIONS` 에
# 경로가 없어 **아무것도 남지 않았다**(`action_for` 가 None 이면 record_request 는
# 조용히 끝난다). 회귀는 가짜 감사 모듈을 끼워 '호출됐는가'만 봤으므로 침묵했다.
# 그래서 게이트가 센다 — 배선과 등록부가 **양방향으로** 맞아야 통과한다.
# --------------------------------------------------------------------------
AUDIT_CALLS = ("_audit.record(", "_audit.record_request(")


def _code(text):
    """줄 주석을 뺀 코드만 — 주석 속 언급은 배선이 아니다."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def _audit_actions_registry():
    """api/_audit.py 의 ADMIN_ACTIONS 를 **실행 없이** 읽는다(ast 리터럴)."""
    import ast
    path = os.path.join(API_DIR, "_audit.py")
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except Exception:                          # noqa: BLE001
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "ADMIN_ACTIONS":
                    try:
                        return dict(ast.literal_eval(node.value))
                    except Exception:          # noqa: BLE001
                        return None
    return None


def check_audit_actions(sources):
    reg = _audit_actions_registry()
    if not reg:
        return False, "api/_audit.py 의 ADMIN_ACTIONS 등록부를 읽을 수 없음"
    wired, missing = set(), []
    for name, text in sorted(sources.items()):
        if name.startswith("_") or "class handler" not in text:
            continue                           # 공용 모듈은 라우트가 아니다
        code = _code(text)
        if not any(c in code for c in AUDIT_CALLS):
            continue                           # 감사를 배선하지 않은 라우트
        route = "/api/" + name[:-3]
        wired.add(route)
        if route not in reg:
            missing.append(route)
    stale = sorted(set(reg) - wired)
    msgs = []
    if missing:
        msgs.append("감사 기록이 남지 않는 라우트: %s "
                    "(api/_audit.py ADMIN_ACTIONS 에 액션명을 적을 것)"
                    % ", ".join(sorted(missing)))
    if stale:
        msgs.append("등록부 잔재(배선 없음): %s" % ", ".join(stale))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "등록 %d개 · 배선 %d개 일치" % (len(reg), len(wired)))


# --------------------------------------------------------------------------
# spoken_numbers 게이트
#
# 고객이 **귀로 듣는 수치**는 두 곳에서 나온다.
#   (1) 시나리오 프롬프트(`PROMPT_*`) — 봇이 그대로 읽는 문장
#   (2) 데모 주문 백엔드의 툴 결과 — LLM 컨텍스트로 들어가 그대로 인용되고
#       `/api/chat` 의 `log[].out` 으로 화면까지 나간다
# 어느 쪽이든 숫자를 박아 두면 **모든 통화에서 같은 거짓이 반복**되고, 화면의
# 가짜 KPI 와 달리 「샘플 데이터」 워터마크를 붙일 자리가 없다(§8 가짜 수치 금지).
# 27차 `queue_position: 2`, 28차 `eta_days: 3`·`eta_minutes: 25`·대본의
# 「미납금 37,000원」이 전부 **한 줄짜리 리터럴**이었다 — 리뷰에서 눈에 띄지 않는다.
# 그래서 게이트가 센다.
#
# 검사 범위는 일부러 좁혔다(과차단되면 사람이 게이트를 끈다):
#   · 프롬프트는 **금액 표기**만 본다(`…원`·`%`). 통화 길이('약 2분')처럼 우리가
#     통제하는 수치나 '생년월일 6자리' 같은 자릿수 안내는 고객 돈에 대한 거짓이 아니다.
#   · 데모 백엔드는 **메서드 return 의 dict 리터럴**만 본다. 데모 주문(DEMO_ORDER)의
#     금액은 시연용 주문 '장부'이고 정책 한도·견적이 그 값에서 계산돼 나오므로 서로
#     어긋나지 않는다 — 장부와 무관하게 메서드에 박힌 숫자가 거짓이 되는 쪽이다.
# 수치를 '모른다'로 남기는 방법은 `queue_position`·`eta_days` 를 보면 된다
# (칸을 지우지 않고 null + `*_known: false` + 사유 — 빈자리는 모델이 채운다).
# --------------------------------------------------------------------------
SPOKEN_PROMPT_PREFIX = "PROMPT_"
DEMO_BACKEND_FILE = "_order_backend.py"
DEMO_BACKEND_CLASS = "DemoOrderBackend"
RX_MONEY = re.compile(r"\d[\d,]*\s*(?:원|%|퍼센트)")


def _prompt_money(text):
    """(검사한 프롬프트 수, [(상수명, 걸린 표기)]) — 실행 없이 ast 로 읽는다."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return 0, []                           # 문법 오류는 py_compile 게이트가 잡는다
    seen, hits = 0, []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets
                 if isinstance(t, ast.Name) and t.id.startswith(SPOKEN_PROMPT_PREFIX)]
        if not names:
            continue
        seen += 1
        for s in ast.walk(node.value):
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                m = RX_MONEY.search(s.value)
                if m:
                    hits.append((names[0], m.group(0).strip()))
                    break
    return seen, hits


def _dict_pairs(node):
    """return 값 안의 모든 dict 항목 (키, 값노드). 중첩 dict·리스트도 포함."""
    import ast
    out = []
    for d in ast.walk(node):
        if not isinstance(d, ast.Dict):
            continue
        for k, v in zip(d.keys, d.values):
            key = (k.value if isinstance(k, ast.Constant) and isinstance(k.value, str)
                   else "?")
            out.append((key, v))
    return out


def _demo_tool_numbers(text):
    """(검사한 메서드 수, [걸린 칸]) — 데모 백엔드 return 에 박힌 숫자.
    클래스를 찾지 못하면 None(등록부 잔재와 같은 취급: 통과시키지 않는다)."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return None
    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == DEMO_BACKEND_CLASS:
            cls = node
            break
    if cls is None:
        return None
    n, hits = 0, []
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        n += 1
        for r in ast.walk(fn):
            if not isinstance(r, ast.Return) or r.value is None:
                continue
            for key, val in _dict_pairs(r.value):
                if (isinstance(val, ast.Constant)
                        and isinstance(val.value, (int, float))
                        and not isinstance(val.value, bool)):
                    # bool 은 수치가 아니라 사실(transferred/known)이다.
                    hits.append("%s:%d %s=%r" % (DEMO_BACKEND_FILE, val.lineno,
                                                 key, val.value))
    return n, hits


def check_spoken_numbers(sources):
    prompts, msgs = 0, []
    for name, text in sorted(sources.items()):
        seen, hits = _prompt_money(text)
        prompts += seen
        for const, tok in hits:
            msgs.append("프롬프트에 박힌 금액: %s 의 %s (%s) — 조회한 값만 말하게 "
                        "하고, 모르면 '확인 후 안내'로 둘 것" % (name, const, tok))
    if not prompts:
        return False, "시나리오 프롬프트(PROMPT_*)를 찾을 수 없음"
    demo = _demo_tool_numbers(sources.get(DEMO_BACKEND_FILE, ""))
    if demo is None:
        return False, "%s 의 %s 를 읽을 수 없음" % (DEMO_BACKEND_FILE, DEMO_BACKEND_CLASS)
    tools, hits = demo
    if hits:
        msgs.append("데모 툴 결과에 박힌 수치: %s — 장부에서 계산하거나 null + "
                    "`*_known: false` + 사유로 둘 것" % ", ".join(hits[:8]))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "프롬프트 %d개 · 데모 툴 %d개 검사" % (prompts, tools))


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
    a_ok, a_msg = check_audit_actions(srcs)
    n_ok, n_msg = check_spoken_numbers(srcs)

    steps = [
        {"step": "py_compile", "ok": p_ok, "detail": p_msg},
        {"step": "html_parse", "ok": h_ok, "detail": h_msg},
        {"step": "dup_id", "ok": d_ok, "detail": d_msg},
        {"step": "banned_words", "ok": b_ok, "detail": b_msg},
        {"step": "welfare_terms", "ok": w_ok, "detail": w_msg},
        {"step": "outbound", "ok": o_ok, "detail": o_msg},
        {"step": "request_log", "ok": l_ok, "detail": l_msg},
        {"step": "log_fields", "ok": f_ok, "detail": f_msg},
        {"step": "audit_actions", "ok": a_ok, "detail": a_msg},
        {"step": "spoken_numbers", "ok": n_ok, "detail": n_msg},
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
