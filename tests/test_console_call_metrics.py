# -*- coding: utf-8 -*-
"""public/admin.html 「실측 통화 지표」 카드 회귀 테스트.

이 카드는 콘솔에서 유일하게 **실측치**를 그리는 자리다. 데모 수치와 섞이거나
없는 값을 0으로 채워 그리면, 샘플 데이터 워터마크를 붙여 놓은 의미가 사라진다.

  1) 카드가 존재하고 monitor 화면·자동갱신·기간 전환에 연결돼 있다
  2) 호출 대상이 실제 엔드포인트(/api/ops_stats)이고 measured 블록만 읽는다
  3) 화면이 읽는 필드가 실제 응답 키에 존재한다(드리프트 차단)
  4) 빈 상태·부분 표본·오류·로딩 표시가 있다(QUALITY_BAR §1)
  5) **가짜 수치를 하드코딩하지 않았다**(2R 가이드 §8) — 값은 서버 응답에서만 온다
  6) 표본 0건일 때 비율을 0%로 그리지 않는다(없는 것은 '—')

실행: python3 -m pytest tests/test_console_call_metrics.py -q
"""
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import call_metrics  # noqa: E402
import ops_stats     # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


@pytest.fixture(scope="module")
def html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def js(html):
    """카드 렌더 함수 본문만 잘라낸다 — 다른 화면 코드에 속지 않기 위해."""
    i = html.find("/* B160: 실측 통화 지표")
    assert i > 0, "실측 지표 렌더 코드가 없다"
    j = html.find("/* B147: view-monitor 카드 자동 갱신", i)
    assert j > i
    return html[i:j]


# ---------------------------------------------------------------- 1) 배선
def test_card_exists_once(html):
    assert html.count('id="cmBody"') == 1
    assert html.count('id="cmScope"') == 1


def test_card_is_in_monitor_view(html):
    i = html.find('<section id="view-monitor"')
    j = html.find("</section>", i)
    assert i > 0 and i < html.find('id="cmBody"') < j, "monitor 화면 밖에 있다"


def test_loaded_on_view_open(html):
    assert "if(typeof cmLoad==='function')cmLoad();" in html, "화면 진입 시 로드되지 않는다"


def test_refreshed_by_autorefresh_and_period(html):
    assert "cmLoad(true,true)" in html, "자동갱신(30초)에 연결되지 않았다"
    assert "cmLoad(true);" in html, "기간 전환에 연결되지 않았다"


def test_manual_refresh_button(html):
    assert 'onclick="cmLoad(true)"' in html


# ------------------------------------------------------- 2) 실제 엔드포인트
def test_calls_real_endpoint(js):
    assert "'/api/ops_stats?period='+encodeURIComponent(p)" in js
    assert "cache:'no-store'" in js


def test_reads_measured_block_only(js):
    assert "var m=d.measured;" in js, "measured 블록이 아닌 데모 수치를 읽고 있다"
    # 데모 헤드라인(calls.today 등)을 이 카드가 그리면 실측과 섞인다
    assert "d.calls" not in js


# ------------------------------------------------- 3) 필드 드리프트 차단
def test_fields_exist_in_payload(js):
    call_metrics.reset()
    try:
        m = ops_stats.get_ops_summary()["measured"]
    finally:
        call_metrics.reset()
    for key in ("sample_size", "window_sec", "partial", "auto_rate",
                "transfer_rate", "avg_duration_sec", "avg_turns",
                "data_source", "period", "definition"):
        assert key in m, key
        assert ("m." + key) in js or ('m["%s"]' % key) in js, key
    for key in ("total", "bot_completed", "transferred", "abandoned",
                "failed", "in_progress"):
        assert key in m["calls"], key
        assert ("c." + key) in js, key
    for key in ("errors", "orphan_finishes", "dropped", "scope"):
        assert key in m["collector"], key
        assert ("col." + key) in js or ("collector." + key) in js, key


def test_unavailable_state_matches_server(js):
    assert "m.data_source==='unavailable'" in js
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "call_metrics":
            raise RuntimeError("down")
        return real(name, *a, **kw)
    builtins.__import__ = _boom
    try:
        assert ops_stats._measured("today")["data_source"] == "unavailable"
    finally:
        builtins.__import__ = real


# ------------------------------------------- 4) 빈 상태·오류·로딩 (QUALITY_BAR)
def test_loading_indicator(js, html):
    assert "불러오는 중" in js and "불러오는 중" in html


def test_empty_state_present(js):
    assert "if(n===0){" in js, "빈 상태 분기가 없다"
    # 다음 행동을 알려준다(무엇을 하면 값이 쌓이는지)
    assert "시뮬레이션" in js


def test_error_states_use_alert_role(js):
    assert js.count('role="alert"') >= 2, "네트워크 실패·수집기 장애 안내가 없다"


def test_partial_sample_warned(js):
    assert "m.partial" in js and "_cmWin(m.window_sec)" in js


def test_collector_anomaly_surfaced(js):
    # 오류를 삼키지 않는다 — 수집 이상이 화면에 드러난다
    assert "col.errors||col.orphan_finishes||col.dropped" in js


def test_live_region_and_label(html):
    i = html.find('id="cmBody"')
    seg = html[max(0, i - 300):i + 200]
    assert 'aria-live="polite"' in seg and 'role="status"' in seg


def test_output_is_escaped(js):
    assert "_osEsc(" in js, "HTML 이스케이프를 거치지 않는다"


# ------------------------------------------------- 5) 허위 수치 금지 (§8)
def test_no_hardcoded_numbers(js):
    """값은 전부 서버 응답에서 온다 — 화면이 수치를 지어내지 않는다."""
    for demo in ("214", "1486", "6120", "72%", "0.72", "154"):
        assert demo not in js, demo


def test_no_fallback_to_demo(js):
    # 다른 카드(monSetPeriod)에는 데모 폴백(FB)이 있다. 실측 카드에는 없어야 한다.
    assert "FB[" not in js and "var FB=" not in js


def test_demo_headline_untouched(html):
    """기존 샘플 데이터 워터마크와 KPI 문구를 건드리지 않았다."""
    assert "샘플 데이터" in html
    assert html.count('id="mIn"') == 1


def test_card_declares_measured_and_limits(html):
    i = html.find('id="cmBody"')
    seg = html[max(0, i - 900):i + 900]
    assert "실측" in seg
    assert "[승인 필요]" in seg, "인스턴스 한계·영속 집계 승인 표기가 없다"
    assert "0%" in seg, "표본 없을 때 0%로 표시하지 않는다는 설명이 없다"


# ------------------------------------------------- 6) 없는 값은 '—'
def test_null_rate_rendered_as_dash(js):
    # _cmPct/_cmSec/_cmAvg 는 숫자가 아닐 때 em dash 를 돌려준다
    for fn in ("_cmPct", "_cmSec", "_cmAvg"):
        m = re.search(re.escape("function " + fn + "(v){") + r"[^\n]*", js)
        assert m, fn
        assert ("\\u2014" in m.group(0) or "\u2014" in m.group(0)), fn
        assert "isFinite(v)" in m.group(0), fn


def test_zero_sample_shows_dash_not_zero_percent(js):
    call_metrics.reset()
    m = ops_stats.get_ops_summary()["measured"]
    assert m["auto_rate"] is None          # 서버가 null 을 준다
    assert "_cmPct(m.auto_rate)" in js     # 화면은 그 null 을 '—' 로 그린다
