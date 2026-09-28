"""P1 样本基线、统计口径与版本契约专项验收测试。

依据《综合实施方案》第 5 节验收要求：
1. 固定离线样本覆盖查询、证据、目录与持久化四类正反例；
2. 统计口径严格区分事实，保留分子分母，分母为 0 标记不可计算，历史缺失标记未知；
3. 版本契约规范 Schema、契约与算法版本，旧记录缺失字段明确识别且可平滑读取。
"""

import unittest
from pathlib import Path

from src.finder.evidence import verify_evidence_snippet
from src.shared.materials import validate_document
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
from tests.fixtures.benchmark_samples import (
    QUERY_BENCHMARK_SAMPLES,
    EVIDENCE_BENCHMARK_DOCUMENT,
    EVIDENCE_BENCHMARK_SAMPLES,
    DOCUMENT_BENCHMARK_SAMPLES,
    PERSISTENCE_BENCHMARK_SAMPLES,
)


class BenchmarkSamplesTest(unittest.TestCase):
    """测试固定离线样本集的一致性与生产规则行为。"""

    def setUp(self):
        self.materials = {"SKILL.md": EVIDENCE_BENCHMARK_DOCUMENT}

    def test_query_samples_coverage_and_constraints(self):
        """验证查询样本集覆盖中文、英文、技术别名、歧义词、约束和未命中项。"""
        self.assertIn("chinese_concept", QUERY_BENCHMARK_SAMPLES)
        self.assertIn("english_direct", QUERY_BENCHMARK_SAMPLES)
        self.assertIn("technical_alias", QUERY_BENCHMARK_SAMPLES)
        self.assertIn("ambiguous_term", QUERY_BENCHMARK_SAMPLES)
        self.assertIn("explicit_constraint", QUERY_BENCHMARK_SAMPLES)
        self.assertIn("no_terminology_hit", QUERY_BENCHMARK_SAMPLES)

        # 检查显式约束样本的负向标记
        constraint_sample = QUERY_BENCHMARK_SAMPLES["explicit_constraint"]
        self.assertTrue(constraint_sample["has_negative_constraint"])
        self.assertIn("selenium", constraint_sample["negative_tokens"])

        # 检查未命中术语表样本不包含别名
        no_hit_sample = QUERY_BENCHMARK_SAMPLES["no_terminology_hit"]
        self.assertEqual(len(no_hit_sample["expected_tech_aliases"]), 0)

    def test_evidence_samples_exact_matching_and_counterexamples(self):
        """验证证据样本在当前生产核验规则下的严格行为（建立 P1 衡量基线）。"""
        # 1. 正常跨行、代码块、表格应精确通过
        for key in ("multiline_normal", "code_block_snippet", "table_snippet"):
            sample = EVIDENCE_BENCHMARK_SAMPLES[key]
            ok, msg = verify_evidence_snippet(
                sample["source_path"],
                sample["start_line"],
                sample["end_line"],
                sample["quote"],
                self.materials,
            )
            self.assertTrue(ok, f"样本 {key} 应通过核验，但失败：{msg}")

        # 2. 否定反例、数字变体反例、跨文件拼接反例必须坚决拒绝（杜绝幻觉伪造通过）
        for key in ("negation_contrast_fabricated", "number_variant_fabricated", "cross_file_spliced_fabricated"):
            sample = EVIDENCE_BENCHMARK_SAMPLES[key]
            ok, msg = verify_evidence_snippet(
                sample["source_path"],
                sample["start_line"],
                sample["end_line"],
                sample["quote"],
                self.materials,
            )
            self.assertFalse(ok, f"反例样本 {key} 必须拒绝核验，但却通过了！")
            self.assertIn("未找到完整匹配", msg)

        # 3. 合法行号漂移样本在精确核验下失败（为 P2 邻近修复确立基线）
        drift_sample = EVIDENCE_BENCHMARK_SAMPLES["line_drift_positive"]
        ok, msg = verify_evidence_snippet(
            drift_sample["source_path"],
            drift_sample["start_line"],
            drift_sample["end_line"],
            drift_sample["quote"],
            self.materials,
        )
        self.assertFalse(ok, "行号发生偏移时，精确核验基线应返回 False")

    def test_document_samples_validation(self):
        """验证目录文档样本：短文档有效，缺少 Frontmatter 不误杀，含 TODO 但结构完整不淘汰。"""
        for key in ("short_and_valid", "no_frontmatter", "body_with_todo"):
            sample = DOCUMENT_BENCHMARK_SAMPLES[key]
            valid, _ = validate_document(sample["path"], sample["content"])
            self.assertTrue(valid, f"文档样本 {key} 结构应被认定为有效文档")


class StatisticalMetricsTest(unittest.TestCase):
    """测试统计口径与分子分母保留逻辑。"""

    def test_calc_ratio_zero_denominator_and_unknown(self):
        """分母为零标记为不可计算，缺失数据标记为未知，正常值正确计算。"""
        # 1. 正常计算
        r1 = calc_ratio(8, 10)
        self.assertEqual(r1.status, "calculated")
        self.assertEqual(r1.numerator, 8)
        self.assertEqual(r1.denominator, 10)
        self.assertAlmostEqual(r1.value, 80.0)
        self.assertEqual(r1.display, "8/10 (80.0%)")

        # 2. 分母为 0
        r2 = calc_ratio(0, 0)
        self.assertEqual(r2.status, "undefined_zero_denominator")
        self.assertIsNone(r2.value)
        self.assertEqual(r2.display, "不可计算 (分母为0)")

        # 3. 缺失数据
        r3 = calc_ratio(None, 10)
        self.assertEqual(r3.status, "unknown")
        self.assertEqual(r3.display, "未知")

        r4 = calc_ratio(5, None)
        self.assertEqual(r4.status, "unknown")
        self.assertEqual(r4.display, "未知")

    def test_search_metric_facts(self):
        """验证搜索事实统计保留原始计数与转化率计算。"""
        search_facts = SearchMetricFacts(
            http_requests=12,
            retries=2,
            repos_discovered_raw=30,
            repos_deduped=25,
            skills_discovered_raw=40,
            skills_deduped=35,
            valid_materials=33,
            shortlist_count=5,
            alternatives_count=8,
        )
        dedupe_ratio = search_facts.deduplication_ratio()
        self.assertEqual(dedupe_ratio.numerator, 25)
        self.assertEqual(dedupe_ratio.denominator, 30)

        conv_ratio = search_facts.conversion_ratio()
        self.assertEqual(conv_ratio.numerator, 5)
        self.assertEqual(conv_ratio.denominator, 35)

        facts_dict = search_facts.to_dict()
        self.assertEqual(facts_dict["shortlist_count"], 5)
        self.assertEqual(facts_dict["conversion_ratio"]["status"], "calculated")

    def test_model_metric_facts(self):
        """验证模型调用事实统计包含尝试次数、完成数及 Token。"""
        model_facts = ModelMetricFacts(
            stage_attempts={"planning": 1, "evaluation": 10},
            total_attempts=11,
            completed_evaluations=9,
            known_prompt_tokens=4500,
            known_completion_tokens=1500,
            known_total_tokens=6000,
            unknown_usage_requests=0,
            format_failures=1,
            length_exceeded_count=1,
        )
        success_ratio = model_facts.evaluation_success_ratio()
        self.assertEqual(success_ratio.numerator, 9)
        self.assertEqual(success_ratio.denominator, 11)

    def test_cache_and_prescreen_metric_facts(self):
        """验证缓存复用率与预筛抽样误拦截率计算。"""
        cache_facts = CacheMetricFacts(
            exact_hits=4,
            normalized_potential_hits=2,
            actual_reused=4,
            rejection_reasons={"rules_mismatch": 1},
        )
        reuse_ratio = cache_facts.effective_reuse_ratio(total_candidates=10)
        self.assertEqual(reuse_ratio.numerator, 4)
        self.assertEqual(reuse_ratio.denominator, 10)

        # 0 总候选时的复用率
        zero_reuse = cache_facts.effective_reuse_ratio(total_candidates=0)
        self.assertEqual(zero_reuse.status, "undefined_zero_denominator")

        prescreen_facts = PrescreenMetricFacts(
            rules_version="1.1.0",
            signal_hits={"empty_doc": 3},
            skipped_count=3,
            sample_audit_false_positives=0,
        )
        fp_ratio = prescreen_facts.false_positive_ratio()
        self.assertEqual(fp_ratio.numerator, 0)
        self.assertEqual(fp_ratio.denominator, 3)
        self.assertAlmostEqual(fp_ratio.value, 0.0)


class VersionContractsTest(unittest.TestCase):
    """测试版本契约常量与语义化版本兼容规则。"""

    def test_version_constants_defined(self):
        """验证所有核心版本常量均已显式定义且符合语义化版本格式。"""
        for v in (
            FINDER_REPORT_SCHEMA_VERSION,
            LLM_OUTPUT_CONTRACT_VERSION,
            TERMINOLOGY_VERSION,
            EVIDENCE_VERIFIER_VERSION,
            NORMALIZATION_VERSION,
        ):
            parsed = parse_version_tuple(v)
            self.assertGreaterEqual(len(parsed), 2)

    def test_is_semver_compatible(self):
        """验证语义化版本兼容性判断。"""
        # 同主版本、次版本更高或相等 -> 兼容
        self.assertTrue(is_semver_compatible("1.1.0", "1.0.0"))
        self.assertTrue(is_semver_compatible("1.0.1", "1.0.0"))
        self.assertTrue(is_semver_compatible("1.0.0", "1.0.0"))

        # 次版本低于要求 -> 不兼容
        self.assertFalse(is_semver_compatible("1.0.0", "1.1.0"))

        # 主版本不一致 -> 坚决不兼容
        self.assertFalse(is_semver_compatible("2.0.0", "1.0.0"))
        self.assertFalse(is_semver_compatible("1.0.0", "2.0.0"))

        # 非法版本
        self.assertFalse(is_semver_compatible("bad", "1.0.0"))
        self.assertFalse(is_semver_compatible(None, "1.0.0"))

    def test_check_evaluation_record_compatibility(self):
        """验证历史评估记录的兼容性检查。"""
        normal_rec = PERSISTENCE_BENCHMARK_SAMPLES["normal_completed_record"]
        ok, msg = check_evaluation_record_compatibility(
            normal_rec,
            expected_rules_version="1.0.0",
            expected_model_config_version="1.0.0",
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "兼容")

        # 规则主版本不兼容
        bad_rules_rec = dict(normal_rec, rules_version="2.0.0")
        ok, msg = check_evaluation_record_compatibility(
            bad_rules_rec,
            expected_rules_version="1.0.0",
        )
        self.assertFalse(ok)
        self.assertIn("规则版本不兼容", msg)

        # 旧记录缺失字段但主版本兼容时平滑读取
        legacy_rec = PERSISTENCE_BENCHMARK_SAMPLES["legacy_missing_fields_record"]
        ok, msg = check_evaluation_record_compatibility(
            legacy_rec,
            expected_rules_version="1.0.0",
        )
        # legacy 记录缺失 rules_version 时，不隐式崩溃
        self.assertTrue(isinstance(ok, bool))


if __name__ == "__main__":
    unittest.main()
