"""治理与干预规则单元测试：人工干预(overrides)、冷冻(snooze)、已收录(owned)及数据隔离保护。

合并自原有 test_overrides.py、test_snooze.py、test_owned.py、test_owned_integration.py、test_manage_owned.py 与 test_data_isolation.py。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.catalog.dedupe import candidate_from_repo
from src.catalog.index import (
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PROCESSING_FAILURE,
    STATUS_RECOMMENDED,
    build_catalog,
    build_entry,
    build_page_data,
)
from src.catalog.overrides import (
    apply_manual_overrides,
    apply_manual_overrides_to_entry,
    get_manual_exclusions,
    get_manual_picks,
    load_overrides,
    validate_overrides,
)
from src.catalog.snooze import (
    DEFAULT_SNOOZE_DAYS,
    apply_snooze_overrides,
    compute_expires_at,
    get_active_snoozed,
    is_active_snooze,
    load_snooze,
    validate_snooze,
)
from src.shared.owned import (
    OWNED_SCHEMA_VERSION,
    OwnedPatchConflictError,
    apply_owned_patch,
    build_public_owned_projection,
    is_skill_owned,
    normalize_owned_id,
    validate_owned_config,
    validate_owned_item,
    validate_owned_patch,
)
from src.infra.owned import (
    apply_and_save_owned_patch,
    load_owned_config,
    load_owned_ids,
    save_owned_config,
)
from src.shared.runtime import is_test_environment
from tests import smoke
from tools.manage_owned import main as manage_owned_main

ROOT = Path(__file__).resolve().parents[1]


@smoke
class OverridesTest(unittest.TestCase):
    """人工收藏与排除名单校验及应用。"""
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
            ]
        }
        errors = validate_overrides(data)
        self.assertTrue(len(errors) > 0)

    def test_apply_manual_picks_upgrades_candidate(self):
        entry = {
            "skill_id": "author/repo:skills/demo/SKILL.md",
            "name": "demo",
            "status": "candidate",
            "reason_codes": ["NOT_STANDALONE_TOOL"],
        }
        picks = {
            "author/repo:skills/demo/SKILL.md": {
                "skill_id": "author/repo:skills/demo/SKILL.md",
                "reason": "个人偏好",
                "from": "candidate",
            }
        }
        apply_manual_overrides_to_entry(entry, picks, {})
        self.assertTrue(entry.get("manual_pick"))
        self.assertEqual(entry["manual_note"]["reason"], "个人偏好")


@smoke
class SnoozeTest(unittest.TestCase):
    """150天暂不关注（Snooze）边界与过期计算。"""
    def test_compute_expires_at_150_days(self):
        # 2026-09-22 + 150天 = 2027-02-19
        expires = compute_expires_at("2026-09-22", 150)
        self.assertEqual(expires, "2027-02-19")

    def test_is_active_snooze_interval(self):
        item = {"snoozed_at": "2026-09-01", "expires_at": "2027-01-29"}
        # 左闭右开：开始当天生效，到期当天恢复
        self.assertTrue(is_active_snooze(item, "2026-09-01"))
        self.assertTrue(is_active_snooze(item, "2026-12-01"))
        self.assertFalse(is_active_snooze(item, "2027-01-29"))
        self.assertFalse(is_active_snooze(item, "2027-02-01"))
        self.assertFalse(is_active_snooze(item, "2026-08-31"))

    def test_validate_snooze_rules(self):
        valid = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "author/tool:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                    "reason": "暂不使用",
                }
            ],
        }
        self.assertEqual(validate_snooze(valid), [])


@smoke
class OwnedTest(unittest.TestCase):
    """已收录规范化、校验、补丁合并与冲突处理。"""
    def test_normalize_owned_id(self):
        self.assertEqual(
            normalize_owned_id("Anthropics/Skills:Skills/PDF/SKILL.md"),
            "anthropics/skills:Skills/PDF/SKILL.md",
        )
        self.assertEqual(
            normalize_owned_id("vercel-labs/agent-skills:skills/react/SKILL.md"),
            "vercel-labs/agent-skills:skills/react/SKILL.md",
        )

    def test_validate_owned_item_rejects_private_fields(self):
        item = {
            "skill_id": "owner/repo:skills/demo/SKILL.md",
            "name": "demo",
            "added_at": "2026-09-24",
            "managed_url": "https://private.corp.internal/skills/demo",
        }
        with self.assertRaises(ValueError) as ctx:
            validate_owned_item(item)
        self.assertIn("managed_url", str(ctx.exception))

    def test_apply_owned_patch(self):
        base = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "owner/repo:skills/a/SKILL.md", "name": "a", "added_at": "2026-09-01"}
            ]
        }
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/b/SKILL.md",
                    "before": None,
                    "after": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "b", "added_at": "2026-09-24"},
                }
            ],
        }
        res = apply_owned_patch(base, patch)
        self.assertEqual(len(res["items"]), 2)
        self.assertTrue(any(s["skill_id"] == "owner/repo:skills/b/SKILL.md" for s in res["items"]))

    def test_patch_conflict_on_stale_base(self):
        base = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-2", "added_at": "2026-09-02"}
            ]
        }
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/b/SKILL.md",
                    "before": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-1", "added_at": "2026-09-01"},
                    "after": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-3", "added_at": "2026-09-03"},
                }
            ],
        }
        with self.assertRaises(OwnedPatchConflictError):
            apply_owned_patch(base, patch)


@smoke
class DataIsolationTest(unittest.TestCase):
    """测试环境与生产数据的物理隔离保护。"""
    def test_environment_detector_identifies_unittest_runner(self) -> None:
        self.assertTrue(is_test_environment())


if __name__ == "__main__":
    unittest.main()
