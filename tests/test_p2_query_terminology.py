"""P2.1 单元测试：查询统一与技术术语观察模式。

覆盖范围：
1. 初始、交互与反思路径共用的空白规范化、操作符清洗与大小写无关去重；
2. 小型带版本技术术语表与固定基准样本（QUERY_BENCHMARK_SAMPLES）缺口检测；
3. 观察模式行为验证：绝不自动增加真实请求（applied=False），解释性事实完整记录；
4. 报告渲染与公共投影兼容性。
"""

from __future__ import annotations

import json
import unittest

from src.finder.plan import (
    MAX_PLAN_QUERIES,
    build_reflection_prompt,
    parse_query_plan,
    parse_reflection_queries,
)
from src.finder.report import (
    render_find_markdown_report,
    sanitize_report_for_public,
)
from src.finder.terminology import (
    SEARCH_OPERATOR_PATTERN,
    TERMINOLOGY_TABLE,
    TERMINOLOGY_VERSION,
    build_terminology_observation,
    deduplicate_queries,
    detect_terminology_gaps,
    generate_suggested_queries,
    has_search_operator,
    normalize_query,
    strip_search_operators,
)
from tests.fixtures.benchmark_samples import QUERY_BENCHMARK_SAMPLES


class TestQueryNormalizationAndDeduplication(unittest.TestCase):
    """测试统一的查询清洗、操作符处理与去重。"""

    def test_normalize_query(self) -> None:
        self.assertEqual(normalize_query("   "), "")
        self.assertEqual(normalize_query("  k8s   pod   monitor  "), "k8s pod monitor")
        self.assertEqual(normalize_query("\t\n web   scraping \n"), "web scraping")
        self.assertEqual(normalize_query(None), "")  # type: ignore

    def test_has_search_operator(self) -> None:
        self.assertTrue(has_search_operator("prompt in:readme"))
        self.assertTrue(has_search_operator("repo:owner/repo"))
        self.assertTrue(has_search_operator("language:python web scraper"))
        self.assertTrue(has_search_operator("stars:>100"))
        self.assertTrue(has_search_operator("site:github.com tool"))
        self.assertFalse(has_search_operator("prompt generator"))
        self.assertFalse(has_search_operator("k8s pod 诊断"))

    def test_strip_search_operators(self) -> None:
        raw = "web scraping in:readme language:python stars:>50 tool"
        stripped = strip_search_operators(raw)
        self.assertEqual(stripped, "web scraping tool")

        clean = strip_search_operators("  k8s pod 诊断  ")
        self.assertEqual(clean, "k8s pod 诊断")

    def test_deduplicate_queries(self) -> None:
        queries = ["Web Scraping", "web scraping", "WEB SCRAPING", "Data Cleaning", "data cleaning"]
        deduped = deduplicate_queries(queries)
        # 大小写去重并保留首次出现的大小写
        self.assertEqual(deduped, ["Web Scraping", "Data Cleaning"])

    def test_deduplicate_queries_with_previous(self) -> None:
        previous = ["web scraping"]
        queries = ["Web Scraping", "PDF Parser", "pdf parser", "  "]
        deduped = deduplicate_queries(queries, previous=previous)
        self.assertEqual(deduped, ["PDF Parser"])


class TestPlanParsingUnifiedBehavior(unittest.TestCase):
    """测试 plan.py 在统一清洗与去重后的行为。"""

    def test_parse_query_plan_strips_operators_and_deduplicates(self) -> None:
        plan_json = json.dumps(
            {
                "intent": "网页数据抓取与提取",
                "queries": [
                    "web scraper in:readme",
                    "Web Scraper",  # 大小写重复
                    "爬虫工具 language:python",
                    "data extraction",
                ],
                "criteria": [
                    {"id": "extract", "kind": "required", "description": "支持抓取与提取"},
                ],
            }
        )
        plan = parse_query_plan(plan_json, topic="网页数据抓取")
        self.assertEqual(plan["intent"], "网页数据抓取与提取")
        # Web Scraper 应该与 web scraper 去重，只保留首次出现的 "web scraper"
        self.assertEqual(plan["queries"], ["web scraper", "爬虫工具", "data extraction"])

        raw_queries = [
            "web scraper in:readme",
            "Web Scraper",
            "爬虫工具 language:python",
            "data extraction",
        ]
        obs = build_terminology_observation(
            topic="网页数据抓取",
            intent=plan["intent"],
            queries=plan["queries"],
            raw_queries=raw_queries,
        )
        self.assertEqual(obs["version"], TERMINOLOGY_VERSION)
        self.assertEqual(obs["raw_queries"], raw_queries)
        self.assertEqual(obs["queries"], plan["queries"])
        self.assertFalse(obs["applied"])

    def test_parse_reflection_queries_rejects_operators(self) -> None:
        reflection_json = json.dumps(
            {
                "queries": ["web scraper in:readme", "spider tool", "data fetcher"],
            }
        )
        with self.assertRaises(ValueError) as ctx:
            parse_reflection_queries(reflection_json, previous=["old"])
        self.assertIn("反思关键词不得包含搜索操作符", str(ctx.exception))

    def test_parse_reflection_queries_deduplicates_casefold(self) -> None:
        reflection_json = json.dumps(
            {
                "queries": [" Spider Tool ", "SPIDER TOOL", "data fetcher", "HTML Parser"],
            }
        )
        result = parse_reflection_queries(reflection_json, previous=["html parser"])
        # " Spider Tool " 保留，"SPIDER TOOL" 大小写去重丢弃；"html parser" 与 previous 重复丢弃
        self.assertEqual(result, ["Spider Tool", "data fetcher"])


class TestTerminologyBenchmarkCoverage(unittest.TestCase):
    """测试技术术语表基于 QUERY_BENCHMARK_SAMPLES 的缺口识别准确性。"""

    def test_chinese_basic_query_detects_english_gaps(self) -> None:
        sample = QUERY_BENCHMARK_SAMPLES["chinese_concept"]
        queries = ["网页爬取工具", "数据提取脚本"]
        gaps = detect_terminology_gaps(sample["topic"], sample["topic"], queries)
        concept_ids = {g["concept_id"] for g in gaps}
        self.assertIn("web_scraping", concept_ids)

        ws_gap = next(g for g in gaps if g["concept_id"] == "web_scraping")
        self.assertIn("web scraping", ws_gap["missing_terms"])
        self.assertIn("网页爬取", ws_gap["covered_terms"])
        self.assertEqual(ws_gap["boundary"], "不默认增加登录、验证码或特定框架要求")

    def test_technical_alias_query_detects_english_terms(self) -> None:
        sample = QUERY_BENCHMARK_SAMPLES["technical_alias"]
        queries = ["提示词优化工具"]
        gaps = detect_terminology_gaps(sample["topic"], sample["topic"], queries)
        concept_ids = {g["concept_id"] for g in gaps}
        self.assertIn("prompt_optimization", concept_ids)

        po_gap = next(g for g in gaps if g["concept_id"] == "prompt_optimization")
        self.assertIn("prompt optimization", po_gap["missing_terms"])
        self.assertIn("提示词优化", po_gap["covered_terms"])
        self.assertEqual(po_gap["boundary"], "不把优化自动等同于生成或评测")

    def test_tech_alias_k8s_detects_kubernetes(self) -> None:
        topic = "k8s pod 诊断与运维"
        queries = ["k8s pod monitor"]
        gaps = detect_terminology_gaps(topic, topic, queries)
        k8s_gap = next((g for g in gaps if g["concept_id"] == "k8s_kubernetes"), None)
        self.assertIsNotNone(k8s_gap)
        self.assertIn("kubernetes", k8s_gap["missing_terms"])
        self.assertIn("k8s", k8s_gap["covered_terms"])

    def test_ambiguous_non_tech_query_detects_zero_gaps(self) -> None:
        queries = ["苹果管理"]
        gaps = detect_terminology_gaps("苹果管理", "管理苹果", queries)
        self.assertEqual(len(gaps), 0)

    def test_explicit_constraint_query_respects_boundary(self) -> None:
        sample = QUERY_BENCHMARK_SAMPLES["explicit_constraint"]
        queries = ["网页抓取 requests"]
        gaps = detect_terminology_gaps(sample["topic"], sample["topic"], queries)
        ws_gap = next((g for g in gaps if g["concept_id"] == "web_scraping"), None)
        self.assertIsNotNone(ws_gap)
        self.assertEqual(ws_gap["boundary"], "不默认增加登录、验证码或特定框架要求")

    def test_no_terminology_hit_detects_zero_gaps(self) -> None:
        sample = QUERY_BENCHMARK_SAMPLES["no_terminology_hit"]
        queries = ["量子退火拓扑求解"]
        gaps = detect_terminology_gaps(sample["topic"], sample["topic"], queries)
        self.assertEqual(len(gaps), 0)


class TestObservationModeStrictBoundaries(unittest.TestCase):
    """测试观察模式事实契约：绝不增加额外请求，事实完整记录。"""

    def test_build_terminology_observation_structure(self) -> None:
        topic = "网页爬取与数据清洗"
        intent = "爬取网页并做清洗"
        queries = ["网页爬取"]

        obs = build_terminology_observation(topic, intent, queries)
        self.assertEqual(obs["version"], TERMINOLOGY_VERSION)
        self.assertEqual(obs["mode"], "observation")
        # 绝不在观察模式自动标记已应用或修改查询
        self.assertFalse(obs["applied"])
        self.assertGreater(obs["gaps_count"], 0)
        self.assertTrue(any("web scraping" in q.lower() for q in obs["suggested_queries"]))

    def test_suggested_queries_bounded_and_deduplicated(self) -> None:
        gaps = [
            {
                "concept_id": "c1",
                "missing_terms": ["term1", "term2", "term3", "term4", "term5"],
            }
        ]
        sug = generate_suggested_queries(gaps, existing_queries=["term1"], max_suggestions=3)
        self.assertEqual(len(sug), 3)
        self.assertNotIn("term1", sug)  # 已存在的被排除
        self.assertEqual(sug, ["term2", "term3", "term4"])

    def test_markdown_report_includes_observation_fact(self) -> None:
        report = {
            "topic": "网页爬取",
            "run_id": "test-run",
            "started_at": "2026-09-28T09:00:00",
            "status": "completed",
            "stop_reason": "target_reached",
            "parameters": {"max_rounds": 1, "max_tokens": 1000},
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "search": {"queries_executed": [], "repos_discovered": 0, "candidates_found": 0, "rounds_history": []},
            "plan": {
                "intent": "爬取网页",
                "criteria": [],
                "terminology_observation": {
                    "version": TERMINOLOGY_VERSION,
                    "mode": "observation",
                    "applied": False,
                    "gaps": [{"concept_id": "web_scraping"}],
                    "suggested_queries": ["web scraping"],
                },
            },
            "shortlist": [],
            "alternatives": [],
        }
        md = render_find_markdown_report(report)
        self.assertIn("术语覆盖观察", md)
        self.assertIn("检测到 1 处概念缺口", md)
        self.assertIn("拟补充短语：web scraping", md)
        self.assertIn("当前为观察模式，未追加请求", md)

    def test_sanitize_report_for_public_includes_observation(self) -> None:
        report = {
            "run_id": "test-run",
            "topic": "网页爬取",
            "status": "completed",
            "parameters": {},
            "usage": {},
            "plan": {
                "intent": "爬取",
                "criteria": [],
            },
            "terminology_observation": {
                "version": TERMINOLOGY_VERSION,
                "mode": "observation",
                "applied": False,
                "gaps": [{"concept_id": "test"}],
                "suggested_queries": ["test"],
            },
        }
        sanitized = sanitize_report_for_public(report)
        obs_proj = sanitized["terminology_observation"]
        self.assertIsNotNone(obs_proj)
        self.assertEqual(obs_proj["version"], TERMINOLOGY_VERSION)
        self.assertEqual(obs_proj["mode"], "observation")
        self.assertFalse(obs_proj["applied"])
        self.assertEqual(obs_proj["gaps_count"], 1)


if __name__ == "__main__":
    unittest.main()
