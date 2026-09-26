"""目录持久化排他协调与 0-Token 恢复测试（落实 T19、T20）。

- T19: mutate_catalog 读-改-写排他锁互斥与并发冲突抛出 LockConflict
- T20: public/data/catalog.json 缺失或损坏时的 0-Token 页面重新投影与恢复
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.catalog.store import (
    LockConflict,
    catalog_lock,
    mutate_catalog,
    recover_catalog_projections,
)
from src.catalog.index import build_page_data
from src.infra.files import read_json, write_json_atomic


class StoreLockAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.public_dir = self.root / "public"
        self.data_dir.mkdir(parents=True)
        self.public_dir.mkdir(parents=True)

        self.initial_catalog = {
            "catalog_version": "1.0.0",
            "rules_version": "1.0.1",
            "generated_at": "2026-09-23T10:00:00",
            "counts": {"recommended": 1},
            "entries": [
                {
                    "skill_id": "tool/demo:SKILL.md",
                    "name": "demo",
                    "url": "https://github.com/tool/demo",
                    "author": "tool",
                    "summary_zh": "测试工具",
                    "status": "recommended",
                    "tags": ["utility"],
                    "needs_review": False,
                    "review_note": None,
                    "pending_review": None,
                    "reason_codes": [],
                    "limitations": None,
                    "first_seen": "2026-09-01",
                    "last_checked": "2026-09-23",
                    "content_changed_at": None,
                    "upstream_status": "ok",
                    "license": "MIT",
                    "source_type": "official",
                    "dependencies_declared": [],
                    "platform_declared": None,
                    "main_category": {"id": "dev", "name": "开发"},
                }
            ],
        }
        write_json_atomic(self.data_dir / "catalog.json", self.initial_catalog)
        write_json_atomic(self.public_dir / "data" / "catalog.json", build_page_data(self.initial_catalog))

    def test_t19_mutate_catalog_updates_both_data_and_public_in_order(self):
        """T19-1: mutate_catalog 成功更新主目录并自动同步生成 public/data/catalog.json。"""
        def add_entry(cat: dict) -> dict:
            new_cat = dict(cat)
            entries = list(new_cat.get("entries") or [])
            entries.append({
                "skill_id": "tool/new:SKILL.md",
                "name": "new",
                "url": "https://github.com/tool/new",
                "author": "tool",
                "summary_zh": "新工具",
                "status": "candidate",
                "tags": [],
                "needs_review": False,
                "review_note": None,
                "pending_review": None,
                "reason_codes": [],
                "limitations": None,
                "first_seen": "2026-09-23",
                "last_checked": "2026-09-23",
                "content_changed_at": None,
                "upstream_status": "ok",
                "license": None,
                "source_type": None,
                "dependencies_declared": [],
                "platform_declared": None,
                "main_category": None,
            })
            new_cat["entries"] = entries
            new_cat["counts"] = {"recommended": 1, "candidate": 1}
            return new_cat

        updated = mutate_catalog(self.root, add_entry)
        self.assertEqual(len(updated["entries"]), 2)

        # 检查主数据已更新
        saved_data = read_json(self.data_dir / "catalog.json")
        self.assertEqual(len(saved_data["entries"]), 2)

        # 检查前端公开数据也同步投影更新
        page_data = read_json(self.public_dir / "data" / "catalog.json")
        self.assertEqual(page_data["counts"]["candidate"], 1)
        self.assertEqual(page_data["counts"]["recommended"], 1)
        self.assertEqual(len(page_data["candidates"]), 1)
        self.assertEqual(page_data["candidates"][0]["skill_id"], "tool/new:SKILL.md")

    def test_t19_lock_conflict_raises_exception_when_held(self):
        """T19-2: 当锁被其他操作占有时，mutate_catalog 在超时后抛出 LockConflict，严禁静默覆盖。"""
        lock_file = self.data_dir / ".catalog.lock"
        # 模拟外部进程持有排他锁
        with catalog_lock(lock_file):
            with self.assertRaises(LockConflict):
                mutate_catalog(self.root, lambda cat: cat, timeout=0.01)

        # 外部锁释放后，可以正常重新获取
        res = mutate_catalog(self.root, lambda cat: cat)
        self.assertEqual(res["counts"]["recommended"], 1)

    def test_t20_recover_catalog_projections_restores_missing_public_page_data(self):
        """T20-1: 当 public/data/catalog.json 缺失时，通过 recover_catalog_projections 0-Token 重新投影。"""
        page_file = self.public_dir / "data" / "catalog.json"
        self.assertTrue(page_file.exists())
        page_file.unlink()
        self.assertFalse(page_file.exists())

        # 恢复投影
        recovered = recover_catalog_projections(self.root)
        self.assertTrue(recovered, "应成功恢复丢失的公开数据")
        self.assertTrue(page_file.exists())

        # 恢复的数据内容应是主目录的前端投影，且与初始一致
        restored_data = read_json(page_file)
        expected = build_page_data(self.initial_catalog)
        self.assertEqual(restored_data, expected)

        # 再次执行无需恢复，返回 False
        self.assertFalse(recover_catalog_projections(self.root))

    def test_t20_recover_catalog_projections_fixes_corrupted_public_data(self):
        """T20-2: 当 public/data/catalog.json 内容损坏或过时时，自动修正投影。"""
        page_file = self.public_dir / "data" / "catalog.json"
        # 写入损坏或不一致的内容
        page_file.write_text('{"corrupted": true}', encoding="utf-8")

        recovered = recover_catalog_projections(self.root)
        self.assertTrue(recovered, "应修复损坏的公开数据")

        restored_data = read_json(page_file)
        expected = build_page_data(self.initial_catalog)
        self.assertEqual(restored_data, expected)


    def test_recover_completed_results_does_not_overwrite_newer_entry_with_same_fingerprint(self):
        """恢复历史评估时，同指纹的历史旧记录绝不能覆盖既有目录中的新结论或让时间倒退。"""
        import shutil
        from src.catalog.maintenance import recover_completed_results

        # 1. 准备配置目录
        repo_root = Path(__file__).resolve().parents[1]
        config_dir = self.root / "config"
        if (repo_root / "config").exists():
            shutil.copytree(repo_root / "config", config_dir, dirs_exist_ok=True)
        else:
            config_dir.mkdir(parents=True, exist_ok=True)

        # 2. 准备目录已有较新的条目 (evaluated_at: 2026-09-20)
        catalog_path = self.data_dir / "catalog.json"
        write_json_atomic(catalog_path, {
            "catalog_version": "1.0.0",
            "rules_version": "1.0.1",
            "generated_at": "2026-09-20T12:00:00+00:00",
            "entries": [
                {
                    "skill_id": "test/skill:SKILL.md",
                    "name": "skill",
                    "url": "https://github.com/test/skill",
                    "author": "test",
                    "summary_zh": "最新评估结论（推荐）",
                    "status": "recommended",
                    "content_fingerprint": "fp_identical_123",
                    "last_evaluation_id": "eval_new_20260920",
                    "evaluated_at": "2026-09-20T10:00:00+00:00",
                    "last_checked": "2026-09-20T12:00:00+00:00",
                    "evaluation_rules_version": "1.0.1",
                    "main_category": {"id": "coding", "name": "编程与开发"},
                    "tags": ["coding"],
                    "key_features": ["最新亮点"],
                    "example_requests": ["测试请求"],
                    "dependencies_declared": [],
                    "reason_codes": [],
                    "needs_review": False,
                    "review_note": None,
                    "pending_review": None,
                    "first_seen": "2026-09-01",
                    "content_changed_at": None,
                    "upstream_status": "ok",
                    "license": "MIT",
                    "source_type": "official",
                    "platform_declared": None,
                    "limitations": None,
                }
            ]
        })

        # 3. 准备历史旧记录 (evaluated_at: 2026-09-01, 同指纹，旧结论为 candidate)
        evals_dir = self.data_dir / "state" / "evaluations"
        evals_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(evals_dir / "eval_old.json", {
            "evaluation_id": "eval_old_20260901",
            "skill_id": "test/skill:SKILL.md",
            "status": "completed",
            "updated_at": "2026-09-01T10:00:00+00:00",
            "rules_version": "1.0.0",
            "outcome": {
                "candidate": {
                    "skill_id": "test/skill:SKILL.md",
                    "owner": "test",
                    "repo": "skill",
                    "name": "skill",
                    "content_fingerprint": "fp_identical_123"
                },
                "prescreen": {
                    "skill_id": "test/skill:SKILL.md",
                    "decision": "pass",
                    "domains": ["coding"]
                },
                "evaluation": {
                    "status": "candidate",
                    "summary_zh": "旧评估结论（普通候选）",
                    "evaluation_rules_version": "1.0.0"
                },
                "decision": {
                    "decision": "candidate"
                },
                "evaluated_at": "2026-09-01T10:00:00+00:00"
            }
        })

        # 4. 执行恢复
        res = recover_completed_results(self.root)
        self.assertEqual(res["restored"], 0, "旧记录不应被回放覆盖")

        # 5. 校验目录条目仍为最新结论
        after_cat = read_json(catalog_path)
        entry = after_cat["entries"][0]
        self.assertEqual(entry["status"], "recommended")
        self.assertEqual(entry["summary_zh"], "最新评估结论（推荐）")
        self.assertEqual(entry["last_evaluation_id"], "eval_new_20260920")
        self.assertEqual(entry["last_checked"], "2026-09-20T12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
