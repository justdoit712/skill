"""定向查找主流程核心单元测试：基本检索流程闭环与公开报告敏感信息脱敏。

保留 2 项核心行为：
1. 基本检索流程（规划、检索与候选处理闭环）。
2. 公开报告脱敏（剔除本地绝对文件路径与敏感字段）。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.finder.report import sanitize_report_for_public
from src.finder.run import execute_find_skill
from src.infra.llm import ModelCallResult
from tests import smoke


class FinderTest(unittest.TestCase):
    """定向查找核心契约测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @smoke
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_basic_search_flow(self, mock_call, mock_search) -> None:
        """验证基本检索执行流程：生成规划后执行检索并正常停止与记录。"""
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "test",
                "queries": ["q1"],
                "criteria": [{"id": "c1", "kind": "required", "description": "d1"}],
            }),
            usage={"total_tokens": 100},
        )
        mock_search.return_value = (True, [], None)
        report = execute_find_skill("测试需求", root_dir=self.root, max_rounds=1)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["shortlist_count"], 0)
        self.assertEqual(mock_call.call_count, 1)

    def test_sanitize_report_for_public_removes_local_paths(self):
        """公开快照白名单脱敏机制必须彻底移除报告中的所有本地文件系统路径。"""
        raw_report = {
            "query": "test query",
            "status": "completed",
            "stop_reason": "target_reached",
            "report_paths": {
                "json": "C:/Users/Admin/AppData/Local/find-skill/report.json",
                "markdown": "C:/Users/Admin/AppData/Local/find-skill/report.md",
            },
            "candidates": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "evaluation": {"match": "strong"},
                    "local_cache_path": "D:/Code/cache/data.json",
                }
            ],
            "shortlist": [{"skill_id": "owner/repo:SKILL.md"}],
        }
        public_view = sanitize_report_for_public(raw_report)
        self.assertNotIn("report_paths", public_view)
        for cand in public_view.get("candidates", []):
            self.assertNotIn("local_cache_path", cand)


if __name__ == "__main__":
    unittest.main()
