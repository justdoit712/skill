import unittest
from pathlib import Path

from src.catalog.models import Candidate, PrescreenResult
from src.catalog.prescreen import (
    PrescreenConfig,
    analyze_static_tier,
    prescreen,
    TIER_CLEAR_PLACEHOLDER,
    TIER_CONTENT_DEFECT,
    TIER_SUSPECT,
    TIER_NORMAL,
    TIER_PENDING,
    ACTION_SUGGEST_SKIP,
    ACTION_SUGGEST_REVIEW,
    ACTION_SUGGEST_EVALUATE,
    ACTION_SUGGEST_PENDING,
)
from src.catalog.local import _record_static_observation
from src.shared.metrics import build_run_metrics
from src.shared.versions import STATIC_HEURISTIC_VERSION
from tests.fixtures.benchmark_samples import DOCUMENT_BENCHMARK_SAMPLES


class TestStaticTiersObservation(unittest.TestCase):
    """Unit 6 / P3.1: 静态规则分级观察模式与安全基准测试。"""

    def setUp(self):
        self.cfg = PrescreenConfig(
            taxonomy={},
            rules={"rules_version": "1.0.0"},
            self_repos=set(),
            out_of_scope_repos=set(),
            out_of_scope_orgs=set(),
            domain_terms={"tools": ["calc", "sync", "tool", "clean"]},
            domain_names={"tools": "工具类"},
            manual_exclusions=set(),
        )

    def test_benchmark_samples_tier_classification(self):
        """测试 5+1 个标准文档基准样本的分级表现。"""
        # 1. 短而有效 (short_and_valid)：绝不可因过短误判为跳过
        sample = DOCUMENT_BENCHMARK_SAMPLES["short_and_valid"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="mini-calc",
            description="Tiny calculator",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_NORMAL)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_EVALUATE)
        self.assertIn("SHORT_VALID_DOCUMENT", res["signals"])
        self.assertFalse(res["applied"])
        self.assertEqual(res["mode"], "observation")

        # 2. 无 Frontmatter (no_frontmatter)：绝不可因无头字段误判为跳过
        sample = DOCUMENT_BENCHMARK_SAMPLES["no_frontmatter"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="clean-tool",
            description="Clean tool",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_NORMAL)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_EVALUATE)
        self.assertIn("NO_FRONTMATTER_VALID", res["signals"])

        # 3. 明确空壳占位 (explicit_boilerplate)：识别为纯占位建议跳过
        sample = DOCUMENT_BENCHMARK_SAMPLES["explicit_boilerplate"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="todo-skill",
            description="TODO: add description",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_CLEAR_PLACEHOLDER)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_SKIP)
        self.assertIn("EXPLICIT_BOILERPLATE", res["signals"])
        self.assertIn("TODO_PLACEHOLDER_ONLY", res["signals"])

        # 4. 正文含路线图 TODO 但功能完备 (body_with_todo)：绝不可误杀
        sample = DOCUMENT_BENCHMARK_SAMPLES["body_with_todo"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="sync-tool",
            description="Realtime sync",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_NORMAL)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_EVALUATE)
        self.assertIn("BODY_CONTAINS_ROADMAP_TODO", res["signals"])

        # 5. 模板型技能 (template_skill)：具备参数替换与调用指引，正常评估
        sample = DOCUMENT_BENCHMARK_SAMPLES["template_skill"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="template-runner",
            description="Template runner tool",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_NORMAL)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_EVALUATE)

        # 6. 依赖参考资料型 (reference_dependent)：正常评估
        sample = DOCUMENT_BENCHMARK_SAMPLES["reference_dependent"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="schema-checker",
            description="Schema validator",
        )
        res = analyze_static_tier(c, sample["content"])
        self.assertEqual(res["tier"], TIER_NORMAL)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_EVALUATE)

    def test_special_boundary_conditions(self):
        """测试特殊边界情况：未抓取、空内容、截断、格式错误、敏感模式。"""
        c = Candidate(
            skill_id="test/repo:skills/demo/SKILL.md",
            owner="test",
            repo="repo",
            path="skills/demo/SKILL.md",
            name="demo-skill",
            description="Demo skill",
        )

        # 1. 未抓取材料
        res = analyze_static_tier(c, None, is_fetched=False)
        self.assertEqual(res["tier"], TIER_PENDING)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_PENDING)
        self.assertIn("UNFETCHED_MATERIAL", res["signals"])

        # 2. 空白正文
        res = analyze_static_tier(c, "   \n\n\t  ")
        self.assertEqual(res["tier"], TIER_CONTENT_DEFECT)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_SKIP)
        self.assertIn("EMPTY_CONTENT", res["signals"])

        # 3. 抓取被截断
        res = analyze_static_tier(c, "Some normal text", is_truncated=True)
        self.assertEqual(res["tier"], TIER_CONTENT_DEFECT)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_SKIP)
        self.assertIn("FETCH_TRUNCATED", res["signals"])

        # 4. 格式无效（HTML 错误页）
        res = analyze_static_tier(c, "<html>404 Not Found</html>", is_valid_doc=False, doc_reason="HTML error page")
        self.assertEqual(res["tier"], TIER_CONTENT_DEFECT)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_SKIP)
        self.assertIn("HTML_ERROR_PAGE", res["signals"])

        # 5. 命中高危敏感词 (tier_suspect)
        c_suspect = Candidate(
            skill_id="test/repo:skills/trading/SKILL.md",
            owner="test",
            repo="repo",
            path="skills/trading/SKILL.md",
            name="auto-trader",
            description="Tool connecting to broker-api for live-trading execution",
        )
        res = analyze_static_tier(c_suspect, "Run python trade.py to place-order via broker-api.")
        self.assertEqual(res["tier"], TIER_SUSPECT)
        self.assertEqual(res["suggested_action"], ACTION_SUGGEST_REVIEW)
        self.assertIn("LIVE_TRADING_SUSPECT", res["signals"])

    def test_prescreen_observation_mode_purity(self):
        """测试预筛观察模式绝对纯净：不改变已有的排队决策或排除决策。"""
        # 即便内容是明确的空壳占位，prescreen() 的 decision 依然保持原有逻辑（queued），不直接过滤
        sample = DOCUMENT_BENCHMARK_SAMPLES["explicit_boilerplate"]
        c = Candidate(
            skill_id="test/repo:" + sample["path"],
            owner="test",
            repo="repo",
            path=sample["path"],
            name="todo-skill",
            description="TODO: add description",
        )
        pres_res = prescreen(c, self.cfg, sample["content"])
        self.assertEqual(pres_res.decision, "queued")
        self.assertFalse(pres_res.excluded)
        self.assertIsNotNone(pres_res.static_observation)
        self.assertEqual(pres_res.static_observation["tier"], TIER_CLEAR_PLACEHOLDER)
        self.assertEqual(pres_res.static_observation["suggested_action"], ACTION_SUGGEST_SKIP)
        self.assertFalse(pres_res.static_observation["applied"])

        # 排除路径（如 DSH 插件）在排除时依然附带静态观察指标
        c_dsh = Candidate(
            skill_id="dsh/plugin:skills/dsh/SKILL.md",
            owner="dsh",
            repo="plugin",
            path="skills/dsh/SKILL.md",
            name="dsh_plugin",
            description="DSH plugin implementation",
        )
        pres_dsh = prescreen(c_dsh, self.cfg, "Some content")
        self.assertEqual(pres_dsh.decision, "excluded")
        self.assertTrue(pres_dsh.excluded)
        self.assertIsNotNone(pres_dsh.static_observation)

    def test_record_static_observation_and_metrics(self):
        """测试本地运行报告对静态指标的统计与 build_run_metrics 汇聚。"""
        report = {
            "run_id": "test_run_001",
            "prescreen_excluded": 2,
            "static_heuristics": {
                "version": STATIC_HEURISTIC_VERSION,
                "observed_count": 0,
                "tier_counts": {},
                "signal_counts": {},
                "suggested_actions": {},
            },
        }

        # 模拟记录一个 normal 和一个 clear placeholder
        obs1 = {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_NORMAL,
            "signals": ["VALID_STRUCTURE", "SHORT_VALID_DOCUMENT"],
            "suggested_action": ACTION_SUGGEST_EVALUATE,
        }
        obs2 = {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_CLEAR_PLACEHOLDER,
            "signals": ["EXPLICIT_BOILERPLATE", "TODO_PLACEHOLDER_ONLY"],
            "suggested_action": ACTION_SUGGEST_SKIP,
        }

        _record_static_observation(report, obs1)
        _record_static_observation(report, obs2)

        sh = report["static_heuristics"]
        self.assertEqual(sh["observed_count"], 2)
        self.assertEqual(sh["tier_counts"][TIER_NORMAL], 1)
        self.assertEqual(sh["tier_counts"][TIER_CLEAR_PLACEHOLDER], 1)
        self.assertEqual(sh["suggested_actions"][ACTION_SUGGEST_EVALUATE], 1)
        self.assertEqual(sh["suggested_actions"][ACTION_SUGGEST_SKIP], 1)
        self.assertEqual(sh["signal_counts"]["SHORT_VALID_DOCUMENT"], 1)
        self.assertEqual(sh["signal_counts"]["EXPLICIT_BOILERPLATE"], 1)

        # 校验 metrics.py build_run_metrics 结构
        metrics = build_run_metrics(report, kind="catalog")
        pres_metrics = metrics["prescreen"]
        self.assertEqual(pres_metrics["rules_version"], STATIC_HEURISTIC_VERSION)
        self.assertEqual(pres_metrics["recommended_actions"][ACTION_SUGGEST_SKIP], 1)
        self.assertEqual(pres_metrics["signal_hits"]["EXPLICIT_BOILERPLATE"], 1)
        self.assertEqual(pres_metrics["skipped_count"], 2)


if __name__ == "__main__":
    unittest.main()
