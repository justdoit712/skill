"""周额度账本：覆盖 §7.3 列出的全部验证项。

§7.3 要求验证：新 runner 读回账本、同周两次手动运行、并发触发、预留后崩溃、
模型超时、push 失败、周边界和跨年周标识，且总预留不得超过 50。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.catalog.budget import (
    SHANGHAI_TZ,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_RECOVERY,
    BudgetLedger,
    QuotaExceeded,
    evaluation_filename,
    week_id,
)

CAP = 50


def entry(n: int) -> dict:
    return {
        "evaluation_id": f"acme/repo{n}|sha256:fp{n}|1.0.1|1.0.0",
        "skill_id": f"acme/repo{n}",
        "content_fingerprint": f"sha256:fp{n}",
        "rules_version": "1.0.1",
        "model_config_version": "1.0.0",
    }


class WeekIdTest(unittest.TestCase):
    def test_format(self) -> None:
        self.assertRegex(week_id(), r"^\d{4}-W\d{2}$")

    def test_same_iso_week_same_id(self) -> None:
        monday = datetime(2026, 9, 14, 0, 0, tzinfo=SHANGHAI_TZ)
        sunday = datetime(2026, 9, 20, 23, 59, tzinfo=SHANGHAI_TZ)
        self.assertEqual(week_id(monday), week_id(sunday))

    def test_week_boundary_is_monday_midnight_local(self) -> None:
        sunday_last = datetime(2026, 9, 20, 23, 59, 59, tzinfo=SHANGHAI_TZ)
        monday_first = datetime(2026, 9, 21, 0, 0, 0, tzinfo=SHANGHAI_TZ)
        self.assertNotEqual(week_id(sunday_last), week_id(monday_first))

    def test_utc_maps_into_shanghai_week(self) -> None:
        """UTC 周日 16:00 已进入上海的周一，必须落到新的一周。"""
        utc_sunday = datetime(2026, 9, 20, 16, 0, tzinfo=timezone_utc())
        self.assertEqual(week_id(utc_sunday), week_id(datetime(2026, 9, 21, 0, 0, tzinfo=SHANGHAI_TZ)))

    def test_cross_year_uses_iso_year(self) -> None:
        """ISO 周所属年份可能与日历年不同，标识必须用 ISO 年。"""
        case = datetime(2027, 1, 1, 10, 0, tzinfo=SHANGHAI_TZ)
        expected = case.isocalendar()
        self.assertEqual(week_id(case), f"{expected.year}-W{expected.week:02d}")
        self.assertEqual(week_id(case), "2026-W53")


def timezone_utc():
    from datetime import timezone

    return timezone.utc


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = Path(self._tmp.name) / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ledger(self, **kwargs) -> BudgetLedger:
        return BudgetLedger.load(self.state, CAP, **kwargs)

    def test_new_runner_reads_back_ledger(self) -> None:
        led = self.ledger()
        led.reserve([entry(1), entry(2)])
        # 模拟全新的 runner：重新加载
        fresh = self.ledger()
        self.assertEqual(fresh.reserved_count, 2)
        self.assertEqual(fresh.remaining, CAP - 2)

    def test_two_manual_runs_same_week_share_ledger(self) -> None:
        first = self.ledger()
        first.reserve([entry(1)])
        second = self.ledger()
        second.reserve([entry(2)])
        third = self.ledger()
        self.assertEqual(third.reserved_count, 2)

    def test_concurrent_trigger_does_not_lose_reservations(self) -> None:
        """两个账本实例先后预留：第二个必须在预留前同步磁盘，不能覆盖。"""
        a = self.ledger()
        b = self.ledger()  # 与 a 同时加载，尚未看到 a 的预留
        a.reserve([entry(1)])
        b.reserve([entry(2)])
        merged = self.ledger()
        self.assertEqual(merged.reserved_count, 2)

    def test_total_reserved_never_exceeds_cap(self) -> None:
        led = self.ledger()
        led.reserve([entry(i) for i in range(CAP)])
        self.assertEqual(led.reserved_count, CAP)
        self.assertEqual(led.remaining, 0)
        with self.assertRaises(QuotaExceeded):
            led.reserve([entry(CAP + 1)])

    def test_over_cap_batch_is_rejected_whole(self) -> None:
        """超额不做部分预留。"""
        led = self.ledger()
        led.reserve([entry(i) for i in range(CAP - 1)])
        with self.assertRaises(QuotaExceeded):
            led.reserve([entry(100), entry(101)])
        self.assertEqual(self.ledger().reserved_count, CAP - 1)

    def test_reservation_survives_crash(self) -> None:
        """预留后进程被杀：名额不释放，重启后仍计入。"""
        led = self.ledger()
        led.reserve([entry(1)])
        led.begin_attempt(entry(1)["evaluation_id"])
        # 此处不 complete、不 fail，模拟崩溃
        recovered = self.ledger()
        self.assertEqual(recovered.reserved_count, 1)
        self.assertEqual(recovered.remaining, CAP - 1)
        self.assertEqual(recovered.mark_in_progress_as_needs_recovery(), [entry(1)["evaluation_id"]])
        self.assertEqual(recovered.get(entry(1)["evaluation_id"])["status"], STATUS_NEEDS_RECOVERY)

    def test_model_timeout_keeps_slot_and_counts_attempts(self) -> None:
        led = self.ledger()
        led.reserve([entry(1)])
        eid = entry(1)["evaluation_id"]

        self.assertEqual(led.begin_attempt(eid), 1)
        led.fail(eid, "MODEL_ERROR", "超时")
        self.assertEqual(led.reserved_count, 1, "失败不释放名额")
        self.assertEqual(led.get(eid)["status"], STATUS_FAILED)

        self.assertEqual(led.begin_attempt(eid), 2)
        led.fail(eid, "MODEL_ERROR", "再次超时")

        allowed, reason = led.can_attempt(eid)
        self.assertFalse(allowed)
        self.assertIn("尝试上限", reason)

    def test_manual_rerun_does_not_reset_attempts(self) -> None:
        led = self.ledger()
        led.reserve([entry(1)])
        eid = entry(1)["evaluation_id"]
        led.begin_attempt(eid)
        led.fail(eid, "MODEL_ERROR", "失败")
        # 新一次手动运行：重新加载账本
        rerun = self.ledger()
        self.assertEqual(rerun.get(eid)["attempts"], 1)
        rerun.begin_attempt(eid)
        self.assertEqual(rerun.get(eid)["attempts"], 2)

    def test_completed_id_reuses_result_and_is_not_recalled(self) -> None:
        led = self.ledger()
        led.reserve([entry(1)])
        eid = entry(1)["evaluation_id"]
        led.begin_attempt(eid)
        led.complete(eid, {"decision": "recommended"})

        allowed, reason = led.can_attempt(eid)
        self.assertFalse(allowed)
        self.assertIn("复用", reason)

        # 再次预留同一 ID 不重复占名额
        led.reserve([entry(1)])
        self.assertEqual(led.reserved_count, 1)
        self.assertEqual(led.get(eid)["status"], STATUS_COMPLETED)

    def test_push_failure_equivalent_reservation_is_durable_before_call(self) -> None:
        """§7.2 步骤 3：预留必须在调用模型之前就已落盘。

        若推送失败导致账本未进入仓库，下一次运行看不到这次预留；因此本测试固定
        「reserve() 返回后，磁盘上必须已经可见」这一契约。
        """
        led = self.ledger()
        led.reserve([entry(1)])
        on_disk = (self.state / "budget.json").read_text(encoding="utf-8")
        self.assertIn(entry(1)["evaluation_id"], on_disk)

    def test_week_rollover_starts_fresh_and_archives(self) -> None:
        monday = datetime(2026, 9, 14, 10, 0, tzinfo=SHANGHAI_TZ)
        led = BudgetLedger.load(self.state, CAP, moment=monday)
        led.reserve([entry(1)], moment=monday)
        self.assertEqual(led.reserved_count, 1)

        next_monday = monday + timedelta(days=7)
        rolled = BudgetLedger.load(self.state, CAP, moment=next_monday)
        self.assertTrue(led.ledger_path.exists(), "load must not archive files")
        rolled.rollover(next_monday)
        self.assertEqual(rolled.reserved_count, 0, "新周从空开始")
        self.assertEqual(rolled.remaining, CAP)
        self.assertEqual(rolled.rollover_from, week_id(monday))
        self.assertTrue((self.state / "history" / f"budget-{week_id(monday)}.json").exists())

    def test_records_contain_no_secrets(self) -> None:
        """§7.3：记录不含密钥，也不含个人健康或交易账户信息。"""
        led = self.ledger()
        led.reserve([entry(1)])
        led.begin_attempt(entry(1)["evaluation_id"])
        blob = led.record_path(entry(1)["evaluation_id"]).read_text(encoding="utf-8").lower()
        for marker in ("api_key", "authorization", "bearer ", "sk-", "password", "account"):
            self.assertNotIn(marker, blob, f"记录中出现可疑字段 {marker}")

    def test_evaluation_filename_is_path_safe(self) -> None:
        name = evaluation_filename("acme/repo|sha256:fp|1.0.1|1.0.0")
        self.assertRegex(name, r"^[0-9a-f]{24}\.json$")


if __name__ == "__main__":
    unittest.main()
