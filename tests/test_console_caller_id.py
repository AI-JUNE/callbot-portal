# -*- coding: utf-8 -*-
"""public/admin.html 「발신번호 등록 관리」 패널 회귀 테스트.

발신번호 등록은 법정 요건이라, 화면과 API 가 어긋나면 "대장에는 등록됐는데 실제로는
아닌" 상태가 된다. 아래를 고정한다.

  1) 라우팅(nav·titles·MENU·section id)이 서로 연결돼 있다
  2) 호출 대상이 실제 엔드포인트(/api/caller_id)이고 GET/POST·op 가 서버와 맞다
  3) 화면이 읽는 응답 필드가 실제 API 응답에 존재한다(드리프트 차단)
  4) 증빙 종류를 하드코딩하지 않고 정책 API 에서 받는다
  5) 빈 상태·오류 상태·로딩 표시·인라인 검증·접근성(QUALITY_BAR §1·§4)
  6) 되돌리기 어려운 동작(중지·반려)에 확인 절차가 있다(QUALITY_BAR §3)
  7) 새 가짜 KPI 수치를 넣지 않았다(§8) · 번호 원문을 그리지 않는다

실행: python3 -m pytest tests/test_console_caller_id.py -q
"""
import io
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import caller_id  # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


@pytest.fixture(scope="module")
def html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def panel(html):
    i = html.find('<section id="view-callerid"')
    assert i > 0, "발신번호 등록 패널 섹션이 없다"
    j = html.find("</section>", i)
    assert j > i
    return html[i:j]


@pytest.fixture(scope="module")
def js(html):
    i = html.find("function ciEsc(")
    assert i > 0, "패널 스크립트가 없다"
    j = html.find("view-callerid');if(!s||!window.MutationObserver)", i)
    assert j > i
    return html[i:j + 300]


# ---------------------------------------------------------------- 1) 라우팅
def test_routing_wired(html):
    assert html.count('<section id="view-callerid"') == 1
    assert 'data-v="callerid"' in html, "좌측 메뉴 항목이 없다"
    assert "callerid:['발신번호 등록'" in html, "titles 등록이 없다 — show() 가 무시한다"
    assert "['callerid','발신번호 등록']" in html, "상단 메뉴(MENU) 등록이 없다"


def test_section_starts_hidden(panel):
    assert 'class="hidden"' in panel.split(">")[0] + ">"


# ---------------------------------------------------------------- 2) 엔드포인트
def test_calls_real_endpoint(js):
    assert "fetch('/api/caller_id'" in js
    assert "fetch('/api/caller_id?op=list')" in js
    assert "fetch('/api/caller_id?op=policy')" in js
    assert "fetch('/api/caller_id?op=history')" in js
    assert "method:'POST'" in js


def test_no_hardcoded_localhost_or_other_host(js):
    for bad in ("localhost", "127.0.0.1", "http://"):
        assert bad not in js, bad


def test_ops_match_server_transitions(js):
    """화면 버튼이 부르는 op 가 서버 전이표에 전부 있다."""
    used = set(re.findall(r"ciAct\([^>]{0,60}?\\'(\w+)\\'\)", js))
    assert used, "조치 버튼이 없다"
    assert used <= set(caller_id.TRANSITIONS), used - set(caller_id.TRANSITIONS)
    assert "register" in js and "op:'register'" in js


def test_evidence_required_ops_send_evidence(js):
    """서버가 증빙을 요구하는 op 는 화면도 증빙을 함께 보낸다."""
    need = [k for k, v in caller_id.TRANSITIONS.items() if v["needs_evidence"]]
    for op in need:
        assert "op==='%s'" % op in js, op
    assert "body.evidence_type=e.evidence_type" in js
    assert "body.issued_at=e.issued_at" in js


# ---------------------------------------------------------------- 3) 응답 필드
def test_rendered_fields_exist_in_api_response(js):
    caller_id._clear_for_tests()
    now = time.time()
    rec = caller_id.register("demo", "010-1234-5678", "대표", now=now)
    caller_id.verify(rec["id"], "통신서비스 이용증명원", now - 86400, now=now)
    view = caller_id.list_numbers(now=now)[0]
    for field in ("id", "number_masked", "label", "tenant_id", "status",
                  "evidence_type", "expires_at", "days_left", "expiring_soon",
                  "outbound_ready"):
        assert "n." + field in js, field
        assert field in view, field
    caller_id._clear_for_tests()


def test_summary_fields_exist(js):
    s = caller_id.summary()
    for field in ("total", "counts", "expiring_soon", "expired", "cpaas_live",
                  "persistence", "activation_note"):
        assert field in s, field
        assert ("d." + field) in js or ("d.counts" in js and field == "counts"), field
    assert "d.vault" in js and "vault" in s


def test_status_badges_cover_all_view_statuses(js):
    for st in caller_id.VIEW_STATUSES:
        assert st + ":[" in js, "상태 %s 의 배지가 없다 — 원시 값이 그대로 노출된다" % st


def test_policy_fields_exist(js):
    p = caller_id.policy()
    for field in ("evidence_types", "evidence_max_age_days", "default_valid_days",
                  "expiry_warn_days", "max_numbers", "number_rules", "legal",
                  "transitions"):
        assert field in p, field
        assert "d." + field in js, field


def test_history_fields_exist(js):
    caller_id._clear_for_tests()
    caller_id.register("demo", "010-1234-5678")
    h = caller_id.history()[0]
    for field in ("ts", "id", "action", "detail", "actor"):
        assert field in h, field
        assert "h." + field in js, field
    caller_id._clear_for_tests()


# ---------------------------------------------------------------- 4) 증빙 목록
def test_evidence_types_not_hardcoded(panel, js):
    """증빙 종류는 정책 API 에서 받아 채운다 — 서버 목록이 바뀌면 화면도 따라온다."""
    for t in caller_id.EVIDENCE_TYPES:
        assert '<option value="%s"' % t not in panel, t
    assert "ciFillEvidence" in js
    assert "d.evidence_types" in js


def test_valid_days_bounds_match_server(panel):
    assert 'min="1"' in panel and 'max="1825"' in panel, "유효기간 입력 상한이 서버와 다르다"


# ---------------------------------------------------------------- 5) 품질 바
def test_empty_state_message(panel):
    assert "아직 불러온 내용이 없습니다" in panel


def test_empty_list_message(js):
    assert "등록된 번호가 없습니다" in js
    assert "변경 이력이 없습니다" in js


def test_loading_and_error_roles(js):
    assert "role=\"status\"" in js.replace("'", '"'), "로딩 표시에 role=status 가 없다"
    assert "role=\"alert\"" in js.replace("'", '"'), "오류 표시에 role=alert 가 없다"


def test_network_and_http_errors_not_swallowed(js):
    assert js.count(".catch(function(e){ciFail(") >= 4, "네트워크 실패를 삼키는 경로가 있다"
    assert "ciHttpErr" in js
    assert "x.s===401||x.s===403" in js, "세션 만료 안내가 없다"
    assert "x.s===501" in js and "[승인 필요]" in js


def test_inline_field_errors(panel, js):
    for f in ("ciTenantErr", "ciNumberErr", "ciEvidenceErr", "ciIssuedErr", "ciValidErr"):
        assert 'id="%s"' % f in panel, f
        assert f in js, f
    assert "aria-invalid" in js


def test_labels_and_aria(panel):
    for fid in ("ciTenant", "ciNumber", "ciLabel", "ciEvidence", "ciIssued", "ciValid"):
        assert 'for="%s"' % fid in panel, fid
        assert 'id="%s"' % fid in panel, fid
    assert 'aria-live="polite"' in panel
    assert panel.count('role="alert"') >= 5


def test_keyboard_operable(panel):
    assert "event.key==='Enter'" in panel, "Enter 로 실행할 수 없다"
    assert panel.count("<button") >= 5


def test_buttons_disabled_while_busy(js):
    assert "function ciBtns(" in js
    assert js.count("ciBtns(true)") >= 4
    assert js.count("ciBtns(false)") >= 8    # then/catch 양쪽에서 반드시 되돌린다


def test_tenant_rule_matches_server(js):
    m = re.search(r"/\^\[a-z0-9\]\[a-z0-9_-\]\{0,39\}\$/", js)
    assert m, "화면의 tenant_id 규칙이 없다"
    assert caller_id.TENANT_RE.pattern == "^[a-z0-9][a-z0-9_-]{0,39}$"


# ---------------------------------------------------------------- 6) 확인 절차
def test_irreversible_actions_confirm(js):
    assert "op==='revoke'&&!window.confirm(" in js, "사용 중지에 확인 절차가 없다"
    assert "op==='reject'&&!window.confirm(" in js, "반려에 확인 절차가 없다"
    assert "암호 파기" in js, "중지가 번호 원문을 파기한다는 사실을 알리지 않는다"


# ---------------------------------------------------------------- 7) 안전
def test_escapes_output(js):
    assert "function ciEsc(" in js
    assert js.count("ciEsc(") >= 30
    assert "innerHTML" in js and "ciSet(" in js


def test_no_fake_metrics(panel, js):
    """§8 — 새 가짜 수치를 넣지 않는다. 화면은 서버 응답만 그린다."""
    blob = re.sub(r'style="[^"]*"', "", panel) + js      # CSS 의 width:100% 는 수치가 아니다
    assert not re.search(r"(?<![\w.:])\d{1,3}(\.\d+)?%", blob), "하드코딩 퍼센트 수치"
    assert "샘플 데이터" not in panel, "가짜 KPI 카드를 새로 만들지 않는다"
    for word in ("평균 처리", "절감", "만족도"):
        assert word not in blob, word


def test_no_raw_number_rendered(js):
    """목록·이력 어디에도 원문 번호 필드를 그리지 않는다."""
    for bad in ("n.number_sealed", "n.number_digits", "number_digits_masked_key",
                "reveal_number"):
        assert bad not in js, bad
    assert "n.number_masked" in js


def test_does_not_toggle_live_switches(js):
    """콘솔이 실발신 게이트를 켜는 요청을 보내지 않는다."""
    for bad in ("CPAAS_LIVE", "cpaas_live:true", "mode:'live'", "mode=live"):
        assert bad not in js, bad
    assert "d.cpaas_live" in js     # 읽어서 보여주기만 한다
