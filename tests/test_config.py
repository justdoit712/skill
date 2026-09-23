"""配置完整性：四个配置文件之间必须互相闭合。

这些断言把此前用临时脚本手工跑过的交叉校验固定下来，防止后续改动悄悄破坏一致性。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from src.catalog.config import load_all_config, load_automation, precheck

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

EXPECTED_CHECK_IDS = [
    "scope_match",
    "purpose_clarity",
    "instruction_completeness",
    "evidence_traceability",
    "dependency_transparency",
    "risk_review",
]


def load(name: str) -> dict:
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


class ConfigIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.taxonomy = load("taxonomy.json")
        cls.rules = load("rules.json")
        cls.sources = load("sources.json")
        cls.searches = load("searches.json")
        cls.model = load("model.example.json")
        cls.taxonomy_ids = [c["id"] for c in cls.taxonomy["main_categories"]]
        cls.all_reason_codes = {
            code
            for group in cls.rules["reason_codes"].values()
            for code in group
        }

    def test_ten_categories_with_unique_ids(self) -> None:
        self.assertEqual(len(self.taxonomy_ids), 10)
        self.assertEqual(len(set(self.taxonomy_ids)), 10)

    def test_issue_template_categories_match_taxonomy(self) -> None:
        """§7.5：投稿模板分类值必须与 taxonomy 严格一致。"""
        template = yaml.safe_load(
            (ROOT / ".github" / "ISSUE_TEMPLATE" / "submit-skill.yml").read_text(encoding="utf-8")
        )
        category = next(b for b in template["body"] if b.get("id") == "category")
        self.assertEqual(
            category["attributes"]["options"],
            [c["name"] for c in self.taxonomy["main_categories"]],
        )

    def test_search_domains_cover_all_categories(self) -> None:
        self.assertEqual(sorted(self.searches["per_domain"]), sorted(self.taxonomy_ids))
        for domain_id, spec in self.searches["per_domain"].items():
            self.assertTrue(spec.get("zh"), f"{domain_id} 缺中文词")
            self.assertTrue(spec.get("en"), f"{domain_id} 缺英文词")

    def test_query_template_covers_skill_md(self) -> None:
        """§4.3 要求搜索至少约束到 SKILL.md；实测不加 in:readme 命中为 0。"""
        template = self.searches["file_constraint"]["query_template"]
        self.assertIn("{term}", template)
        self.assertIn("SKILL.md", template)
        self.assertIn("in:readme", template)

    def test_rules_checks_match_spec(self) -> None:
        self.assertEqual([c["id"] for c in self.rules["checks"]], EXPECTED_CHECK_IDS)

    def test_required_fields_cover_checks_and_extras(self) -> None:
        required = self.rules["output_schema"]["required_fields"]
        for check_id in EXPECTED_CHECK_IDS:
            self.assertIn(check_id, required)
        for extra in ("domain_checks", "reason_codes", "rules_version", "source_fingerprint"):
            self.assertIn(extra, required)

    def test_sources_domain_ids_are_defined(self) -> None:
        for source in self.sources["sources"]:
            for domain_id in source["domain_ids"]:
                self.assertIn(domain_id, self.taxonomy_ids, f"{source['id']} 的 domain_id 越界")

    def test_source_verification_fields_consistent(self) -> None:
        for source in self.sources["sources"]:
            self.assertTrue(source.get("verified_at"), f"{source['id']} 缺核验时间")
            if source["verification_status"] == "not_found":
                self.assertIsNone(source["url"], f"{source['id']} not_found 却有 url")
            if source["verification_status"] == "verified":
                self.assertTrue(source["url"], f"{source['id']} verified 却没有 url")

    def test_every_reason_code_reference_resolves(self) -> None:
        for sample in self.rules["validation_samples"]:
            code = sample.get("reason_code")
            if code:
                self.assertIn(code, self.all_reason_codes, f"样本 {sample['id']} 引用了未定义的码")
        for source in self.sources["sources"]:
            code = (source.get("exclusion") or {}).get("reason_code")
            if code:
                self.assertIn(code, self.all_reason_codes, f"{source['id']} 引用了未定义的码")

    def test_nine_validation_samples(self) -> None:
        self.assertEqual(len(self.rules["validation_samples"]), 9)

    def test_model_quota_matches_rules(self) -> None:
        self.assertEqual(self.model["limits"]["max_calls_per_week"], self.rules["weekly_quota"])

    def test_model_example_has_no_credentials(self) -> None:
        blob = json.dumps(self.model, ensure_ascii=False).lower()
        for marker in ("sk-", "bearer ey", "secret\":", "password"):
            self.assertNotIn(marker, blob, f"示例配置疑似含凭据标记 {marker}")

    def test_example_config_keeps_api_key_empty(self) -> None:
        """model.example.json 会被提交，其 api_key 必须保持为空。"""
        self.assertIsNone(self.model["auth"].get("api_key"))


class AutomationSwitchTest(unittest.TestCase):
    """自动化开关：定时采集的开启/关闭由 config/automation.json 决定。"""

    def test_shipped_file_declares_a_boolean(self) -> None:
        data = load("automation.json")
        self.assertIn("scheduled_sync_enabled", data, "必须显式声明开关")
        self.assertIsInstance(data["scheduled_sync_enabled"], bool, "开关必须是 true 或 false")

    def test_missing_file_is_treated_as_disabled(self) -> None:
        """缺失按停用处理：无人值守的定时任务不会因为文件丢失而开始花钱。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.assertIs(load_automation(tmp)["scheduled_sync_enabled"], False)

    def test_non_boolean_value_is_reported_by_precheck(self) -> None:
        """`"true"` 这类字符串不能被当成真值：gate 按 false 处理，precheck 必须报错。"""
        cfg = load_all_config(CONFIG)
        cfg["automation"] = {"scheduled_sync_enabled": "true"}
        self.assertTrue(
            any("scheduled_sync_enabled" in p for p in precheck(cfg)),
            "字符串形式的开关必须被 precheck 拦下",
        )
        cfg["automation"] = {"scheduled_sync_enabled": True}
        self.assertEqual(
            [p for p in precheck(cfg) if "scheduled_sync_enabled" in p], [],
            "合法布尔值不应产生问题",
        )

    def test_shipped_config_passes_precheck(self) -> None:
        self.assertEqual(precheck(load_all_config(CONFIG)), [], "配置文件整体预检必须通过")


if __name__ == "__main__":
    unittest.main()
