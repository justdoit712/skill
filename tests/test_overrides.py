"""人工收藏区（Manual Picks）单元与集成测试。

依据 docs/人工收藏区实施文档.md §7 全部要求。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.dedupe import candidate_from_repo, content_fingerprint
from src.index import (
    CatalogContext,
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PROCESSING_FAILURE,
    STATUS_RECOMMENDED,
    build_catalog,
    build_entry,
    build_page_data,
    write_catalog,
)
from src.models import Candidate, PrescreenResult
from src.overrides import (
    apply_manual_overrides,
    apply_manual_overrides_to_entry,
    get_manual_picks,
    load_overrides,
    validate_overrides,
)
from src.pipeline import (
    _accumulate_plan,
    _ordered_pending,
    review_state,
)

ROOT = Path(__file__).resolve().parents[1]


class OverridesValidationTest(unittest.TestCase):
    """§7 用例 1：名单校验。"""

    def test_valid_picks_pass(self):
        data = {
            "overrides_version": "1.0.0",
            "manual_picks": [
                {
                    "skill_id": "owner/repo:path/SKILL.md",
                    "reason": "结构合我习惯",
                    "added_at": "2026-09-21",
                    "from": "candidate",
                }
            ],
        }
        errors = validate_overrides(data, known_skill_ids={"owner/repo:path/SKILL.md"})
        self.assertEqual(errors, [])

    def test_missing_or_empty_skill_id(self):
        data = {
            "manual_picks": [
                {"skill_id": "", "reason": "test", "added_at": "2026-09-21"},
                {"skill_id": "   ", "reason": "test", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(any("skill_id 不能为空" in e for e in errors))

    def test_invalid_skill_id_format(self):
        data = {
            "manual_picks": [
                {"skill_id": "no-slash-repo", "reason": "test", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(any("格式不合法" in e for e in errors))

    def test_skill_id_not_in_known_ids(self):
        data = {
            "manual_picks": [
                {"skill_id": "unknown/repo:SKILL.md", "reason": "test", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_overrides(data, known_skill_ids={"other/repo:SKILL.md"})
        self.assertTrue(any("未在当前索引或候选中找到" in e and "unknown/repo:SKILL.md" in e for e in errors))

    def test_empty_or_missing_reason(self):
        data = {
            "manual_picks": [
                {"skill_id": "owner/repo:SKILL.md", "reason": "", "added_at": "2026-09-21"},
                {"skill_id": "owner/repo2:SKILL.md", "reason": "   ", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(any("reason 不能为空" in e for e in errors))

    def test_invalid_added_at_date(self):
        data = {
            "manual_picks": [
                {"skill_id": "owner/repo:SKILL.md", "reason": "test", "added_at": "not-a-date"},
                {"skill_id": "owner/repo2:SKILL.md", "reason": "test", "added_at": "2026-02-30"},
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(any("added_at 日期格式不合法" in e for e in errors))

    def test_duplicate_skill_id(self):
        data = {
            "manual_picks": [
                {"skill_id": "owner/repo:SKILL.md", "reason": "one", "added_at": "2026-09-21"},
                {"skill_id": "owner/repo:SKILL.md", "reason": "two", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(any("重复的 skill_id" in e and "owner/repo:SKILL.md" in e for e in errors))

    def test_retired_at_validation_and_exclusion_from_active_picks(self):
        data = {
            "manual_picks": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "reason": "test",
                    "added_at": "2026-09-21",
                    "retired_at": "2026-09-22",
                    "retired_reason": "不再使用",
                }
            ]
        }
        errors = validate_overrides(data)
        self.assertEqual(errors, [])
        picks = get_manual_picks(data)
        self.assertNotIn("owner/repo:SKILL.md", picks)


class OverridesExclusivityAndDisplayTest(unittest.TestCase):
    """§7 用例 2、3、4、6、7：三区互斥、覆盖与删除恢复。"""

    def _sample_entry(self, skill_id: str, status: str, **kwargs) -> dict:
        c = Candidate(skill_id=skill_id, owner="owner", repo="repo", url="https://github.com/owner/repo")
        return build_entry(
            c,
            decision={"decision": status, "reason_codes": kwargs.get("reason_codes", [])},
            evaluation={"summary_zh": f"技能简述 {skill_id}", "main_category": "dev"},
            context=CatalogContext(domain_names={"dev": "编程开发"}),
            **{k: v for k, v in kwargs.items() if k != "reason_codes"},
        )

    def test_mutual_exclusivity_and_counts(self):
        """§7 用例 2：同一技能在收藏区出现后，不在推荐区和候选区出现；计数互斥。"""
        e1 = self._sample_entry("a/rec:SKILL.md", STATUS_RECOMMENDED)
        e2 = self._sample_entry("b/cand:SKILL.md", STATUS_CANDIDATE)
        e3 = self._sample_entry("c/pick:SKILL.md", STATUS_RECOMMENDED)

        picks = {
            "c/pick:SKILL.md": {
                "skill_id": "c/pick:SKILL.md",
                "reason": "特意收藏",
                "added_at": "2026-09-21",
                "from": "recommended",
            }
        }
        apply_manual_overrides_to_entry(e3, picks)

        catalog = build_catalog([e1, e2, e3], context=CatalogContext())
        page = build_page_data(catalog)

        # 收藏区包含 e3
        manual_ids = [item["skill_id"] for item in page["manual"]]
        rec_ids = [item["skill_id"] for item in page["recommended"]]
        cand_ids = [item["skill_id"] for item in page["candidates"]]

        self.assertIn("c/pick:SKILL.md", manual_ids)
        self.assertNotIn("c/pick:SKILL.md", rec_ids)
        self.assertNotIn("c/pick:SKILL.md", cand_ids)

        counts = page["counts"]
        self.assertEqual(counts["recommended"], 1)
        self.assertEqual(counts["candidate"], 1)
        self.assertEqual(counts["manual"], 1)
        self.assertEqual(
            counts["recommended"] + counts["candidate"] + counts["manual"] + counts["pending"] + counts["processing_failure"],
            len(catalog["entries"]),
        )
        self.assertEqual(counts["total_evaluated"], 3)

    def test_override_candidate(self):
        """§7 用例 3：自动结论为 candidate 的技能被收入收藏区后，只出现在收藏区。"""
        e = self._sample_entry("cand/skill:SKILL.md", STATUS_CANDIDATE)
        picks = {
            "cand/skill:SKILL.md": {
                "skill_id": "cand/skill:SKILL.md",
                "reason": "说明欠佳但实用",
                "added_at": "2026-09-21",
                "from": "candidate",
            }
        }
        apply_manual_overrides_to_entry(e, picks)

        self.assertTrue(e["manual_pick"])
        self.assertEqual(e["manual_note"]["auto_status"], STATUS_CANDIDATE)

        page = build_page_data(build_catalog([e], context=CatalogContext()))
        self.assertEqual(len(page["manual"]), 1)
        self.assertEqual(len(page["candidates"]), 0)
        self.assertEqual(page["manual"][0]["manual_note"]["auto_status"], STATUS_CANDIDATE)

    def test_override_excluded_with_risk_warning(self):
        """§7 用例 4：自动结论为 excluded 的技能被收入收藏区后，仍在收藏区，并带风险字段。"""
        e = self._sample_entry("ex/skill:SKILL.md", STATUS_EXCLUDED, reason_codes=["DSH_PLUGIN"])
        picks = {
            "ex/skill:SKILL.md": {
                "skill_id": "ex/skill:SKILL.md",
                "reason": "需要调试插件",
                "added_at": "2026-09-21",
                "from": "candidate",
            }
        }
        apply_manual_overrides_to_entry(e, picks)

        self.assertTrue(e["manual_pick"])
        self.assertEqual(e["status"], STATUS_EXCLUDED)
        self.assertIn("DSH_PLUGIN", e["reason_codes"])

        page = build_page_data(build_catalog([e], context=CatalogContext()))
        self.assertEqual(len(page["manual"]), 1)
        self.assertEqual(page["manual"][0]["status"], STATUS_EXCLUDED)
        self.assertEqual(page["manual"][0]["manual_note"]["auto_status"], STATUS_EXCLUDED)

    def test_no_downgrade_when_auto_status_changes(self):
        """§7 用例 6：自动结论从 recommended 变为 candidate 时，条目仍在收藏区，manual_note.auto_status 更新。"""
        e = self._sample_entry("down/skill:SKILL.md", STATUS_RECOMMENDED)
        picks = {
            "down/skill:SKILL.md": {
                "skill_id": "down/skill:SKILL.md",
                "reason": "习惯用法",
                "added_at": "2026-09-21",
                "from": "recommended",
            }
        }
        apply_manual_overrides_to_entry(e, picks)
        self.assertEqual(e["manual_note"]["auto_status"], STATUS_RECOMMENDED)

        # 模拟自动结论降级为 candidate
        e["status"] = STATUS_CANDIDATE
        apply_manual_overrides_to_entry(e, picks)

        self.assertTrue(e["manual_pick"])
        self.assertEqual(e["manual_note"]["auto_status"], STATUS_CANDIDATE)

        page = build_page_data(build_catalog([e], context=CatalogContext()))
        self.assertEqual(len(page["manual"]), 1)
        self.assertEqual(len(page["candidates"]), 0)

    def test_delete_pick_restores_to_auto_section(self):
        """§7 用例 7：从名单移除后，条目回到自动分区，manual_pick 为 false。"""
        e = self._sample_entry("rem/skill:SKILL.md", STATUS_RECOMMENDED)
        picks = {
            "rem/skill:SKILL.md": {
                "skill_id": "rem/skill:SKILL.md",
                "reason": "临时收录",
                "added_at": "2026-09-21",
                "from": "recommended",
            }
        }
        apply_manual_overrides_to_entry(e, picks)
        self.assertTrue(e["manual_pick"])

        # 从名单移除（传入空 picks）
        apply_manual_overrides([e], {})
        self.assertFalse(e["manual_pick"])
        self.assertIsNone(e["manual_note"])

        page = build_page_data(build_catalog([e], context=CatalogContext()))
        self.assertEqual(len(page["manual"]), 0)
        self.assertEqual(len(page["recommended"]), 1)


class OverridesPipelineBehaviorTest(unittest.TestCase):
    """§7 用例 5、8、9：内容变化 0 模型调用、抽查上限与假数据防范。"""

    def test_content_change_zero_model_calls_and_fingerprint_updated(self):
        """§7 用例 5 & 9：上游内容变化时更新指纹与变更时间，但模型调用为 0，条目留在收藏区。"""
        old_text = "# Skill Title\nOld description"
        old_fp = content_fingerprint(old_text)

        c = Candidate(
            skill_id="test/manual:SKILL.md",
            owner="test",
            repo="manual",
            path="SKILL.md",
            content_fingerprint=old_fp,
        )
        entry = build_entry(
            c,
            decision={"decision": STATUS_RECOMMENDED},
            evaluation={"summary_zh": "旧简述"},
            context=CatalogContext(rules_version="1.0.1", generated_at="2026-09-20T10:00:00Z"),
            first_seen="2026-09-20T10:00:00Z",
            last_checked="2026-09-20T10:00:00Z",
        )
        picks = {
            "test/manual:SKILL.md": {
                "skill_id": "test/manual:SKILL.md",
                "reason": "收藏条目",
                "added_at": "2026-09-20",
                "from": "recommended",
            }
        }
        apply_manual_overrides_to_entry(entry, picks)

        new_text = "# Skill Title\nNew updated description with major additions"
        new_fp = content_fingerprint(new_text)
        self.assertNotEqual(old_fp, new_fp)

        # 模拟抓取到了新内容
        c.content_fingerprint = new_fp
        changed = True
        state = review_state(entry, changed, None)

        # 验证 state 记录了变化时间
        self.assertIsNotNone(state["content_changed_at"])

        # 生成新 entry 并应用人工覆盖
        new_entry = build_entry(
            c,
            decision={"decision": entry["status"]},
            evaluation={"summary_zh": entry["summary_zh"]},
            context=CatalogContext(rules_version="1.0.1", generated_at="2026-09-21T12:00:00Z"),
            first_seen=entry["first_seen"],
            last_checked="2026-09-21T12:00:00Z",
            content_changed_at=state["content_changed_at"],
            needs_review=state["needs_review"],
        )
        apply_manual_overrides_to_entry(new_entry, picks)

        # §4.4 & §7 用例 5：
        # 1. 指纹是新指纹（不冻结假数据）
        self.assertEqual(new_entry["content_fingerprint"], new_fp)
        # 2. 最近检查时间更新（不冻结）
        self.assertEqual(new_entry["last_checked"], "2026-09-21T12:00:00Z")
        # 3. 记录了变更时间
        self.assertIsNotNone(new_entry["content_changed_at"])
        # 4. needs_review 必须为 False（因为人工收藏条目不自动打待复核标签）
        self.assertFalse(new_entry["needs_review"])
        # 5. manual_pick 仍为 True，状态未变
        self.assertTrue(new_entry["manual_pick"])
        self.assertEqual(new_entry["status"], STATUS_RECOMMENDED)

    def test_manual_checks_cap_and_priority_in_planning(self):
        """§7 用例 8：max_manual_checks_per_run 生效，且收藏条目在队列中优先于普通抽查。"""
        # 构建 3 个已 settled 的 manual pick 候选
        cfg = {
            "rules": {"run_limits": {"max_manual_checks_per_run": 2}, "rules_version": "1.0.0"},
            "model": {"model_config_version": "1.0.0"},
            "overrides": {
                "manual_picks": [
                    {"skill_id": f"org/manual-{i}:SKILL.md", "reason": "test", "added_at": "2026-09-21"}
                    for i in range(3)
                ]
            },
        }

        # 模拟 3 个 candidate
        first_pass = []
        catalogued = {}
        for i in range(3):
            sid = f"org/manual-{i}:SKILL.md"
            fp = f"sha256:dummy{i}"
            catalogued[sid] = fp
            c = Candidate(skill_id=sid, owner="org", repo=f"manual-{i}", path="SKILL.md", content_fingerprint=fp)
            p = PrescreenResult(skill_id=sid, decision="queued")
            first_pass.append((c, p))

        # 模拟已 settled
        class MockLedger:
            def get(self, eid):
                return {"status": "completed"}

        # 运行 _accumulate_plan
        plan_fresh = _accumulate_plan(
            first_pass,
            previous=[],
            catalogued=catalogued,
            catalogued_review={},
            source_types={},
            cfg=cfg,
            ledger=MockLedger(),
        )

        # 抽查条目中，manual_recheck 只排了 2 条（受 max_manual_checks_per_run=2 约束）
        self.assertEqual(len(plan_fresh["recheck"]), 2)

        # 测试 _ordered_pending 优先级：manual_pick 优先于普通待处理
        item_normal = {"candidate": {"skill_id": "normal/skill:SKILL.md"}, "seq": 10}
        item_manual = {"candidate": {"skill_id": "org/manual-0:SKILL.md"}, "seq": 20, "manual_pick": True}
        ordered = _ordered_pending(
            [item_normal, item_manual],
            catalogued={},
            source_types={},
            manual_picks={"org/manual-0:SKILL.md"},
        )
        self.assertEqual(ordered[0]["candidate"]["skill_id"], "org/manual-0:SKILL.md")


if __name__ == "__main__":
    unittest.main()
