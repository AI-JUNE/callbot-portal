# -*- coding: utf-8 -*-
"""public/admin.html 「안부 전화 연동」 시연 패널 회귀 테스트.

심사관이 전화 없이 흐름을 확인하는 화면이므로, 화면과 API 가 어긋나면
"버튼은 있는데 아무 일도 안 일어나는" 상태가 된다. 아래를 고정한다.

  1) 패널이 존재하고 라우팅(nav·titles·section id)이 서로 연결돼 있다
  2) 호출 대상이 실제 배포 엔드포인트(/api/wellbeing/call)다
  3) 화면의 프로필 목록이 wellbeing.PROFILES 와 정확히 일치한다(드리프트 차단)
  4) 화면이 읽는 필드가 실제 페이로드 키에 존재한다
  5) 빈 상태·오류 상태·로딩 표시가 있다(QUALITY_BAR §1)
  6) 새 가짜 KPI 수치를 넣지 않았다(2R 가이드 §8)

실행: python3 -m pytest tests/test_console_wellbeing.py -q
"""
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import wellbeing  # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


@pytest.fixture(scope="module")
def html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def panel(html):
    """패널 섹션 본문만 잘라낸다 — 다른 화면의 문자열에 속지 않기 위해."""
    i = html.find('<section id="view-wellbeing"')
    assert i > 0, "안부 시연 패널 섹션이 없다"
    j = html.find("</section>", i)
    assert j > i
    return html[i:j]


# ---------------------------------------------------------------- 1) 라우팅
def test_routing_wired(html):
    assert html.count('<section id="view-wellbeing"') == 1
    assert 'data-v="wellbeing"' in html, "좌측 메뉴 항목이 없다"
    assert "wellbeing:['안부 전화 연동'" in html, "titles 등록이 없다 — show() 가 무시한다"


def test_show_function_requires_both(html):
    """show() 는 titles 와 section 이 모두 있어야 화면을 바꾼다."""
    assert "if(!titles[v]||!document.getElementById('view-'+v))return;" in html


# ------------------------------------------------------- 2) 실제 엔드포인트
def test_calls_real_endpoint(html):
    assert "'/api/wellbeing/call'" in html, "호출 대상이 실제 엔드포인트가 아니다"
    assert "'/api/wellbeing?op=recent'" in html
    assert "method:'POST'" in html


def test_no_mocked_payload_in_panel(panel):
    """패널이 하드코딩 페이로드를 그리지 않는다 — 화면은 서버 응답만 보여준다."""
    assert "mood_score:" not in panel
    assert "risk_level:" not in panel


# --------------------------------------------------- 3) 프로필 목록 드리프트
def test_profile_options_match_server(panel):
    opts = set(re.findall(r'<option value="([^"]+)"', panel))
    assert opts == set(wellbeing.PROFILES), (
        "화면 프로필과 서버 PROFILES 가 다르다: %s vs %s"
        % (sorted(opts), sorted(wellbeing.PROFILES)))


def test_profile_options_are_accepted_by_server(panel):
    """각 옵션이 실제로 실행 가능해야 한다(dry-run, 네트워크 미사용)."""
    for p in re.findall(r'<option value="([^"]+)"', panel):
        out = wellbeing.run_wellbeing("SR-TEST", None, profile=p)
        assert out["ok"] is True
        assert out["payload"]["schema"] == wellbeing.SCHEMA


# ------------------------------------------------------------ 4) 필드 정합
def test_rendered_fields_exist_in_payload(html):
    payload = wellbeing.run_wellbeing("SR-TEST", None, profile="watch")["payload"]
    for key in ("risk_level", "mood_score", "answered",
                "transcript_summary", "dimensions", "raw_ref"):
        assert "p.%s" % key in html, "화면이 %s 를 읽지 않는다" % key
        assert key in payload, "페이로드에 %s 가 없다" % key


def test_risk_labels_cover_all_levels(html):
    i = html.find("function wbRiskTag(")
    assert i > 0
    fn = html[i:i + 400]
    for lv in (wellbeing.RISK_LOW, wellbeing.RISK_MID,
               wellbeing.RISK_HIGH, wellbeing.RISK_UNKNOWN):
        assert lv in fn, "위험도 %s 에 대한 라벨이 없다" % lv


# ------------------------------------------------- 5) 빈 상태·오류·로딩·접근성
def test_empty_state_present(panel):
    assert "아직 실행한 건이 없습니다" in panel


def test_error_and_loading_paths(html):
    assert "function wbFail(" in html and 'role="alert"' in html
    assert "function wbBusy(" in html and 'role="status"' in html
    assert ".catch(function(e){wbFail(" in html, "네트워크 실패를 삼키면 안 된다"
    assert "if(x.s>=400)" in html, "HTTP 오류를 성공처럼 처리하면 안 된다"


def test_approval_required_marked_for_live(html):
    assert "[승인 필요]" in html, "실회선(501) 안내에 [승인 필요] 표기가 필요하다"


def test_accessibility_labels(panel):
    for fid in ("wbSenior", "wbProfile", "wbCallback"):
        assert 'for="%s"' % fid in panel, "%s 에 라벨이 없다" % fid
    assert 'aria-live="polite"' in panel
    assert "Enter" in panel, "키보드만으로 실행할 수 없다"


def test_inline_validation_present(html):
    assert "대상자 ID를 입력하세요" in html
    assert "aria-invalid" in html
    assert "https:// 로 시작해야 합니다" in html


# ------------------------------------------------------------ 6) 허위 수치 금지
def test_no_fabricated_metrics(panel):
    """패널에 근거 없는 성과 수치(건수·%·원)를 넣지 않는다."""
    # style/CSS 는 제외한다 — width:100% 같은 레이아웃 값은 성과 수치가 아니다.
    text = re.sub(r'style="[^"]*"', "", panel)
    text = re.sub(r"<[^>]+>", " ", text)
    bad = re.findall(r"\d+(?:\.\d+)?\s*(?:%|건 처리|만원|억)", text)
    assert not bad, "근거 없는 수치로 보이는 표기: %s" % bad


def test_escaping_helper_used(html):
    """서버 응답을 innerHTML 로 그리므로 이스케이프가 필수."""
    i = html.find("function wbEsc(")
    assert i > 0
    fn = html[i:i + 200]
    for ch in ("&", "<", ">"):
        assert ch in fn
    assert "wbEsc(JSON.stringify(p,null,2))" in html
