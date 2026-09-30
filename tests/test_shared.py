"""公共库单元测试：配置交叉完整性、材料契约、预筛规则、版本兼容性与统计度量。

合并自原有 test_config.py、test_materials.py、test_prescreen.py 与 test_p1_baseline_metrics_versions.py。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest

from src.catalog.config import load_all_config, load_automation, precheck
from src.catalog.dedupe import candidate_from_repo
from src.catalog.prescreen import DECISION_EXCLUDED, DECISION_QUEUED, load_config, prescreen
from src.shared.materials import (
    DocumentSnapshot,
    MaterialBundle,
    validate_document,
)
from src.shared.metrics import (
    RatioMetric,
    calc_ratio,
    SearchMetricFacts,
    ModelMetricFacts,
    CacheMetricFacts,
    PrescreenMetricFacts,
)
from src.shared.versions import (
    FINDER_REPORT_SCHEMA_VERSION,
    LLM_OUTPUT_CONTRACT_VERSION,
    TERMINOLOGY_VERSION,
    EVIDENCE_VERIFIER_VERSION,
    NORMALIZATION_VERSION,
    parse_version_tuple,
    is_semver_compatible,
    check_evaluation_record_compatibility,
)
from tests import smoke
from tests.fixtures.benchmark_samples import (
    QUERY_BENCHMARK_SAMPLES,
    EVIDENCE_BENCHMARK_DOCUMENT,
    EVIDENCE_BENCHMARK_SAMPLES,
    DOCUMENT_BENCHMARK_SAMPLES,
    PERSISTENCE_BENCHMARK_SAMPLES,
)

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


def _resolve_config_path(name: str) -> Path:
    p = CONFIG / name
    if p.is_file():
        return p
    for sub in ("standards", "discovery", "governance", "runners", "models"):
        sub_p = CONFIG / sub / name
        if sub_p.is_file():
            return sub_p
    return p


def load_cfg(name: str) -> dict:
    return json.loads(_resolve_config_path(name).read_text(encoding="utf-8"))


@smoke
class ConfigIntegrityTest(unittest.TestCase):
    """配置完整性：各配置文件之间闭合与格式检查。"""
    @classmethod
    def setUpClass(cls) -> None:
        cls.taxonomy = load_cfg("taxonomy.json")
        cls.rules = load_cfg("rules.json")
        cls.sources = load_cfg("sources.json")
        cls.searches = load_cfg("searches.json")
        model_path = _resolve_config_path("model.local.json")
        if model_path.is_file():
            cls.model = json.loads(model_path.read_text(encoding="utf-8"))
        else:
            cls.model = {"limits": {"max_calls_per_week": cls.rules["weekly_quota"]}, "auth": {}}
        cls.taxonomy_ids = [c["id"] for c in cls.taxonomy["main_categories"]]
        cls.all_reason_codes = {
            code
            for group in cls.rules["reason_codes"].values()
            for code in group
        }

    def test_rules_checks_match_expected_six(self) -> None:
        actual = [c["id"] for c in self.rules["checks"]]
        self.assertEqual(actual, EXPECTED_CHECK_IDS)

    def test_ten_categories_with_unique_ids(self) -> None:
        self.assertEqual(len(self.taxonomy_ids), 10)
        self.assertEqual(len(set(self.taxonomy_ids)), 10)

    def test_search_domains_cover_all_categories(self) -> None:
        self.assertEqual(sorted(self.searches["per_domain"]), sorted(self.taxonomy_ids))

    def test_shipped_config_passes_precheck(self) -> None:
        cfg = load_all_config(CONFIG)
        self.assertEqual(precheck(cfg), [], "配置文件预检应通过")


@smoke
class MaterialsTest(unittest.TestCase):
    """材料不可变快照与确定性集合指纹。"""
    def test_document_snapshot_immutability(self):
        doc = DocumentSnapshot(
            path="skills/math/SKILL.md",
            text="# Math Skill\nUseful for math.",
            fingerprint="sha256:11111111111111111111111111111111",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/math",
        )
        with self.assertRaises(FrozenInstanceError):
            doc.text = "modified text"

    def test_material_bundle_fingerprint_determinism(self):
        doc1 = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/tool",
        )
        bundle_a = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1,
            referenced_docs=(),
        )
        doc1_later = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T12:00:00Z",
            source_url="https://example.com/tool",
        )
        bundle_b = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1_later,
            referenced_docs=(),
        )
        self.assertEqual(bundle_a.bundle_fingerprint, bundle_b.bundle_fingerprint)


@smoke
class PrescreenTest(unittest.TestCase):
    """预筛与排除规则验证。"""
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(ROOT / "config")

    def test_dsh_plugin_excluded(self) -> None:
        cand = candidate_from_repo("someone", "dsh-ui-skin-switcher", url="https://github.com/someone/dsh-ui-skin-switcher")
        result = prescreen(cand, self.cfg)
        self.assertEqual(result.decision, DECISION_EXCLUDED)
        self.assertIn("DSH_PLUGIN", result.reason_codes)

    def test_self_repo_excluded(self) -> None:
        cand = candidate_from_repo("justdoit712", "skill", url="https://github.com/justdoit712/skill")
        result = prescreen(cand, self.cfg)
        self.assertEqual(result.decision, DECISION_EXCLUDED)
        self.assertIn("SELF_REPO", result.reason_codes)

    def test_normal_skill_is_queued(self) -> None:
        cand = candidate_from_repo("author", "awesome-skill", path="skills/my-tool/SKILL.md", url="https://github.com/author/awesome-skill")
        result = prescreen(cand, self.cfg)
        self.assertEqual(result.decision, DECISION_QUEUED)


@smoke
class VersionCompatibilityTest(unittest.TestCase):
    """版本契约规范与向后兼容性。"""
    def test_parse_version_tuple(self):
        self.assertEqual(parse_version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version_tuple("1.0.0"), (1, 0, 0))
        self.assertEqual(parse_version_tuple("invalid"), ())

    def test_is_semver_compatible(self):
        self.assertTrue(is_semver_compatible("1.0.0", "1.0.0"))
        self.assertTrue(is_semver_compatible("1.2.0", "1.0.0"))
        self.assertFalse(is_semver_compatible("2.0.0", "1.0.0"))
        self.assertFalse(is_semver_compatible("0.9.0", "1.0.0"))

    def test_metrics_calculation_and_zero_denominator(self):
        m = calc_ratio(10, 50)
        self.assertEqual(m.value, 20.0)
        self.assertEqual(m.status, "calculated")

        m_zero = calc_ratio(0, 0)
        self.assertEqual(m_zero.status, "undefined_zero_denominator")
        self.assertIsNone(m_zero.value)


if __name__ == "__main__":
    unittest.main()
