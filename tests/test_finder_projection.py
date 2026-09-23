"""测试定向查找架构隔离与安全报告投影 (P4 · Finder Package & Report Projection)。

涵盖验收测试：
- T08: Finder 完全绕过目录规则（跳过排除词、黑名单与冷冻），且对主目录索引、候选池与账本产生 0 副作用。
- T11: 公共快照 (public/data/find-report.json) 白名单脱敏，绝对无本机绝对路径或本地目录泄漏。
- T11-2: 公共快照四态更新发布条件矩阵（0 匹配覆盖写空 vs 0 成功异常失败保留旧快照）。
- T13: 前端定向查找视图与多停止状态渲染适配（usage_unknown, token_limit, interrupted 等）。
- 架构守卫：src/finder/ 物理隔离，绝对不导入 src.catalog 任何模块。
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from src.infra.llm import ModelCallResult
from src.finder.report import (
    render_find_markdown_report,
    sanitize_report_for_public,
    should_update_public_snapshot,
    update_public_snapshot,
)
from src.finder.run import (
    STATUS_CANDIDATES_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_STOPPED,
    STATUS_USAGE_UNKNOWN,
    execute_find_skill,
)
from src.shared.models import Candidate

ROOT = Path(__file__).resolve().parents[1]


class TestT08FinderCatalogDecoupling(unittest.TestCase):
    """T08: Finder 物理与运行时解耦测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)

        # 构造测试环境与目录文件
        (self.root / "config").mkdir(parents=True)
        (self.root / "data" / "state").mkdir(parents=True)
        (self.root / "data" / "local").mkdir(parents=True)
        (self.root / "public" / "data").mkdir(parents=True)

        # 模型配置
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

        # 目录规则（排除词、分类、黑名单、冷冻）
        (self.root / "config" / "categories.json").write_text(
            json.dumps({"categories": [{"id": "dev", "name": "开发", "excluded_keywords": ["ai 开发", "prompt"]}]}),
            encoding="utf-8",
        )
        (self.root / "config" / "overrides.json").write_text(
            json.dumps({"manual_exclusions": [{"skill_id": "test/repo:SKILL.md", "reason": "已排除"}]}),
            encoding="utf-8",
        )
        (self.root / "config" / "snooze.json").write_text(
            json.dumps({"snoozed": [{"skill_id": "test/repo:SKILL.md", "expires_at": "2099-01-01"}]}),
            encoding="utf-8",
        )

        # 目录数据文件（必须零副作用）
        self.catalog_file = self.root / "data" / "catalog.json"
        self.catalog_file.write_text(json.dumps({"entries": [{"skill_id": "existing/one"}]}), encoding="utf-8")

        self.pool_file = self.root / "data" / "local" / "pool.json"
        self.pool_file.write_text(json.dumps({"candidates": []}), encoding="utf-8")

        self.budget_file = self.root / "data" / "state" / "budget.json"
        self.budget_file.write_text(json.dumps({"total_tokens": 1000}), encoding="utf-8")

        self.catalog_hash_before = hashlib.sha256(self.catalog_file.read_bytes()).hexdigest()
        self.pool_hash_before = hashlib.sha256(self.pool_file.read_bytes()).hexdigest()
        self.budget_hash_before = hashlib.sha256(self.budget_file.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_finder_zero_catalog_imports_static_guard(self) -> None:
        """架构红线：src/finder/ 中的所有模块严禁直接或间接导入 src.catalog。"""
        finder_dir = ROOT / "src" / "finder"
        for py_file in finder_dir.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("src.catalog") or alias.name == "catalog",
                            f"{py_file.name} 违规导入了 catalog 模块: {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertFalse(
                        mod.startswith("src.catalog") or mod == "catalog",
                        f"{py_file.name} 违规从 catalog 模块导入: from {mod} import ...",
                    )

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_finder_completely_ignores_catalog_rules_and_zero_side_effects(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """即使技能包含主目录排除词或在黑名单中，Finder 仍正常检索评估；主目录文件 0 副作用。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps(
                {
                    "intent": "找 AI 开发助手",
                    "queries": ["ai 开发"],
                    "criteria": [{"id": "c1", "kind": "required", "description": "支持 AI 开发"}],
                }
            ),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        eval_res = ModelCallResult(
            ok=True,
            content=json.dumps(
                {
                    "match": "strong",
                    "documentation": "clear",
                    "summary_zh": "优秀 AI 开发助手",
                    "criteria_results": [
                        {
                            "criterion_id": "c1",
                            "status": "supported",
                            "explanation": "明确支持",
                            "evidence": [{"source_path": "SKILL.md", "start_line": 1, "end_line": 1, "quote": "支持 AI 开发"}],
                        }
                    ],
                }
            ),
            usage={"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
        )
        mock_call.side_effect = [plan_res, eval_res]
        mock_search.return_value = (True, [{"owner": "test", "repo": "repo", "url": "https://github.com/test/repo", "description": "desc"}], None)

        cand = Candidate(
            skill_id="test/repo:SKILL.md",
            owner="test",
            repo="repo",
            path="SKILL.md",
            url="https://github.com/test/repo/blob/HEAD/SKILL.md",
            repo_url="https://github.com/test/repo",
            name="repo",
            description="",
            discovered_at="2026-01-01T00:00:00Z",
        )
        mock_expand.return_value = ([cand], [])
        mock_fetch.return_value = (True, {"SKILL.md": "支持 AI 开发"}, None)

        report = execute_find_skill("AI 开发", root_dir=self.root)

        # 断言结果正常产出，完全无视目录排除与黑名单规则
        self.assertEqual(report["status"], STATUS_COMPLETED)
        self.assertEqual(len(report["shortlist"]), 1)
        self.assertEqual(report["shortlist"][0]["candidate"]["skill_id"], "test/repo:SKILL.md")

        # 核心断言：主目录数据、候选池与账本文件绝对没有任何写入（哈希完全一致）
        self.assertEqual(hashlib.sha256(self.catalog_file.read_bytes()).hexdigest(), self.catalog_hash_before)
        self.assertEqual(hashlib.sha256(self.pool_file.read_bytes()).hexdigest(), self.pool_hash_before)
        self.assertEqual(hashlib.sha256(self.budget_file.read_bytes()).hexdigest(), self.budget_hash_before)


class TestT11PublicProjectionSanitization(unittest.TestCase):
    """T11: 前端展示层白名单投影与脱敏安全性测试。"""

    def test_sanitize_report_removes_absolute_paths_and_internal_diagnostics(self) -> None:
        """白名单投影转换器必须物理剔除本机盘符、绝对路径、内部诊断，保证公共快照安全。"""
        raw_report = {
            "run_id": "20260923-010203-abcdef",
            "started_at": "2026-09-23T01:02:03+08:00",
            "topic": "生成高质量 Prompt",
            "status": "completed",
            "stop_reason": "target_reached",
            "parameters": {"limit": 5, "max_evaluations": 20, "max_tokens": 200000},
            "model": "deepseek-chat",
            "plan": {"intent": "生成提示词", "criteria": [{"id": "c1", "kind": "required", "description": "支持生成"}]},
            "search": {
                "queries_executed": [{"query": "prompt generator", "ok": True, "repos_returned": 10, "error": None}],
                "repos_discovered": 10,
                "candidates_found": 50,
                "expansions": [{"repo": "user/repo", "local_fs_trace": "D:\\secret\\tmp"}],
            },
            "evaluation_attempts": 3,
            "evaluated_count": 3,
            "shortlist": [
                {
                    "candidate": {
                        "skill_id": "author/skill:SKILL.md",
                        "name": "prompt-craft",
                        "repo_url": "https://github.com/author/skill",
                        "url": "https://github.com/author/skill/blob/HEAD/SKILL.md",
                        "author": "author",
                        "path": "SKILL.md",
                        "content_fingerprint": "sha256:abc123",
                    },
                    "evaluation": {
                        "match": "strong",
                        "documentation": "clear",
                        "summary_zh": "生成提示词工具",
                        "why_consider": "高质量输出",
                        "criteria_results": [
                            {
                                "criterion_id": "c1",
                                "status": "supported",
                                "explanation": "支持生成",
                                "evidence": [{"source_path": "SKILL.md", "start_line": 5, "end_line": 5, "quote": "prompt builder"}],
                            }
                        ],
                    },
                }
            ],
            "alternatives": [],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            # 必须被过滤掉的内部敏感信息
            "report_paths": {
                "json": "D:\\Code\\opensource\\skill\\data\\local\\find-skills\\20260923-010203-abcdef\\report.json",
                "md": "D:\\Code\\opensource\\skill\\data\\local\\find-skills\\20260923-010203-abcdef\\report.md",
            },
            "evaluations": [{"raw_material_dump": "huge string"}],
        }

        projection = sanitize_report_for_public(raw_report)
        json_str = json.dumps(projection, ensure_ascii=False)

        # 断言绝不包含 report_paths
        self.assertNotIn("report_paths", projection)
        self.assertNotIn("evaluations", projection)
        self.assertNotIn("local_fs_trace", json_str)

        # 断言绝不包含本地盘符路径
        self.assertNotIn("D:\\", json_str)
        self.assertNotIn("C:\\", json_str)
        self.assertNotIn("/Users/", json_str)
        self.assertNotIn("/home/", json_str)

        # 断言包含安全白名单结构
        self.assertEqual(projection["schema_version"], "1.0.0")
        self.assertEqual(projection["run_id"], "20260923-010203-abcdef")
        self.assertEqual(projection["shortlist_count"], 1)
        self.assertEqual(projection["alternatives_count"], 0)
        self.assertIn("candidate", projection["shortlist"][0])
        self.assertIn("evaluation", projection["shortlist"][0])
        # 同时检查扁平快捷字段
        self.assertEqual(projection["shortlist"][0]["skill_id"], "author/skill:SKILL.md")


class TestT11_2PublicSnapshotUpdateConditions(unittest.TestCase):
    """T11-2: 公共快照 (find-report.json) 四态更新发布条件矩阵测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.public_dir = Path(self.temp_dir.name) / "public" / "data"
        self.public_dir.mkdir(parents=True)
        self.snapshot_file = self.public_dir / "find-report.json"

        # 模拟已有上次有效的快照
        self.baseline_snapshot = {
            "topic": "上次有效需求",
            "status": "completed",
            "shortlist_count": 2,
            "evaluated_count": 5,
        }
        self.snapshot_file.write_text(json.dumps(self.baseline_snapshot), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_case1_normal_finish_with_recommendations_updates_snapshot(self) -> None:
        """场景 1：正常完成，有推荐结果 -> 更新公共快照。"""
        report = {
            "topic": "新需求",
            "status": STATUS_COMPLETED,
            "stop_reason": "target_reached",
            "evaluated_count": 3,
            "shortlist": [{"candidate": {"name": "skill1"}, "evaluation": {"match": "strong"}}],
            "alternatives": [],
        }
        self.assertTrue(should_update_public_snapshot(report))
        updated = update_public_snapshot(report, self.public_dir)
        self.assertTrue(updated)

        saved = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["topic"], "新需求")
        self.assertEqual(saved["shortlist_count"], 1)

    def test_case2_normal_finish_zero_matches_overwrites_with_empty(self) -> None:
        """场景 2：正常完成但 0 匹配 -> 覆盖写入空结果快照（防止旧需求残留误导）。"""
        report = {
            "topic": "极罕见冷门需求",
            "status": STATUS_COMPLETED,
            "stop_reason": STATUS_CANDIDATES_EXHAUSTED,
            "evaluated_count": 0,
            "shortlist": [],
            "alternatives": [],
        }
        self.assertTrue(should_update_public_snapshot(report))
        updated = update_public_snapshot(report, self.public_dir)
        self.assertTrue(updated)

        saved = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["topic"], "极罕见冷门需求")
        self.assertEqual(saved["shortlist_count"], 0)
        self.assertEqual(saved["alternatives_count"], 0)

    def test_case3_stopped_with_partial_results_updates_with_status(self) -> None:
        """场景 3：异常熔断或中断，但已有部分有效条目 -> 写入带明确状态的部分结果。"""
        report = {
            "topic": "部分完成需求",
            "status": STATUS_STOPPED,
            "stop_reason": STATUS_USAGE_UNKNOWN,
            "evaluated_count": 2,
            "shortlist": [{"candidate": {"name": "skill1"}, "evaluation": {"match": "strong"}}],
            "alternatives": [],
        }
        self.assertTrue(should_update_public_snapshot(report))
        updated = update_public_snapshot(report, self.public_dir)
        self.assertTrue(updated)

        saved = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["topic"], "部分完成需求")
        self.assertEqual(saved["status"], STATUS_STOPPED)
        self.assertEqual(saved["stop_reason"], STATUS_USAGE_UNKNOWN)
        self.assertEqual(saved["evaluated_count"], 2)

    def test_case4_early_failure_zero_success_retains_previous_snapshot(self) -> None:
        """场景 4：完全失败/启动错误（0 成功） -> 坚决保留上次有效公共快照，不破坏已有展示。"""
        report = {
            "topic": "网络故障失败需求",
            "status": STATUS_ERROR,
            "stop_reason": "plan_failed",
            "evaluated_count": 0,
            "shortlist": [],
            "alternatives": [],
        }
        self.assertFalse(should_update_public_snapshot(report))
        updated = update_public_snapshot(report, self.public_dir)
        self.assertFalse(updated)

        # 核心断言：快照文件内容坚决保持上次有效数据，绝不被清空或覆盖为错误数据
        saved = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["topic"], "上次有效需求")
        self.assertEqual(saved["evaluated_count"], 5)


class TestT13FrontendFindViewRendering(unittest.TestCase):
    """T13: 前端定向查找视图与多停止状态渲染测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        js_files = sorted((ROOT / "public" / "js").glob("*.js"))
        js_content = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        cls.html_content = html + "\n" + js_content
        cls.css_content = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    def test_frontend_handles_all_stop_states(self) -> None:
        """断言 index.html 的 renderFindView 包含针对各种停止状态的友好提示。"""
        self.assertIn("usage_unknown", self.html_content)
        self.assertIn("token_limit", self.html_content)
        self.assertIn("evaluation_limit", self.html_content)
        self.assertIn("interrupted", self.html_content)
        self.assertIn("model_failures", self.html_content)

    def test_frontend_find_styles_exist(self) -> None:
        """断言 styles.css 包含状态条样式。"""
        self.assertIn(".find-status-banner", self.css_content)
        self.assertIn(".find-status-banner.warning", self.css_content)
        self.assertIn(".find-status-banner.danger", self.css_content)

    def test_frontend_renders_both_nested_and_flat_card_models(self) -> None:
        """断言 index.html 既支持嵌套的 candidate/evaluation，也支持脱敏扁平属性。"""
        self.assertIn("item.candidate || item", self.html_content)
        self.assertIn("item.evaluation || item", self.html_content)
