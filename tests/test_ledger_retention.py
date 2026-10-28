# -*- coding: utf-8 -*-
"""40차 — 장부가 **버린 것을 말하지 않던** 자리의 회귀.

고친 결함 두 건은 같은 모양이다 — **상한·보관기간은 있는데 그 결과를 아무도
말하지 않았다**. 상한(`mem_caps`)은 메모리가 자라는 것을 막고 만료
(`stale_state`)는 '지금'을 바로잡지만, "무엇이 사라졌는지"를 내보내지 않으면
화면은 남은 것을 **전부**로 그린다.

  1) 관리 장부 변경 이력 — `caller_id`·`partners`·`disclosure`
     상한 100건을 넘기면 오래된 줄을 버리는데 세지도 내보내지도 않았고,
     라우트는 기본 50건만 돌려주면서 잘렸다는 말을 하지 않았다.
     콘솔은 그 위에 「최근 N건 · append-only」 라고 적었다(거짓).
  2) 녹취 메타 보관기간 — `_recording_audit`
     레코드마다 `expires_at` 을 계산해 두지만 읽는 곳은 `purge_due()` 하나이고
     **그것을 부르는 배선이 없다**. 그래서 보관기간이 지난 메타가 `active` 로
     남고, 콘솔은 그 값을 「보관 중」으로 그렸다.

검증 축:
  A) 이력 손실 — 버린 줄을 세고, 잘렸다고 말하고, 거짓말하지 않는다(3 모듈 공통)
  B) 라우트 — 소켓 E2E 로 같은 말을 하는가 + `?limit=` 경계
  C) 녹취 보관기간 — 세기만 하고 **파기하지 않는다** + 노출
  D) 콘솔 — 값과 **보일지 말지를 정하는 조건**을 함께 고정(39차 교훈)
  E) 릴리스 게이트 — `mem_caps` 가 형 주석 선언을 보는가 + 과차단하지 않는가

네트워크·LLM 미사용(E2E 는 루프백 소켓). 실행: python -m pytest tests/test_ledger_retention.py -q
"""
import io
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import caller_id                 # noqa: E402
import disclosure                # noqa: E402
import partners                  # noqa: E402
import ops_stats                 # noqa: E402
import _recording_audit as ra    # noqa: E402
import verify                    # noqa: E402

ADMIN = os.path.join(ROOT, "public", "admin.html")

# (모듈, 이력 1줄을 남기는 호출) — 세 장부를 같은 규약으로 본다.
LEDGERS = ("caller_id", "partners", "disclosure")


def _admin_html():
    with io.open(ADMIN, encoding="utf-8") as f:
        return f.read()


def _mod(name):
    return {"caller_id": caller_id, "partners": partners,
            "disclosure": disclosure}[name]


def _append(name, i):
    """장부별로 이력 1줄을 남긴다 — 공개 경로를 쓴다(내부 리스트를 직접 밀지 않는다)."""
    if name == "caller_id":
        caller_id._hist("register", "CID-%04d" % i, "tester", "010****0000")
    elif name == "partners":
        with partners._LOCK:
            partners._log_event("test", tenant_id="t%d" % i, actor="tester")
    else:
        with disclosure._LOCK:
            disclosure._hist({"ts": "t", "tenant_id": "t%d" % i, "op": "set",
                              "version": i, "from_version": i - 1, "actor": "tester"})


def _clear(name):
    _mod(name)._clear_for_tests()


# --------------------------------------------------------------------------
# A) 이력 손실 — 세 장부 공통 규약
# --------------------------------------------------------------------------
class TestHistoryLossIsCounted(unittest.TestCase):
    def tearDown(self):
        for n in LEDGERS:
            _clear(n)

    def test_overflow_is_counted_not_swallowed(self):
        """상한을 넘긴 줄은 버려지되 **건수가 남는다**."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                over = 7
                for i in range(m.HISTORY_MAX + over):
                    _append(n, i + 1)
                s = m.history_stats(limit=m.HISTORY_MAX)
                self.assertEqual(s["retained"], m.HISTORY_MAX)
                self.assertEqual(s["dropped"], over)
                self.assertEqual(s["capacity"], m.HISTORY_MAX)
                self.assertEqual(s["count"], m.HISTORY_MAX)
                self.assertFalse(s["truncated"])

    def test_no_loss_is_reported_honestly(self):
        """버린 것이 없으면 0 이다 — 상한을 넘겼다고 꾸미지 않는다."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(3):
                    _append(n, i + 1)
                s = m.history_stats()
                self.assertEqual((s["dropped"], s["retained"], s["count"]), (0, 3, 3))
                self.assertFalse(s["truncated"])

    def test_response_truncation_is_announced(self):
        """한 응답에 limit 건만 실으면 **잘렸다고 말한다**."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(10):
                    _append(n, i + 1)
                s = m.history_stats(limit=4)
                self.assertEqual(s["count"], 4)
                self.assertEqual(s["retained"], 10)
                self.assertTrue(s["truncated"])
                self.assertEqual(s["limit"], 4)

    def test_rows_are_the_newest_ones(self):
        """잘릴 때 남는 쪽은 **최근**이다(오래된 것부터 버린다)."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(m.HISTORY_MAX + 5):
                    _append(n, i + 1)
                rows = m.history_stats(limit=2)["history"]
                blob = json.dumps(rows, ensure_ascii=False)
                self.assertIn(str(m.HISTORY_MAX + 5), blob)

    def test_limit_is_clamped_not_crashed(self):
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                _append(n, 1)
                for bad in (0, -5, None, m.HISTORY_MAX * 10):
                    s = m.history_stats(limit=bad)
                    self.assertTrue(1 <= s["limit"] <= m.HISTORY_MAX, bad)

    def test_keys_match_the_exposure_registry(self):
        """`HISTORY_KEYS` 는 등록부다 — 응답에 그 칸이 전부 있어야 한다."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                s = m.history_stats()
                for k in m.HISTORY_KEYS:
                    self.assertIn(k, s, k)
                self.assertIn("dropped", m.HISTORY_KEYS,
                              "손실 칸이 등록부에서 빠지면 게이트가 울어야 한다")

    def test_clear_resets_the_counter_too(self):
        """장부를 비웠으면 손실 카운터도 되돌린다(테스트 간 누수 방지)."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(m.HISTORY_MAX + 2):
                    _append(n, i + 1)
                self.assertGreater(m.history_stats()["dropped"], 0)
                _clear(n)
                self.assertEqual(m.history_stats()["dropped"], 0)

    def test_legacy_history_list_still_works(self):
        """하위호환 — 목록만 돌려주던 함수는 그대로 있다."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                _append(n, 1)
                rows = m.history(limit=5)
                self.assertIsInstance(rows, list)
                self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------
# B) 라우트 — 소켓 E2E
#
# 계약은 함수 반환값이 아니라 **화면이 읽는 응답**이다. 라우팅·가드·직렬화를
# 모두 거친 뒤에도 같은 말을 하는지 본다. 루프백 + 임의 포트, 과금 경로 없음.
# --------------------------------------------------------------------------
_E2E_ENVS = ("CALLBOT_STRICT", "CALLBOT_API_KEY", "CALLBOT_ALLOWED_ORIGINS")


class _Server(object):
    """핸들러 하나를 루프백에 띄운다. 포트는 OS 가 고른다(충돌 없음)."""

    def __init__(self, handler):
        import http.server
        import threading
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def get(self, path):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.origin + path, method="GET")
        req.add_header("Origin", self.origin)
        req.add_header("Sec-Fetch-Site", "same-origin")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw[:200]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._t.join(timeout=5)


class TestHistoryOverHttp(unittest.TestCase):
    ROUTES = {"caller_id": ("/api/caller_id", lambda: caller_id.handler),
              "partners": ("/api/partners", lambda: partners.handler),
              "disclosure": ("/api/disclosure", lambda: disclosure.handler)}

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k) for k in _E2E_ENVS}
        for k in _E2E_ENVS:
            os.environ.pop(k, None)
        cls.srv = {n: _Server(h()) for n, (_p, h) in cls.ROUTES.items()}

    @classmethod
    def tearDownClass(cls):
        for s in cls.srv.values():
            s.close()
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def tearDown(self):
        for n in LEDGERS:
            _clear(n)

    def test_loss_and_truncation_are_in_the_response(self):
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(m.HISTORY_MAX + 3):
                    _append(n, i + 1)
                status, d = self.srv[n].get("?op=history&limit=5")
                self.assertEqual(status, 200)
                self.assertTrue(d["ok"])
                self.assertEqual(len(d["history"]), 5)
                self.assertEqual(d["count"], 5)
                self.assertEqual(d["retained"], m.HISTORY_MAX)
                self.assertEqual(d["dropped"], 3)
                self.assertEqual(d["capacity"], m.HISTORY_MAX)
                self.assertTrue(d["truncated"])

    def test_default_limit_still_says_it_truncated(self):
        """기본값(50)으로도 잘린 사실이 드러난다 — 고치기 전의 결함 그 자리."""
        for n in LEDGERS:
            with self.subTest(ledger=n):
                m = _mod(n)
                _clear(n)
                for i in range(m.HISTORY_DEFAULT + 10):
                    _append(n, i + 1)
                status, d = self.srv[n].get("?op=history")
                self.assertEqual(status, 200)
                self.assertEqual(d["count"], m.HISTORY_DEFAULT)
                self.assertTrue(d["truncated"])
                self.assertGreater(d["retained"], d["count"])

    def test_bad_limit_is_rejected_with_the_field_named(self):
        for n in LEDGERS:
            with self.subTest(ledger=n):
                status, d = self.srv[n].get("?op=history&limit=0")
                self.assertEqual(status, 400)
                self.assertEqual([f.get("field") for f in d.get("details") or []],
                                 ["limit"])
                status2, _d2 = self.srv[n].get("?op=history&limit=abc")
                self.assertEqual(status2, 400)

    def test_empty_ledger_does_not_lie(self):
        for n in LEDGERS:
            with self.subTest(ledger=n):
                _clear(n)
                status, d = self.srv[n].get("?op=history")
                self.assertEqual(status, 200)
                self.assertEqual((d["count"], d["retained"], d["dropped"]), (0, 0, 0))
                self.assertFalse(d["truncated"])


# --------------------------------------------------------------------------
# C) 녹취 메타 보관기간 — 세기만 하고 파기하지 않는다
# --------------------------------------------------------------------------
T0 = 1_700_000_000.0


class TestRecordingRetentionExposed(unittest.TestCase):
    def _store(self, days=90):
        return ra.RecordingStore(retention_days=days)

    def test_overdue_record_is_counted(self):
        st = self._store()
        st.register("sim-1", "refund", 10, consent=False, now=T0)
        day = 86400.0
        self.assertEqual(st.overdue(now=T0 + 10)[0], 0)
        n, oldest = st.overdue(now=T0 + 91 * day)
        self.assertEqual(n, 1)
        self.assertEqual(oldest, int(day))

    def test_counting_does_not_purge(self):
        """조회는 장부를 바꾸지 않는다 — 파기는 되돌릴 수 없다([승인 필요])."""
        st = self._store()
        r = st.register("sim-1", "refund", 10, consent=False, now=T0)
        before = len(st.audit_log())
        s = st.stats(now=T0 + 10 ** 7)
        self.assertEqual(s["expired_unpurged"], 1)
        self.assertEqual(s["active"], 1)          # 여전히 active 다
        self.assertEqual(s["purged"], 0)
        self.assertEqual(st._records[r["record_id"]]["state"], "active")
        self.assertEqual(len(st.audit_log()), before)   # 감사 기록도 늘지 않는다

    def test_boundary_is_inclusive_like_purge_due(self):
        """`purge_due` 와 같은 경계(만료 시각 == 지금은 만료)."""
        st = self._store()
        r = st.register("sim-1", "refund", 10, consent=False, now=T0)
        exp = st._records[r["record_id"]]["expires_at"]
        self.assertEqual(st.overdue(now=exp)[0], 1)
        self.assertEqual(st.overdue(now=exp - 1)[0], 0)

    def test_purged_records_are_not_counted(self):
        st = self._store()
        st.register("sim-1", "refund", 10, consent=False, now=T0)
        st.purge_due(now=T0 + 10 ** 7)
        s = st.stats(now=T0 + 10 ** 7)
        self.assertEqual(s["expired_unpurged"], 0)
        self.assertEqual(s["oldest_expired_sec"], 0)
        self.assertEqual(s["purged"], 1)

    def test_retention_days_is_reported(self):
        st = self._store(days=30)
        self.assertEqual(st.stats()["retention_days"], 30)

    def test_mixed_ledger_reports_the_oldest(self):
        st = self._store()
        day = 86400.0
        st.register("old", "refund", 1, consent=False, now=T0)
        st.register("newer", "refund", 1, consent=False, now=T0 + 10 * day)
        st.register("fresh", "refund", 1, consent=False, now=T0 + 100 * day)
        s = st.stats(now=T0 + 101 * day)
        self.assertEqual(s["expired_unpurged"], 2)
        self.assertEqual(s["oldest_expired_sec"], int(11 * day))

    def test_broken_expires_at_does_not_crash_the_count(self):
        st = self._store()
        r = st.register("sim-1", "refund", 1, consent=False, now=T0)
        st._records[r["record_id"]]["expires_at"] = "nope"
        self.assertEqual(st.overdue(now=T0 + 10 ** 7)[0], 0)


class TestOpsStatsExposesRetention(unittest.TestCase):
    def setUp(self):
        self._saved = ra.STORE
        ra.STORE = ra.RecordingStore(retention_days=90)

    def tearDown(self):
        ra.STORE = self._saved

    def test_recording_keys_carry_the_new_counters(self):
        for k in ("expired_unpurged", "oldest_expired_sec", "retention_days"):
            self.assertIn(k, ops_stats.RECORDING_KEYS, k)

    def test_summary_normalizes_them_as_ints(self):
        ra.STORE.register("sim-1", "refund", 1, consent=False,
                          now=time.time() - 200 * 86400)
        rec = ops_stats.get_ops_summary()["recording"]
        self.assertEqual(rec["source"], "sim")
        self.assertEqual(rec["expired_unpurged"], 1)
        self.assertEqual(rec["active"], 1)
        self.assertGreater(rec["oldest_expired_sec"], 0)
        self.assertEqual(rec["retention_days"], 90)
        for k in ops_stats.RECORDING_KEYS:
            self.assertIsInstance(rec[k], int, k)

    def test_unavailable_source_keeps_the_schema(self):
        norm = ops_stats._norm_stats(None, ops_stats.RECORDING_KEYS,
                                     ops_stats.RECORDING_META)
        self.assertEqual(norm["source"], "unavailable")
        for k in ops_stats.RECORDING_KEYS:
            self.assertEqual(norm[k], 0, k)


class TestOpsStatsOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_env = {k: os.environ.get(k) for k in _E2E_ENVS}
        for k in _E2E_ENVS:
            os.environ.pop(k, None)
        cls.srv = _Server(ops_stats.handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self._store = ra.STORE
        ra.STORE = ra.RecordingStore(retention_days=90)

    def tearDown(self):
        ra.STORE = self._store

    def test_overdue_count_reaches_the_dashboard(self):
        ra.STORE.register("old", "refund", 1, consent=False,
                          now=time.time() - 200 * 86400)
        ra.STORE.register("fresh", "refund", 1, consent=False)
        status, d = self.srv.get("/api/ops_stats")
        self.assertEqual(status, 200)
        rec = d["recording"]
        self.assertEqual(rec["active"], 2)
        self.assertEqual(rec["expired_unpurged"], 1)
        self.assertEqual(rec["retention_days"], 90)
        # 읽기 전용 — 조회가 파기하지 않는다
        self.assertEqual(rec["purged"], 0)
        status2, d2 = self.srv.get("/api/ops_stats")
        self.assertEqual(status2, 200)
        self.assertEqual(d2["recording"]["active"], 2)


# --------------------------------------------------------------------------
# D) 콘솔 — 값과 **조건**을 함께 고정한다
#
# 39차에 콘솔 변이가 한 번 놓쳐졌다: 값을 그리는 자리만 보고 **보일지 말지를
# 정하는 조건**을 보지 않아, 조건에서만 빼면 회귀가 통과했다. 같은 실수를
# 반복하지 않도록 조건식까지 적는다.
# --------------------------------------------------------------------------
class TestConsoleTellsTheTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _admin_html()

    def _panel(self, fn):
        i = self.html.find(fn)
        self.assertGreater(i, 0, fn)
        return self.html[i:self.html.find("\n}", i) + 2]

    def test_no_false_append_only_claim(self):
        """「append-only」 라고 적으면서 오래된 줄을 버리던 문구가 사라졌다.

        화면에 **그려지는 문자열**만 본다 — 왜 고쳤는지 적은 주석은 대상이 아니다.
        """
        for fn in ("function dcHistory(", "function ciHistory("):
            body = self._panel(fn)
            self.assertTrue("append-only" not in body,
                            "%s 에 거짓 append-only 표기가 남았다" % fn)
            self.assertTrue("수정·삭제되지 않습니다" not in body,
                            "%s 에 '삭제되지 않는다'는 거짓이 남았다" % fn)

    def test_history_note_reads_only_server_fields(self):
        i = self.html.find("function _histNote(")
        self.assertGreater(i, 0, "이력 머리글 함수가 없다")
        body = self.html[i:self.html.find("\n}", i)]
        for field in ("d.count", "d.retained", "d.dropped", "d.capacity",
                      "d.truncated"):
            self.assertIn(field, body, field)

    def test_history_note_condition_is_wired(self):
        i = self.html.find("function _histNote(")
        body = self.html[i:self.html.find("\n}", i)]
        self.assertIn("drop>0", body.replace(" ", ""),
                      "버린 건수를 보일지 말지의 조건이 없다")
        self.assertIn("버린", body)

    def test_both_history_panels_use_it(self):
        for fn in ("function dcHistory(", "function ciHistory("):
            i = self.html.find(fn)
            self.assertGreater(i, 0, fn)
            body = self.html[i:self.html.find("\n}", i) + 2]
            self.assertIn("_histNote(x.d)", body, fn)

    def test_recording_tiles_show_the_losses(self):
        i = self.html.find("_osTiles('녹취·감사(메타)'")
        self.assertGreater(i, 0, "녹취 타일이 없다")
        tiles = self.html[i:self.html.find("]))", i) + 3]
        for key in ("expired_unpurged", "dropped_active", "audit_dropped"):
            self.assertIn("'%s'" % key, tiles, key)

    def test_retention_note_is_rendered_and_conditioned(self):
        self.assertIn("_osRecRet(d.recording)", self.html,
                      "보관기간 안내가 그려지지 않는다")
        i = self.html.find("function _osRecRet(")
        self.assertGreater(i, 0)
        body = self.html[i:self.html.find("\n}", i)]
        for field in ("d.retention_days", "d.expired_unpurged",
                      "d.oldest_expired_sec"):
            self.assertIn(field, body, field)
        self.assertIn("due>0", body.replace(" ", ""),
                      "만료 건수를 보일지 말지의 조건이 없다")
        self.assertIn("[승인 필요]", body, "파기 배선이 없다는 사실을 말해야 한다")

    def test_no_hardcoded_retention_number(self):
        """하드코딩한 「90일」 을 그리지 않는다 — 서버가 준 값만 쓴다(§8)."""
        i = self.html.find("function _osRecRet(")
        body = self.html[i:self.html.find("\n}", i)]
        self.assertNotIn("90", body)


# --------------------------------------------------------------------------
# E) 릴리스 게이트 — 형 주석 선언을 보는가
# --------------------------------------------------------------------------
GOOD = (
    "CAP = 10\n"
    "_STORE: dict = {}\n"
    "def put(k, v):\n"
    "    if len(_STORE) >= CAP:\n"
    "        _STORE.pop(next(iter(_STORE)))\n"
    "    _STORE[k] = v\n"
)


class TestMemCapsSeesAnnotatedStores(unittest.TestCase):
    KEY = ("fake.py", "_STORE")

    def setUp(self):
        self._saved = dict(verify.MEM_STORES)
        verify.MEM_STORES.clear()
        verify.MEM_STORES[self.KEY] = ("CAP", "테스트용")

    def tearDown(self):
        verify.MEM_STORES.clear()
        verify.MEM_STORES.update(self._saved)

    def _run(self, text):
        return verify.check_mem_caps({"fake.py": text})

    def test_annotated_store_is_found(self):
        ok, msg = self._run(GOOD)
        self.assertTrue(ok, msg)

    def test_annotated_store_without_cap_fails(self):
        """고치기 전에는 이 모양이 **통과**했다 — 선언이 보이지 않았으므로."""
        verify.MEM_STORES.clear()
        ok, msg = self._run(GOOD)
        self.assertFalse(ok, "형 주석 선언이 게이트 밖이다")
        self.assertIn("_STORE", msg)
        self.assertIn("MEM_STORES", msg)

    def test_cap_symbol_removal_still_fails(self):
        """상한 기호가 사라지면 등록부만 남는다 — 그것도 실패다."""
        ok, msg = self._run(GOOD.replace("CAP", "OTHER"))
        self.assertFalse(ok)
        self.assertIn("CAP", msg)

    def test_bare_annotation_is_not_a_store(self):
        """`X: dict` 는 저장소를 만들지 않는다 — 과차단 금지."""
        verify.MEM_STORES.clear()
        ok, msg = self._run("_STORE: dict\n")
        self.assertTrue(ok, msg)

    def test_annotated_literal_with_values_is_not_a_store(self):
        """값이 든 리터럴은 자라지 않는다 — `_order_backend._CACHE` 의 모양."""
        verify.MEM_STORES.clear()
        ok, msg = self._run('_CACHE: dict = {"key": None, "obj": None}\n')
        self.assertTrue(ok, msg)

    def test_annotated_singleton_attribute_is_found(self):
        verify.MEM_STORES.clear()
        src = ("class S:\n"
               "    def __init__(self):\n"
               "        self._recs: dict = {}\n"
               "STORE = S()\n")
        ok, msg = verify.check_mem_caps({"fake.py": src})
        self.assertFalse(ok, "싱글턴의 형 주석 속성도 프로세스 수명을 산다")
        self.assertIn("S._recs", msg)

    def test_syntax_error_is_left_to_py_compile(self):
        verify.MEM_STORES.clear()
        ok, msg = self._run("def (:\n")
        self.assertTrue(ok, msg)


class TestRegistryMatchesReality(unittest.TestCase):
    def test_repository_passes_today(self):
        ok, msg = verify.check_mem_caps(verify.api_sources())
        self.assertTrue(ok, msg)
        self.assertIn("%d개" % len(verify.MEM_STORES), msg)

    def test_previously_invisible_ledgers_are_registered(self):
        """형 주석으로 선언된 운영 장부가 등록부에 있다."""
        for key in (("caller_id.py", "_NUMBERS"), ("caller_id.py", "_HISTORY"),
                    ("partners.py", "_PARTNERS"), ("partners.py", "_ACCOUNTS"),
                    ("partners.py", "_HISTORY"), ("disclosure.py", "_TENANTS"),
                    ("disclosure.py", "_HISTORY"), ("settlement.py", "_USAGE"),
                    ("_pii_vault.py", "_KID_CACHE")):
            self.assertIn(key, verify.MEM_STORES, key)

    def test_every_registered_cap_symbol_exists(self):
        srcs = verify.api_sources()
        for (fname, store), (symbol, why) in verify.MEM_STORES.items():
            self.assertIn(fname, srcs, fname)
            self.assertIn(symbol, srcs[fname], "%s 의 %s" % (fname, store))
            self.assertTrue(why.strip(), "사유가 비어 있다: %s" % store)

    def test_removing_a_history_cap_is_caught(self):
        """저장소 파일 그대로 — 상한을 지우면 게이트가 운다.

        기호를 **전부** 바꾼다. 선언 한 줄만 바꾸면 쓰는 자리에 이름이 남아
        검사를 통과한다(효과 없는 변이는 변이가 틀린 것이다 · 37·39차 교훈).
        """
        srcs = verify.api_sources()
        srcs["caller_id.py"] = srcs["caller_id.py"].replace(
            "HISTORY_MAX", "HIST_LIMIT")
        ok, msg = verify.check_mem_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("HISTORY_MAX", msg)

    def test_losing_the_annotated_declaration_is_caught(self):
        """선언이 사라지면 등록부 잔재로 운다(양방향 규약)."""
        srcs = verify.api_sources()
        srcs["caller_id.py"] = srcs["caller_id.py"].replace(
            "_NUMBERS: dict = {}", "_NUMBERS = {'seed': 1}")
        ok, msg = verify.check_mem_caps(srcs)
        self.assertFalse(ok)
        self.assertIn("잔재", msg)


class TestCounterExposureCoversHistory(unittest.TestCase):
    def test_repository_passes_today(self):
        ok, msg = verify.check_counter_exposure(verify.api_sources())
        self.assertTrue(ok, msg)

    def test_history_ledgers_are_registered(self):
        for f in ("caller_id.py", "partners.py", "disclosure.py"):
            self.assertIn((f, "history_stats"), verify.COUNTER_EXPOSURE, f)

    def test_dropping_the_loss_key_from_the_registry_fails(self):
        """`dropped` 를 노출 목록에서 빼면 게이트가 운다(36차와 같은 규약)."""
        srcs = verify.api_sources()
        srcs["caller_id.py"] = srcs["caller_id.py"].replace(
            '"count", "retained", "dropped", "capacity", "truncated"',
            '"count", "retained", "capacity", "truncated"')
        ok, msg = verify.check_counter_exposure(srcs)
        self.assertFalse(ok)
        self.assertIn("dropped", msg)

    def test_removing_the_recording_counter_from_keys_fails(self):
        srcs = verify.api_sources()
        srcs["ops_stats.py"] = srcs["ops_stats.py"].replace(
            '"expired_unpurged", "oldest_expired_sec", "retention_days"',
            '"oldest_expired_sec", "retention_days"')
        ok, msg = verify.check_counter_exposure(srcs)
        self.assertFalse(ok)
        self.assertIn("expired_unpurged", msg)


class TestGateIsReportedByMain(unittest.TestCase):
    def test_main_reports_both_checks(self):
        import contextlib
        buf = io.StringIO()
        argv = sys.argv[:]
        sys.argv = ["verify.py"]
        try:
            with contextlib.redirect_stdout(buf):
                verify.main()
        finally:
            sys.argv = argv
        out = buf.getvalue()
        self.assertIn("mem_caps", out)
        self.assertIn("counter_exposure", out)
        self.assertIn("저장소 %d개" % len(verify.MEM_STORES), out)


if __name__ == "__main__":
    unittest.main()
