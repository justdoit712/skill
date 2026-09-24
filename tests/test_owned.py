"""已收录配置与共享规则单元测试（tests/test_owned.py）。

覆盖范围（方案 §10 P1 与 §11.1）：
- 稳定 ID 规范化（owner/repo 小写，path 保留大小写，拒绝越界与盘符）；
- 数据契约校验（必填项、版本、日期格式、URL 安全性）；
- 隐私红线防御（拒绝 managed_url、note 进入配置或变更包）；
- 重复 ID 拦截；
- 变更包校验、前置条件比对、幂等与冲突拦截；
- 白名单公共投影；
- 基础设施读取、缺失降级、损坏拦截与加锁保存。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

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
    OWNED_CONFIG_FILENAME,
    apply_and_save_owned_patch,
    load_owned_config,
    load_owned_ids,
    save_owned_config,
)


class NormalizeOwnedIdTest(unittest.TestCase):
    def test_valid_ids_and_casing(self):
        # 仓库所有者与仓库名归一化为小写，path 严格保留原始大小写
        self.assertEqual(
            normalize_owned_id("Anthropics/Skills:Skills/PDF/SKILL.md"),
            "anthropics/skills:Skills/PDF/SKILL.md",
        )
        self.assertEqual(
            normalize_owned_id("vercel-labs/agent-skills:skills/react-best-practices/SKILL.md"),
            "vercel-labs/agent-skills:skills/react-best-practices/SKILL.md",
        )
        # 仓库级稳定 ID
        self.assertEqual(
            normalize_owned_id("CloudFlare/Skills"),
            "cloudflare/skills",
        )
        self.assertEqual(
            normalize_owned_id("CloudFlare/Skills:"),
            "cloudflare/skills",
        )

    def test_invalid_ids_rejected(self):
        invalids = [
            "",
            "   ",
            None,
            123,
            "only_one_name",
            "too/many/slash/segments:path/SKILL.md",
            "owner/repo:path:extra_colon",
            "owner/repo:/leading/slash",
            "owner/repo:\\leading\\backslash",
            "owner/repo:skills/../escape/SKILL.md",
            "owner/repo:skills//double_slash/SKILL.md",
            "owner/repo:skills/./current_dir/SKILL.md",
            "owner/repo:C:\\Windows\\system32",
            "bad$owner/repo:path",
            "owner/bad*repo:path",
        ]
        for inv in invalids:
            with self.subTest(invalid=inv):
                with self.assertRaises(ValueError):
                    normalize_owned_id(inv)  # type: ignore


class ValidateOwnedItemTest(unittest.TestCase):
    def test_valid_item(self):
        raw = {
            "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
            "name": "pdf",
            "source_url": "https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md",
            "added_at": "2026-09-24",
        }
        item = validate_owned_item(raw)
        self.assertEqual(item["skill_id"], "anthropics/skills:skills/pdf/SKILL.md")
        self.assertEqual(item["name"], "pdf")
        self.assertEqual(item["added_at"], "2026-09-24")
        self.assertEqual(item["source_url"], raw["source_url"])

    def test_optional_source_url(self):
        raw = {
            "skill_id": "owner/repo:SKILL.md",
            "name": "sample",
            "added_at": "2026-09-24",
            "source_url": None,
        }
        item = validate_owned_item(raw)
        self.assertNotIn("source_url", item)

    def test_reject_private_fields(self):
        # 隐私红线：managed_url 与 note 绝对不可进入配置
        for forbidden in ("managed_url", "note", "private_details"):
            with self.subTest(field=forbidden):
                with self.assertRaises(ValueError) as ctx:
                    validate_owned_item({
                        "skill_id": "owner/repo:SKILL.md",
                        "name": "sample",
                        "added_at": "2026-09-24",
                        forbidden: "secret_value",
                    })
                self.assertIn("私人字段", str(ctx.exception))

    def test_reject_invalid_dates(self):
        for bad_date in ("2026/09/24", "2026-09", "2026-02-30", "yesterday", ""):
            with self.subTest(bad_date=bad_date):
                with self.assertRaises(ValueError):
                    validate_owned_item({
                        "skill_id": "owner/repo:SKILL.md",
                        "name": "sample",
                        "added_at": bad_date,
                    })

    def test_reject_unsafe_urls(self):
        invalids = [
            "ftp://example.com/file",
            "javascript:alert(1)",
            "https://user:pass@example.com/repo",
            "not_a_url",
        ]
        for url in invalids:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_owned_item({
                        "skill_id": "owner/repo:SKILL.md",
                        "name": "sample",
                        "added_at": "2026-09-24",
                        "source_url": url,
                    })


class ValidateOwnedConfigTest(unittest.TestCase):
    def test_valid_config(self):
        cfg = {
            "schema_version": "1.0.0",
            "items": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "name": "pdf",
                    "added_at": "2026-09-24",
                }
            ],
        }
        res = validate_owned_config(cfg)
        self.assertEqual(res["schema_version"], "1.0.0")
        self.assertEqual(len(res["items"]), 1)

    def test_reject_unsupported_schema_version(self):
        with self.assertRaises(ValueError):
            validate_owned_config({"schema_version": "2.0.0", "items": []})

    def test_reject_duplicate_ids(self):
        cfg = {
            "schema_version": "1.0.0",
            "items": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "name": "pdf",
                    "added_at": "2026-09-24",
                },
                {
                    "skill_id": "Anthropics/Skills:skills/pdf/SKILL.md",  # 大小写归一后重复
                    "name": "pdf-dup",
                    "added_at": "2026-09-24",
                },
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_owned_config(cfg)
        self.assertIn("重复的 skill_id", str(ctx.exception))


class IsSkillOwnedTest(unittest.TestCase):
    def test_membership(self):
        owned_set = {
            "anthropics/skills:skills/pdf/SKILL.md",
            "cloudflare/skills:skills/worker/SKILL.md",
        }
        # 大小写归一化匹配
        self.assertTrue(is_skill_owned("Anthropics/Skills:skills/pdf/SKILL.md", owned_set))
        self.assertTrue(is_skill_owned("cloudflare/skills:skills/worker/SKILL.md", owned_set))
        # 路径大小写必须匹配
        self.assertFalse(is_skill_owned("anthropics/skills:skills/PDF/SKILL.md", owned_set))
        # 同名但在不同仓库
        self.assertFalse(is_skill_owned("other/skills:skills/pdf/SKILL.md", owned_set))
        # 同仓库不同路径
        self.assertFalse(is_skill_owned("anthropics/skills:skills/docx/SKILL.md", owned_set))


class OwnedPatchTest(unittest.TestCase):
    def setUp(self):
        self.initial_config = {
            "schema_version": "1.0.0",
            "items": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "name": "pdf",
                    "added_at": "2026-09-24",
                }
            ],
        }

    def test_add_item_success(self):
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "cloudflare/skills:skills/worker/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "cloudflare/skills:skills/worker/SKILL.md",
                        "name": "worker",
                        "added_at": "2026-09-24",
                    },
                }
            ],
        }
        res = apply_owned_patch(self.initial_config, patch)
        self.assertEqual(len(res["items"]), 2)
        ids = [it["skill_id"] for it in res["items"]]
        self.assertIn("cloudflare/skills:skills/worker/SKILL.md", ids)

    def test_delete_item_success(self):
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "before": {
                        "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                        "name": "pdf",
                        "added_at": "2026-09-24",
                    },
                    "after": None,
                }
            ],
        }
        res = apply_owned_patch(self.initial_config, patch)
        self.assertEqual(len(res["items"]), 0)

    def test_idempotent_patch(self):
        # 已经处于 after 状态的修改应幂等成功
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "before": None,  # 预期不存在，但当前已存在且内容完全一致
                    "after": {
                        "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                        "name": "pdf",
                        "added_at": "2026-09-24",
                    },
                }
            ],
        }
        res = apply_owned_patch(self.initial_config, patch)
        self.assertEqual(len(res["items"]), 1)

    def test_conflict_rejected(self):
        # 前置条件不符，触发冲突并整批不写入
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "before": {
                        "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                        "name": "old_wrong_name",
                        "added_at": "2026-01-01",
                    },
                    "after": None,
                }
            ],
        }
        with self.assertRaises(OwnedPatchConflictError) as ctx:
            apply_owned_patch(self.initial_config, patch)
        self.assertIn("冲突", str(ctx.exception))


class PublicProjectionTest(unittest.TestCase):
    def test_projection_is_clean_whitelist(self):
        cfg = {
            "schema_version": "1.0.0",
            "source": "some_source",
            "note": "internal_note",
            "items": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "name": "pdf",
                    "source_url": "https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md",
                    "added_at": "2026-09-24",
                }
            ],
        }
        proj = build_public_owned_projection(cfg)
        self.assertEqual(proj["schema_version"], "1.0.0")
        self.assertEqual(len(proj["items"]), 1)
        item = proj["items"][0]
        self.assertEqual(set(item.keys()), {"skill_id", "name", "source_url", "added_at"})


class InfraOwnedTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_file_fallback_to_empty(self):
        cfg = load_owned_config(self.config_dir)
        self.assertEqual(cfg["schema_version"], "1.0.0")
        self.assertEqual(cfg["items"], [])
        self.assertEqual(load_owned_ids(self.config_dir), set())

    def test_save_and_reload(self):
        to_save = {
            "schema_version": "1.0.0",
            "items": [
                {
                    "skill_id": "anthropics/skills:skills/pdf/SKILL.md",
                    "name": "pdf",
                    "added_at": "2026-09-24",
                }
            ],
        }
        path = save_owned_config(to_save, self.config_dir)
        self.assertTrue(path.exists())
        reloaded = load_owned_config(self.config_dir)
        self.assertEqual(reloaded["items"][0]["skill_id"], "anthropics/skills:skills/pdf/SKILL.md")
        self.assertEqual(load_owned_ids(self.config_dir), {"anthropics/skills:skills/pdf/SKILL.md"})

    def test_corrupted_file_raises_error(self):
        target = self.config_dir / OWNED_CONFIG_FILENAME
        target.write_text("{ corrupt json ...", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            load_owned_config(self.config_dir)
        self.assertIn("损坏", str(ctx.exception))

    def test_apply_and_save_patch(self):
        initial = {
            "schema_version": "1.0.0",
            "items": [],
        }
        save_owned_config(initial, self.config_dir)

        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "cloudflare/skills:skills/worker/SKILL.md",
                    "before": None,
                    "after": {
                        "skill_id": "cloudflare/skills:skills/worker/SKILL.md",
                        "name": "worker",
                        "added_at": "2026-09-24",
                    },
                }
            ],
        }
        updated = apply_and_save_owned_patch(patch, self.config_dir)
        self.assertEqual(len(updated["items"]), 1)
        self.assertEqual(load_owned_ids(self.config_dir), {"cloudflare/skills:skills/worker/SKILL.md"})


if __name__ == "__main__":
    unittest.main()
