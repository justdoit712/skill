"""定向查找与已收录名单（Owned Skills）集成测试。

根据《已收录 Skill 管理：详细实施方案》§5.5 及 §11.3 验证：
1. 候选全已收录时，正文抓取与模型评估调用均为 0，保留真实规划 Token 用量，状态为 completed / all_candidates_owned，CLI 返回 0；
2. 过滤在调度截断前完成，已收录候选不占名额，后续未收录候选正常进入调度与评估；
3. 搜索部分失败且所有已发现候选均已收录时，仍保留 coverage_incomplete 覆盖不足警告；
4. 损坏或非法 owned-skills.json 时报错停止，0 模型调用，CLI 返回 2；
5. 离线重建查找报告兼容已收录跳过字段并能正常更新公共快照；
6. 严守双向绝缘，不读取或修改主目录、池或周额度。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.finder.run import (
    STATUS_ALL_CANDIDATES_OWNED,
    STATUS_COMPLETED,
    execute_find_skill,
    main,
)
from src.finder.report import rebuild_find_report
from src.infra.llm import ModelCallResult
from src.shared.identity import candidate_from_repo


class TestFinderOwnedIntegration(unittest.TestCase):
    """定向查找与已收录名单过滤测试集。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )
        (self.root / "config" / "find-skill.json").write_text(
            json.dumps({"limit": 5, "max_evaluations": 10, "max_tokens": 50000}),
            encoding="utf-8",
        )
        (self.root / "config" / "owned-skills.json").write_text(
            json.dumps({
                "schema_version": "1.0.0",
                "items": [
                    {
                        "skill_id": "test-owner/test-repo:skills/pdf/SKILL.md",
                        "name": "pdf",
                        "source_url": "https://github.com/test-owner/test-repo",
                        "added_at": "2026-09-24",
                    }
                ],
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_all_candidates_owned_skips_fetch_and_eval(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """所有发现的候选均已收录：0 次抓取，0 次评估，保留规划 Token，正常完成。"""
        # 1. 规划调用成功
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "处理 PDF 需求",
                "queries": ["pdf-skill"],
                "criteria": [{"id": "c1", "kind": "required", "description": "支持提取 PDF"}],
            }),
            usage={"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
        )
        mock_call.return_value = plan_res

        # 2. 搜索发现 1 个仓库
        mock_search.return_value = (True, [{"owner": "test-owner", "repo": "test-repo", "url": "u", "description": "d"}], None)

        # 3. 展开发现 1 个候选，正是已收录的 pdf
        owned_cand = candidate_from_repo(
            owner="test-owner",
            repo="test-repo",
            path="skills/pdf/SKILL.md",
            url="https://github.com/test-owner/test-repo/blob/HEAD/skills/pdf/SKILL.md",
            repo_url="https://github.com/test-owner/test-repo",
            name="pdf",
            description="PDF 工具",
            discovered_at="2026-09-24T00:00:00Z",
        )
        mock_expand.return_value = ([owned_cand], [{"owner": "test-owner", "repo": "test-repo", "ok": True}])

        # 执行查找
        report = execute_find_skill("PDF 工具", root_dir=self.root, max_rounds=1)

        # 验证：仅发生了 1 次规划模型调用，抓取与评估模型调用为 0
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(mock_fetch.call_count, 0)

        # 验证报告状态
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "round_limit")
        self.assertEqual(report["evaluated_count"], 0)
        self.assertEqual(len(report["shortlist"]), 0)
        self.assertEqual(len(report["alternatives"]), 0)

        # 验证搜索统计与跳过记录
        self.assertEqual(report["search"]["candidates_found"], 1)
        self.assertEqual(report["search"]["skipped_owned"], 1)
        self.assertIn("test-owner/test-repo:skills/pdf/SKILL.md", report["search"]["skipped_owned_ids"])

        # 验证保留了真实规划 Token 用量
        self.assertEqual(report["usage"]["total_tokens"], 180)
        self.assertEqual(report["usage"]["prompt_tokens"], 120)
        self.assertEqual(report["usage"]["completion_tokens"], 60)

        # 验证 Markdown 报告内容
        report_md_path = Path(report["report_paths"]["md"])
        self.assertTrue(report_md_path.exists())
        md_text = report_md_path.read_text(encoding="utf-8")
        self.assertIn("本次发现的候选已全部收录", md_text)
        self.assertIn("已收录跳过 1 个", md_text)

        # 验证本地与公共快照更新
        public_snapshot = self.root / "public" / "data" / "find-report.json"
        self.assertTrue(public_snapshot.exists())
        snap_data = json.loads(public_snapshot.read_text(encoding="utf-8"))
        self.assertEqual(snap_data["status"], "stopped")
        self.assertEqual(snap_data["stop_reason"], "round_limit")
        self.assertEqual(snap_data["search"]["skipped_owned"], 1)

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_owned_candidate_does_not_consume_quota(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """过滤必须在调度截断前完成：已收录项不占名额，后续未收录候选正常进入调度与评估。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "测试意图",
                "queries": ["q1"],
                "criteria": [{"id": "c1", "kind": "required", "description": "d1"}],
            }),
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        )
        eval_res = ModelCallResult(
            ok=True,
            content=json.dumps({
                "match": "strong",
                "documentation": "clear",
                "summary_zh": "未收录候选评估成功",
                "why_consider": "符合需求",
                "usage_zh": "使用指引",
                "criteria_results": [
                    {"criterion_id": "c1", "status": "supported", "explanation": "已满足",
                     "evidence": [{"source_path": "skills/excel/SKILL.md", "start_line": 1, "end_line": 1, "quote": "extract excel"}]}
                ],
            }),
            usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
        )
        mock_call.side_effect = [plan_res, eval_res]

        mock_search.return_value = (True, [{"owner": "test-owner", "repo": "test-repo", "url": "u", "description": "d"}], None)

        owned_cand = candidate_from_repo(
            owner="test-owner",
            repo="test-repo",
            path="skills/pdf/SKILL.md",
            url="https://github.com/test-owner/test-repo/blob/HEAD/skills/pdf/SKILL.md",
            repo_url="https://github.com/test-owner/test-repo",
            name="pdf",
            description="已收录项",
            discovered_at="2026-09-24T00:00:00Z",
        )
        unowned_cand = candidate_from_repo(
            owner="test-owner",
            repo="test-repo",
            path="skills/excel/SKILL.md",
            url="https://github.com/test-owner/test-repo/blob/HEAD/skills/excel/SKILL.md",
            repo_url="https://github.com/test-owner/test-repo",
            name="excel",
            description="未收录项",
            discovered_at="2026-09-24T00:00:00Z",
        )
        mock_expand.return_value = ([owned_cand, unowned_cand], [{"owner": "test-owner", "repo": "test-repo", "ok": True}])
        mock_fetch.return_value = (True, {"skills/excel/SKILL.md": "extract excel"}, None)

        report = execute_find_skill("测试", root_dir=self.root, limit=1)

        # 验证：第 1 个候选跳过，第 2 个未收录候选成功评估
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(mock_call.call_count, 2)
        self.assertEqual(report["status"], STATUS_COMPLETED)
        self.assertEqual(report["evaluated_count"], 1)
        self.assertEqual(report["search"]["skipped_owned"], 1)
        self.assertEqual(report["evaluations"][0]["candidate"]["skill_id"], "test-owner/test-repo:skills/excel/SKILL.md")

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_partial_search_failure_preserves_coverage_warning_with_all_owned(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """搜索部分失败且发现的候选全已收录：stop_reason=all_candidates_owned，且 coverage_incomplete=True。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "双查询意图",
                "queries": ["q-fail", "q-ok"],
                "criteria": [{"id": "c1", "kind": "required", "description": "d1"}],
            }),
            usage={"prompt_tokens": 60, "completion_tokens": 30, "total_tokens": 90},
        )
        mock_call.return_value = plan_res

        # q-fail 失败，q-ok 成功
        mock_search.side_effect = [
            (False, [], "GitHub API 500 error"),
            (False, [], "GitHub API 500 error"),
            (False, [], "GitHub API 500 error"),
            (True, [{"owner": "test-owner", "repo": "test-repo", "url": "u", "description": "d"}], None),
        ]

        owned_cand = candidate_from_repo(
            owner="test-owner",
            repo="test-repo",
            path="skills/pdf/SKILL.md",
            url="https://github.com/test-owner/test-repo/blob/HEAD/skills/pdf/SKILL.md",
            repo_url="https://github.com/test-owner/test-repo",
            name="pdf",
            description="已收录",
            discovered_at="2026-09-24T00:00:00Z",
        )
        mock_expand.return_value = ([owned_cand], [{"owner": "test-owner", "repo": "test-repo", "ok": True}])

        report = execute_find_skill("测试覆盖率", root_dir=self.root, max_rounds=1)

        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "round_limit")
        self.assertTrue(report["coverage_incomplete"])

        md_text = Path(report["report_paths"]["md"]).read_text(encoding="utf-8")
        self.assertIn("本次发现的候选已全部收录", md_text)
        self.assertIn("本次检索覆盖不完整", md_text)

    @patch("src.finder.run.call_model")
    def test_corrupted_owned_config_fails_fast_zero_model_calls(self, mock_call) -> None:
        """损坏或非法的 owned-skills.json：直接抛出 ValueError，0 次模型调用。"""
        (self.root / "config" / "owned-skills.json").write_text("{bad json}", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            execute_find_skill("测试", root_dir=self.root, limit=1)
        self.assertIn("已收录配置文件 JSON 格式损坏", str(ctx.exception))
        self.assertEqual(mock_call.call_count, 0)

        # 验证 CLI main 返回 2
        code = main(["测试", "--limit", "3"], root=self.root)
        self.assertEqual(code, 2)

    def test_rebuild_find_report_supports_all_candidates_owned(self) -> None:
        """离线重建查找报告兼容 all_candidates_owned 结果。"""
        run_dir = self.root / "data" / "local" / "find-skills" / "test-run"
        run_dir.mkdir(parents=True)
        report_data = {
            "schema_version": "1.0.0",
            "run_id": "test-run",
            "topic": "离线重建测试",
            "status": "completed",
            "stop_reason": "all_candidates_owned",
            "parameters": {"limit": 5, "max_evaluations": 10, "max_tokens": 50000},
            "evaluated_count": 0,
            "shortlist": [],
            "alternatives": [],
            "search": {
                "queries_executed": [{"query": "q", "ok": True, "repos_returned": 1}],
                "repos_discovered": 1,
                "candidates_found": 1,
                "skipped_owned": 1,
                "skipped_owned_ids": ["test-owner/test-repo:skills/pdf/SKILL.md"],
            },
        }
        (run_dir / "report.json").write_text(json.dumps(report_data), encoding="utf-8")

        public_data = self.root / "public" / "data"
        rebuilt = rebuild_find_report(run_dir, public_data_dir=public_data)
        self.assertEqual(rebuilt["stop_reason"], "all_candidates_owned")
        self.assertTrue((run_dir / "report.md").exists())
        self.assertTrue((public_data / "find-report.json").exists())

    @patch("src.finder.run.call_model")
    def test_runtime_plan_parse_error_finalizes_run_and_preserves_tokens(self, mock_call) -> None:
        """O-04 验收：规划请求返回无效 JSON 时，必须收尾生成终态报告并保留 Token 用量。"""
        # 模型返回非 JSON 文本，但有真实 Token 消耗
        mock_call.return_value = ModelCallResult(
            ok=True,
            content="这不是合法的 JSON 格式内容",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

        report = execute_find_skill("测试规划解析异常", root_dir=self.root)

        # 验证没有直接抛出 ValueError，而是收尾进入错误终态
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["stop_reason"], "plan_failed")
        self.assertEqual(report["usage"]["total_tokens"], 150)
        self.assertEqual(report["usage"]["requests"], 1)

        # 验证错误阶段记录
        planning_errors = [e for e in report.get("errors", []) if e.get("stage") == "planning"]
        self.assertTrue(len(planning_errors) >= 1)
        self.assertEqual(planning_errors[0]["code"], "plan_failed")

        # 验证本地事实 report.json 终态不再为 running，且 report.md 已成功生成
        run_json = Path(report["report_paths"]["json"])
        self.assertTrue(run_json.exists())
        saved_data = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(saved_data["status"], "error")
        self.assertEqual(saved_data["stop_reason"], "plan_failed")
        self.assertTrue(Path(report["report_paths"]["md"]).exists())

        # 验证 CLI main 返回 1（运行期错误，非参数错误 2）
        code = main(["测试规划解析异常"], root=self.root)
        self.assertEqual(code, 1)

    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_runtime_adapter_error_finalizes_run_as_execution_error(self, mock_call, mock_search) -> None:
        """O-04 验收：规划成功后搜索适配器抛出 ValueError，必须记录 execution 阶段收尾。"""
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "测试适配器异常",
                "queries": ["query1"],
                "criteria": [{"id": "c1", "kind": "required", "description": "测试条件"}],
            }),
            usage={"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
        )
        mock_search.side_effect = ValueError("网络适配器运行时抛错")

        report = execute_find_skill("测试适配器异常", root_dir=self.root)

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["stop_reason"], "execution_error")
        exec_errors = [e for e in report.get("errors", []) if e.get("stage") == "execution"]
        self.assertTrue(len(exec_errors) >= 1)
        self.assertEqual(exec_errors[0]["code"], "execution_error")
        self.assertTrue(Path(report["report_paths"]["md"]).exists())

        code = main(["测试适配器异常"], root=self.root)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
