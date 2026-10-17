# -*- coding: utf-8 -*-
"""녹취 장부 보관 상한 회귀 (31차) — 네트워크 미사용.

왜 이 파일이 필요한가
  30차가 통화 세션(`voice._Session`)에 상한을 넣으면서 **범위 밖으로 둔 구멍**을
  보고서에 그대로 적어 뒀다: `_recording_audit.STORE._records` 에는 상한이 없다.
  이 장부는 모듈 전역 싱글턴(`STORE = RecordingStore()`)이라 수명이 프로세스와
  같고, 더 나쁜 성질이 하나 더 있다 — `purge_due()` 는 레코드를 **지우지 않고**
  묘비(state="purged")로 바꾼다. 파기 증적을 남기려는 설계지만, 그래서 **파기해도
  건수는 줄지 않는다**. 26차 `_escalation`·30차 `voice._Session` 과 같은 끝:
  메모리가 자라 결국 진행 중인 통화가 죽는다.

  감사 버퍼(`_audit`)도 같다. 등록·접근·거부·복호·파기가 전부 한 줄씩 쌓이는데
  상한이 없었다.

  방어 코드는 **초과할 때만** 돌기 때문에, 회귀가 없으면 '있는 줄만 알았지 실제로는
  안 도는' 상태가 된다(30차에 `scrub` 이 그랬다). 그래서 상한을 실제로 넘긴다.

실행: python3 -m pytest tests/test_recording_store_caps.py -q
"""
import base64
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _pii_vault as pii_vault            # noqa: E402
import _recording_audit as recording_audit  # noqa: E402

ENVS = ("RECORDING_LIVE", pii_vault.KEY_ENV, pii_vault.KEY_ENV + "_OLD")


class Base(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ENVS}
        for k in ENVS:
            os.environ.pop(k, None)
        self._urlopen = urllib.request.urlopen

        def blocked(*a, **k):               # 이 장부는 밖으로 나갈 일이 없다
            raise AssertionError("테스트가 네트워크를 호출했다: %r" % (a[:1],))

        urllib.request.urlopen = blocked

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def fill(self, st, n, consent=False, now=None):
        """n 건 등록. now 를 주면 started_at 을 1초씩 늘려 순서를 만든다."""
        out = []
        for i in range(n):
            kw = {} if now is None else {"now": now + i}
            out.append(st.register("sim-%d" % i, "refund", 1, consent=consent, **kw))
        return out


# ==========================================================================
# 1) 레코드 상한 — 실제로 넘겨서 확인한다
# ==========================================================================
class TestRecordCap(Base):
    def test_store_stops_growing_at_the_cap(self):
        st = recording_audit.RecordingStore()
        self.fill(st, recording_audit.MAX_RECORDS + 25)
        s = st.stats()
        self.assertEqual(s["total"], recording_audit.MAX_RECORDS)
        self.assertEqual(s["dropped"], 25)
        self.assertEqual(s["capacity"], recording_audit.MAX_RECORDS)

    def test_registration_keeps_working_after_the_cap(self):
        """상한에 닿아도 등록은 실패하지 않는다 — 새 통화가 거부되면 그게 장애다."""
        st = recording_audit.RecordingStore()
        self.fill(st, recording_audit.MAX_RECORDS + 5)
        r = st.register("sim-last", "refund", 1, consent=False)
        self.assertEqual(st.access("a", r["record_id"], "qa")["record_id"],
                         r["record_id"])

    def test_purged_records_are_evicted_before_active_ones(self):
        """파기된 묘비부터 버린다 — active 를 먼저 버리면 `purge_due()` 가
        파기해야 할 것을 영영 못 찾는다(파기 안 된 채 '없는 일'이 된다)."""
        st = recording_audit.RecordingStore(retention_days=1)
        old = self.fill(st, 10, now=0.0)                   # 곧 파기될 것들
        st.purge_due(now=10 * 86400.0)                     # → 묘비 10 건
        keep = self.fill(st, 10, now=10 ** 9)              # 아직 보관기간이 남은 것들
        # 상한보다 5 건 더 — 버릴 것은 5 건이고 묘비가 10 건 있으니 전부 묘비여야 한다.
        self.fill(st, recording_audit.MAX_RECORDS - 15, now=2 * 10 ** 9)
        alive = set(st._records)
        self.assertEqual(st.stats()["dropped"], 5)
        self.assertEqual(st.stats()["dropped_active"], 0)
        self.assertEqual(len([r for r in old if r["record_id"] in alive]), 5)
        for r in keep:
            self.assertIn(r["record_id"], alive, "묘비를 두고 active 를 버렸다")

    def test_evicting_live_records_is_counted_separately(self):
        """묘비가 하나도 없으면 active 를 버릴 수밖에 없다 — 그 사실을 따로 센다."""
        st = recording_audit.RecordingStore()
        self.fill(st, recording_audit.MAX_RECORDS + 3)
        s = st.stats()
        self.assertGreater(s["dropped_active"], 0)
        self.assertEqual(s["dropped_active"], s["dropped"])

    def test_oldest_active_goes_first(self):
        st = recording_audit.RecordingStore()
        first = st.register("sim-oldest", "refund", 1, now=0.0)
        self.fill(st, recording_audit.MAX_RECORDS, now=10 ** 6)
        self.assertNotIn(first["record_id"], st._records)

    def test_eviction_is_not_silent(self):
        st = recording_audit.RecordingStore()
        self.fill(st, recording_audit.MAX_RECORDS + 2)
        evicts = [a for a in st.audit_log() if a["action"] == "evict"]
        self.assertTrue(evicts, "축출이 감사기록에 남지 않았다")
        self.assertIn("보관 상한", evicts[-1]["note"])

    def test_eviction_note_carries_no_identifiers(self):
        """사유 문구는 우리가 만든 고정 문구뿐 — 세션ID·참조가 새지 않는다."""
        st = recording_audit.RecordingStore()
        self.fill(st, recording_audit.MAX_RECORDS + 2)
        for a in st.audit_log():
            if a["action"] == "evict":
                self.assertNotIn("sim-", a["note"])
                self.assertNotIn("s3://", a["note"])

    def test_sealed_envelope_is_shredded_before_the_record_is_dropped(self):
        """버리기 전에 복호 재료를 지운다 — dict 사본이 남아도 되돌릴 수 없다."""
        os.environ["RECORDING_LIVE"] = "1"
        os.environ[pii_vault.KEY_ENV] = base64.b64encode(b"K" * 32).decode()
        st = recording_audit.RecordingStore()
        sealed = st.register("sim-sealed", "refund", 1, consent=True,
                             audio_ref="s3://a/rec.wav", now=0.0)
        victim = st._records[sealed["record_id"]]
        self.fill(st, recording_audit.MAX_RECORDS, now=10 ** 6)
        self.assertNotIn(sealed["record_id"], st._records)
        self.assertTrue(pii_vault.is_shredded(victim["audio_ref"]),
                        "봉투가 복호 가능한 채로 버려졌다")
        with self.assertRaises(pii_vault.VaultShredded):
            pii_vault.unseal(victim["audio_ref"], "%s/audio" % sealed["record_id"])

    def test_purge_still_works_on_what_survived(self):
        st = recording_audit.RecordingStore(retention_days=1)
        self.fill(st, recording_audit.MAX_RECORDS + 5, now=0.0)
        purged = st.purge_due(now=10 * 86400.0)
        self.assertTrue(purged)
        self.assertEqual(st.stats()["active"], 0)


# ==========================================================================
# 2) 감사 버퍼 상한
# ==========================================================================
class TestAuditCap(Base):
    def _spin(self, st, rid, n):
        for _ in range(n):
            try:
                st.access("a", rid, "marketing")     # 거부도 한 줄씩 쌓인다
            except PermissionError:
                pass

    def test_audit_buffer_stops_growing(self):
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 1)
        self._spin(st, r["record_id"], recording_audit.MAX_AUDIT + 50)
        self.assertLessEqual(len(st.audit_log()), recording_audit.MAX_AUDIT)

    def test_dropped_audit_lines_are_counted(self):
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 1)
        self._spin(st, r["record_id"], recording_audit.MAX_AUDIT + 50)
        # register 1 줄 + 거부 MAX+50 줄 → 상한을 넘긴 51 줄이 버려진다
        self.assertEqual(st.stats()["audit_dropped"], 51)

    def test_newest_lines_survive(self):
        """오래된 것부터 버린다 — 방금 일어난 접근이 먼저 사라지면 쓸모가 없다."""
        st = recording_audit.RecordingStore()
        r = st.register("sim-1", "refund", 1)
        self._spin(st, r["record_id"], recording_audit.MAX_AUDIT + 10)
        self.assertEqual(st.audit_log()[-1]["action"], "access_denied")
        self.assertNotIn("register", [a["action"] for a in st.audit_log()])

    def test_audit_log_is_still_a_copy(self):
        st = recording_audit.RecordingStore()
        st.register("sim-1", "refund", 1)
        got = st.audit_log()
        got[0]["action"] = "tampered"
        self.assertNotEqual(st.audit_log()[0]["action"], "tampered")


# ==========================================================================
# 3) 드러남 — 센 것을 보여주지 않으면 센 것이 아니다
# ==========================================================================
class TestExposure(Base):
    def test_stats_declares_the_nature_of_this_ledger(self):
        s = recording_audit.RecordingStore().stats()
        self.assertEqual(s["scope"], "instance")      # 클러스터 합산 아님
        self.assertTrue(s["volatile"])                # 재시작 시 소멸

    def test_ops_stats_exports_the_loss(self):
        import _recording_audit as ra
        import ops_stats
        orig = ra.STORE
        ra.STORE = recording_audit.RecordingStore()
        try:
            self.fill(ra.STORE, recording_audit.MAX_RECORDS + 4)
            block = ops_stats.get_ops_summary()["recording"]
        finally:
            ra.STORE = orig
        self.assertEqual(block["source"], "sim")
        self.assertGreater(block["dropped"], 0)
        self.assertIn("audit_dropped", block)
        self.assertEqual(block["scope"], "instance")

    def test_ops_stats_keys_cover_every_loss_counter(self):
        import ops_stats
        for k in ("dropped", "dropped_active", "audit_dropped"):
            self.assertIn(k, ops_stats.RECORDING_KEYS,
                          "%s 를 세기만 하고 내보내지 않는다" % k)

    def test_caps_follow_the_house_scale(self):
        """눈금 규약 — 모듈마다 제각각이면 운영자가 어느 것이 큰지 알 수 없다."""
        import _call_metrics
        import _escalation
        self.assertEqual(recording_audit.MAX_RECORDS, _call_metrics.MAX_RECORDS)
        self.assertEqual(recording_audit.MAX_AUDIT, _escalation.MAX_AUDIT)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
