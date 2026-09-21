# -*- coding: utf-8 -*-
"""public/admin.html 「정산 리포트」 화면 회귀 테스트.

이 화면은 돈 이야기를 그린다. 그래서 검사 기준이 다른 화면보다 하나 더 있다:
**화면이 스스로 숫자를 만들면 안 된다.** 값이 없으면 0 이 아니라 '—' 이고,
요율이 없으면 금액 칸은 비어 있어야 한다.

  1) 메뉴·화면이 연결돼 있고 화면 진입 시 조회된다
  2) 호출 대상이 실제 엔드포인트(/api/settlement)이고 읽기 전용이다
  3) 화면이 읽는 필드가 서버 응답 키에 실제로 있다(드리프트 차단)
  4) 빈 상태·로딩(role=status)·오류(role=alert)·인라인 검증·aria-live·라벨
  5) 이스케이프 — 서버 문자열을 innerHTML 에 그대로 넣지 않는다
  6) **하드코딩된 금액·요율이 없다**(§8) — 숫자는 서버 응답에서만 온다

실행: python3 -m pytest tests/test_console_settlement.py -q
"""
import io
import os
import re
import sys
import json
import calendar

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import partners            # noqa: E402
import settlement as st    # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")
AUG10 = float(calendar.timegm((2026, 8, 10, 3, 0, 0, 0, 0, 0)))
SEP05 = float(calendar.timegm((2026, 9, 5, 3, 0, 0, 0, 0, 0)))


@pytest.fixture(scope="module")
def html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def js(html):
    """정산 화면 스크립트만 잘라낸다 — 다른 화면 코드에 속지 않기 위해."""
    i = html.find("/* ---- 정산 리포트(파트너)")
    assert i > 0, "정산 화면 렌더 코드가 없다"
    j = html.find("function ciEsc(", i)
    assert j > i
    return html[i:j]


@pytest.fixture(scope="module")
def sample():
    """서버가 실제로 내주는 리포트 — 화면이 읽는 키를 여기에 대조한다."""
    partners._clear_for_tests()
    st._clear_for_tests()
    try:
        partners.create_partner("ch-alpha", "알파채널", now=AUG10 - 40 * 86400)
        partners.attach("acme", "partner_managed", "ch-alpha",
                        contracted_at=AUG10 - 30 * 86400, now=AUG10 - 30 * 86400)
        st.record_usage("acme", ts=AUG10, calls=10, minutes=20.0, revenue_krw=100000)
        st.record_usage("ghost", ts=AUG10, calls=1)
        st.set_rate_card({"version": "t", "rates": {"ch-alpha/*": {"commission_pct": 10}}})
        rep = st.report("2026-08", now=SEP05)
        cov = st.usage_coverage("2026-08", now=SEP05)
        return rep, cov
    finally:
        st._clear_for_tests()
        partners._clear_for_tests()


# ---------------------------------------------------------------- 1) 배선
def test_menu_and_view_exist_once(html):
    assert html.count('data-v="settle"') == 1
    assert html.count('id="view-settle"') == 1
    assert "['settle','정산 리포트']" in html
    assert "settle:['정산 리포트'" in html


def test_controls_live_inside_the_view(html):
    i = html.find('<section id="view-settle"')
    j = html.find("</section>", i)
    assert i > 0
    for el in ('id="slMonth"', 'id="slPartner"', 'id="slOut"',
               'id="slRunBtn"', 'id="slCsvBtn"', 'id="slRateBtn"', 'id="slCovBtn"'):
        assert i < html.find(el) < j, el


def test_loaded_on_view_open(js):
    assert "view-settle" in js and "slRun();" in js, "화면 진입 시 조회되지 않는다"


def test_enter_key_runs_report(html):
    i = html.find('<section id="view-settle"')
    j = html.find("</section>", i)
    seg = html[i:j]
    assert seg.count("event.key==='Enter'") >= 2, "키보드만으로 조회할 수 없다"


# ---------------------------------------------------------------- 2) 엔드포인트
def test_calls_real_endpoints(js):
    for url in ("/api/settlement?op=report", "/api/settlement?op=export",
                "/api/settlement?op=ratecard", "/api/settlement?op=usage"):
        assert url in js, url


def test_ops_exist_on_server(js):
    for op in re.findall(r"/api/settlement\?op=([a-z]+)", js):
        assert op in st.GET_OPS, op


def test_read_only_no_write_methods(js):
    assert "method:'POST'" not in js and 'method:"POST"' not in js
    assert "op:'create'" not in js


def test_no_cache_on_reads(js):
    assert js.count("cache:'no-store'") >= 4


# ---------------------------------------------------------------- 3) 드리프트
def test_report_fields_exist(sample, js):
    rep, _cov = sample
    line = rep["lines"][0]
    for f in ("partner_id", "partner_name", "tenant_id", "channel_label", "from",
              "to", "calls", "minutes", "revenue_krw", "rate_key",
              "commission_krw", "status", "basis", "split", "revenue_partial"):
        assert "l." + f in js, f
        assert f in line, f


def test_summary_fields_exist(sample, js):
    rep, _cov = sample
    for f in ("month", "in_progress", "currency", "vat", "rounding", "note",
              "attention", "by_partner", "lines", "totals", "rate_card", "usage"):
        assert f in rep, f
    for f in ("lines", "calls", "commission_krw", "complete"):
        assert "t." + f in js, f
        assert f in rep["totals"], f
    for f in ("configured", "version", "rules", "problems"):
        assert "rc." + f in js, f
        assert f in rep["rate_card"], f


def test_coverage_fields_exist(sample, js):
    _rep, cov = sample
    for f in ("month", "data_source", "tenants", "calls", "minutes",
              "revenue_reported_buckets", "buckets"):
        assert "d." + f in js, f
        assert f in cov, f
    for f in ("errors", "dropped", "retention_days", "scope"):
        assert "c." + f in js, f
        assert f in cov["collector"], f


def test_status_labels_match_server(js):
    for s in st.LINE_STATUSES:
        assert re.search(re.escape(s) + r"\s*:\s*\[", js), s


# ---------------------------------------------------------------- 4) 상태·접근성
def test_loading_and_error_roles(js):
    assert "role=\"status\"" in js and "role=\"alert\"" in js


def test_empty_states_present(html, js):
    i = html.find('<section id="view-settle"')
    j = html.find("</section>", i)
    assert "아직 불러온 내용이 없습니다" in html[i:j]
    assert "집계된 이용 실적이 없습니다" in js
    assert "등록된 요율이 없습니다" in js


def test_inline_field_validation(js):
    assert "slFieldErr('month'" in js and "slFieldErr('partner'" in js
    assert "aria-invalid" in js
    assert "f0.field" in js, "서버가 지목한 필드를 화면이 반영하지 않는다"


def test_aria_live_and_labels(html):
    i = html.find('<section id="view-settle"')
    j = html.find("</section>", i)
    seg = html[i:j]
    assert 'id="slOut"' in seg and 'aria-live="polite"' in seg
    for f in ("slMonth", "slPartner"):
        assert 'for="%s"' % f in seg, f
        assert 'aria-describedby="%sErr"' % f in seg, f


def test_http_errors_are_not_swallowed(js):
    assert "slHttpErr" in js and "요청 실패" in js
    assert "HTTP '+x.s" in js
    assert "x.s===401||x.s===403" in js
    assert "x.s===405" in js, "읽기 전용 거절을 설명하지 않는다"


def test_buttons_disabled_while_busy(js):
    assert "slBtns(true)" in js and "slBtns(false)" in js


# ---------------------------------------------------------------- 5) 이스케이프
def test_server_strings_are_escaped(js):
    for expr in ("l.tenant_id", "l.basis", "a.note", "d.note", "p.partner_name"):
        assert re.search(r"slEsc\(" + re.escape(expr), js), expr


def test_escape_helper_covers_html_chars(js):
    m = re.search(r"function slEsc\(s\)\{[^\n]*", js)
    assert m
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;"):
        assert ch in m.group(0)


# ---------------------------------------------------------------- 6) 수치 날조 금지
def test_missing_values_render_as_dash_not_zero(js):
    m = re.search(r"function slNum\(v\)\{[^\n]*", js)
    assert m, "숫자 포맷 함수가 없다"
    assert "null" in m.group(0) and "undefined" in m.group(0)
    assert ("\\u2014" in m.group(0) or "—" in m.group(0)), "없는 값을 0 으로 그린다"


def test_amount_columns_use_slnum(js):
    for f in ("l.commission_krw", "l.revenue_krw", "p.commission_krw",
              "t.commission_krw"):
        assert "slNum(" + f + ")" in js, f


def test_no_hardcoded_money_or_rate(js):
    """화면에 금액·요율 상수가 박히면 서버 응답과 다른 숫자가 보인다."""
    # 화면 문자열 안에 "숫자 + 원/%/건" 이 박혀 있으면 서버가 아닌 코드가 말한 수치다.
    # "0원으로 계산하지 않습니다" 는 수치 주장이 아니라 원칙 문구라 제외한다.
    body = js.replace("0원으로 계산하지 않습니다", "")
    for m in re.finditer(r"[\d,]{1,15}\s*(?:원|%|건)", body):
        assert not re.match(r"\d", m.group(0)), "코드에 박힌 수치: %s" % m.group(0)
    for token in ("15%", "10%", "수수료 15", "예상 정산", "평균 수수료"):
        assert token not in js, token
    # 표시되는 모든 수치는 서버 응답 필드를 거친다(상수 대입이 없다).
    assert not re.search(r"(commission_krw|revenue_krw|calls)\s*=\s*\d", js)


def test_rate_missing_warning_present(js):
    # 요율이 없을 때만 뜨는 경고가 실제로 조건에 걸려 있어야 한다(문구만으로는 부족).
    assert re.search(r"if\(!rc\.configured\)\s*warn\s*\+=", js), "요율 미설정 경고가 조건에 없다"
    assert "PARTNER_RATE_CARD" in js
    assert "[승인 필요]" in js
    assert "0원으로 계산하지 않습니다" in js
    assert "요율표가 설정되지 않았습니다" in js


def test_draft_only_wording(html, js):
    i = html.find('<section id="view-settle"')
    j = html.find("</section>", i)
    assert "초안" in html[i:j]
    assert "초안(draft)" in js
    assert "확정" in html[i:j] and "승인 필요" in html[i:j]


def test_csv_download_uses_server_file(js):
    """CSV 를 화면에서 조립하면 서버 계산과 어긋난다 — 서버 파일을 그대로 받는다."""
    assert "op=export" in js
    assert "Content-Disposition" in js and "download" in js
    assert "join(',')" not in js and "\\r\\n" not in js
