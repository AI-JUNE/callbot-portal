# -*- coding: utf-8 -*-
"""상담사 전환 티켓 조회 경로 회귀 (36차) — 네트워크·LLM 미호출.

왜 이 파일이 필요한가
  전환 티켓은 봇이 고객에게 **“상담사에게 연결해 드리겠습니다”라고 말한
  약속**의 유일한 기록이다. 그런데 사람이 볼 수 있는 곳이 없었다 —
  `/api/ops_stats` 는 `escalation.queued=3` 같은 **숫자만** 내보냈고,
  누가 무엇 때문에 기다리는지는 아무도 알 수 없었다(32~35차가 네 번 미룬 자리).

  그 숫자마저 절반이었다. `_escalation.stats()` 는 `audit_dropped`(상한으로
  버린 상태 전이 기록)·`summary_dropped`(개인정보를 가릴 수 없어 싣지 않은
  요약)를 **세고 있었는데** `ops_stats.ESCALATION_KEYS` 에 없어서 응답에
  나오지 않았다. 바로 옆 `RECORDING_KEYS` 는 `audit_dropped` 를 이미
  내보내고 있다 — 26·31·35차가 같은 자리에서 고친 '절반만 지켜진 규칙'이다.
  세어 두고 보여주지 않으면 센 것이 아니다.

무엇을 고정하는가
  1) 손실·오류 카운터가 **빠짐없이** 응답에 실린다(등록부 드리프트 포함)
  2) 티켓 목록 — 필터·정렬·상한·잘림 고지
  3) 내보내는 칸은 화이트리스트뿐이다(저장소에 칸이 늘어도 자동으로 새지 않는다)
  4) 개인정보 — 발화 원문·연락처가 목록으로 새지 않는다
  5) 빈 요약의 뜻을 구별한다(버린 것 ≠ 없는 것)
  6) HTTP 계약 — 200/400/403, 표준 봉투, 요청ID, 캐시 금지
  7) 감사 — 티켓 **열람**이 집계 조회와 구별돼 남는다
  8) 읽기 전용 — 조회가 큐를 바꾸지 않는다. 큐 장애가 대시보드를 죽이지 않는다
  9) 콘솔 배선 — 화면이 서버 화이트리스트와 어긋나지 않고 가짜 티켓이 없다

실행: python3 -m pytest tests/test_escalation_view.py -q
"""
import io
import json
import os
import re
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _audit         # noqa: E402
import _escalation    # noqa: E402
import _log           # noqa: E402
import _ratelimit     # noqa: E402
import ops_stats      # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")


# --------------------------------------------------------------------------
# 최소 핸들러 대역 (소켓 없이 do_GET 을 돌린다 — test_ops_stats.py 와 같은 모양)
# --------------------------------------------------------------------------
class FakeHeaders(dict):
    def get(self, k, d=None):
        return dict.get(self, k.lower(), dict.get(self, k, d))


class FakeWFile(object):
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class Resp(object):
    def __init__(self):
        self.status = None
        self.sent = []
        self.wfile = FakeWFile()

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None

    def body(self):
        return json.loads(self.wfile.data.decode("utf-8"))


def call(path="/api/ops_stats?op=escalations", headers=None):
    r = Resp()
    hdrs = FakeHeaders({k.lower(): v for k, v in (headers or SAME_ORIGIN).items()})
    inst = ops_stats.handler.__new__(ops_stats.handler)
    inst.headers = hdrs
    inst.path = path
    inst.wfile = r.wfile
    inst.rfile = io.BytesIO(b"")
    inst.send_response = lambda c: setattr(r, "status", c)
    inst.send_header = lambda k, v: r.sent.append((k, str(v)))
    inst.end_headers = lambda: None
    inst.do_GET()
    return r


SAME_ORIGIN = {"sec-fetch-site": "same-origin", "x-forwarded-for": "10.0.0.7"}

ENV = ("CALLBOT_API_KEY", "CALLBOT_STRICT", "CALLBOT_DEBUG_ERRORS",
       "CALLBOT_AUDIT", "CALLBOT_AUDIT_SALT")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        for k in [k for k in list(os.environ) if k.startswith("CALLBOT_RATE_LIMIT")]:
            os.environ.pop(k, None)
        self._restore = []
        _ratelimit.reset()
        _audit.reset()
        _escalation.QUEUE.__init__()          # 같은 싱글턴을 비운다

        def watchdog(*a, **kw):
            raise AssertionError("네트워크 호출 금지")

        self.patch(urllib.request, "urlopen", watchdog)

    def tearDown(self):
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _ratelimit.reset()
        _audit.reset()
        _escalation.QUEUE.__init__()

    def patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def seed(self, n=3, reason="request", summary="상담원 연결 요청"):
        """티켓 n건(오래된 것부터)."""
        return [_escalation.QUEUE.enqueue("call-%03d" % i, reason,
                                          "%s %d" % (summary, i), "refund")
                for i in range(n)]


# ==========================================================================
# 1) [결함] 손실·오류 카운터가 세어지기만 하고 나오지 않았다
# ==========================================================================
LOSS_WORDS = ("dropped", "errors", "evicted", "failed", "lost", "skipped")


def _is_loss(key):
    parts = str(key).split("_")
    return parts[0] in LOSS_WORDS or parts[-1] in LOSS_WORDS


class TestLossCountersAreExposed(Base):
    def test_every_loss_counter_the_queue_keeps_is_exposed(self):
        """등록부 드리프트까지 잡는다 — 큐가 새 손실 칸을 세기 시작하면 여기서 운다."""
        counted = {k for k in _escalation.EscalationQueue().stats() if _is_loss(k)}
        self.assertTrue(counted)
        missing = sorted(counted - set(ops_stats.ESCALATION_KEYS))
        self.assertEqual(missing, [], "세어 두고 내보내지 않는 칸: %s" % missing)

    def test_the_two_that_were_missing(self):
        """결함의 실제 모양 — 이 두 칸이 빠져 있었다."""
        for k in ("audit_dropped", "summary_dropped"):
            self.assertIn(k, ops_stats.ESCALATION_KEYS, k)

    def test_the_recording_block_kept_the_same_rule(self):
        """옆 블록은 이미 지키고 있었다(절반만 지켜진 규칙이었다는 근거)."""
        counted = {k for k in ("dropped", "dropped_active", "audit_dropped")}
        self.assertTrue(counted <= set(ops_stats.RECORDING_KEYS))

    def test_the_summary_response_carries_the_real_counts(self):
        """상수 0 이 아니라 **이 인스턴스가 실제로 버린 수**가 실린다."""
        self.patch(_escalation, "_scrub", None)       # 마스킹 불가 → 요약 버림
        self.seed(2)
        block = ops_stats.get_ops_summary()["escalation"]
        self.assertEqual(block["summary_dropped"], 2)
        self.assertEqual(block["source"], "sim")

    def test_audit_dropped_reaches_the_response(self):
        q = _escalation.QUEUE
        over = 4
        for _ in range(_escalation.MAX_AUDIT + over):
            q._log("ESC-0001", "queued", "assigned", "t")
        self.assertEqual(ops_stats.get_ops_summary()["escalation"]["audit_dropped"],
                         over)

    def test_missing_source_still_reports_zero_not_absent(self):
        """소스를 못 읽어도 칸은 사라지지 않는다(콘솔이 깨지지 않는다)."""
        n = ops_stats._norm_stats(None, ops_stats.ESCALATION_KEYS,
                                  ops_stats.ESCALATION_META)
        for k in ("audit_dropped", "summary_dropped"):
            self.assertEqual(n[k], 0)


# ==========================================================================
# 2) 티켓 목록 — 필터·정렬·상한
# ==========================================================================
class TestListing(Base):
    def test_empty_queue_is_an_empty_list_not_a_demo(self):
        e = ops_stats.get_escalations()
        self.assertEqual((e["tickets"], e["matched"], e["count"]), ([], 0, 0))
        self.assertFalse(e["truncated"])
        self.assertEqual(e["data_source"], "measured")

    def test_queue_order_is_oldest_first(self):
        made = self.seed(3)
        got = [t["id"] for t in ops_stats.get_escalations()["tickets"]]
        self.assertEqual(got, [t["id"] for t in made])

    def test_newest_order_is_the_reverse(self):
        made = self.seed(3)
        got = [t["id"] for t in ops_stats.get_escalations(order="newest")["tickets"]]
        self.assertEqual(got, [t["id"] for t in reversed(made)])

    def test_unknown_order_falls_back_to_queue_order(self):
        made = self.seed(2)
        for bad in ("sideways", "", None, 7):
            e = ops_stats.get_escalations(order=bad)
            self.assertEqual(e["filter"]["order"], "oldest", repr(bad))
            self.assertEqual([t["id"] for t in e["tickets"]],
                             [t["id"] for t in made])

    def test_state_filter(self):
        made = self.seed(3)
        _escalation.QUEUE.transition(made[0]["id"], "abandoned")
        _escalation.QUEUE.transition(made[1]["id"], "assigned")
        self.assertEqual(ops_stats.get_escalations(state="queued")["count"], 1)
        self.assertEqual(ops_stats.get_escalations(state="assigned")["count"], 1)
        self.assertEqual(ops_stats.get_escalations(state="abandoned")["count"], 1)
        self.assertEqual(ops_stats.get_escalations(state="resolved")["count"], 0)
        self.assertEqual(ops_stats.get_escalations()["count"], 3)

    def test_open_means_not_yet_picked_up(self):
        """`open` 의 정의는 `_escalation.OPEN_STATES` 가 갖는다(복제하지 않는다)."""
        made = self.seed(3)
        _escalation.QUEUE.transition(made[0]["id"], "abandoned")
        _escalation.QUEUE.transition(made[1]["id"], "assigned")
        e = ops_stats.get_escalations(state="open")
        self.assertEqual({t["state"] for t in e["tickets"]},
                         {"queued", "assigned"})
        self.assertEqual(e["open_states"], list(_escalation.OPEN_STATES))

    def test_open_definition_is_not_copied_into_the_route(self):
        """판정 복제 금지 — 큐가 정의를 바꾸면 라우트가 따라온다."""
        self.patch(_escalation, "OPEN_STATES", ("assigned",))
        made = self.seed(2)
        _escalation.QUEUE.transition(made[0]["id"], "assigned")
        e = ops_stats.get_escalations(state="open")
        self.assertEqual([t["id"] for t in e["tickets"]], [made[0]["id"]])

    def test_truncation_is_announced_not_silent(self):
        self.seed(5)
        e = ops_stats.get_escalations(limit=2)
        self.assertEqual((e["count"], e["matched"], e["truncated"]), (2, 5, True))

    def test_limit_is_clamped_to_a_sane_range(self):
        self.seed(3)
        for bad in (0, -1, "x", None, [], 10 ** 9):
            lim = ops_stats.get_escalations(limit=bad)["filter"]["limit"]
            self.assertTrue(1 <= lim <= ops_stats.TICKET_LIMIT_MAX, repr(bad))

    def test_matched_counts_the_filter_not_the_page(self):
        made = self.seed(4)
        _escalation.QUEUE.transition(made[0]["id"], "abandoned")
        e = ops_stats.get_escalations(state="queued", limit=1)
        self.assertEqual((e["matched"], e["count"]), (3, 1))

    def test_age_is_reported_and_never_negative(self):
        self.seed(1)
        t = ops_stats.get_escalations()["tickets"][0]
        self.assertIsInstance(t["age_sec"], int)
        self.assertGreaterEqual(t["age_sec"], 0)

    def test_a_clock_that_runs_backwards_does_not_produce_negative_age(self):
        future = {"id": "ESC-9999", "state": "queued", "ts": 10 ** 12}
        self.assertEqual(ops_stats._ticket_view(future, 0.0)["age_sec"], 0)

    def test_a_ticket_without_a_timestamp_reports_no_age(self):
        v = ops_stats._ticket_view({"id": "ESC-1", "ts": None}, 1000.0)
        self.assertIsNone(v["age_sec"])
        self.assertEqual(v["ts"], 0)

    def test_volatility_is_stated_not_implied(self):
        e = ops_stats.get_escalations()
        self.assertEqual(e["scope"], "instance")
        self.assertIs(e["volatile"], True)


# ==========================================================================
# 3) 내보내는 칸은 화이트리스트뿐이다
# ==========================================================================
class TestFieldWhitelist(Base):
    def test_exactly_the_registered_keys(self):
        self.seed(1)
        t = ops_stats.get_escalations()["tickets"][0]
        self.assertEqual(set(t), set(ops_stats.TICKET_KEYS))

    def test_a_new_store_field_does_not_leak(self):
        """저장소에 칸이 하나 늘어도 HTTP 로 자동으로 나가지 않는다."""
        made = self.seed(1)
        _escalation.QUEUE._tickets[made[0]["id"]]["secret_note"] = "내부 메모"
        body = json.dumps(ops_stats.get_escalations(), ensure_ascii=False)
        self.assertNotIn("secret_note", body)
        self.assertNotIn("내부 메모", body)

    def test_session_id_is_renamed_to_call_ref(self):
        self.seed(1)
        e = ops_stats.get_escalations()
        self.assertEqual(e["tickets"][0]["call_ref"], "call-000")
        self.assertNotIn("session_id", json.dumps(e, ensure_ascii=False))

    def test_oversized_store_values_are_cut_again(self):
        """저장소 규약이 깨져도 HTTP 로 통째로 새지 않는다(2차 방어선)."""
        v = ops_stats._ticket_view({"id": "x" * 5000, "ts": 1}, 2.0)
        self.assertLessEqual(len(v["id"]), ops_stats._TICKET_FIELD_MAX)

    def test_non_dict_rows_do_not_raise(self):
        for junk in (None, "ticket", 5, []):
            v = ops_stats._ticket_view(junk, 1.0)
            self.assertEqual(set(v), set(ops_stats.TICKET_KEYS))

    def test_a_value_that_cannot_be_stringified_does_not_raise(self):
        class Hostile(object):
            def __str__(self):
                raise RuntimeError("no")

            __repr__ = __str__

        self.assertEqual(ops_stats._cell(Hostile()), "-")

    def test_a_junk_timestamp_does_not_raise(self):
        for bad in ("언젠가", object(), [1]):
            v = ops_stats._ticket_view({"id": "ESC-1", "ts": bad}, 5.0)
            self.assertEqual((v["ts"], v["age_sec"]), (0, None), repr(bad))


# ==========================================================================
# 4) 개인정보 — 발화 원문·연락처가 목록으로 새지 않는다
# ==========================================================================
class TestPrivacy(Base):
    def test_contacts_in_the_summary_are_masked(self):
        _escalation.QUEUE.enqueue("call-x", "request",
                                  "고객 010-1234-5678 / 880101-1234567 환불 요청")
        body = json.dumps(ops_stats.get_escalations(), ensure_ascii=False)
        self.assertNotIn("010-1234-5678", body)
        self.assertNotIn("880101-1234567", body)

    def test_contacts_in_the_call_reference_are_masked(self):
        _escalation.QUEUE.enqueue("01012345678", "request", "요청")
        body = json.dumps(ops_stats.get_escalations(), ensure_ascii=False)
        self.assertNotIn("01012345678", body)

    def test_names_are_a_known_limitation_not_a_claim(self):
        """**성명은 가려지지 않는다** — `monitoring.scrub` 는 패턴(연락처·주민번호·
        카드·계좌·이메일)만 본다. 가린다고 적어 두고 안 가리는 쪽이 더 나쁘므로
        사실을 여기에 못으로 박는다. 화면 문구도 같은 말을 해야 한다.
        """
        _escalation.QUEUE.enqueue("call-x", "request", "김민수 고객 환불 요청")
        body = json.dumps(ops_stats.get_escalations(), ensure_ascii=False)
        self.assertIn("김민수", body)
        with open(ADMIN, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("성명은 가려지지 않습니다", html)
        self.assertNotIn("요약은 저장 전에 <b>개인정보 마스킹</b>을 거치며", html)

    def test_http_response_carries_no_contacts_either(self):
        _escalation.QUEUE.enqueue("call-x", "request", "연락처 010-9999-8888")
        r = call()
        self.assertEqual(r.status, 200)
        self.assertNotIn("010-9999-8888", r.wfile.data.decode("utf-8"))


# ==========================================================================
# 5) 빈 요약의 뜻을 구별한다 (버린 것 ≠ 없는 것)
# ==========================================================================
class TestDroppedSummaryIsDistinguishable(Base):
    def test_a_dropped_summary_says_so(self):
        self.patch(_escalation, "_scrub", None)       # 가릴 수 없는 상태
        _escalation.QUEUE.enqueue("call-x", "request", "고객 발화 원문")
        t = ops_stats.get_escalations()["tickets"][0]
        self.assertEqual(t["summary"], "")
        self.assertIs(t["summary_dropped"], True)

    def test_a_merely_absent_summary_is_not_marked_as_dropped(self):
        _escalation.QUEUE.enqueue("call-x", "request", "")
        t = ops_stats.get_escalations()["tickets"][0]
        self.assertEqual(t["summary"], "")
        self.assertIs(t["summary_dropped"], False)

    def test_a_normal_summary_is_not_marked(self):
        _escalation.QUEUE.enqueue("call-x", "request", "환불 문의")
        t = ops_stats.get_escalations()["tickets"][0]
        self.assertEqual(t["summary"], "환불 문의")
        self.assertIs(t["summary_dropped"], False)

    def test_the_per_ticket_flag_and_the_counter_agree(self):
        self.patch(_escalation, "_scrub", None)
        self.seed(3)
        e = ops_stats.get_escalations()
        marked = sum(1 for t in e["tickets"] if t["summary_dropped"])
        self.assertEqual(marked, e["stats"]["summary_dropped"])

    def test_old_tickets_without_the_flag_read_as_false(self):
        v = ops_stats._ticket_view({"id": "ESC-1", "summary": "a", "ts": 1}, 2.0)
        self.assertIs(v["summary_dropped"], False)


# ==========================================================================
# 6) HTTP 계약
# ==========================================================================
class TestHttp(Base):
    def test_default_op_is_the_old_summary_behaviour(self):
        """기존 호출부는 한 줄도 바뀌지 않는다."""
        r = call("/api/ops_stats")
        self.assertEqual(r.status, 200)
        b = r.body()
        self.assertEqual((b["data_source"], b["period"]), ("demo", "today"))
        self.assertIn("calls", b)

    def test_escalations_op_returns_the_list(self):
        self.seed(2)
        b = call().body()
        self.assertTrue(b["ok"])
        self.assertEqual(b["op"], "escalations")
        self.assertEqual(len(b["tickets"]), 2)

    def test_query_filters_reach_the_handler(self):
        made = self.seed(3)
        _escalation.QUEUE.transition(made[0]["id"], "abandoned")
        b = call("/api/ops_stats?op=escalations&state=queued&limit=1&order=newest").body()
        self.assertEqual((b["count"], b["matched"], b["truncated"]), (1, 2, True))
        self.assertEqual(b["tickets"][0]["id"], made[2]["id"])

    def test_bad_values_are_400_with_the_standard_envelope(self):
        for q, field in (("op=delete", "op"), ("op=escalations&state=pending", "state"),
                         ("op=escalations&order=sideways", "order"),
                         ("op=escalations&limit=abc", "limit"),
                         ("op=escalations&limit=0", "limit"),
                         ("op=escalations&limit=99999", "limit")):
            r = call("/api/ops_stats?" + q)
            self.assertEqual(r.status, 400, q)
            b = r.body()
            self.assertFalse(b["ok"])
            self.assertEqual(b["code"], "INVALID_REQUEST", q)
            self.assertEqual([d["field"] for d in b["details"]], [field], q)
            self.assertTrue(b["request_id"])

    def test_the_error_envelope_request_id_matches_the_header(self):
        r = call("/api/ops_stats?op=nope")
        self.assertEqual(r.body()["request_id"], r.header("X-Request-Id"))

    def test_cross_origin_is_refused_before_the_queue_is_read(self):
        seen = []
        self.patch(_escalation.QUEUE, "list",
                   lambda *a, **kw: seen.append(1) or [])
        r = call(headers={"origin": "https://evil.example", "host": "callbot-portal.vercel.app"})
        self.assertEqual(r.status, 403)
        self.assertEqual(seen, [])

    def test_responses_are_not_cached(self):
        self.assertEqual(call().header("Cache-Control"), "no-store")

    def test_request_id_header_is_present_on_success(self):
        self.assertTrue(call().header("X-Request-Id"))


# ==========================================================================
# 7) 감사 — 티켓 열람이 집계 조회와 구별돼 남는다
# ==========================================================================
class TestAudit(Base):
    def test_viewing_tickets_is_audited_with_the_op(self):
        call()
        recs = _audit.recent()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["action"], "ops.stats.read")
        self.assertEqual(recs[0]["result"], "allow")
        self.assertEqual(recs[0]["extra"]["op"], "escalations")

    def test_the_summary_read_is_distinguishable(self):
        call("/api/ops_stats")
        extra = _audit.recent()[0]["extra"]
        self.assertEqual(extra["op"], "summary")
        self.assertEqual(extra["period"], "today")

    def test_a_ticket_read_does_not_claim_a_period(self):
        """기간은 집계 조회의 칸이다 — 티켓 열람에 적으면 거짓 분류가 된다."""
        call()
        self.assertNotIn("period", _audit.recent()[0]["extra"])

    def test_a_refused_read_is_audited_too(self):
        call(headers={"origin": "https://evil.example", "host": "callbot-portal.vercel.app"})
        recs = _audit.recent()
        self.assertEqual([r["result"] for r in recs], ["deny"])

    def test_the_op_field_is_registered(self):
        """등록부 밖의 칸은 기록되지 않고 extra_dropped 로 버려진다."""
        self.assertIn("op", _audit.FIELDS)
        self.assertEqual(_audit.counters()["extra_dropped"], 0)
        call()
        self.assertEqual(_audit.counters()["extra_dropped"], 0)

    def test_the_two_streams_describe_op_the_same_way(self):
        """같은 뜻에 다른 설명이면 상관 분석이 어긋난다(`tests/test_audit_fields.py` 규약)."""
        self.assertEqual(_audit.FIELDS["op"], _log.FIELDS["op"])


# ==========================================================================
# 8) 읽기 전용 · 장애 흡수
# ==========================================================================
class TestReadOnlyAndResilience(Base):
    def test_reading_does_not_change_the_queue(self):
        made = self.seed(2)
        before = (_escalation.QUEUE.stats(), len(_escalation.QUEUE.audit_log()))
        for st in (None, "open", "queued", "resolved"):
            ops_stats.get_escalations(state=st)
        call()
        self.assertEqual(_escalation.QUEUE.stats()["total"], before[0]["total"])
        self.assertEqual(len(_escalation.QUEUE.audit_log()), before[1])
        self.assertEqual([t["state"] for t in _escalation.QUEUE.list()],
                         ["queued"] * len(made))

    def test_mutating_the_returned_rows_does_not_touch_the_store(self):
        made = self.seed(1)
        e = ops_stats.get_escalations()
        e["tickets"][0]["state"] = "resolved"
        self.assertEqual(_escalation.QUEUE._tickets[made[0]["id"]]["state"], "queued")

    def test_a_broken_queue_is_reported_not_faked(self):
        def boom(*a, **kw):
            raise RuntimeError("queue down")

        self.patch(_escalation.QUEUE, "list", boom)
        e = ops_stats.get_escalations()
        self.assertEqual((e["source"], e["data_source"]),
                         ("unavailable", "unavailable"))
        self.assertEqual((e["tickets"], e["matched"]), ([], 0))
        self.assertTrue(e["ok"])

    def test_a_broken_queue_still_answers_200_over_http(self):
        def boom(*a, **kw):
            raise RuntimeError("queue down")

        self.patch(_escalation.QUEUE, "list", boom)
        r = call()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body()["source"], "unavailable")

    def test_a_missing_module_falls_back_without_raising(self):
        self.patch(ops_stats, "_esc_module", lambda: None)
        e = ops_stats.get_escalations(state="open")
        self.assertEqual(e["source"], "unavailable")
        self.assertEqual(e["open_states"], list(ops_stats._FALLBACK_OPEN_STATES))

    def test_the_payload_is_serialisable(self):
        self.seed(2)
        json.dumps(ops_stats.get_escalations(), ensure_ascii=False)


# ==========================================================================
# 9) 콘솔 배선 — 화면이 서버와 어긋나지 않는다
# ==========================================================================
class TestConsole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ADMIN, encoding="utf-8") as f:
            cls.html = f.read()

    def test_the_card_exists_in_the_monitor_view(self):
        self.assertIn('id="etBody"', self.html)
        self.assertIn('id="etState"', self.html)

    def test_it_calls_the_real_endpoint(self):
        self.assertIn("/api/ops_stats?op=escalations", self.html)

    def test_the_state_options_match_the_server_whitelist(self):
        block = re.search(r'<select id="etState".*?</select>', self.html, re.S)
        self.assertTrue(block)
        opts = set(re.findall(r'<option value="([^"]*)"', block.group(0)))
        self.assertEqual(opts - {""}, set(ops_stats.TICKET_FILTER_STATES))

    def test_every_rendered_ticket_field_is_one_the_server_sends(self):
        row = re.search(r"function _etRow\(t\)\{.*?\n\}", self.html, re.S)
        self.assertTrue(row)
        used = set(re.findall(r"\bt\.([a-z_]+)", row.group(0)))
        self.assertTrue(used)
        self.assertEqual(used - set(ops_stats.TICKET_KEYS), set())

    def test_the_loss_counters_are_drawn(self):
        for k in ("dropped_waiting", "audit_dropped", "summary_dropped"):
            self.assertIn("'%s'" % k, self.html, k)

    def test_no_hardcoded_tickets(self):
        """화면은 서버 응답만 그린다 — 가짜 티켓·가짜 수치 금지(§8)."""
        fn = re.search(r"function _etRender\(d\)\{.*?\n\}", self.html, re.S)
        self.assertTrue(fn)
        self.assertNotIn("ESC-", fn.group(0))

    def test_empty_and_error_states_exist(self):
        self.assertIn("아직 상담사 전환 티켓이 없습니다", self.html)
        self.assertIn("전환 큐가 응답하지 않아", self.html)
        self.assertIn("권한이 없거나 세션이 만료됐습니다", self.html)

    def test_http_failures_are_not_swallowed(self):
        fn = re.search(r"function etLoad\(force,quiet\)\{.*?\n\}", self.html, re.S)
        self.assertTrue(fn)
        self.assertIn("r.status", fn.group(0))
        self.assertIn("role=\"alert\"", self.html)

    def test_truncation_is_shown_to_the_operator(self):
        self.assertIn("목록 상한", self.html)

    def test_the_volatility_warning_is_on_screen(self):
        card = self.html[self.html.index('id="etBody"'):]
        card = card[:card.index("<!--")]
        self.assertIn("재시작하면 대기 티켓이 사라집니다", card)
        self.assertIn("읽기 전용", card)
        self.assertIn("[승인 필요]", card)

    def test_the_select_has_a_label(self):
        self.assertIn('<label for="etState"', self.html)

    def test_values_are_escaped(self):
        row = re.search(r"function _etRow\(t\)\{.*?\n\}", self.html, re.S)
        raw = re.findall(r"\+\s*t\.([a-z_]+)\s*\+", row.group(0))
        self.assertEqual(raw, [], "이스케이프 없이 끼워 넣는 칸: %s" % raw)


if __name__ == "__main__":
    unittest.main()
