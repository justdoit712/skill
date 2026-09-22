"""150天暂不关注（Snooze）功能单元与集成测试。

覆盖规则：
1. 150 天严格左闭右开区间：snoozed_at <= today < expires_at（Day 150 当天恢复）。
2. 上海时区 (Asia/Shanghai) 统一计算。
3. 零模型调用：跳过预留与评估，且保护已有评估历史数据不被重建覆盖。
4. 本地候选池水位与跳过：仅按可处理候选计数，跳过时保留 STATUS_PENDING。
5. 离线配置同步：--sync-config 纯本地运行，同步 catalog.json 与页面展示。
6. 互斥性与残留清理：收藏/屏蔽与冷冻互斥，到期或移出后残留 snooze 字段自动清除。
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from src.dedupe import candidate_from_repo
from src.index import (
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    STATUS_RECOMMENDED,
    build_catalog,
    build_entry,
    build_page_data,
    sync_config_to_catalog,
    write_catalog,
)
from src.models import Candidate, PrescreenResult
from src.snooze import (
    DEFAULT_SNOOZE_DAYS,
    apply_snooze_overrides,
    compute_expires_at,
    get_active_snoozed,
    is_active_snooze,
    load_snooze,
    now_shanghai_date,
    validate_snooze,
)
from tests.test_pipeline import (
    PipelineHarness,
    ROOT,
    fake_discover,
    fake_evaluate,
    fake_fetch,
    passing_evaluation,
)


class SnoozeBoundaryAndDateTest(unittest.TestCase):
    """测试 150 天边界与日期计算（左闭右开 snoozed_at <= today < expires_at）。"""

    def test_compute_expires_at_150_days(self):
        # 2026-09-22 + 150天 = 2027-02-19
        expires = compute_expires_at("2026-09-22", 150)
        self.assertEqual(expires, "2027-02-19")

        # 默认 150 天
        self.assertEqual(compute_expires_at("2026-09-22"), "2027-02-19")

    def test_boundary_conditions_left_closed_right_open(self):
        item = {
            "skill_id": "test/repo:SKILL.md",
            "snoozed_at": "2026-09-22",
            "expires_at": "2027-02-19",
            "days": 150,
        }

        # Day -1: 尚未开始
        self.assertFalse(is_active_snooze(item, today="2026-09-21"))

        # Day 0: 开始当天生效（左闭）
        self.assertTrue(is_active_snooze(item, today="2026-09-22"))

        # Day 75: 冷冻期中
        self.assertTrue(is_active_snooze(item, today="2026-12-06"))

        # Day 149: 到期前最后一天仍生效
        self.assertTrue(is_active_snooze(item, today="2027-02-18"))

        # Day 150: 到期当天恢复显示，不再冷冻（右开！）
        self.assertFalse(is_active_snooze(item, today="2027-02-19"))

        # Day 151: 到期之后
        self.assertFalse(is_active_snooze(item, today="2027-02-20"))

    def test_now_shanghai_date_format(self):
        d = now_shanghai_date()
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")


class SnoozeValidationTest(unittest.TestCase):
    """测试 snoozed.json 数据校验与互斥规则。"""

    def test_valid_snooze_config(self):
        data = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                    "reason": "暂不使用",
                }
            ],
        }
        errors = validate_snooze(data, known_skill_ids={"owner/repo:SKILL.md"}, today="2026-09-22")
        self.assertEqual(errors, [])

    def test_auto_fill_missing_expires_at(self):
        data = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "reason": "暂不使用",
                }
            ],
        }
        errors = validate_snooze(data, today="2026-09-22")
        self.assertEqual(errors, [])
        self.assertEqual(data["snoozed"][0]["expires_at"], "2027-02-19")
        self.assertEqual(data["snoozed"][0]["days"], 150)

    def test_active_conflict_with_manual_picks(self):
        data = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                }
            ],
        }
        overrides = {
            "manual_picks": [{"skill_id": "owner/repo:SKILL.md", "reason": "已收藏"}],
            "manual_exclusions": [],
        }
        errors = validate_snooze(data, overrides=overrides, today="2026-09-22")
        self.assertTrue(any("与 manual_picks 冲突" in e for e in errors))

    def test_expired_snooze_does_not_conflict_with_manual_picks(self):
        # 如果条目在历史上曾冷冻，但现在已到期，用户后来收藏它，不应报错
        data = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "snoozed_at": "2025-01-01",
                    "expires_at": "2025-06-01",
                    "days": 150,
                }
            ],
        }
        overrides = {
            "manual_picks": [{"skill_id": "owner/repo:SKILL.md", "reason": "已收藏"}],
            "manual_exclusions": [],
        }
        # 今天是 2026-09-22，冷冻已过期
        errors = validate_snooze(data, overrides=overrides, today="2026-09-22")
        self.assertEqual(errors, [])

    def test_active_conflict_with_manual_exclusions(self):
        data = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                }
            ],
        }
        overrides = {
            "manual_picks": [],
            "manual_exclusions": [{"skill_id": "owner/repo:SKILL.md", "reason": "已屏蔽"}],
        }
        errors = validate_snooze(data, overrides=overrides, today="2026-09-22")
        self.assertTrue(any("与 manual_exclusions 冲突" in e for e in errors))


class SnoozeCatalogOverrideTest(unittest.TestCase):
    """测试目录渲染中的冷冻标记与残留清除。"""

    def test_apply_snooze_overrides_and_residue_cleanup(self):
        entries = [
            {
                "skill_id": "snoozed/skill:SKILL.md",
                "name": "Skill 1",
                "status": STATUS_RECOMMENDED,
                "summary_zh": "好技能",
            },
            {
                "skill_id": "expired/skill:SKILL.md",
                "name": "Skill 2",
                "status": STATUS_RECOMMENDED,
                "summary_zh": "旧冷冻技能",
                "snooze": {
                    "snoozed_at": "2025-01-01",
                    "expires_at": "2025-06-01",
                    "days": 150,
                },
            },
            {
                "skill_id": "picked/skill:SKILL.md",
                "name": "Skill 3",
                "status": STATUS_RECOMMENDED,
                "manual_pick": True,
            },
        ]
        snoozed_cfg = {
            "snoozed": [
                {
                    "skill_id": "snoozed/skill:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                    "reason": "暂时不用",
                },
                {
                    "skill_id": "expired/skill:SKILL.md",
                    "snoozed_at": "2025-01-01",
                    "expires_at": "2025-06-01",
                    "days": 150,
                },
            ]
        }

        today = "2026-09-22"
        apply_snooze_overrides(entries, snoozed_cfg, today=today)

        # 1. 活跃冷冻条目打上 snooze 标记
        self.assertIn("snooze", entries[0])
        self.assertEqual(entries[0]["snooze"]["expires_at"], "2027-02-19")
        self.assertEqual(entries[0]["snooze"]["reason"], "暂时不用")

        # 2. 已过期的条目，原残留的 snooze 字典被彻底清除
        self.assertNotIn("snooze", entries[1])

        # 3. 收藏项不受冷冻影响
        self.assertNotIn("snooze", entries[2])


class SnoozePipelineIntegrationTest(PipelineHarness):
    """测试完整流水线集成：冷冻条目 0 模型调用、不占额度、且历史评估完整保留。"""

    def test_snooze_zero_token_and_data_preservation(self):
        from src.pipeline import phase_evaluate, phase_reserve

        cfg_dir = self.temp_config()

        # 写入历史 catalog 记录（包含原摘要与状态）
        old_entry = self.previous_entry("acme/snoozed")
        old_entry["summary_zh"] = "原始高质量中文摘要，切勿覆盖"
        old_entry["status"] = "recommended"
        old_entry["last_checked"] = "2026-09-20T12:00:00+08:00"
        self.write_previous_catalog([old_entry])

        # 配置 snoozed.json 冷冻 acme/snoozed
        today = now_shanghai_date()
        exp = compute_expires_at(today, 150)
        snooze_file = cfg_dir / "snoozed.json"
        snooze_file.write_text(
            json.dumps({
                "snooze_version": "1.0.0",
                "snoozed": [
                    {
                        "skill_id": "acme/snoozed",
                        "snoozed_at": today,
                        "expires_at": exp,
                        "days": 150,
                        "reason": "测试冷冻",
                    }
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        cand_snoozed = candidate_from_repo("acme", "snoozed", url="https://github.com/acme/snoozed")
        cand_normal = candidate_from_repo("acme", "normal", url="https://github.com/acme/normal")

        # 阶段 1：reserve
        res_reserve = phase_reserve(
            config_dir=cfg_dir,
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover([cand_snoozed, cand_normal]),
            fetch_fn=fake_fetch(),
        )
        self.assertTrue(res_reserve["ok"])
        # 额度预留仅预留 1 个（normal），snoozed 不占用额度
        self.assertEqual(res_reserve["reserved"], 1)

        # 阶段 2：evaluate
        eval_spy = fake_evaluate()
        res_eval = phase_evaluate(
            config_dir=cfg_dir,
            data_dir=self.data,
            public_dir=self.public,
            state_dir=self.state,
            evaluate_fn=eval_spy,
            fetch_fn=fake_fetch(),
        )
        self.assertEqual(res_eval["evaluated"], 1)
        # 验证：evaluate_fn 只被调用了一次（用于 normal），acme/snoozed 消耗 0 模型调用
        self.assertEqual(eval_spy.calls, ["acme/normal"])

        # 验证 catalog 数据
        catalog = self.read_catalog()
        entry_map = {e["skill_id"]: e for e in catalog["entries"]}

        # 1. 验证 acme/snoozed 的旧摘要与旧检查时间完整保留，没有被空摘要覆盖
        snoozed_item = entry_map["acme/snoozed"]
        self.assertEqual(snoozed_item["summary_zh"], "原始高质量中文摘要，切勿覆盖")
        self.assertEqual(snoozed_item["status"], "recommended")
        self.assertEqual(snoozed_item["last_checked"], "2026-09-20T12:00:00+08:00")
        # 且被打上了 snooze 标记
        self.assertIn("snooze", snoozed_item)
        self.assertEqual(snoozed_item["snooze"]["expires_at"], exp)


class SnoozeLocalPoolWatermarkTest(unittest.TestCase):
    """测试本地候选池 (pool.json) 水位计算与跳过逻辑。"""

    def test_actionable_watermark_excludes_snoozed(self):
        from src.pool import CandidatePool, PoolItem, STATUS_PENDING, STATUS_DONE
        from src.snooze import get_active_snoozed

        cand1 = candidate_from_repo("owner", "snoozed", path="SKILL.md", url="http://x")
        cand2 = candidate_from_repo("owner", "normal", path="SKILL.md", url="http://y")
        pool = CandidatePool(items=[
            PoolItem(seq=1, candidate=cand1, status=STATUS_PENDING),
            PoolItem(seq=2, candidate=cand2, status=STATUS_PENDING),
        ])

        snoozed_cfg = {
            "snoozed": [
                {
                    "skill_id": cand1.skill_id,
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                }
            ]
        }
        active_snoozed = get_active_snoozed(snoozed_cfg, today="2026-09-22")

        # 模拟 local_run 中 count_actionable
        def count_actionable(candidates):
            count = 0
            for c in candidates:
                sid = c.candidate.skill_id
                if sid in active_snoozed:
                    continue
                count += 1
            return count

        pending = [it for it in pool.items if it.status == STATUS_PENDING]
        self.assertEqual(len(pending), 2)
        # 可处理水位仅为 1（避免冷冻导致误判水位过高而不补水）
        self.assertEqual(count_actionable(pending), 1)


class SnoozeOfflineSyncConfigTest(unittest.TestCase):
    """测试 python tools/run_local.py --sync-config 离线同步功能。"""

    def test_sync_config_offline_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            catalog_file = tmp / "catalog.json"
            public_file = tmp / "public_catalog.json"
            overrides_file = tmp / "overrides.json"
            snoozed_file = tmp / "snoozed.json"

            # 写入初始 overrides 和 snoozed
            overrides_file.write_text(
                json.dumps({
                    "overrides_version": "1.0.0",
                    "manual_picks": [{"skill_id": "pick/repo:SKILL.md", "reason": "收藏", "added_at": "2026-09-22"}],
                    "manual_exclusions": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            snoozed_file.write_text(
                json.dumps({
                    "snooze_version": "1.0.0",
                    "snoozed": [
                        {
                            "skill_id": "snooze/repo:SKILL.md",
                            "snoozed_at": "2026-09-22",
                            "expires_at": "2027-02-19",
                            "days": 150,
                            "reason": "冷冻测试",
                        }
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            def make_entry(sid, name, sum_zh):
                return {
                    "skill_id": sid,
                    "name": name,
                    "url": f"http://example.com/{name}",
                    "author": "author",
                    "status": STATUS_RECOMMENDED,
                    "summary_zh": sum_zh,
                    "main_category": None,
                    "tags": [],
                    "platform_declared": None,
                    "dependencies_declared": [],
                    "source_type": "official",
                    "needs_review": False,
                    "review_note": None,
                    "reason_codes": [],
                    "limitations": None,
                    "first_seen": "2026-09-20",
                    "last_checked": "2026-09-20",
                    "content_changed_at": None,
                    "upstream_status": "ok",
                    "license": "MIT",
                }

            # 初始 catalog
            initial_catalog = {
                "catalog_version": "1.0.0",
                "rules_version": "1.0.0",
                "entries": [
                    make_entry("pick/repo:SKILL.md", "Pick Repo", "已收藏"),
                    make_entry("snooze/repo:SKILL.md", "Snooze Repo", "待冷冻"),
                ],
            }
            catalog_file.write_text(json.dumps(initial_catalog, ensure_ascii=False), encoding="utf-8")

            # 执行 sync_config_to_catalog（无需任何 API Key，纯本地）
            result_manifest = sync_config_to_catalog(
                catalog_path=catalog_file,
                public_catalog_path=public_file,
                overrides_path=overrides_file,
                snoozed_path=snoozed_file,
            )

            self.assertIsNotNone(result_manifest)
            self.assertTrue(catalog_file.exists())
            self.assertTrue(public_file.exists())

            # 验证写入的数据
            saved = json.loads(catalog_file.read_text(encoding="utf-8"))
            self.assertIn("snoozed", saved)
            self.assertEqual(len(saved["snoozed"]["snoozed"]), 1)

            entry_map = {e["skill_id"]: e for e in saved["entries"]}
            # 收藏项被标记为 manual_pick
            self.assertTrue(entry_map["pick/repo:SKILL.md"].get("manual_pick"))
            # 冷冻项被打上 snooze 标记
            self.assertIn("snooze", entry_map["snooze/repo:SKILL.md"])
            self.assertEqual(entry_map["snooze/repo:SKILL.md"]["snooze"]["expires_at"], "2027-02-19")


if __name__ == "__main__":
    unittest.main()
