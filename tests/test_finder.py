"""定向查找主流程核心单元测试：基本检索流程闭环与公开报告敏感信息脱敏。

保留 2 项核心行为：
1. 基本检索流程（规划、检索与候选处理闭环）。
2. 公开报告脱敏（剔除本地绝对文件路径与敏感字段）。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    @smoke
    def test_resumed_special_candidates_are_counted_once(self):
        """恢复旧队列后跳过专区候选，反复消费和大小写变体不重复计数。"""
        from src.finder.refill import run_rounds

        sid = "special/repo:SKILL.md"
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                queue = [{"skill_id": value, "owner": "special", "repo": "repo", "path": "SKILL.md"}
                         for value in (sid, "Special/Repo:SKILL.md")]
                if mixed:
                    queue.append({"skill_id": "normal/repo:SKILL.md", "owner": "normal", "repo": "repo", "path": "SKILL.md"})
                # 旧报告没有专区统计字段，且已记录其他类型的跳过事实。
                report = {"search": {"current_round": 1,
                    "rounds_history": [{"phase": "processing", "evaluation_start": 0}],
                    "candidate_queue": queue, "skipped": [{"skill_id": "owned/repo:SKILL.md", "code": "owned"}]},
                    "parameters": {"max_rounds": 1}, "evaluations": [], "evaluation_attempts": 0}
                snapshots = []
                state = SimpleNamespace(report=report, stop_reason=lambda: None,
                                        save=lambda: snapshots.append(deepcopy(report)))
                external = Mock(side_effect=AssertionError("unexpected network/model call"))

                def evaluate(state, batch, *args, **kwargs):
                    self.assertEqual([c.skill_id for c in batch], ["normal/repo:SKILL.md"])
                    state.report["search"]["processed_skill_ids"].extend(c.skill_id for c in batch)
                    return "candidates_exhausted"

                evaluate_mock = Mock(side_effect=evaluate)
                for _ in range(2):
                    report["search"]["rounds_history"][-1]["phase"] = "processing"
                    reason = run_rounds(state, {}, "", external, external, external, external,
                        evaluate_mock, set(), lambda _: None, lambda _: None, special_ids={sid})
                    self.assertEqual(reason, "round_limit")
                    search = snapshots[-1]["search"]
                    self.assertEqual(search["skipped_special"], 1)
                    self.assertEqual(search["skipped_special_ids"], [sid])
                    self.assertEqual([s for s in search["skipped"] if s["code"] == "special"],
                                     [{"skill_id": sid, "code": "special"}])
                    self.assertIn({"skill_id": "owned/repo:SKILL.md", "code": "owned"}, search["skipped"])
                    self.assertNotIn(sid, search["processed_skill_ids"])
                self.assertEqual(evaluate_mock.call_count, int(mixed))
                external.assert_not_called()
                self.assertEqual(report["search"]["candidate_queue"], queue)

    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_special_skills_skipped_in_finder(self, mock_call, mock_search, mock_expand) -> None:
        """验证定向查找跳过专区技能：专区候选直接记入 skipped_special 并忽略评估。"""
        from src.shared.models import Candidate
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "test",
                "queries": ["q1"],
                "criteria": [{"id": "c1", "kind": "required", "description": "d1"}],
            }),
            usage={"total_tokens": 100},
        )
        mock_search.return_value = (True, [{"owner": "special", "repo": "test-repo"}], None)
        mock_expand.return_value = (
            [
                Candidate(skill_id="special/test-repo:SKILL.md", owner="special", repo="test-repo", path="SKILL.md", url="https://github.com/special/test-repo", name="special-skill"),
                Candidate(skill_id="Special/Test-Repo:SKILL.md", owner="Special", repo="Test-Repo", path="SKILL.md", url="https://github.com/Special/Test-Repo", name="special-skill"),
            ],
            [{"ok": True}],
        )
        report = execute_find_skill(
            "测试需求",
            root_dir=self.root,
            max_rounds=1,
            special_ids={"special/test-repo:SKILL.md"},
        )
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["search"]["skipped_special"], 1)
        self.assertIn("special/test-repo:SKILL.md", report["search"]["skipped_special_ids"])
        self.assertEqual([s for s in report["search"]["skipped"] if s["code"] == "special"],
                         [{"skill_id": "special/test-repo:SKILL.md", "code": "special"}])
        self.assertEqual(len(report.get("evaluations", [])), 0)
        self.assertEqual(mock_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
