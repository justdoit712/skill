"""tools/manage_owned.py CLI 单元与端到端集成测试。"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tools.manage_owned import apply_changes, check_changes, main
from src.infra.owned import load_owned_config, save_owned_config


def make_entry(skill_id: str, name: str, status: str = "candidate") -> dict:
    return {
        "skill_id": skill_id,
        "name": name,
        "url": f"https://github.com/{skill_id.split(':')[0]}",
        "author": "author",
        "summary_zh": f"{name} 中文简述",
        "skill_type": "skill",
        "example_requests": [],
        "key_features": [],
        "main_category": {"id": "dev", "name": "开发"},
        "tags": ["test"],
        "platform_declared": [],
        "dependencies_declared": [],
        "source_type": "github_repo",
        "status": status,
        "manual_pick": False,
        "manual_note": None,
        "snooze": None,
        "needs_review": False,
        "review_note": None,
        "pending_review": None,
        "reason_codes": [],
        "limitations": [],
        "first_seen": "2026-09-20T10:00:00+08:00",
        "last_checked": "2026-09-20T10:00:00+08:00",
        "content_changed_at": None,
        "upstream_status": "ok",
        "license": "MIT",
    }


class ManageOwnedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.public_data_dir = self.root / "public" / "data"

        self.config_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        self.public_data_dir.mkdir(parents=True)

        # 初始配置
        save_owned_config(
            {
                "schema_version": "1.0.0",
                "items": [
                    {
                        "skill_id": "owner/repo:skills/existing/SKILL.md",
                        "name": "Existing Skill",
                        "added_at": "2026-09-20",
                        "source_url": "https://github.com/owner/repo",
                    }
                ],
            },
            config_dir=self.config_dir,
        )

        # 准备 dummy catalog.json
        dummy_catalog = {
            "version": "1.0.0",
            "generated_at": "2026-09-20T10:00:00+08:00",
            "rules_version": "1.0.0",
            "entries": [
                make_entry("owner/repo:skills/existing/SKILL.md", "Existing Skill", "candidate"),
                make_entry("owner/repo:skills/new/SKILL.md", "New Skill", "recommended"),
            ],
            "overrides": {"manual_picks": [], "manual_exclusions": []},
            "snoozed": {"snoozed": []},
        }
        (self.data_dir / "catalog.json").write_text(json.dumps(dummy_catalog, ensure_ascii=False), encoding="utf-8")
        (self.public_data_dir / "catalog.json").write_text(json.dumps(dummy_catalog, ensure_ascii=False), encoding="utf-8")
        (self.config_dir / "overrides.json").write_text(json.dumps({"manual_picks": [], "manual_exclusions": []}), encoding="utf-8")
        (self.config_dir / "snoozed.json").write_text(json.dumps({"snoozed": []}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_check_changes_valid(self) -> None:
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/new/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "owner/repo:skills/new/SKILL.md",
                        "name": "New Skill",
                        "added_at": "2026-09-24",
                        "source_url": "https://github.com/owner/repo",
                    },
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        logs = []
        code = check_changes(patch_file, config_dir=self.config_dir, log_fn=logs.append)
        self.assertEqual(code, 0)
        self.assertTrue(any("合法" in msg for msg in logs))

    def test_check_changes_conflict(self) -> None:
        # Before mismatch: expected existing name to be "Wrong Name"
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/existing/SKILL.md",
                    "before": {
                        "skill_id": "owner/repo:skills/existing/SKILL.md",
                        "name": "Wrong Name",
                        "added_at": "2026-09-20",
                    },
                    "after": None,
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        logs = []
        code = check_changes(patch_file, config_dir=self.config_dir, log_fn=logs.append)
        self.assertEqual(code, 1)
        self.assertTrue(any("冲突" in msg for msg in logs))

    def test_check_changes_nonexistent_file(self) -> None:
        logs = []
        code = check_changes(self.root / "nonexistent.json", config_dir=self.config_dir, log_fn=logs.append)
        self.assertEqual(code, 1)

    def test_apply_changes_e2e_and_sync(self) -> None:
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/new/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "owner/repo:skills/new/SKILL.md",
                        "name": "New Skill",
                        "added_at": "2026-09-24",
                        "source_url": "https://github.com/owner/repo",
                    },
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        logs = []
        code = apply_changes(patch_file, root_dir=self.root, config_dir=self.config_dir, log_fn=logs.append)
        if code != 0:
            print("apply_changes failed logs:", [m.encode("ascii", "backslashreplace").decode("ascii") for m in logs])
        self.assertEqual(code, 0)
        self.assertTrue(any("离线同步完成" in msg for msg in logs))

        # 验证 config 写入
        cfg = load_owned_config(self.config_dir)
        ids = {it["skill_id"] for it in cfg["items"]}
        self.assertIn("owner/repo:skills/new/SKILL.md", ids)
        self.assertEqual(len(ids), 2)

        # 验证 public/data/catalog.json 中的投影
        public_catalog = json.loads((self.public_data_dir / "catalog.json").read_text(encoding="utf-8"))
        self.assertIn("owned", public_catalog)
        self.assertIn("owned_entries", public_catalog)
        owned_entry_ids = {e["skill_id"] for e in public_catalog["owned_entries"]}
        self.assertIn("owner/repo:skills/new/SKILL.md", owned_entry_ids)

    def test_apply_changes_idempotent(self) -> None:
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/existing/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "owner/repo:skills/existing/SKILL.md",
                        "name": "Existing Skill",
                        "added_at": "2026-09-20",
                        "source_url": "https://github.com/owner/repo",
                    },
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        logs = []
        code = apply_changes(patch_file, root_dir=self.root, config_dir=self.config_dir, log_fn=logs.append)
        self.assertEqual(code, 0)

    def test_apply_changes_conflict_no_mutation(self) -> None:
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/existing/SKILL.md",
                    "before": {
                        "skill_id": "owner/repo:skills/existing/SKILL.md",
                        "name": "Different Name",
                        "added_at": "2026-09-20",
                    },
                    "after": None,
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        logs = []
        code = apply_changes(patch_file, root_dir=self.root, config_dir=self.config_dir, log_fn=logs.append)
        self.assertEqual(code, 1)

        # 验证配置未发生任何变动
        cfg = load_owned_config(self.config_dir)
        self.assertEqual(len(cfg["items"]), 1)
        self.assertEqual(cfg["items"][0]["skill_id"], "owner/repo:skills/existing/SKILL.md")

    def test_main_cli_list_and_check(self) -> None:
        # CLI --list
        stdout = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = stdout
            code = main(["--list", "--config-dir", str(self.config_dir)], root=self.root)
            self.assertEqual(code, 0)
            self.assertIn("Existing Skill", stdout.getvalue())
        finally:
            sys.stdout = old_stdout

        # CLI --check-changes
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/new/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "owner/repo:skills/new/SKILL.md",
                        "name": "New Skill",
                        "added_at": "2026-09-24",
                    },
                }
            ],
        }
        patch_file = self.root / "patch.json"
        patch_file.write_text(json.dumps(patch), encoding="utf-8")

        code = main(["--check-changes", str(patch_file), "--config-dir", str(self.config_dir)], root=self.root)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
