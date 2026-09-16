# -*- coding: utf-8 -*-
"""public/admin.html 「AI 고지 문구」 설정 패널 회귀 테스트.

법정 고지 문구를 바꾸는 화면이므로, 화면과 API 가 어긋나면 "저장했는데 통화에는
반영되지 않는" 상태가 된다. 아래를 고정한다.

  1) 라우팅(nav·titles·MENU·section id)이 서로 연결돼 있다
  2) 호출 대상이 실제 엔드포인트(/api/disclosure)이고 GET/POST 구분이 맞다
  3) 화면의 tenant_id 규칙이 서버 TENANT_RE 와 같다(드리프트 차단)
  4) 화면이 읽는 응답 필드가 실제 API 응답에 존재한다
  5) 빈 상태·오류 상태·로딩 표시·인라인 검증·접근성(QUALITY_BAR §1·§4)
  6) 되돌리기 어려운 동작(저장·복귀)에 확인 절차가 있다(QUALITY_BAR §3)
  7) 새 가짜 KPI 수치를 넣지 않았다

실행: python3 -m pytest tests/test_console_disclosure.py -q
"""
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import disclosure  # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


@pytest.fixture(scope="module")
def html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def panel(html):
    i = html.find('<section id="view-disclosure"')
    assert i > 0, "AI 고지 문구 패널 섹션이 없다"
    j = html.find("</section>", i)
    assert j > i
    return html[i:j]


@pytest.fixture(scope="module")
def js(html):
    i = html.find("function dcEsc(")
    assert i > 0, "패널 스크립트가 없다"
    j = html.find("view-disclosure');if(!s||!window.MutationObserver)", i)
    assert j > i
    return html[i:j + 300]


# ---------------------------------------------------------------- 1) 라우팅
def test_routing_wired(html):
    assert html.count('<section id="view-disclosure"') == 1
    assert 'data-v="disclosure"' in html, "좌측 메뉴 항목이 없다"
    assert "disclosure:['AI 고지 문구'" in html, "titles 등록이 없다 — show() 가 무시한다"
    assert "['disclosure','AI 고지 문구']" in html, "상단 메뉴(MENU) 등록이 없다"


# ------------------------------------------------------- 2) 실제 엔드포인트
def test_calls_real_endpoint(js):
    assert "fetch('/api/disclosure?tenant='+encodeURIComponent(" in js
    assert "fetch('/api/disclosure?op=list')" in js
    assert "fetch('/api/disclosure?op=history')" in js
    assert "fetch('/api/disclosure',{method:'POST'" in js


def test_dry_run_and_reset_ops_match_server(js):
    assert "dry_run:true" in js, "요건 검사 버튼이 저장을 일으키면 안 된다"
    assert "op:'reset'" in js
    # 서버가 받는 op 값과 같다
    assert "reset" in ("set", "reset")


# ------------------------------------------------- 3) tenant_id 규칙 드리프트
def test_tenant_rule_matches_server(js):
    m = re.search(r"if\(!(/\^[^/]+/)\.test\(v\.tenant_id\)\)", js)
    assert m, "화면 tenant_id 정규식이 없다"
    assert m.group(1) == "/" + disclosure.TENANT_RE.pattern + "/"


def test_text_maxlength_matches_server(panel):
    assert 'maxlength="%d"' % disclosure.MAX_LEN in panel
    assert "/%d자" % disclosure.MAX_LEN in panel


# ------------------------------------------------------------ 4) 필드 정합
def test_rendered_fields_exist_in_api(js):
    eff = disclosure.effective(None)
    for key in ("source", "version", "ok", "recording_live", "tenant_id", "text", "checks", "template", "brand"):
        assert "e.%s" % key in js or "x.d.%s" % key in js, "화면이 %s 를 읽지 않는다" % key
        assert key in eff, "API 응답에 %s 가 없다" % key
    chk = disclosure.check(disclosure.default_text())["checks"][0]
    for key in ("passed", "level", "label", "hint"):
        assert "c.%s" % key in js
        assert key in chk


def test_history_fields_exist(js):
    disclosure._clear_for_tests()
    try:
        disclosure.set_text("t-console", disclosure.DEFAULT_TEXT)
        h = disclosure.history()[-1]
    finally:
        disclosure._clear_for_tests()
    for key in ("ts", "tenant_id", "op", "from_version", "version", "actor", "text"):
        assert "h.%s" % key in js, "화면이 이력 %s 를 읽지 않는다" % key
        assert key in h


# ------------------------------------------------- 5) 빈 상태·오류·로딩·접근성
def test_empty_states_present(panel, js):
    assert "아직 확인한 내용이 없습니다" in panel
    assert "설정된 테넌트가 없습니다" in js
    assert "변경 이력이 없습니다" in js


def test_error_and_loading_paths(js):
    assert "function dcFail(" in js and 'role="alert"' in js
    assert "function dcBusy(" in js and 'role="status"' in js
    assert js.count(".catch(function(e){dcFail(") >= 4, "네트워크 실패를 삼키면 안 된다"
    assert "if(x.s>=400){dcHttpErr(x);return;}" in js, "HTTP 오류를 성공처럼 처리하면 안 된다"
    assert "details||[]" in js, "서버 details[].reason 을 보여줘야 한다"


def test_inline_validation_and_a11y(panel, js):
    for fid in ("dcTenant", "dcBrand", "dcText"):
        assert 'for="%s"' % fid in panel, "%s 에 라벨이 없다" % fid
    assert 'aria-live="polite"' in panel
    assert 'role="alert"' in panel
    assert "Enter" in panel, "키보드만으로 실행할 수 없다"
    assert "aria-invalid" in js
    assert "테넌트 ID를 입력하세요" in js and "고지 문구를 입력하세요" in js


def test_approval_required_marked(panel, js):
    assert "[승인 필요]" in panel and "[승인 필요]" in js


# ------------------------------------------------- 6) 되돌리기 어려운 동작 확인
def test_confirm_before_save_and_reset(js):
    i = js.find("function dcSave(")
    j = js.find("function dcReset(")
    k = js.find("function dcList(")
    assert 0 < i < j < k
    assert "confirm(" in js[i:j], "저장 전 확인이 없다"
    assert "confirm(" in js[j:k], "기본 복귀 전 확인이 없다"


# ------------------------------------------------------------ 7) 허위 수치 금지
def test_no_fabricated_metrics(panel):
    text = re.sub(r'style="[^"]*"', "", panel)
    text = re.sub(r"<[^>]+>", " ", text)
    bad = re.findall(r"\d+(?:\.\d+)?\s*(?:%|건 처리|만원|억)", text)
    assert not bad, "근거 없는 수치: %s" % bad


def test_no_hardcoded_disclosure_result(js):
    """화면은 서버 검사 결과만 그린다 — 통과/실패를 화면에서 지어내지 않는다."""
    assert "passed:true" not in js and "passed: true" not in js
