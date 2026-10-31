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
 11. spoken_rules  — 시나리오 프롬프트가 "조회하지 않은 값은 말하지 않는다"를 지시하는가
 12. mem_caps     — 인스턴스 메모리 저장소에 보관 상한이 있는가(등록부 대조)
 13. history_caps — run_turn 호출부가 대화 이력 상한을 두는가(등록부 대조)
 14. speech_rules — 발화 판정 정규식이 등록부에 있고 거절 판정이 짝으로 있는가
 15. keyword_tables— 한글 낱말표가 등록부에 있고 1글자 낱말에 사유가 적혔는가
 16. judge_inputs — 판정 함수에 **잘린 글**(`s[:N]`)을 넘기지 않는가
 17. counter_exposure— 손실·오류 카운터를 세어 두고 응답에 싣는가(등록부 대조)
 18. live_gates    — `*_LIVE` 안전선을 요청마다 읽고 같은 규칙으로 판정하는가
 19. stale_state   — '진행 중'을 뜻하는 저장소에 시간 만료가 있고 배선돼 있는가
 20. host_bypass   — 주소 표기 우회(끝점 FQDN·짧은 꼴 IPv4…)가 SSRF 가드에 막히는가
 21. audit_outcome — 감사 기록이 **응답과 같은 말**을 하는가(성공을 미리 적지 않는다)

검사 범위: 게이트는 **고객에게 도달하는 것**만 막는다. 내부 운영 문서까지 막으면
사람이 게이트를 끄게 되고, 그러면 게이트가 없는 것과 같다.

사용: python3 scripts/verify.py [--json]
종료코드 0=통과, 1=실패.
`--json` 은 콘솔 코드페이지와 무관하게 **UTF-8** 로 나간다(`_emit` 참조).
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
# 2026-10-19: 「이음」은 공식 연계 대상(안부 결과·현장 음성 요청 웹훅)으로 랜딩 「이음 연계」 절에 노출하므로 목록에서 뺀다.
WELFARE = ["광산구", "3세대", "상생"]
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
    "_eum_request.py":   "이음 현장 음성 요청 접수 — EUM_REQUEST_URL(환경변수, _urlguard 필수·기본 허용 호스트 eum.gowon.co.kr)",
    "_speech_providers.py": "자체 학습 전화 STT 서버 — GOWON_STT_URL(환경변수, _urlguard 필수)",
}
# 주소가 **고정 상수가 아닌** 파일. 공용 가드(api/_urlguard.py)를 반드시 거친다.
# 요청 본문에서 오는 것(voice·wellbeing)과 설정에서 오는 것(_order_backend·_vstudio·
# _monitoring·_speech_providers)을 같이 둔다 — 환경변수라고 검증을 빼면 오타·잘못
# 복사한 값으로 주문 정보와 Bearer 키, 합성할 발화와 HMAC 서명, 전사할 **통화 음성**과
# 그 서명, 오류 봉투와 DSN 공개키가 평문 http·사설망·클라우드 메타데이터로
# 나간다(쓰기 승인 뒤에는 환불 접수가 엉뚱한 주소로 간다).
URLGUARD_REQUIRED = {"voice.py", "wellbeing.py", "_order_backend.py",
                     "_vstudio.py", "_monitoring.py", "_eum_request.py",
                     "_speech_providers.py"}
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


# --------------------------------------------------------------------------
# spoken_rules 게이트
#
# `spoken_numbers` 는 **박혀 있는 숫자**를 잡는다. 29차에 드러난 것은 그 반대쪽이다 —
# 숫자가 한 글자도 없어도 「간결·정확하게 답한다」 한 줄이면 모델이 자기 사전지식으로
# 요율·세액을 말한다(연체 대본의 일반 질의 자리: 인지세·만기 전 상환 비용). 통화
# 시나리오 프롬프트는 전부 `use_tools=False` 이거나 "툴 결과만 인용"이라 그 값을
# 조회하는 경로가 아예 없다 — 그러면 **모르는 값은 말하지 않는다**가 프롬프트 안에
# 문장으로 있어야 한다. 새 시나리오가 그 규칙 없이 추가되면 통과하지 못한다.
#
# 한계는 숨기지 않는다: 이것은 **규칙이 있는가**를 보는 게이트이지 모델이 그 규칙을
# 지키는가를 보는 게이트가 아니다(지시는 게이트가 아니다 — 28차 보고서의 한계 그대로).
# --------------------------------------------------------------------------
SPOKEN_RULE_MARKERS = ("말하지 않는다", "지어내지")
# 상수가 아니라 함수로 조립되는 프롬프트(CS 시스템 프롬프트는 발신번호가 섞인 f-string).
SPOKEN_PROMPT_FUNCS = ("_sys",)


def _prompt_texts(text):
    """[(이름, 문자열 전체)] — 시나리오 프롬프트 상수와 등록된 프롬프트 함수.
    실행 없이 ast 로 읽는다(이어붙인 문자열·f-string 안쪽까지 본다)."""
    import ast

    def _join(node):
        return "\n".join(s.value for s in ast.walk(node)
                         if isinstance(s, ast.Constant) and isinstance(s.value, str))

    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return []                              # 문법 오류는 py_compile 게이트가 잡는다
    out = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets
                     if isinstance(t, ast.Name)
                     and t.id.startswith(SPOKEN_PROMPT_PREFIX)]
            if names:
                out.append((names[0], _join(node.value)))
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in SPOKEN_PROMPT_FUNCS):
            # 반환값만 본다 — 주석·독스트링은 봇이 말하는 문장이 아니다.
            body = [r.value for r in ast.walk(node)
                    if isinstance(r, ast.Return) and r.value is not None]
            out.append((node.name + "()", "\n".join(_join(v) for v in body)))
    return out


def check_spoken_rules(sources):
    seen, bad, funcs = 0, [], set()
    for name, text in sorted(sources.items()):
        for const, body in _prompt_texts(text):
            seen += 1
            if const.endswith("()"):
                funcs.add(const[:-2])
            if not any(m in body for m in SPOKEN_RULE_MARKERS):
                bad.append("%s 의 %s" % (name, const))
    if not seen:
        return False, "시나리오 프롬프트(PROMPT_*)를 찾을 수 없음"
    msgs = []
    if bad:
        msgs.append("모르는 값을 말하지 않는다는 규칙이 없는 프롬프트: %s "
                    "(이 통화에서 조회하지 않은 수치는 말하지 않도록 한 줄 적을 것)"
                    % ", ".join(sorted(bad)[:8]))
    stale = sorted(set(SPOKEN_PROMPT_FUNCS) - funcs)
    if stale:
        msgs.append("등록부 잔재(프롬프트 함수 없음): %s" % ", ".join(stale))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "프롬프트 %d개에 수치 규칙 있음" % seen)


# --------------------------------------------------------------------------
# mem_caps 게이트
#
# 이 서비스에는 영속 저장소가 없다(승인 전) — 그래서 통화 세션·감사 버퍼·요율
# 카운터·안부 결과가 전부 **인스턴스 메모리**에 쌓인다. 웜 인스턴스는 수 시간을
# 살기 때문에 상한이 없는 저장소는 두 가지로 끝난다: 메모리가 자라 **진행 중인
# 통화**가 죽고(26차 `_escalation`), 개인정보 보관 기간이 '인스턴스 수명'이
# 된다(30차 `voice._Session`). 둘 다 리뷰에서 보이지 않는다 — 저장소 선언은
# `X = {}` 한 줄이고, 상한이 없다는 것은 **코드에 없는 것**이라 눈에 띄지 않는다.
#
# 그래서 게이트가 센다: `api/` 의 모듈·클래스 본문에서 **빈** dict/list 로
# 시작하는 이름(= import 시점에 만들어져 프로세스 수명을 사는 저장소)은
# 등록부에 상한 기호와 함께 적혀 있어야 하고, 그 기호가 같은 파일에 실제로
# 있어야 한다(상한을 지우고 등록부만 남기는 드리프트를 막는다).
#
# 31차에 범위를 한 칸 넓혔다 — **모듈 전역 싱글턴**(`STORE = RecordingStore()`
# 처럼 import 시점에 딱 하나 만들어지는 인스턴스) 클래스의 인스턴스 속성
# (`self._records = {}`)도 프로세스 수명을 산다. 30차가 범위 밖으로 두고
# 보고서에 적어 둔 구멍이 바로 거기였다(`_recording_audit.STORE`).
#
# 그래도 범위는 여전히 좁다 — 호출부가 매번 새로 만드는 클래스
# (`_sip_adapter` 의 어댑터, `_log` 의 요청 컨텍스트)는 요청당 수명이라 보지
# 않는다. 과차단되면 사람이 게이트를 끄고, 그러면 게이트가 없는 것과 같다
# (28·30차와 같은 이유). 값이 든 리터럴(상수·등록부·요율표)도 자라지 않으므로
# 대상이 아니다.
#
# 40차 결함 수정 — 이 게이트가 `ast.Assign` 만 보고 있었다. 파이썬에서
# `_NUMBERS: dict = {}` 는 `Assign` 이 아니라 **`AnnAssign`** 이라, 형 주석을
# 붙여 선언한 저장소는 **선언 자체가 보이지 않았다**. 그 눈 밖에 운영 장부
# 10개가 있었다(발신번호·파트너·고객사·정산 버킷·고지 테넌트·변경 이력 3종·
# 키 지문 캐시·백엔드 캐시). 지금은 전부 상한이 있지만, 게이트는 「저장소
# 15개 상한 확인」이라고만 말했다 — **검사한 적이 없는 것을 통과로 보고**했다.
# 한 글자 차이(`:`)로 게이트 밖으로 나가는 구멍이라 리뷰에서 보이지 않는다.
# --------------------------------------------------------------------------
MEM_STORES = {
    ("_audit.py", "_BUF"):
        ("DEFAULT_BUFFER", "감사 기록 버퍼 — 오래된 것부터 버린다(CALLBOT_AUDIT_BUFFER)"),
    ("_ratelimit.py", "_HITS"):
        ("MAX_KEYS", "요율 카운터 — 만료분 정리 + 하드 상한"),
    ("_vstudio.py", "_VOICES_CACHE"):
        ("VOICES_CACHE_TTL", "목소리 목록 캐시 — 키는 VOICE_ENGINE_URL 하나, TTL 로 만료"),
    ("voice.py", "RECENT"):
        ("MAX_RECENT", "인입 콜 로그 — 최근 N건(발화는 마스킹 후 보관)"),
    ("voice.py", "_mem"):
        ("SESSION_MAX", "통화 세션(대화 원문) — 상한 초과는 LRU 축출 + SESSION_TTL 만료"),
    ("wellbeing.py", "FAILED"):
        ("_MAX_FAILED", "안부 웹훅 전송 실패 보관 — 최근 N건"),
    ("wellbeing.py", "RECENT"):
        ("_MAX_RECENT", "안부 최근 실행 — 최근 N건"),
    ("wellbeing.py", "RESULTS"):
        ("_MAX_RESULTS", "안부 결과 사본(raw_ref 조회) — 최근 N건"),
    ("wellbeing.py", "_RESULT_ORDER"):
        ("_MAX_RESULTS", "안부 결과 보관 순서 — RESULTS 와 같은 상한"),
    # 모듈 전역 싱글턴의 인스턴스 속성(31차 확장분)
    ("_call_metrics.py", "_Store._open"):
        ("MAX_OPEN", "진행 중 통화 관측치 — 상한 초과는 가장 오래된 것부터"),
    ("_call_metrics.py", "_Store._done"):
        ("MAX_RECORDS", "종료 통화 관측치 — 상한 + RETENTION_SEC 보관 한도"),
    ("_escalation.py", "EscalationQueue._tickets"):
        ("MAX_TICKETS", "전환 티켓 — 상한 초과는 종결된 것부터 축출"),
    ("_escalation.py", "EscalationQueue._audit"):
        ("MAX_AUDIT", "전환 상태 전이 기록 — 오래된 것부터(audit_dropped 로 드러냄)"),
    ("_recording_audit.py", "RecordingStore._records"):
        ("MAX_RECORDS", "녹취 메타 — 상한 초과는 파기된 것부터 축출"),
    ("_recording_audit.py", "RecordingStore._audit"):
        ("MAX_AUDIT", "녹취 접근 감사 기록 — 오래된 것부터(audit_dropped 로 드러냄)"),
    # 형 주석을 붙여 선언한 저장소(40차 확장분). 그 전까지 게이트 밖이었다.
    ("caller_id.py", "_NUMBERS"):
        ("MAX_NUMBERS", "발신번호 대장 — 등록 상한(초과 시 등록 거부)"),
    ("caller_id.py", "_HISTORY"):
        ("HISTORY_MAX", "발신번호 변경 이력 — 최근 N건(버린 줄은 history_stats.dropped)"),
    ("partners.py", "_PARTNERS"):
        ("MAX_PARTNERS", "파트너 대장 — 등록 상한(초과 시 등록 거부)"),
    ("partners.py", "_ACCOUNTS"):
        ("MAX_ACCOUNTS", "고객사 귀속 대장 — 등록 상한(초과 시 등록 거부)"),
    ("partners.py", "_HISTORY"):
        ("HISTORY_MAX", "파트너 변경 이력 — 최근 N건(버린 줄은 history_stats.dropped)"),
    ("disclosure.py", "_TENANTS"):
        ("MAX_TENANTS", "고지 문구 테넌트 설정 — 상한(초과 시 저장 거부)"),
    ("disclosure.py", "_HISTORY"):
        ("HISTORY_MAX", "고지 문구 변경 이력 — 최근 N건(버린 줄은 history_stats.dropped)"),
    ("settlement.py", "_USAGE"):
        ("MAX_USAGE_BUCKETS", "이용 실적 버킷 — 보관기간 정리 + 하드 상한"),
    ("_pii_vault.py", "_KID_CACHE"):
        ("KID_CACHE_MAX", "키 지문 캐시 — 회전 유예로도 넘지 않는 상한, 넘으면 비운다"),
}


def _assigned(node):
    """`X = v` 와 `X: T = v` 를 같은 자리에서 본다 → (대상 노드들, 값).

    형 주석이 붙은 대입은 ast 에서 `Assign` 이 아니라 `AnnAssign` 이다.
    `Assign` 만 보면 `_NUMBERS: dict = {}` 같은 선언이 **통째로 보이지
    않는다** — 40차에 그 눈 밖에서 운영 장부 10개가 발견됐다. 값이 없는
    선언(`X: dict`)은 저장소가 만들어지지 않으므로 대상이 아니다.
    """
    import ast
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


def _singleton_classes(tree):
    """모듈 전역에 인스턴스로 박히는 클래스 이름들.

    `STORE = RecordingStore()` 처럼 import 시점에 만들어져 **프로세스 수명을
    사는** 인스턴스만 찾는다. 호출부가 요청마다 새로 만드는 클래스까지 보면
    과차단이 되고, 과차단되면 사람이 게이트를 끈다.
    """
    import ast
    defined = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    out = set()
    for node in tree.body:
        _targets, value = _assigned(node)
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if isinstance(func, ast.Name) and func.id in defined:
            out.add(func.id)
    return out


def _mem_targets(text):
    """[(이름, 줄번호)] — 프로세스 수명을 사는 빈 dict/list 저장소.

    대상은 둘이다.
      · 모듈·클래스 본문의 `X = {}`            → 이름 `X`
      · 모듈 전역 싱글턴 클래스의 `self._x = {}` → 이름 `클래스._x`
    """
    import ast

    def _empty(node):
        if isinstance(node, ast.Dict):
            return not node.keys
        if isinstance(node, ast.List):
            return not node.elts
        # dict()/list() 도 같은 저장소다(리터럴만 보면 우회된다)
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("dict", "list")
                and not node.args and not node.keywords)

    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return []                              # 문법 오류는 py_compile 게이트가 잡는다
    out, bodies = [], [tree.body]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bodies.append(node.body)
    for body in bodies:
        for node in body:
            targets, value = _assigned(node)
            if value is None or not _empty(value):
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    out.append((t.id, node.lineno))

    # 싱글턴 클래스의 인스턴스 속성 — 메서드 어디에서 만들어지든(생성자든
    # reset() 이든) 그 인스턴스는 하나뿐이므로 수명은 프로세스와 같다.
    singles = _singleton_classes(tree)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in singles:
            continue
        for sub in ast.walk(node):
            targets, value = _assigned(sub)
            if value is None or not _empty(value):
                continue
            for t in targets:
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    out.append(("%s.%s" % (node.name, t.attr), sub.lineno))

    seen, uniq = set(), []                     # 같은 저장소를 여러 곳에서 비워도 1건
    for name, line in out:
        if name in seen:
            continue
        seen.add(name)
        uniq.append((name, line))
    return uniq


def check_mem_caps(sources):
    unregistered, missing, seen = [], [], set()
    for name, text in sorted(sources.items()):
        for store, line in _mem_targets(text):
            key = (name, store)
            if key not in MEM_STORES:
                unregistered.append("%s:%d %s" % (name, line, store))
                continue
            seen.add(key)
            symbol = MEM_STORES[key][0]
            if not re.search(r"\b%s\b" % re.escape(symbol), text):
                missing.append("%s 의 %s (상한 기호 %s 없음)" % (name, store, symbol))
    msgs = []
    if unregistered:
        msgs.append("상한 등록부에 없는 저장소: %s (scripts/verify.py MEM_STORES 에 "
                    "상한 기호와 사유를 적을 것 — 인스턴스 메모리는 무한히 자란다)"
                    % ", ".join(unregistered[:8]))
    if missing:
        msgs.append("상한이 사라진 저장소: %s" % ", ".join(missing[:8]))
    stale = sorted(set(MEM_STORES) - seen)
    if stale:
        msgs.append("등록부 잔재(저장소 없음): %s"
                    % ", ".join("%s 의 %s" % k for k in stale[:8]))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "저장소 %d개 상한 확인" % len(MEM_STORES))


# --------------------------------------------------------------------------
# history_caps 게이트 (32차)
#
# `mem_caps` 는 **저장소 선언**(`X = {}`)을 본다. 대화 이력은 그 눈으로 보이지
# 않는다 — 선언은 `{"messages": []}` 한 칸이고, 자라는 것은 그 **안쪽**이다.
# 31차가 모델 창(`_window`)을, 32차가 저장 이력(`trim_history`)을 막았지만 둘 다
# **호출부가 거쳐야** 효력이 있다. 새 핸들러가 `run_turn` 을 호출하면서 이력을
# 그냥 쌓으면(지금까지 `voice.py` 가 그랬다) 아무 게이트도 걸리지 않는다.
#
# 그래서 센다: `api/` 에서 `run_turn` 을 호출하는 파일은 등록부에 **어떻게
# 상한을 두는지** 적혀 있어야 하고, 그 상한 기호가 같은 파일에 실제로 있어야
# 한다(상한을 지우고 등록만 남기는 드리프트를 막는다. `mem_caps`·`log_fields`·
# `audit_actions` 와 같은 양방향 규약).
#
# 범위는 좁게 — 호출 여부만 ast 로 본다. "저장하는가"를 정적으로 판정하려 들면
# 과차단되고, 과차단되면 사람이 게이트를 끈다(28·30·31차와 같은 이유).
# --------------------------------------------------------------------------
HISTORY_CALLERS = {
    "chat.py": ("MAX_CONTENT",
                "클라이언트가 보낸 이력 — 라우트가 건수(100)·본문 길이로 막는다"),
    "voice.py": ("trim_history",
                 "세션에 저장하는 이력 — 턴마다 창으로 자르고 가드 기억은 접어 넘긴다"),
    "sim_call.py": ("SCRIPTS",
                    "대본 길이가 상한 — 한 요청 안에서만 자라고 저장되지 않는다"),
}


def _calls_run_turn(text):
    """`run_turn(...)` 을 **호출**하는가(import·주석 언급은 아니다)."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return False                           # 문법 오류는 py_compile 게이트가 잡는다
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "run_turn":
            return True
        if isinstance(f, ast.Attribute) and f.attr == "run_turn":
            return True
    return False


def check_history_caps(sources):
    msgs, seen = [], set()
    for name, text in sorted(sources.items()):
        if name == "_engine.py":
            continue                           # 상한을 정의하는 본인
        if not _calls_run_turn(text):
            continue
        if name not in HISTORY_CALLERS:
            msgs.append("%s (scripts/verify.py HISTORY_CALLERS 에 상한 기호와 사유를 "
                        "적을 것 — 이력은 턴마다 자란다)" % name)
            continue
        seen.add(name)
        symbol = HISTORY_CALLERS[name][0]
        if not re.search(r"\b%s\b" % re.escape(symbol), _code(text)):
            msgs.append("%s 의 이력 상한이 사라졌다(기호 %s 없음)" % (name, symbol))
    stale = sorted(set(HISTORY_CALLERS) - seen)
    if stale:
        msgs.append("등록부 잔재(run_turn 호출 없음): %s" % ", ".join(stale))
    # 엔진 자신의 상한 두 축이 사라지면 호출부 배선은 의미가 없다.
    eng = sources.get("_engine.py", "")
    for symbol in ("HISTORY_MAX_MSGS", "HISTORY_MAX_CHARS", "def trim_history",
                   "mem_prior"):
        if symbol not in eng:
            msgs.append("_engine.py 의 %s 가 없다" % symbol)
    return (not msgs), (" / ".join(msgs) if msgs
                        else "run_turn 호출부 %d개 상한 확인" % len(seen))


# --------------------------------------------------------------------------
# 14) speech_rules — 고객 발화를 **긍정으로 읽는** 규칙에 부정 처리가 있는가
#
# 33차에 같은 결함이 두 곳에서 나왔다. 둘 다 한글 키워드 **부분 일치**로
# 긍정을 판정하면서 부정문을 보지 않았다.
#   `_engine._AFFIRM`  = (네|예|응|좋아|…|할게|동의)  → 「환불 **취소할게**요」·
#                        「**동의**하지 않습니다」가 환불 확정의 2단계 관문을 통과
#   `wellbeing._RULES` 의 4·5점 규칙 → 「잘 자지 못해요」가 '수면 양호'
# 리뷰에서 보이지 않는 결함이다 — 코드에는 **없는 것**(부정 처리)이 문제이고,
# 1글자 한글 대안은 다른 낱말 안에서 걸린다는 사실이 패턴만 봐서는 드러나지 않는다.
#
# 그래서 게이트가 센다.
#  (a) 모듈 전역 `X = re.compile("… | …")` 에 한글 대안이 있으면 등록부에
#      **방향**(affirm/deny)과 사유가 적혀 있어야 한다.
#  (b) 방향이 affirm 인 패턴에는 **1글자 한글 대안**을 둘 수 없다(= 33차 결함의 모양).
#      deny 쪽은 느슨해도 된다 — 넓게 걸리면 '동의가 아니다'로 끝나고 봇이 다시 묻는다.
#  (c) 등록부와 코드가 **양방향**으로 맞아야 한다(기호 소멸·등록부 잔재 모두 실패).
#  (d) 동의·점수를 올리는 판정이 있는 파일에는 부정 처리 기호가 실제로 있어야 한다.
#
# 범위는 좁다(28·30~32차와 같은 이유 — 과차단되면 사람이 게이트를 끈다):
# 모듈 전역의 **상수 문자열** `re.compile` 만 ast 로 본다. 함수 안에서 만드는
# 정규식·문자열 리스트 키워드표(`wellbeing._RULES` 자신)는 보지 않는다 —
# 그쪽은 (d)의 기호 검사와 회귀가 지킨다.
# --------------------------------------------------------------------------
SPEECH_RULES = {
    ("_engine.py", "_AFFIRM_PHRASE"):
        ("affirm", "동의 표현 — 2글자 이상만. 1글자 감탄사는 _AFFIRM_TOKENS 어절 일치"),
    ("_engine.py", "_DENY"):
        ("deny", "거절·보류·되묻기 — 넓게 걸리는 쪽이 안전(동의를 놓치면 다시 묻는다)"),
    ("wellbeing.py", "_NEG"):
        ("deny", "부정 표현 — 긍정 규칙(4·5점)을 부정문에서 끈다"),
    ("wellbeing.py", "_POS_IDIOM"):
        ("deny", "부정형이지만 긍정 의미인 관용구 — 부정 탐지에서 가린다"),
}
# 판정 기호(부정 처리 배선) — 사라지면 등록부만 남는다.
SPEECH_REQUIRED = {
    "_engine.py": ("_AFFIRM_TOKENS", "def _affirm_hit"),
    "wellbeing.py": ("def _negated", "NEGATED_SCORE", "POSITIVE_SCORE"),
}
RX_HANGUL = re.compile(r"[가-힣]")


def _hangul_regexes(text):
    """모듈 전역 `X = re.compile("…")` 중 **한글 대안**을 가진 것 → {기호: 패턴}."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return {}                              # 문법 오류는 py_compile 게이트가 잡는다
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        v = node.value
        if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "compile" and v.args
                and isinstance(v.args[0], ast.Constant)
                and isinstance(v.args[0].value, str)):
            continue
        pat = v.args[0].value
        if "|" not in pat or not RX_HANGUL.search(pat):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                out[t.id] = pat
    return out


def _top_alts(pattern):
    """괄호 **밖**의 `|` 로만 쪼갠 대안들(그룹 안쪽의 `|` 는 대안이 아니다)."""
    out, buf, depth, i = [], [], 0, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            buf.append(pattern[i:i + 2]); i += 2; continue
        if c == "[":                           # 문자 클래스 안은 통째로
            j = pattern.find("]", i + 1)
            j = len(pattern) - 1 if j < 0 else j
            buf.append(pattern[i:j + 1]); i = j + 1; continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    out.append("".join(buf))
    return out


def _syllables(alt):
    """대안이 **글자로 요구하는** 한글(문자 클래스·전후방탐색은 글자가 아니다)."""
    s = re.sub(r"\(\?<?[!=][^)]*\)", "", alt)
    return "".join(RX_HANGUL.findall(re.sub(r"\[[^\]]*\]", "", s)))


def _single_hangul_alts(pattern):
    """**한글 1글자만** 요구하는 대안들(= 다른 낱말 안에서 걸리는 모양).

    `진행(?:해|하)` 처럼 앞에 글자가 붙은 그룹은 1글자가 아니다 — 그룹 안쪽의
    `해`·`하` 는 홀로 걸릴 수 없다. 반면 `(네|예|응|…)` 은 그룹 **전체**가 하나의
    대안이라 안쪽이 그대로 1글자 부분일치가 된다(33차 결함의 모양).
    """
    bad = []
    for alt in _top_alts(pattern):
        if re.search(r"\(\?<?[!=]", alt):
            continue          # 전후방탐색으로 **경계를 보는** 대안은 낱말 안에서 걸리지 않는다
        core = re.sub(r"\\s[*+?]|\s", "", alt)
        g = re.match(r"^\((?:\?:)?(.*)\)$", core)
        cands = _top_alts(g.group(1)) if g else [core]
        for cand in cands:
            if re.search(r"[0-9A-Za-z]", cand):
                continue                       # 영문·숫자 대안은 이 규칙의 대상이 아니다
            syl = _syllables(cand)
            if len(syl) == 1:
                bad.append(syl)
    return bad


def _affirm_files():
    """동의(긍정) 방향 규칙이 있는 파일들."""
    return {f for (f, _s), (d, _r) in SPEECH_RULES.items() if d == "affirm"}


def check_speech_rules(sources):
    msgs, seen = [], set()
    for name, text in sorted(sources.items()):
        for symbol, pattern in sorted(_hangul_regexes(text).items()):
            key = (name, symbol)
            if key not in SPEECH_RULES:
                msgs.append("%s:%s (한글 발화 판정 정규식 — scripts/verify.py "
                            "SPEECH_RULES 에 방향(affirm/deny)과 사유를 적을 것)"
                            % (name, symbol))
                continue
            seen.add(key)
            if SPEECH_RULES[key][0] == "affirm":
                bad = _single_hangul_alts(pattern)
                if bad:
                    msgs.append("%s:%s 에 한글 1글자 대안(%s) — 다른 낱말 안에서 "
                                "걸린다(33차 결함의 모양). 어절 단위로 볼 것"
                                % (name, symbol, "·".join(sorted(set(bad)))))
    stale = sorted(set(SPEECH_RULES) - seen)
    if stale:
        msgs.append("등록부 잔재: %s" % ", ".join("%s:%s" % k for k in stale))
    for name, symbols in sorted(SPEECH_REQUIRED.items()):
        code = _code(sources.get(name, ""))
        for symbol in symbols:
            if symbol not in code:
                msgs.append("%s 의 부정 처리 기호 %s 가 없다" % (name, symbol))
    # 동의 판정이 있는 파일에는 거절 판정이 함께 있어야 한다(한쪽만 있으면 반쪽이다).
    for fname in sorted(_affirm_files()):
        if not any(f == fname and d == "deny"
                   for (f, _s), (d, _r) in SPEECH_RULES.items()):
            msgs.append("%s 에 동의 판정만 있고 거절 판정이 없다" % fname)
    return (not msgs), (" / ".join(msgs) if msgs
                        else "발화 판정 정규식 %d개 확인" % len(seen))


# --------------------------------------------------------------------------
# 15) 낱말표 등록부 (34차) — 문자열 키워드표도 발화를 판정한다
#
# 33차가 `speech_rules` 로 **정규식** 판정을 센 뒤, 스스로 한계 ④를 적어 뒀다:
# 「새 게이트는 모듈 전역 상수 정규식만 본다 — 문자열 리스트 키워드표
# (`wellbeing._RULES`)와 함수 안 정규식은 범위 밖이다」. 34차 결함이 바로 그
# 범위 밖에서 나왔다 — `_eum_request._RULES` 의 `피가`·`숨을` 이 「커**피를**」·
# 「한**숨을**」 에서 걸려 **긴급 연결**로 올라갔다(부분 일치는 정규식만의
# 문제가 아니다. `w in text` 도 같다).
#
# 그래서 센다.
#  (a) 모듈 전역의 **짧은 한글 문자열표**(튜플·리스트·사전)는 등록부
#      (`KEYWORD_TABLES`)에 쓰임(match/mask/label)과 사유가 적혀 있어야 한다.
#      양방향으로 맞아야 한다(표 소멸·등록부 잔재 모두 실패).
#  (b) 쓰임이 `match` 인 표의 **한글 1글자 낱말**은 `KEYWORD_SHORT` 에 사유와
#      함께 적혀 있어야 한다 — 1글자는 다른 낱말 안에서 걸린다(33차 결함의 모양).
#      금지가 아니라 **검토 강제**다: 분류를 틀리는 것은 환불을 보내는 것과
#      무게가 다르므로, 사유를 적고 회귀로 고정했다면 둬도 된다.
#  (c) `KEYWORD_MASKS` 에 적힌 오탐 가림 기호는 그 파일에 **코드로** 있어야 한다
#      (주석은 배선이 아니다 — 33차 (d)와 같은 규약).
#  (d) 38차 — 한 파일에서 오탐으로 밝혀진 바깥 낱말(`KEYWORD_DECOY_HOSTS`)이
#      다른 파일 `match` 표 안에 **조각으로** 들어 있으면 그 파일도 가림을
#      배선해야 한다(아래 등록부 주석 참조).
#
# 범위는 좁다(28·30~33차와 같은 이유 — 과차단되면 사람이 게이트를 끈다):
# 모듈 전역의 리터럴만 ast 로 보고, **짧은 낱말**(12자 이하·문장부호 없음)이
# 3개 이상인 표만 본다. 안내 문구·프롬프트·질문지는 표가 아니다. 함수 안에서
# 만드는 낱말표는 보지 않는다 — 그쪽은 (c)의 기호 검사와 회귀의 몫이다.
# --------------------------------------------------------------------------
KEYWORD_TABLES = {
    ("_eum_request.py", "_RULES"):
        ("match", "이음 3R 요청 유형 분류 — 부분 일치. 긴급이 다른 유형보다 앞선다"),
    ("_eum_request.py", "_DECOY"):
        ("mask", "오탐 낱말(커피·한숨·무사고·돈가스) — 분류 직전에 가린다"),
    ("_eum_request.py", "LABEL_KO"):
        ("label", "유형 한글 이름 — 화면 표시용, 판정에 쓰지 않는다"),
    ("wellbeing.py", "_RULES"):
        ("match", "안부 4항목 점수 규칙 — 부분 일치. 긍정 규칙은 _negated 로 뒤집는다"),
    ("wellbeing.py", "_CRITICAL"):
        ("match", "위험신호 — 평균과 무관하게 high. 자해 암시만 부정을 보지 않고"
                  "(CRITICAL_NO_DENIAL) 나머지는 _DENIAL 로 '없다는 말'을 가린다"),
    ("wellbeing.py", "_DECOY"):
        ("mask", "오탐 낱말(커피·열이틀) — 판정 직전에 가린다. 「한숨」은 빼 둔다"),
    ("wellbeing.py", "_LABEL"):
        ("label", "점수별 판정 라벨 — dimensions 근거 표시용"),
    ("_escalation.py", "HANDOFF_KEYWORDS"):
        ("match", "상담원 연결 요청 — 걸리면 사람에게 넘긴다(넓게 걸리는 쪽이 안전)"),
    ("_escalation.py", "SENSITIVE_KEYWORDS"):
        ("match", "민감 신호(법적·언론) — 걸리면 우선순위를 올린다"),
    ("disclosure.py", "BANNED"):
        ("match", "금지어 — 화면·문서에 있으면 실패시킨다"),
    ("caller_id.py", "EVIDENCE_TYPES"):
        ("label", "발신번호 증빙 종류 — 입력 화이트리스트"),
    ("partners.py", "ROLES"):
        ("label", "파트너 권한 이름 — 표시용"),
    ("settlement.py", "CSV_COLUMNS"):
        ("label", "정산 CSV 머리글"),
    ("settlement.py", "STATUS_LABEL"):
        ("label", "정산 상태 한글 이름"),
    ("_settle_lines.py", "CSV_HEADER"):
        ("label", "과금 라인 CSV 머리글"),
}
# match 표에 둔 **한글 1글자** 낱말과 그 사유. 적지 않은 1글자는 게이트가 막는다.
KEYWORD_SHORT = {
    ("_eum_request.py", "_RULES", "돈"):
        "금전 문제는 1글자로 들어온다(「돈 잃어버리셨어요」). 오탐(돈가스·돈까스)은 _DECOY 가 가린다",
    ("wellbeing.py", "_RULES", "굶"):
        "결식은 어간 1글자로만 공통된다(굶었·굶고·굶으). 위험을 올리는 쪽이라 넓게 본다",
    ("wellbeing.py", "_CRITICAL", "굶"):
        "위 _RULES 와 같은 낱말 — 위험신호는 놓치는 쪽이 더 위험하다",
}
# 오탐 가림 배선 — 사라지면 등록부만 남는다.
KEYWORD_MASKS = {
    "_eum_request.py": ("_DECOY", "def _mask_decoys"),
    "wellbeing.py": ("_POS_IDIOM", "def _negated",
                     "_DECOY", "_DENIAL", "def judge_text"),
}
# (d) 38차 — **오탐은 한 번 알면 전부에 적용된다.**
#     34차가 `_eum_request` 에서 「커**피를**」·「한**숨을**」·「**무사고**」를
#     가렸지만 `wellbeing._RULES` 의 `피가` 는 그대로 남아, 38차까지
#     「커피가 식었어요」가 **거동 곤란·risk high** 로 나갔다. 같은 오탐을
#     파일마다 다시 발견하게 두는 것이 결함이다 — 아래 바깥 낱말이 어느
#     `match` 표 안에 **조각으로** 들어 있으면 그 파일도 가림을 배선해야 한다.
#     판정: 조각 W 가 (바깥 낱말 + 조사) 안에 들어가고 W 가 바깥 낱말을
#     포함하지 않으면 조각이다(`한숨도 못` 처럼 바깥 낱말을 통째로 품은
#     낱말은 일부러 그 말을 보는 것이므로 오탐이 아니다).
KEYWORD_DECOY_HOSTS = {
    "커피": "「커피가」·「커피를」 안에 `피`(출혈)가 숨는다",
    "한숨": "「한숨을」·「한숨이」 안에 `숨`(호흡곤란)이 숨는다",
    "무사고": "「무사고」 안에 `사고`가 숨는다",
    "돈가스": "「돈가스」 안에 `돈`(금전문제)이 숨는다",
    "돈까스": "「돈까스」 안에 `돈`(금전문제)이 숨는다",
    "열이틀": "「열이틀」 안에 `열이`(발열)가 숨는다",
    "열이레": "「열이레」 안에 `열이`(발열)가 숨는다",
}
KW_PARTICLES = ("", "가", "를", "을", "이", "은", "도", "만", "과", "와", "에", "로")
KW_MAX_LEN = 12
RX_KW_PUNCT = re.compile(r"[?!.…·]")


def _decoy_hosts_for(word):
    """이 낱말이 **조각으로** 숨는 바깥 낱말들."""
    out = []
    for host in sorted(KEYWORD_DECOY_HOSTS):
        if host in word:
            continue                           # 바깥 낱말을 품었으면 의도된 일치
        if any(word in (host + p) for p in KW_PARTICLES):
            out.append(host)
    return out


def _keyword_tables(text):
    """모듈 전역의 **짧은 한글 낱말표** → {기호: [낱말…]}."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:                          # noqa: BLE001
        return {}                              # 문법 오류는 py_compile 게이트가 잡는다
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Dict)):
            continue
        words = [n.value for n in ast.walk(node.value)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and RX_HANGUL.search(n.value)]
        if len(words) < 3 or max(len(w) for w in words) > KW_MAX_LEN:
            continue                           # 안내 문구·프롬프트는 낱말표가 아니다
        if any(RX_KW_PUNCT.search(w) for w in words):
            continue                           # 문장(질문지·대본)은 낱말표가 아니다
        for t in node.targets:
            if isinstance(t, ast.Name):
                out[t.id] = words
    return out


def check_keyword_tables(sources):
    msgs, seen, shorts = [], set(), set()
    hosts_used = set()
    for name, text in sorted(sources.items()):
        tables = _keyword_tables(text)
        # 이 파일이 가리는 바깥 낱말(쓰임이 mask 인 표의 낱말 전부)
        masked = set()
        for symbol, words in tables.items():
            if KEYWORD_TABLES.get((name, symbol), ("", ""))[0] == "mask":
                masked.update(words)
        for symbol, words in sorted(tables.items()):
            key = (name, symbol)
            if key not in KEYWORD_TABLES:
                msgs.append("%s:%s (한글 낱말표 — scripts/verify.py KEYWORD_TABLES 에 "
                            "쓰임(match/mask/label)과 사유를 적을 것)" % (name, symbol))
                continue
            seen.add(key)
            if KEYWORD_TABLES[key][0] != "match":
                continue
            for w in sorted(set(w for w in words if len(w) == 1)):
                if (name, symbol, w) in KEYWORD_SHORT:
                    shorts.add((name, symbol, w))
                else:
                    msgs.append("%s:%s 의 한글 1글자 낱말 「%s」 — 다른 낱말 안에서 "
                                "걸린다(33·34차 결함의 모양). KEYWORD_SHORT 에 사유를 "
                                "적거나 어절·조사 꼴로 바꿀 것" % (name, symbol, w))
            for w in sorted(set(words)):
                for host in _decoy_hosts_for(w):
                    hosts_used.add(host)
                    if host not in masked:
                        msgs.append("%s:%s 의 「%s」 는 「%s」 안에서도 걸린다 — %s. "
                                    "이미 다른 파일에서 오탐으로 밝혀진 낱말이다"
                                    "(34·38차). 이 파일의 mask 표에 「%s」 를 넣을 것"
                                    % (name, symbol, w, host,
                                       KEYWORD_DECOY_HOSTS[host], host))
    stale_hosts = sorted(set(KEYWORD_DECOY_HOSTS) - hosts_used)
    if stale_hosts:
        msgs.append("오탐 기록부 잔재(조각을 쓰는 match 표가 없다): %s"
                    % ", ".join(stale_hosts))
    stale = sorted(set(KEYWORD_TABLES) - seen)
    if stale:
        msgs.append("등록부 잔재: %s" % ", ".join("%s:%s" % k for k in stale))
    stale_short = sorted(set(KEYWORD_SHORT) - shorts)
    if stale_short:
        msgs.append("1글자 등록부 잔재: %s" % ", ".join("%s:%s:%s" % k for k in stale_short))
    for fname, symbols in sorted(KEYWORD_MASKS.items()):
        code = _code(sources.get(fname, ""))
        for symbol in symbols:
            if symbol not in code:
                msgs.append("%s 의 오탐 가림 기호 %s 가 없다" % (fname, symbol))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "한글 낱말표 %d개 확인(1글자 등록 %d건 · 오탐 기록부 %d건)"
                             % (len(seen), len(shorts), len(KEYWORD_DECOY_HOSTS)))


# --------------------------------------------------------------------------
# 16) 판정 입력 (35차) — 판정 함수에 **잘린 글**을 넘기지 않는다
#
# 15단계까지는 판정 **규칙**(정규식·낱말표)을 봤다. 35차 결함은 규칙이 아니라
# 규칙에 넣는 **글**이 틀려서 났다: `_eum_request.build_payload` 가 이음에
# 보낼 요약을 먼저 만들고(`clean_summary` = 가림 + 500자 절단) 그 **잘린 글**로
# 분류했다. 라우트는 4000자까지 받으므로 최대 3500자가 분류에서 통째로 빠져
# 있었고, 음성 요청은 용건이 뒤에 오는 일이 흔하다 — 「…(앞말 500자)…
# 어르신이 쓰러지셨어요」가 `other` 로 접수됐다. 규칙은 멀쩡했다.
#
# 리뷰에서 보이지 않는 모양이다. 절단은 `s[:MAX]` 한 칸이고 분류 호출은 그
# 다음 줄이라, 두 줄을 **같이** 읽어야만 보인다.
#
# 그래서 센다: 등록된 판정 함수(`JUDGE_INPUTS`)를 부를 때, 같은 함수 안에서
# **윗쪽을 자른 슬라이스**(`x[:N]`)로 묶인 이름을 인자로 넘기면 실패시킨다.
#
# 범위는 좁다(28·30~34차와 같은 이유 — 과차단되면 사람이 게이트를 끈다):
#  · 등록부에 적은 판정 함수 이름만 본다(자동 수집하지 않는다).
#  · 같은 함수 본문 안의 **직접 대입**만 따라간다(별칭·속성·전역은 범위 밖).
#  · 아래쪽만 자르는 슬라이스(`x[2:]`)와 인덱싱(`x[0]`)은 절단이 아니다.
# 등록부는 양방향이다 — 적어 둔 판정 함수가 그 파일에서 사라지면 잔재로 운다.
# --------------------------------------------------------------------------
JUDGE_INPUTS = {
    ("_eum_request.py", "classify"):
        "요청 유형 분류 — 잘린 요약이 아니라 전문(clean_text)을 본다. 뒤쪽 긴급 신호를 놓친다",
    ("wellbeing.py", "score_answers"):
        "안부 4항목 채점 — 답변을 자르고 채점하면 뒤쪽 부정·위험신호가 사라진다",
    ("wellbeing.py", "_negated"):
        "부정문 판정 — 부정 표현은 문장 **뒤**에 온다(…지 못해요). 자르면 긍정으로 읽는다",
    ("_escalation.py", "evaluate"):
        "에스컬레이션 판단 — 상담원 요청·민감 낱말은 발화 끝에 오는 일이 흔하다",
}


def _called_name(node):
    import ast
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    return f.attr if isinstance(f, ast.Attribute) else ""


def _truncating_producers(tree):
    """같은 파일에서 **잘라서 돌려주는** 함수 이름들.

    35차 결함은 절단이 호출된 함수 **안**에 있었다(`clean_summary` =
    가림 + 500자). 호출부에는 슬라이스가 보이지 않는다 — 슬라이스만 보면
    실제 결함을 놓친다. 등록부를 또 만들지 않고 `return …[:N]` 을 센다.
    """
    import ast
    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for r in ast.walk(fn):
            if isinstance(r, ast.Return) and r.value is not None and _cuts(r.value, ()):
                out.add(fn.name)
                break
    return out


def _cuts(node, producers):
    """이 식이 **윗쪽을 자른** 값인가 — 직접 슬라이스이거나 자르는 함수의 결과."""
    import ast
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice)
                and sub.slice.upper is not None):
            return True
        if isinstance(sub, ast.Call) and _called_name(sub) in producers:
            return True
    return False


def _truncating_names(fn, producers):
    """함수 본문에서 잘린 값으로 묶인 이름들."""
    import ast
    out = set()
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Assign) or not _cuts(sub.value, producers):
            continue
        for t in sub.targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def _judge_calls(fn, names, producers):
    """함수 안의 판정 호출 중 **잘린 값**을 받는 것 → [(함수명, 인자 표기, 줄)]."""
    import ast
    cut = _truncating_names(fn, producers)
    hits = []
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call) or _called_name(sub) not in names:
            continue
        called = _called_name(sub)
        for a in list(sub.args) + [k.value for k in sub.keywords]:
            if isinstance(a, ast.Name) and a.id in cut:
                hits.append((called, a.id, sub.lineno))
            elif not isinstance(a, ast.Name) and _cuts(a, producers):
                hits.append((called, "인자", sub.lineno))
    return hits


def check_judge_inputs(sources):
    import ast
    msgs, seen = [], set()
    for name, text in sorted(sources.items()):
        wanted = {f for (fn, f) in JUDGE_INPUTS if fn == name}
        if not wanted:
            continue
        try:
            tree = ast.parse(text)
        except Exception:                      # noqa: BLE001
            continue                           # 문법 오류는 py_compile 게이트가 잡는다
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for f in sorted(wanted & defined):
            seen.add((name, f))
        producers = _truncating_producers(tree)
        for node in ast.walk(tree):
            # 자르는 함수 **안**도 본다 — 거기서 판정하면 같은 결함이다.
            if not isinstance(node, ast.FunctionDef):
                continue
            for called, arg, line in _judge_calls(node, wanted, producers):
                msgs.append("%s:%d %s() 에 잘린 값 `%s` 을 넘긴다 — %s"
                            % (name, line, called, arg, JUDGE_INPUTS[(name, called)]))
    stale = sorted(set(JUDGE_INPUTS) - seen)
    if stale:
        msgs.append("등록부 잔재(판정 함수 없음): %s"
                    % ", ".join("%s:%s" % k for k in stale))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "판정 함수 %d개의 입력 확인" % len(seen))


# --------------------------------------------------------------------------
# 17) 손실·오류 카운터 노출 (36차) — **세어 두고 보여주지 않으면 센 것이 아니다**
#
# 이 저장소는 같은 결함을 네 번 고쳤다: 26차(전환 티켓 `dropped`)·31차(녹취
# `dropped_active`)·35차(실패 보관 `dropped`). 전부 "버린 사실을 세는 칸을
# 만들었는데 HTTP 응답에 싣는 것을 잊었다"는 한 모양이다. 36차에 또 나왔다 —
# `_escalation.stats()` 는 `audit_dropped`·`summary_dropped` 를 세는데
# `ops_stats.ESCALATION_KEYS` 에 없어서 **어디에도 나오지 않았다**(바로 옆
# `RECORDING_KEYS` 는 `audit_dropped` 를 이미 내보내고 있었다 — 절반만 지켜진
# 규칙이다).
#
# 리뷰에서 보이지 않는 모양이다. 세는 쪽과 내보내는 쪽이 **다른 파일**이고,
# 빠뜨려도 응답은 멀쩡하게 200 으로 나간다(없는 칸은 그냥 없다). 조용히
# 사라지는 숫자가 하필 "유실"과 "오류"라서, 운영자는 아무 일도 없었다고 읽는다.
#
# 그래서 센다: 등록된 stats() 가 만드는 키 중 **손실·오류 이름**(dropped·
# errors·evicted·failed·lost·skipped)은 등록된 노출 목록에 **반드시** 있어야
# 한다. 양방향이다 — 노출 목록에만 있고 stats() 가 만들지 않는 손실 칸은
# 영원히 0 을 내보내는 거짓 안심이므로 그쪽도 운다.
#
# 범위는 좁다(28·30~35차와 같은 이유 — 과차단되면 사람이 게이트를 끄고,
# 그러면 게이트가 없는 것과 같다):
#  · 등록부(`COUNTER_EXPOSURE`)에 적은 (저장소 파일, stats 함수)만 본다.
#  · 키는 **문자열 리터럴**만 센다(`s[r["state"]]` 같은 동적 키는 범위 밖).
#  · 손실·오류 이름이 아닌 칸(`total`·`capacity`·`scope`)은 보지 않는다.
# --------------------------------------------------------------------------
COUNTER_EXPOSURE = {
    ("_escalation.py", "stats"):
        ("ops_stats.py", "ESCALATION_KEYS",
         "전환 큐 손실·오류 — /api/ops_stats 의 escalation 블록"),
    ("_recording_audit.py", "stats"):
        ("ops_stats.py", "RECORDING_KEYS",
         "녹취 장부 손실 — /api/ops_stats 의 recording 블록"),
    # 39차: 실측 수집기도 같은 규약으로 넣는다. 이 자리가 게이트 밖에 있던 동안
    # `stale_opens`(종료 이벤트 유실로 만료시킨 관측치)가 새로 생겼다 —
    # 노출 목록은 `_measured()` 의 폴백 스키마와 같은 등록부를 쓴다.
    ("_call_metrics.py", "stats"):
        ("ops_stats.py", "MEASURED_COLLECTOR_KEYS",
         "실측 수집기 손실·오류 — /api/ops_stats 의 measured.collector 블록"),
    # 40차: 관리 장부 변경 이력. 상한(100)을 넘기면 오래된 줄을 버리는데
    # 세지도 내보내지도 않아, 화면이 받은 건수를 **전부**로 그렸다
    # (콘솔은 「append-only」 라고 적고 있었다). 세는 쪽과 내보내는 쪽이 같은
    # 파일이지만 규약은 같다 — 손실 칸이 빠지면 운다.
    ("caller_id.py", "history_stats"):
        ("caller_id.py", "HISTORY_KEYS",
         "발신번호 변경 이력 손실 — /api/caller_id?op=history"),
    ("partners.py", "history_stats"):
        ("partners.py", "HISTORY_KEYS",
         "파트너·귀속 변경 이력 손실 — /api/partners?op=history"),
    ("disclosure.py", "history_stats"):
        ("disclosure.py", "HISTORY_KEYS",
         "AI 고지 문구 변경 이력 손실 — /api/disclosure?op=history"),
}

# 손실·오류를 뜻하는 이름(앞 또는 뒤 토큰). `dropped_waiting`(앞)·
# `audit_dropped`(뒤) 가 둘 다 걸리도록 양쪽을 본다.
# 39차에 `expired`·`stale` 을 넣었다 — **시간 만료도 손실이다**(종료 이벤트를
# 못 받아 만료시킨 관측치·아무도 집어가지 않아 이탈 처리한 티켓). 그 숫자가
# 보이지 않으면 운영자는 집계가 줄어든 이유를 알 수 없다.
_LOSS_WORDS = ("dropped", "errors", "evicted", "failed", "lost", "skipped",
               "expired", "stale")


def _is_loss_counter(key):
    parts = str(key or "").split("_")
    return bool(parts) and (parts[0] in _LOSS_WORDS or parts[-1] in _LOSS_WORDS)


def _stats_keys(tree, func_name):
    """`stats()` 가 만드는 **문자열 리터럴 키**들.

    세 가지 모양을 센다 — `s["x"] = …` · `s = {"x": …}` · `return {"x": …}`.
    동적 키(`s[r["state"]]`)는 세지 않는다(과차단 방지).
    """
    import ast

    def _dict_keys(node, out):
        if not isinstance(node, ast.Dict):
            return
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.add(k.value)

    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name != func_name:
            continue
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and isinstance(t.slice.value, str)):
                        out.add(t.slice.value)
                _dict_keys(sub.value, out)
            elif isinstance(sub, ast.Return) and sub.value is not None:
                _dict_keys(sub.value, out)
    return out


def _seq_strings(tree, symbol):
    """모듈 전역 `SYMBOL = ("a", "b")` 의 문자열들(없으면 None)."""
    import ast
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            return set()
        return {e.value for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return None


def check_counter_exposure(sources):
    import ast
    msgs, seen = [], set()
    trees = {}
    for name, text in sources.items():
        try:
            trees[name] = ast.parse(text)
        except Exception:                      # noqa: BLE001
            pass                               # 문법 오류는 py_compile 게이트가 잡는다
    for (store, func), (route, symbol, why) in sorted(COUNTER_EXPOSURE.items()):
        st, rt = trees.get(store), trees.get(route)
        if st is None or rt is None:
            msgs.append("등록부 잔재(파일 없음): %s 또는 %s" % (store, route))
            continue
        produced = _stats_keys(st, func)
        if not produced:
            msgs.append("등록부 잔재(%s 에 %s() 가 없다)" % (store, func))
            continue
        exposed = _seq_strings(rt, symbol)
        if exposed is None:
            msgs.append("등록부 잔재(%s 에 %s 가 없다)" % (route, symbol))
            continue
        seen.add((store, func))
        for k in sorted(produced):
            if _is_loss_counter(k) and k not in exposed:
                msgs.append("%s:%s() 가 세는 「%s」 가 %s:%s 에 없다 — 세어 두고 "
                            "보여주지 않으면 센 것이 아니다(%s)"
                            % (store, func, k, route, symbol, why))
        for k in sorted(exposed):
            if _is_loss_counter(k) and k not in produced:
                msgs.append("%s:%s 의 「%s」 를 %s:%s() 가 만들지 않는다 — 영원히 "
                            "0 을 내보내는 거짓 안심이다" % (route, symbol, k, store, func))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "손실·오류 카운터 노출 %d곳 확인" % len(seen))


# --------------------------------------------------------------------------
# 18) 실활성화 게이트 (37차) — **판정은 한 곳에서, 요청마다 다시 읽는다**
#
# `*_LIVE` 환경변수는 이 저장소의 안전선이다(실발신·실과금·실녹취). 37차에
# 같은 안전선에서 결함 두 건이 나왔고, 둘 다 "게이트를 어떻게 읽는가"가 자리마다
# 달랐던 것이다:
#   (1) `voice.py` 만 게이트를 **import 시점에** 상수로 굳혀 두었다 — 웜
#       인스턴스에서는 `CPAAS_LIVE=0` 으로 내려도 캠페인이 계속 `mode="live"`
#       로 답했다(**비상정지가 듣지 않는다**). 같은 순간 `wellbeing` 은 501 로
#       막으므로 한 인스턴스 안에서 라우트마다 게이트가 엇갈렸다.
#       `_speech_providers.is_live()` 가 같은 이유로 이미 고쳐 두고 docstring 에
#       이유까지 적어 둔 자리인데, 그 규약이 거기만 비켜 가 있었다.
#   (2) `ops_stats.gate_flags()` 는 게이트 값을 **자기 규칙으로 다시 해석**했다
#       (`"yes"` 까지 참). 그래서 `CPAAS_LIVE=true` 에서 콘솔은 「ON」, 코드는
#       전부 OFF 였다 — 같은 화면의 다른 패널과 서로 다른 말을 했다.
#
# 둘 다 리뷰에서 보이지 않는다. 한 줄짜리 상수와 한 줄짜리 비교이고, 어느
# 쪽도 테스트를 적색으로 만들지 않는다(기본값이 OFF 라 평상시 답이 같다).
#
# 그래서 센다:
#   (a) 모듈 전역에서 게이트 판정을 **굳히지 않는다**. 환경 조회가 모듈 전역
#       대입에 있으면 실패. 술어를 한 번 불러 두는 하위호환 스냅샷
#       (`LIVE = live()`)만 등록부에 적고 허용한다.
#   (b) 게이트를 읽는 파일은 등록부에 있어야 하고, 그 **비교 규칙**이 게이트의
#       등록 규칙과 같아야 한다(한 글자라도 넓히거나 좁히면 실패).
#   (c) 소유 술어가 실제로 존재하고 환경을 읽으며 같은 규칙을 쓴다.
#   (d) 게이트 현황을 내보내는 자리(`ops_stats._GATE_OWNERS`)가 (c) 와 같은
#       소유자·규칙을 가리킨다 — 화면이 코드와 다른 말을 하지 못한다.
#
# 범위는 좁다(28·30~36차와 같은 이유 — 과차단되면 사람이 게이트를 끄고,
# 그러면 게이트가 없는 것과 같다):
#  · 등록부(`LIVE_GATES`)에 적은 게이트 이름만 본다.
#  · 읽는 자리는 **게이트 이름 문자열이 함수 호출 인자로 들어간 곳**만 본다
#    (`os.environ.get(GATE)` 처럼 변수를 거치는 것은 소유 술어 검사로 덮는다).
#  · 셀프테스트(`if __name__ == "__main__"`) 블록은 배포 코드가 아니라 뺀다.
# --------------------------------------------------------------------------
LIVE_GATES = {
    # 게이트: (규칙 낱말, (소유 파일, 소유 술어), 사유)
    "CPAAS_LIVE": (("1",), ("_sip_adapter.py", "is_live"),
                   "실발신·실과금 — 켜는 것은 [승인 필요]"),
    "SPEECH_LIVE": (("1",), ("_speech_providers.py", "is_live"),
                    "STT/TTS 실호출·키 과금"),
    "RECORDING_LIVE": (("1", "true", "on"), ("_recording_audit.py", "recording_live"),
                       "녹취 — 고지 문안과 장부가 같은 규칙을 봐야 한다"),
}
# 게이트를 읽는 파일(복제 술어 포함). 새 라우트가 읽기 시작하면 여기에 적기
# 전에는 통과하지 못한다 — 안전선을 읽는 자리는 사람이 알아야 한다.
LIVE_GATE_READERS = {
    ("_sip_adapter.py", "CPAAS_LIVE"): "게이트 소유 술어 is_live()",
    ("voice.py", "CPAAS_LIVE"): "통화 웹훅·캠페인 — live()",
    ("wellbeing.py", "CPAAS_LIVE"): "안부 실발신 차단(501) — live_mode()",
    ("caller_id.py", "CPAAS_LIVE"): "발신번호 장부 현황 — cpaas_live()",
    ("health.py", "CPAAS_LIVE"): "헬스 의존성 cpaas — _dep_cpaas()",
    ("health.py", "SPEECH_LIVE"): "헬스 의존성 speech — _speech()",
    ("disclosure.py", "RECORDING_LIVE"): "녹음 고지 요건 — 장부와 같은 규칙",
    ("_recording_audit.py", "RECORDING_LIVE"): "게이트 소유 술어 recording_live()",
}
# 하위호환 스냅샷(모듈 로드 시점에 술어를 한 번 불러 둔 상수). 판정에는 술어를
# 쓴다 — 여기 적힌 이름만 허용하고, 환경 조회를 직접 굳힌 것은 어떤 경우에도 막는다.
LIVE_GATE_SNAPSHOTS = {
    ("_sip_adapter.py", "CPAAS_LIVE"): "하위호환 상수 — 판정은 is_live()",
    ("voice.py", "LIVE"): "하위호환 상수 — 판정은 live()",
}
# 게이트 현황을 HTTP 로 내보내는 자리 — 판정을 복제하지 않고 소유 술어를 부른다.
LIVE_GATE_VIEWS = {
    ("ops_stats.py", "_GATE_OWNERS"):
        "콘솔 모니터 게이트 현황 — /api/ops_stats 의 gates 블록",
}


def _strip_selftest(tree):
    """`if __name__ == "__main__":` 블록을 뺀 모듈 본문(셀프테스트는 배포 코드가 아니다)."""
    import ast
    body = []
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            continue
        body.append(node)
    return body


def _parents(nodes):
    import ast
    out = {}
    stack = list(nodes)
    while stack:
        cur = stack.pop()
        for ch in ast.iter_child_nodes(cur):
            out[ch] = cur
            stack.append(ch)
    return out


def _rule_words(node, parents, gate):
    """읽는 자리를 감싸는 **가장 가까운 비교식**의 비교 대상 낱말들.

    `== "1"` → {"1"} · `in ("1","true","on")` → 셋. **비교 대상만** 본다 —
    왼쪽(환경 조회·기본값 `"0"`·`""`·게이트 이름)은 규칙이 아니라 읽는 방법이고,
    거기까지 세면 모든 읽는 자리가 거짓 드리프트로 걸린다(과차단 변이로 확인).
    없으면 None(비교 없이 쓰는 자리).
    """
    import ast
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Compare):
            words = set()
            for c in cur.comparators:
                for n in ast.walk(c):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        words.add(n.value)
            words.discard(gate)
            return words
        if isinstance(cur, (ast.FunctionDef, ast.Module)):
            break
    return None


def _in_function(node, parents):
    import ast
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return True
    return False


def _gate_reads(nodes, parents):
    """[(게이트, 노드)] — 게이트 이름 문자열이 **함수 호출 인자**로 들어간 곳."""
    import ast
    out = []
    for root in nodes:
        for call in ast.walk(root):
            if not isinstance(call, ast.Call):
                continue
            for arg in list(call.args) + [k.value for k in call.keywords]:
                for n in ast.walk(arg):
                    if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                            and n.value in LIVE_GATES):
                        out.append((n.value, n))
    return out


def _owner_rule(tree, func_name):
    """소유 술어의 (환경을 읽는가, 비교 규칙 낱말). 없으면 None."""
    import ast
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name != func_name:
            continue
        reads_env = "os.environ" in ast.unparse(fn) if hasattr(ast, "unparse") else True
        words = set()
        for cmp_ in ast.walk(fn):
            if not isinstance(cmp_, ast.Compare):
                continue
            for c in cmp_.comparators:
                for n in ast.walk(c):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        words.add(n.value)
        return reads_env, words
    return None


def _view_owners(tree, symbol):
    """`SYMBOL = {"GATE": ("모듈", "술어", ("1",...))}` → {게이트: (파일, 술어, 낱말)}."""
    import ast
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            return {}
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            if not isinstance(v, (ast.Tuple, ast.List)) or len(v.elts) < 3:
                continue
            mod, attr, words = v.elts[0], v.elts[1], v.elts[2]
            if not (isinstance(mod, ast.Constant) and isinstance(attr, ast.Constant)):
                continue
            if not isinstance(words, (ast.Tuple, ast.List)):
                continue
            out[k.value] = (
                "%s.py" % mod.value, attr.value,
                {e.value for e in words.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)})
        return out
    return None


def check_live_gates(sources):
    import ast
    msgs, seen_readers, seen_gates = [], set(), set()
    trees = {}
    for name, text in sources.items():
        try:
            trees[name] = ast.parse(text)
        except Exception:                      # noqa: BLE001
            pass                               # 문법 오류는 py_compile 게이트가 잡는다

    for name in sorted(trees):
        tree = trees[name]
        body = _strip_selftest(tree)
        parents = _parents(body)
        # (a) 모듈 전역에서 판정을 굳히지 않는다.
        for node in body:
            if not isinstance(node, ast.Assign):
                continue
            frozen = [g for g, n in _gate_reads([node.value], _parents([node.value]))]
            if not frozen:
                continue
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                msgs.append("%s:%d %s = … %s 환경 조회 — 게이트를 import 시점에 "
                            "굳혔다. 웜 인스턴스에서는 **비상정지가 듣지 않는다**. "
                            "함수로 옮겨 호출 시점에 읽을 것"
                            % (name, node.lineno, t.id, frozen[0]))
        for node in body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            fn = node.value.func
            called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            # 술어를 한 번 불러 두는 스냅샷 — 등록부에 적힌 것만 허용한다.
            pred_names = set()
            for g, (_w, (ofile, oattr), _why) in LIVE_GATES.items():
                if ofile == name:
                    pred_names.add(oattr)
            for (rfile, g) in LIVE_GATE_READERS:
                if rfile != name:
                    continue
                for f2 in ast.walk(tree):
                    if (isinstance(f2, ast.FunctionDef)
                            and _gate_reads([f2], _parents([f2]))):
                        pred_names.add(f2.name)
            if called not in pred_names:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and (name, t.id) not in LIVE_GATE_SNAPSHOTS:
                    msgs.append("%s:%d %s = %s() — 게이트 판정을 모듈 전역에 "
                                "굳히는 스냅샷이다. 하위호환이 필요하면 "
                                "scripts/verify.py LIVE_GATE_SNAPSHOTS 에 사유를 적을 것"
                                % (name, node.lineno, t.id, called))
        # (b) 읽는 파일과 비교 규칙
        for gate, node in _gate_reads(body, parents):
            seen_gates.add(gate)
            key = (name, gate)
            if key not in LIVE_GATE_READERS:
                msgs.append("%s 가 %s 를 읽는다 — scripts/verify.py "
                            "LIVE_GATE_READERS 에 사유를 적을 것(실활성화 안전선을 "
                            "읽는 자리는 사람이 알아야 한다)" % (name, gate))
                continue
            seen_readers.add(key)
            if not _in_function(node, parents):
                continue                       # (a) 가 이미 보고했다
            want = set(LIVE_GATES[gate][0])
            got = _rule_words(node, parents, gate)
            if got is None:
                msgs.append("%s 의 %s 읽기에 비교가 없다 — 어떤 값을 참으로 보는지 "
                            "코드에서 확인할 수 없다" % (name, gate))
            elif got != want:
                msgs.append("%s 의 %s 판정 규칙 %s 가 등록 규칙 %s 와 다르다 — "
                            "자리마다 다르면 화면과 코드가 서로 다른 말을 한다"
                            % (name, gate, sorted(got), sorted(want)))
    # (c) 소유 술어
    for gate, (words, (ofile, oattr), why) in sorted(LIVE_GATES.items()):
        if gate not in seen_gates and (ofile, gate) not in LIVE_GATE_READERS:
            msgs.append("등록부 잔재(%s 를 읽는 곳이 없다)" % gate)
        tree = trees.get(ofile)
        if tree is None:
            msgs.append("등록부 잔재(소유 파일 없음): %s" % ofile)
            continue
        got = _owner_rule(tree, oattr)
        if got is None:
            msgs.append("등록부 잔재(%s 에 %s() 가 없다 — %s)" % (ofile, oattr, why))
            continue
        reads_env, got_words = got
        if not reads_env:
            msgs.append("%s:%s() 가 환경을 읽지 않는다 — 게이트 소유 술어가 아니다"
                        % (ofile, oattr))
        if got_words != set(words):
            msgs.append("%s:%s() 의 규칙 %s 가 등록 규칙 %s 와 다르다"
                        % (ofile, oattr, sorted(got_words), sorted(words)))
    # (d) 게이트 현황을 내보내는 자리
    for (vfile, vsym), why in sorted(LIVE_GATE_VIEWS.items()):
        tree = trees.get(vfile)
        if tree is None:
            msgs.append("등록부 잔재(파일 없음): %s" % vfile)
            continue
        view = _view_owners(tree, vsym)
        if not view:
            msgs.append("등록부 잔재(%s 에 %s 가 없다 — %s)" % (vfile, vsym, why))
            continue
        for gate, (words, owner, _why) in sorted(LIVE_GATES.items()):
            if gate not in view:
                msgs.append("%s:%s 에 %s 가 없다 — 현황을 못 내보낸다(%s)"
                            % (vfile, vsym, gate, why))
                continue
            vmod, vattr, vwords = view[gate]
            if (vmod, vattr) != owner:
                msgs.append("%s:%s 의 %s 소유자 %s.%s 가 등록 소유자 %s.%s 와 다르다 "
                            "— 화면이 코드와 다른 판정을 부른다"
                            % (vfile, vsym, gate, vmod, vattr, owner[0], owner[1]))
            if vwords != set(words):
                msgs.append("%s:%s 의 %s 폴백 규칙 %s 가 등록 규칙 %s 와 다르다 — "
                            "폴백이 규칙을 넓히면 화면이 코드와 다른 말을 한다"
                            % (vfile, vsym, gate, sorted(vwords), sorted(words)))
    stale = sorted(set(LIVE_GATE_READERS) - seen_readers)
    if stale:
        msgs.append("등록부 잔재(읽는 곳 없음): %s"
                    % ", ".join("%s 의 %s" % k for k in stale[:8]))
    for (sfile, sname) in sorted(LIVE_GATE_SNAPSHOTS):
        tree = trees.get(sfile)
        if tree is None or not re.search(r"^%s\s*=" % re.escape(sname),
                                         sources.get(sfile, ""), re.M):
            msgs.append("등록부 잔재(스냅샷 없음): %s 의 %s" % (sfile, sname))
    return (not msgs), (" / ".join(msgs[:8]) if msgs
                        else "게이트 %d개 · 읽는 곳 %d곳 확인"
                             % (len(LIVE_GATES), len(seen_readers)))


# --------------------------------------------------------------------------
# 19) 진행 중 상태의 시간 만료 (39차) — **상한만으로는 거짓말이 멈추지 않는다**
#
# `mem_caps`(12단계)는 저장소가 **무한히 자라지 않는가**를 본다. 그런데 "지금
# 진행 중"을 뜻하는 저장소는 상한을 지켜도 거짓을 말할 수 있다 — 끝난 지 몇
# 시간 지난 항목이 상한 안에 남아 있으면 그 숫자는 **현재 상태로 읽힌다**.
#   · `_call_metrics._Store._open` → `calls.in_progress`(콘솔 「진행 중」 타일)
#   · `_escalation.EscalationQueue._tickets` → `stats().queued`(「대기」 타일)
# 둘 다 39차에 실제로 틀려 있었다. 종료 웹훅이 유실되거나(앞) 집어가는 배선이
# 아직 없으면(뒤) 그 값은 인스턴스가 사는 동안 **단조 증가**한다. 30차가
# `voice._Session` 에 `SESSION_TTL` 을 준 뒤에도 같은 눈금을 쓰는 두 저장소는
# 받지 못했다 — 또 한 번의 '절반만 지켜진 규칙'이다.
#
# 리뷰에서 보이지 않는 모양이다. 만료는 **코드에 없는 것**이고, 테스트는 보통
# 짧은 시간 안에서 돌아 평상시에는 답이 같다.
#
# 그래서 센다(등록된 저장소만):
#  (a) 상한 등록부(`MEM_STORES`)에도 올라 있어야 한다 — 상한과 만료는 둘 다 필요하다.
#  (b) 만료 기준 기호가 모듈 전역에 **양수**로 있어야 한다.
#  (c) 그 기호를 읽는 **정리 함수**가 있어야 한다(판독 래퍼 한 홉까지 따라간다).
#  (d) 그 정리 함수가 같은 파일의 **다른 자리에서 실제로 불려야** 한다
#      (아무도 부르지 않는 정리 함수는 만료가 없는 것과 같다).
#
# 범위는 좁다(28·30~37차와 같은 이유 — 과차단되면 사람이 게이트를 끄고, 그러면
# 게이트가 없는 것과 같다): 등록부에 적은 저장소만 보고, 만료 **값**이 적절한지는
# 보지 않으며(정책이다), 호출 시점 판독(환경변수)은 `live_gates` 와 달리 요구하지
# 않는다. 새 '진행 중' 저장소를 만들면서 등록을 빼면 걸리지 않는다(자동 수집은
# 과차단이라 두지 않았다 — 한계로 적는다).
# --------------------------------------------------------------------------
STALE_STORES = {
    ("voice.py", "_mem"):
        ("SESSION_TTL", "sweep",
         "통화 세션 — 마지막 접촉 후 만료(30차). 만료가 없으면 통화 원문의 "
         "보관 기간이 '인스턴스 수명'이 된다"),
    ("_call_metrics.py", "_Store._open"):
        ("OPEN_TTL_SEC", "_sweep_open",
         "진행 중 통화 관측치 — `calls.in_progress` 가 '지금 통화 중'을 말해야 "
         "한다. 종료 웹훅 유실분이 영구히 남으면 동시 통화량이 과대보고된다"),
    ("_escalation.py", "EscalationQueue._tickets"):
        ("OPEN_TTL_SEC", "sweep",
         "미처리 전환 티켓 — `queued` 가 '지금 기다리는 고객'을 말해야 한다. "
         "배정 배선이 없는 동안 모든 티켓이 대기로 남았다"),
}


def _ast_names(node):
    import ast
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _ast_called(node):
    """호출된 이름들 — `f()`·`self.f()`·`cls.f()` 를 모두 이름으로 센다."""
    import ast
    out = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def _positive_const(tree, symbol):
    """모듈 전역 `SYMBOL = <양수>` 인가(없으면 None)."""
    import ast
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets):
            continue
        v = node.value
        if (isinstance(v, ast.Constant) and isinstance(v.value, (int, float))
                and not isinstance(v.value, bool)):
            return v.value > 0
        return False
    return None


def check_stale_state(sources):
    import ast
    msgs, seen = [], set()
    for (fname, store), (ttl, sweep, why) in sorted(STALE_STORES.items()):
        text = sources.get(fname)
        if text is None:
            msgs.append("등록부 잔재(파일 없음): %s" % fname)
            continue
        try:
            tree = ast.parse(text)
        except Exception:                      # noqa: BLE001
            continue                           # 문법 오류는 py_compile 게이트가 잡는다
        if store not in {n for n, _l in _mem_targets(text)}:
            msgs.append("등록부 잔재(저장소 없음): %s 의 %s" % (fname, store))
            continue
        seen.add((fname, store))
        if (fname, store) not in MEM_STORES:
            msgs.append("%s 의 %s 는 상한 등록부(MEM_STORES)에도 있어야 한다 — "
                        "상한과 만료는 둘 다 필요하다(%s)" % (fname, store, why))
        pos = _positive_const(tree, ttl)
        if pos is None:
            msgs.append("%s 에 만료 기준 %s 가 없다 — 등록부와 코드가 어긋났다(%s)"
                        % (fname, ttl, why))
            continue
        if not pos:
            msgs.append("%s 의 %s 가 양수가 아니다 — 만료가 사라지는 쪽으로 "
                        "실패하지 않게 한다(%s)" % (fname, ttl, why))
        reads = _reads_symbol(tree, sweep, ttl)
        if reads is None:
            msgs.append("%s 에 정리 함수 %s() 가 없다 — 상한만으로는 '진행 중'이 "
                        "거짓을 말한다(%s)" % (fname, sweep, why))
            continue
        if not reads:
            msgs.append("%s 의 %s() 가 만료 기준 %s 를 읽지 않는다(%s)"
                        % (fname, sweep, ttl, why))
        if not _called_elsewhere(tree, sweep):
            msgs.append("%s 의 %s() 를 아무도 부르지 않는다 — 부르지 않는 정리 "
                        "함수는 만료가 없는 것과 같다(%s)" % (fname, sweep, why))
    stale = sorted(set(STALE_STORES) - seen)
    return (not msgs), (" / ".join(msgs[:8]) if msgs
                        else "진행 중 저장소 %d개 만료 확인" % len(seen))


def _reads_symbol(tree, func_name, symbol):
    """`func_name()` 이 `symbol` 을 읽는가. 없으면 None.

    판독 래퍼를 **한 홉**까지 따라간다(`_sweep_open` → `open_ttl()` →
    `OPEN_TTL_SEC`). 더 깊이 따라가지 않는다 — 과차단보다 얕은 그물이 낫고,
    동작은 `tests/test_stale_state.py` 의 회귀가 지킨다.
    """
    import ast
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    targets = [f for f in funcs if f.name == func_name]
    if not targets:
        return None
    by_name = {}
    for f in funcs:
        by_name.setdefault(f.name, []).append(f)
    for f in targets:
        if symbol in _ast_names(f):
            return True
        for callee in _ast_called(f):
            if any(symbol in _ast_names(g) for g in by_name.get(callee, [])):
                return True
    return False


def _called_elsewhere(tree, func_name):
    """같은 파일의 **다른 함수**가 `func_name` 을 부르는가."""
    import ast
    for f in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if f.name == func_name:
            continue
        if func_name in _ast_called(f):
            return True
    return False


# --------------------------------------------------------------------------
# 20) host_bypass (41차) — **주소 표기 우회 기록부**
#
# 15단계가 낱말에 대해 세운 규약("한 번 밝혀진 오탐은 전부에 적용한다")을
# **주소**에 대해 세운다. 41차 결함이 그 자리에서 났다 —
# `_urlguard.check()` 는 「루프백·사설·메타데이터로는 보낼 수 없다」고 적어 두고
# `https://metadata.google.internal./latest`(끝점 FQDN)와
# `https://169.254.43518/latest`(짧은 꼴 = 169.254.169.254)를 통과시켰다.
# 리졸버는 그 표기를 **같은 곳으로** 푼다. 즉 판정이 틀린 것이 아니라
# **판정 앞의 표기 정규화가 없었다.**
#
# 이 모듈은 안부 결과 웹훅·녹음 다운로드·Sentry DSN·주문 백엔드·보이스 엔진이
# 공용으로 쓴다(6단계 `URLGUARD_REQUIRED`). 한 곳의 구멍이 다섯 경로의 구멍이다.
#
# 그래서 센다: 아래 기록부의 주소를 **실제로 `check()` 에 넣어** 거부를 확인한다.
# 소스를 읽는 다른 단계와 달리 돌려 보는 이유는, 표기 우회는 코드 모양으로
# 드러나지 않기 때문이다(`endswith`·`ipaddress` 는 그대로 거기 있었다).
#
# 과차단을 함께 고정한다(`HOST_ALLOWED`) — 가드가 정상 주소를 막기 시작하면
# 사람이 가드를 끈다(28·30~40차와 같은 이유). 네트워크를 쓰지 않는다(DNS 조회 없음).
# --------------------------------------------------------------------------
HOST_BYPASS = {
    "https://127.0.0.1./x":
        "끝점(trailing dot) FQDN — 리졸버는 점 없는 것과 같은 곳으로 푼다",
    "https://localhost./x":
        "끝점 — BLOCKED_HOSTS 는 문자열 일치라 점 하나로 비켜간다",
    "https://metadata.google.internal./latest":
        "끝점 — `.internal` 접미 검사(endswith)가 성립하지 않는다",
    "https://169.254.169.254./x":
        "끝점 — 클라우드 메타데이터 주소",
    "https://127.1/x":
        "짧은 꼴 IPv4 — inet_aton 이 127.0.0.1 로 푼다",
    "https://10.1/x":
        "짧은 꼴 — 10.0.0.1(사설)",
    "https://169.254.43518/latest":
        "3칸 꼴 — 169.254.169.254(메타데이터)",
    "https://0x7f.1/x":
        "16진 + 짧은 꼴 — 127.0.0.1",
    "https://2130706433/x":
        "10진 32비트 — 127.0.0.1",
    "https://0177.0.0.1/x":
        "8진 — 127.0.0.1",
    "https://x.localhost/x":
        "RFC 6761 — `.localhost` 는 리졸버가 루프백으로 보낸다",
    "https://[::1]/x":
        "IPv6 루프백",
    "https://[::ffff:127.0.0.1]/x":
        "IPv4-mapped IPv6 — 127.0.0.1",
    "https://a..b/x":
        "빈 라벨 — 어떤 리졸버도 풀지 않는다. 해석 실패를 통과로 바꾸지 않는다",
    "http://example.org/x":
        "평문 http — 기본값에서는 거부(개발 플래그는 호출부가 넘긴다)",
    "file:///etc/passwd":
        "스킴 우회 — 로컬 파일 읽기",
}
HOST_ALLOWED = {
    "https://eum.gowon.co.kr/api/channel/aicc/request": "이음 수신(실제 연동 대상)",
    "https://123.example.com/x": "숫자 라벨이 섞인 정상 도메인",
    "https://hooks.example.org:8443/cb": "비표준 포트 정상 주소",
    "https://8.8.8.8/x": "공개 IP 리터럴",
    "https://999.1/x": "범위를 넘긴 숫자 — IP 표기가 아니다(도메인으로 본다)",
    "https://1.2.3.4.5/x": "라벨 5개 — inet_aton 도 IP 로 읽지 않는다",
}


def _load_urlguard():
    """`api/_urlguard.py` 를 파일 경로로 불러온다(의존성 0 모듈이라 안전하다)."""
    import importlib.util
    path = os.path.join(API_DIR, "_urlguard.py")
    try:
        spec = importlib.util.spec_from_file_location("_verify_urlguard", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, ""
    except Exception as e:                         # noqa: BLE001
        return None, type(e).__name__


def check_host_bypass(mod=None):
    """기록부의 표기 우회가 **실제로** 거부되는가 + 정상 주소는 통과하는가.

    `mod` 는 회귀 테스트가 **고치기 전 판정기**를 끼워 보는 자리다 — 게이트가
    그 결함을 실제로 잡는지 확인하려면 판정기를 바꿔 끼울 수 있어야 한다
    (저장소 파일은 건드리지 않는다. 21차 사고 뒤의 규약).
    """
    err = ""
    if mod is None:
        mod, err = _load_urlguard()
    if mod is None:
        return False, "api/_urlguard.py 를 불러올 수 없다(%s)" % err
    if not hasattr(mod, "normalize_host"):
        return False, "_urlguard.normalize_host 가 없다(표기 정규화 배선이 사라졌다)"
    msgs = []
    for url, why in sorted(HOST_BYPASS.items()):
        try:
            ok = bool(mod.check(url)[0])
        except Exception as e:                     # noqa: BLE001
            msgs.append("%s 판정 중 예외(%s)" % (url, type(e).__name__))
            continue
        if ok:
            msgs.append("우회 통과: %s — %s" % (url, why))
    for url, why in sorted(HOST_ALLOWED.items()):
        try:
            ok, reason = mod.check(url)
        except Exception as e:                     # noqa: BLE001
            msgs.append("%s 판정 중 예외(%s)" % (url, type(e).__name__))
            continue
        if not ok:
            msgs.append("과차단: %s 가 막힌다(%s) — %s" % (url, reason, why))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "표기 우회 %d건 거부 · 정상 주소 %d건 통과"
                             % (len(HOST_BYPASS), len(HOST_ALLOWED)))


# --------------------------------------------------------------------------
# 21) 감사 결과 (42차) — 감사 기록이 **응답과 같은 말**을 하는가
#
# 5차가 `ops_stats` 에서 고친 결함이 두 라우트(`wellbeing`·`voice`)에 그대로
# 남아 있었다: 접근 가드를 통과한 **직후** `allow/200` 을 적었으므로, 실발신
# 차단(501)·콜백 전송 실패(502)·입력오류(400)·과대본문(413)·미보관 조회(404)·
# 내부오류(500)가 감사 스트림에서 **전부 성공**으로 보였다. 감사 장부는
# 「누가 외부로 보냈나 · 누가 실발신을 시도했나」의 근거인데, 그 장부가 결과를
# 거짓으로 적고 있으면 조사에 쓸 수 없다.
#
# 리뷰에서 보이지 않는 모양이다 — 기록 한 줄은 멀쩡하고 **놓인 자리**가 틀렸다.
# 그래서 센다(20단계와 같은 방식: 규칙은 자리를, 판정은 실행으로 확인한다).
#  (a) **성공을 미리 적지 않는다** — 감사 호출의 결과 인자가 `"allow"` 리터럴이면,
#      같은 함수 안에서 그 앞에 응답 전송(`_send`·`_send_xml`·`send_response`)이
#      있어야 한다. 거부(`deny`)는 가드 판정 그 자체라 앞에 와도 된다.
#  (b) **실패를 뭉개지 않는다** — 결과 인자에 `"error"` 리터럴을 쓰지 않는다.
#      상태코드가 정하게 한다(`_audit.result_for`). 42차 전까지 관리 장부 넷은
#      입력오류 400 까지 `error` 로 적었고, 그러면 사용자 오타가 `/health.audit`
#      의 error 칸을 올려 **진짜 5xx 가 그 안에 묻힌다**(18차 규약의 감사판).
#  (c) `_audit.result_for` 가 실제로 2xx·3xx=allow · 4xx=deny · 5xx=error 를
#      돌려주는지 **돌려 본다**(해석 불가는 error — 모르는 것을 성공으로 적지 않는다).
#  (d) 등록부 `AUDIT_RESULT_USERS` 와 배선이 **양방향으로** 맞아야 한다.
#
# 범위는 좁다(28·30~41차와 같은 이유 — 과차단되면 사람이 게이트를 끈다):
# 라우트 파일(밑줄 없음·`class handler` 있음·감사 배선 있음)의 함수 본문만 보고,
# 감사 호출은 이름으로 고른다. 셀프테스트 블록은 보지 않는다.
# --------------------------------------------------------------------------
AUDIT_RESULT_USERS = {
    "ops_stats.py": "집계·전환 티켓 조회 — 실패 상태를 그대로 적는다",
    "caller_id.py": "발신번호 대장(실발신 승인 근거) — 입력오류는 deny",
    "partners.py": "파트너 귀속 장부(정산 분쟁 근거)",
    "settlement.py": "요율·과금 명세(청구 근거)",
    "disclosure.py": "AI 고지 문구(법정 고지 변경)",
    "voice.py": "통화 웹훅·캠페인 발신 시도",
    "wellbeing.py": "안부 실행·판정 근거 열람·실발신 차단",
    "health.py": "deep 점검(외부 도달성 시도) — 성공·실패 모두",
}
# 감사 기록 호출로 보는 이름(속성 호출은 끝 이름으로 본다).
AUDIT_SINKS = ("record", "record_request", "_audit_ev", "_audit_safe",
               "_audit_fail", "_audit", "_audit_deep", "_outcome")
# 응답 본문을 실제로 쓰는 호출. `deny` 는 넣지 않는다 — 거부는 가드가 끝낸 요청이고,
# 그 자리가 성공 기록보다 앞에 있다고 해서 성공이 응답 뒤에 적힌 것은 아니다.
AUDIT_SENDS = ("_send", "_send_xml", "_send_csv", "send_response")
# (c) 판정 표 — 상태코드 → 라벨.
AUDIT_RESULT_CASES = (
    (200, "allow"), (201, "allow"), (204, "allow"), (302, "allow"),
    (400, "deny"), (401, "deny"), (403, "deny"), (404, "deny"),
    (413, "deny"), (429, "deny"), (499, "deny"),
    (500, "error"), (501, "error"), (502, "error"), (504, "error"),
    (None, "error"), ("알수없음", "error"), (0, "error"),
)


def _load_api_module(fname, alias):
    """api/<fname> 을 **실행해** 판정 함수를 부를 수 있게 올린다(20단계와 같은 방식)."""
    import importlib.util
    path = os.path.join(API_DIR, fname)
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, ""
    except Exception as e:                         # noqa: BLE001
        return None, type(e).__name__


def _audit_route_files(sources):
    """감사를 배선한 라우트 파일 목록(9단계와 같은 선별 규칙)."""
    out = []
    for name, text in sorted(sources.items()):
        if name.startswith("_") or "class handler" not in text:
            continue
        if not any(c in _code(text) for c in AUDIT_CALLS):
            continue
        out.append(name)
    return out


def _called_tail(node):
    """Call 노드의 호출 이름(마지막 토큰). `self._audit(...)` → `_audit`."""
    import ast
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def _literal_args(node):
    """Call 의 **직접** 인자 리터럴(중첩 표현식은 보지 않는다) → (문자열집합, 정수집합)."""
    import ast
    words, nums = set(), set()
    for a in list(node.args) + [k.value for k in node.keywords]:
        if not isinstance(a, ast.Constant):
            continue
        if isinstance(a.value, bool):
            continue                           # live=False 같은 사실 서술 칸
        if isinstance(a.value, str):
            words.add(a.value)
        elif isinstance(a.value, int):
            nums.add(a.value)
    return words, nums


def check_audit_outcome(sources, mod=None):
    """감사 결과가 응답과 같은 말을 하는가. `mod` 는 회귀가 판정기를 끼우는 자리."""
    import ast
    msgs, used = [], set()
    for name in _audit_route_files(sources):
        try:
            tree = ast.parse(sources[name])
        except Exception:                          # noqa: BLE001
            continue                               # 문법 오류는 py_compile 게이트가 잡는다
        for fn in [n for n in ast.walk(ast.Module(body=_strip_selftest(tree),
                                                  type_ignores=[]))
                   if isinstance(n, ast.FunctionDef)]:
            sends = [c.lineno for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and _called_tail(c) in AUDIT_SENDS]
            for c in ast.walk(fn):
                if not isinstance(c, ast.Call) or _called_tail(c) not in AUDIT_SINKS:
                    continue
                words, nums = _literal_args(c)
                if "error" in words:
                    msgs.append("%s:%d %s(…, \"error\", …) — 실패 라벨을 손으로 적었다. "
                                "상태코드가 정하게 할 것(_audit.result_for) — 4xx 를 "
                                "error 로 적으면 진짜 5xx 가 묻힌다"
                                % (name, c.lineno, _called_tail(c)))
                # 성공을 적는 자리는 둘 중 하나로 드러난다 — 라벨 리터럴(`"allow"`)
                # 이거나 **상태 리터럴이 2xx·3xx** 다(라벨이 헬퍼 안에 숨은 경우).
                if "allow" not in words and not any(200 <= n < 400 for n in nums):
                    continue
                if not any(s < c.lineno for s in sends):
                    msgs.append("%s:%d %s 가 응답 **전에** allow 를 적는다 — 그 뒤에 "
                                "나올 400·501·502·500 이 전부 성공으로 남는다"
                                "(5차 결함의 모양). 결과가 확정된 뒤에 적을 것"
                                % (name, c.lineno, fn.name))
        if "result_for" in _code(sources[name]):
            used.add(name)
    missing = sorted(set(AUDIT_RESULT_USERS) - used)
    if missing:
        msgs.append("상태→라벨 판정 배선이 없다: %s (_audit.result_for 를 쓸 것)"
                    % ", ".join(missing))
    stale = sorted(used - set(AUDIT_RESULT_USERS))
    if stale:
        msgs.append("등록부에 없는 사용처: %s (AUDIT_RESULT_USERS 에 사유를 적을 것)"
                    % ", ".join(stale))
    err = ""
    if mod is None:
        mod, err = _load_api_module("_audit.py", "_verify_audit")
    if mod is None:
        msgs.append("api/_audit.py 를 불러올 수 없다(%s)" % err)
    elif not hasattr(mod, "result_for"):
        msgs.append("_audit.result_for 가 없다(상태→라벨 판정이 사라졌다)")
    else:
        for status, want in AUDIT_RESULT_CASES:
            try:
                got = mod.result_for(status)
            except Exception as e:                 # noqa: BLE001
                msgs.append("result_for(%r) 판정 중 예외(%s)" % (status, type(e).__name__))
                continue
            if got != want:
                msgs.append("result_for(%r) = %r — %r 이어야 한다" % (status, got, want))
    return (not msgs), (" / ".join(msgs) if msgs
                        else "감사 라우트 %d개 · 상태→라벨 %d건 확인"
                             % (len(AUDIT_RESULT_USERS), len(AUDIT_RESULT_CASES)))


def _emit(text, as_json=False):
    """게이트 출력 — 소비자가 둘이다(사람이 읽는 콘솔 · JSON 을 파싱하는 기계).

    38차 결함: `--json` 을 콘솔 인코딩으로 내보내고 있었다. Windows 콘솔은
    cp949 라서 한글 `detail` 이 cp949 바이트로 나가고, UTF-8 로 읽는 소비자
    (CI·`tests/test_live_gates.py`)는 `UnicodeDecodeError` 로 **게이트 결과를
    아예 읽지 못했다**. JSON 은 RFC 8259 가 UTF-8 로 못박았으므로 콘솔
    코드페이지와 무관하게 UTF-8 로 쓴다.

    사람용 출력은 콘솔 인코딩을 그대로 쓴다(cp949 콘솔에서 한글이 보여야
    한다). 다만 표현 못 하는 글자가 섞여도 게이트가 트레이스백으로 죽지는
    않게 한다 — 게이트가 침묵하는 것보다 글자가 깨지는 것이 낫다.
    """
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:                 # 가로챈 stdout(StringIO 등) — 그대로 쓴다
        sys.stdout.write(text + "\n")
        return
    enc = "utf-8" if as_json else (getattr(sys.stdout, "encoding", None) or "utf-8")
    try:
        sys.stdout.flush()
    except Exception:               # pragma: no cover - 닫힌 스트림
        pass
    buf.write((text + "\n").encode(enc, "backslashreplace"))
    buf.flush()


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
    r_ok, r_msg = check_spoken_rules(srcs)
    m_ok, m_msg = check_mem_caps(srcs)
    hc_ok, hc_msg = check_history_caps(srcs)
    sp_ok, sp_msg = check_speech_rules(srcs)
    kt_ok, kt_msg = check_keyword_tables(srcs)
    ji_ok, ji_msg = check_judge_inputs(srcs)
    ce_ok, ce_msg = check_counter_exposure(srcs)
    lg_ok, lg_msg = check_live_gates(srcs)
    ss_ok, ss_msg = check_stale_state(srcs)
    hb_ok, hb_msg = check_host_bypass()
    ao_ok, ao_msg = check_audit_outcome(srcs)

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
        {"step": "spoken_rules", "ok": r_ok, "detail": r_msg},
        {"step": "mem_caps", "ok": m_ok, "detail": m_msg},
        {"step": "history_caps", "ok": hc_ok, "detail": hc_msg},
        {"step": "speech_rules", "ok": sp_ok, "detail": sp_msg},
        {"step": "keyword_tables", "ok": kt_ok, "detail": kt_msg},
        {"step": "judge_inputs", "ok": ji_ok, "detail": ji_msg},
        {"step": "counter_exposure", "ok": ce_ok, "detail": ce_msg},
        {"step": "live_gates", "ok": lg_ok, "detail": lg_msg},
        {"step": "stale_state", "ok": ss_ok, "detail": ss_msg},
        {"step": "host_bypass", "ok": hb_ok, "detail": hb_msg},
        {"step": "audit_outcome", "ok": ao_ok, "detail": ao_msg},
    ]
    ok = all(s["ok"] for s in steps)
    if a.json:
        _emit(json.dumps({"ok": ok, "steps": steps}, ensure_ascii=False, indent=2),
              as_json=True)
    else:
        for s in steps:
            _emit("%s %-14s %s" % ("PASS" if s["ok"] else "FAIL",
                                   s["step"], s["detail"]))
        _emit("---")
        _emit("통과" if ok else "실패")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
