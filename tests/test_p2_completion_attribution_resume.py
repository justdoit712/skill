"""Unit 4 / P2.1 续篇单元测试：有界补全、归因和续跑。

覆盖范围：
1. 有界术语补全：在现有查询上限（<=8）内合并补词，绝不改变意图、required 或 quality_signal；
2. 预算防溢出：当原始查询已满 8 条时，自动截断不补词；
3. 负向约束防御：需求包含明确否定（如“不需要 selenium”）时，绝不推荐相关负向词；
4. 稳定查询身份与来源归因：确定性 hash query_id，准确归因 model 与 terminology；
5. 检索执行与边际收益统计：多查询发现仓库/Skill 的全量来源记录与首次边际归因（注明执行顺序影响）；
6. 续跑契约：恢复已有计划时不重新触发术语扩展，不重复处理已有候选；
7. 报告与脱敏快照渲染：安全展示查询边际收益与有界补全审计信息。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from src.finder.plan import (
    MAX_PLAN_QUERIES,
    parse_plan_with_observation,
    parse_query_plan,
)
from src.finder.refill import (
    _expand_pending,
    _search_pages,
    initialize_search,
)
from src.finder.report import (
    render_find_markdown_report,
    sanitize_report_for_public,
)
from src.finder.run import (
    FinderRunState,
    execute_find_skill,
)
from src.finder.terminology import (
    TERMINOLOGY_VERSION,
    apply_terminology_completion,
    build_terminology_observation,
    extract_negative_constraints,
    generate_query_identity,
    generate_suggested_queries,
)
from src.shared.models import Candidate
from tests.fixtures.benchmark_samples import QUERY_BENCHMARK_SAMPLES


class TestBoundedTerminologyCompletion(unittest.TestCase):
    """测试有界术语补全纯函数逻辑与边界保护。"""

    def test_bounded_completion_merges_queries_within_budget(self) -> None:
        topic = "网页爬取与数据提取"
        intent = "爬取网页内容并解析数据"
        original = ["网页爬取", "数据提取"]

        final_queries, meta = apply_terminology_completion(
            topic,
            intent,
            original,
            max_total_queries=5,
        )

        # 补全后总数不超过 5
        self.assertLessEqual(len(final_queries), 5)
        # 原始查询全部保留在最前
        self.assertEqual(final_queries[:2], original)
        # 至少补充了别名（如 web scraping）
        self.assertTrue(any("web scraping" in q.lower() or "web crawler" in q.lower() for q in final_queries))
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["mode"], "completion")
        self.assertEqual(meta["original_queries"], original)
        self.assertEqual(meta["final_queries"], final_queries)
        self.assertEqual(len(meta["added_queries"]), len(final_queries) - len(original))

    def test_bounded_completion_does_not_exceed_max_when_already_full(self) -> None:
        topic = "网页爬取"
        intent = "爬取网页"
        full_queries = [f"query {i}" for i in range(MAX_PLAN_QUERIES)]

        final_queries, meta = apply_terminology_completion(
            topic,
            intent,
            full_queries,
            max_total_queries=MAX_PLAN_QUERIES,
        )

        self.assertEqual(len(final_queries), MAX_PLAN_QUERIES)
        self.assertEqual(final_queries, full_queries)
        self.assertEqual(meta["added_queries"], [])

    def test_negative_constraint_filtering(self) -> None:
        topic = "纯 Python requests 网页抓取，不需要 Selenium 或无头浏览器"
        intent = "网页抓取"
        original = ["python requests 网页抓取"]

        neg_tokens = extract_negative_constraints(topic)
        self.assertIn("selenium", neg_tokens)

        final_queries, meta = apply_terminology_completion(
            topic,
            intent,
            original,
            max_total_queries=8,
        )

        # 验证绝无可能补充 selenium
        for q in final_queries:
            self.assertNotIn("selenium", q.lower())
            self.assertNotIn("headless", q.lower())

    def test_stable_query_identity(self) -> None:
        qid_1 = generate_query_identity("web scraping")
        qid_2 = generate_query_identity("  Web   Scraping  ")
        qid_3 = generate_query_identity("data cleaning")

        self.assertEqual(qid_1, qid_2)
        self.assertNotEqual(qid_1, qid_3)
        self.assertEqual(len(qid_1), 12)

    def test_query_attributions_distinguish_model_and_terminology(self) -> None:
        topic = "网页抓取工具"
        intent = "抓取网页"
        original = ["网页抓取"]

        final_queries, meta = apply_terminology_completion(
            topic,
            intent,
            original,
            max_total_queries=4,
        )

        attrs = meta["query_attributions"]
        self.assertEqual(len(attrs), len(final_queries))

        # 第一条源自模型
        self.assertEqual(attrs[0]["query"], "网页抓取")
        self.assertEqual(attrs[0]["source"], "model")

        # 补充的短语源自术语表
        if len(attrs) > 1:
            self.assertEqual(attrs[1]["source"], "terminology")
            self.assertEqual(attrs[1]["concept_id"], "web_scraping")
            self.assertEqual(attrs[1]["concept_name"], "网页爬取")


class TestPlanWithObservationVsCompletion(unittest.TestCase):
    """测试 plan.py 中 parse_plan_with_observation 在观察模式与补全模式下的表现。"""

    def setUp(self) -> None:
        self.sample_json = json.dumps(
            {
                "intent": "构建高效网页爬虫",
                "queries": ["网页爬取", "数据提取"],
                "criteria": [
                    {"id": "scraping", "kind": "required", "description": "支持页面解析"},
                    {"id": "speed", "kind": "quality_signal", "description": "异步高并发"},
                ],
            },
            ensure_ascii=False,
        )

    def test_observation_mode_does_not_modify_queries(self) -> None:
        plan, obs = parse_plan_with_observation(
            self.sample_json,
            topic="网页爬取与清洗",
            enable_completion=False,
        )
        self.assertFalse(plan.get("terminology_completion_enabled", False))
        self.assertEqual(plan["queries"], ["网页爬取", "数据提取"])
        self.assertEqual(obs["original_queries"], ["网页爬取", "数据提取"])
        self.assertFalse(obs["applied"])
        self.assertEqual(obs["mode"], "observation")

    def test_completion_mode_augments_queries_and_preserves_criteria(self) -> None:
        plan, meta = parse_plan_with_observation(
            self.sample_json,
            topic="网页爬取与清洗",
            enable_completion=True,
            max_total_queries=6,
        )
        self.assertTrue(plan["terminology_completion_enabled"])
        self.assertEqual(plan["original_queries"], ["网页爬取", "数据提取"])
        self.assertGreater(len(plan["queries"]), len(plan["original_queries"]))
        self.assertTrue(meta["applied"])
        self.assertEqual(meta["mode"], "completion")

        # 极其关键：意图与准则必须原样保留，绝不可被擅自篡改
        self.assertEqual(plan["intent"], "构建高效网页爬虫")
        self.assertEqual(len(plan["criteria"]), 2)
        self.assertEqual(plan["criteria"][0]["kind"], "required")
        self.assertEqual(plan["criteria"][1]["kind"], "quality_signal")


class TestRefillAttributionAndMarginalYield(unittest.TestCase):
    """测试搜索执行中仓库与 Skill 的全量来源追踪及边际新增计算。"""

    def test_multi_query_marginal_repos_and_skill_sources(self) -> None:
        state = FinderRunState("测试主题", {"limit": 5, "max_rounds": 1})
        current = {
            "round": 1,
            "searches": [],
            "new_repos": 0,
            "candidates": 0,
            "evaluation_start": 0,
            "feedback_start": 0,
        }

        # 模拟 search_fn：
        # query1 返回 [repoA, repoB]
        # query2 返回 [repoB, repoC]
        def mock_search_fn(query: str, page: int = 1, **kwargs):
            if query == "query1":
                return True, [
                    {"owner": "org1", "repo": "repoA"},
                    {"owner": "org1", "repo": "repoB"},
                ], None
            if query == "query2":
                return True, [
                    {"owner": "org1", "repo": "repoB"},
                    {"owner": "org2", "repo": "repoC"},
                ], None
            return True, [], None

        initialize_search(state)
        reason = _search_pages(state, current, ["query1", "query2"], mock_search_fn, sleep=lambda _: None, log=lambda *_: None)
        self.assertIsNone(reason)

        search = state.report["search"]
        self.assertEqual(search["repos_discovered"], 3)  # repoA, repoB, repoC

        qy = search["query_yield"]
        # query1: total=2, marginal=2 (首次发现 repoA, repoB)
        self.assertEqual(qy["query1"]["total_repos"], 2)
        self.assertEqual(qy["query1"]["marginal_repos"], 2)

        # query2: total=2, marginal=1 (repoB 已在 query1 出现，仅 repoC 属于边际新增)
        self.assertEqual(qy["query2"]["total_repos"], 2)
        self.assertEqual(qy["query2"]["marginal_repos"], 1)

        # 验证 repoB 记录了全部两个发现查询
        repo_b = next(r for r in search["repository_queue"] if r["key"] == "org1/repob")
        self.assertEqual(repo_b["first_discovered_by"], "query1")
        self.assertIn("query1", repo_b["discovery_queries"])
        self.assertIn("query2", repo_b["discovery_queries"])

        # 现在测试 _expand_pending：展开 repo 生成 Candidate
        def mock_expand(repos, **kwargs):
            candidates = []
            expansions = []
            for r in repos:
                c = Candidate(
                    skill_id=f"{r['owner']}/{r['repo']}:SKILL.md",
                    owner=r["owner"],
                    repo=r["repo"],
                    path="SKILL.md",
                    name=r["repo"],
                )
                candidates.append(c)
                expansions.append({"ok": True, "repo": r["key"]})
            return candidates, expansions

        exp_reason = _expand_pending(state, current, mock_expand, owned_ids=set(), sleep=lambda _: None, log=lambda *_: None)
        self.assertIsNone(exp_reason)

        # 检查候选技能发现归因
        skill_sources = search["skill_discovery_sources"]
        skill_b = skill_sources["org1/repoB:SKILL.md"]
        self.assertEqual(skill_b["first_discovered_by"], "query1")
        self.assertIn("query1", skill_b["discovery_queries"])
        self.assertIn("query2", skill_b["discovery_queries"])

        # 边际技能归因：query1 首次发现了 2 个技能，query2 边际发现了 1 个技能
        self.assertEqual(qy["query1"]["marginal_skills"], 2)
        self.assertEqual(qy["query2"]["marginal_skills"], 1)


class TestResumeContractAndReportRendering(unittest.TestCase):
    """测试续跑复用已有计划契约与报告渲染。"""

    def test_resume_reuses_saved_plan_without_re_expansion(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            run_dir = Path(tmp_dir) / "data" / "local" / "find-skills" / "20260928-test-run"
            run_dir.mkdir(parents=True)

            original_plan = {
                "intent": "固定已存计划",
                "queries": ["saved query 1", "saved query 2"],
                "original_queries": ["saved query 1", "saved query 2"],
                "criteria": [{"id": "c1", "kind": "required", "description": "必需"}],
                "terminology_completion_enabled": True,
                "terminology_version": "1.0.0",
            }

            fake_report = {
                "schema_version": "1.1.0",
                "topic": "测试续跑需求",
                "parameters": {"limit": 5, "max_rounds": 1, "max_evaluations": 10, "max_tokens": 100000},
                "status": "stopped",
                "stop_reason": "round_limit",
                "plan": original_plan,
                "evaluations": [],
                "shortlist": [],
                "alternatives": [],
                "calls": [],
                "errors": [],
                "search": {
                    "queries_executed": [],
                    "query_cursors": {"saved query 1": {"next_page": 2, "exhausted": True}},
                    "repository_queue": [],
                    "candidate_queue": [],
                    "discovered_skill_ids": [],
                    "processed_skill_ids": [],
                    "query_yield": {
                        "saved query 1": {
                            "query": "saved query 1",
                            "total_repos": 10,
                            "marginal_repos": 10,
                            "total_skills": 5,
                            "marginal_skills": 5,
                            "note": "边际新增计数受执行顺序影响",
                        }
                    },
                },
            }
            (run_dir / "report.json").write_text(json.dumps(fake_report, ensure_ascii=False), encoding="utf-8")

            # 续跑执行
            report = execute_find_skill(
                topic="测试续跑需求",
                root_dir=tmp_dir,
                resume_dir=run_dir,
                model_cfg={
                    "endpoint": "https://fake.invalid",
                    "model": "fake",
                    "auth": {"api_key": "fake"},
                },
                search_github_repos_fn=lambda *_, **__: (True, [], None),
                expand_and_collect_candidates_fn=lambda *_, **__: ([], []),
                fetch_candidate_materials_fn=lambda *_, **__: {},
                owned_ids=set(),
            )

            # 计划必须完全保持原样，不可因续跑或当前术语版本更新而被篡改
            self.assertEqual(report["plan"]["queries"], ["saved query 1", "saved query 2"])
            self.assertEqual(report["plan"]["intent"], "固定已存计划")

    def test_report_and_projection_display_query_yield(self) -> None:
        report = {
            "schema_version": "1.1.0",
            "run_id": "test-run",
            "topic": "代码搜索",
            "status": "completed",
            "stop_reason": "target_reached",
            "parameters": {"limit": 5, "max_rounds": 1},
            "plan": {
                "intent": "检索代码工具",
                "terminology_completion_enabled": True,
                "terminology_version": "1.0.0",
                "added_queries": ["code search", "code navigation"],
                "max_total_queries": 8,
                "criteria": [{"id": "c1", "kind": "required", "description": "支持语法分析"}],
            },
            "search": {
                "queries_executed": [{"query": "code search", "ok": True, "repos_returned": 5}],
                "repos_discovered": 5,
                "candidates_found": 3,
                "query_yield": {
                    "code search": {
                        "total_repos": 5,
                        "marginal_repos": 5,
                        "total_skills": 3,
                        "marginal_skills": 3,
                    }
                },
            },
            "evaluations": [],
            "shortlist": [],
            "alternatives": [],
        }

        # 1. 本地 Markdown 报告渲染
        md = render_find_markdown_report(report)
        self.assertIn("术语补全检索", md)
        self.assertIn("查询收益与边际新增", md)
        self.assertIn("code search", md)
        self.assertIn("边际新增 5", md)

        # 2. 公共快照脱敏投影
        pub = sanitize_report_for_public(report)
        self.assertTrue(pub["plan"]["terminology_completion_enabled"])
        self.assertIn("code search", pub["search"]["query_yield"])
        self.assertEqual(pub["search"]["query_yield"]["code search"]["marginal_repos"], 5)


if __name__ == "__main__":
    unittest.main()
