from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.finder.run import execute_find_skill
from src.infra.llm import ModelCallResult
from src.shared.models import Candidate

PLAN = {
    "intent": "emotional support",
    "queries": ["emotional support prompt", "cbt skill"],
    "criteria": [{"id": "support", "kind": "required", "description": "emotional support"}],
}


def mock_response(data, tokens=100):
    return ModelCallResult(
        ok=True,
        content=json.dumps(data),
        attempts=1,
        usage={"prompt_tokens": tokens - 20, "completion_tokens": 20, "total_tokens": tokens},
    )


def mock_evaluated(match="none", path="SKILL.md", tokens=100):
    return mock_response(
        {
            "match": match,
            "documentation": "clear",
            "summary_zh": "evaluation summary",
            "criteria_results": [
                {
                    "criterion_id": "support",
                    "status": "supported" if match == "strong" else "unsupported",
                    "evidence": (
                        [{"source_path": path, "start_line": 1, "end_line": 1, "quote": "emotional support"}]
                        if match == "strong"
                        else []
                    ),
                }
            ],
        },
        tokens,
    )


def make_repo(n, desc=""):
    return {
        "owner": "test-owner",
        "repo": f"r{n}",
        "url": f"https://github.com/test-owner/r{n}",
        "description": desc,
    }


def make_candidate(n, path="SKILL.md", desc=""):
    return Candidate(
        skill_id=f"test-owner/r{n}:{path}",
        owner="test-owner",
        repo=f"r{n}",
        path=path,
        name=f"r{n}",
        description=desc,
        url=f"https://github.com/test-owner/r{n}/blob/main/{path}",
        repo_url=f"https://github.com/test-owner/r{n}",
    )


class TestFinderBatchRefill(unittest.TestCase):
    """测试 Finder 有界批次调度、待展开仓库间隙穿插与主动反思机制 (阶段3)。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_find(self, model_side_effect, search_fn, expand_fn, **kwargs):
        call_model = (
            Mock(side_effect=model_side_effect)
            if isinstance(model_side_effect, (list, tuple))
            else model_side_effect
        )
        args = dict(
            root_dir=self.root,
            model_cfg={"endpoint": "https://fake", "model": "fake", "auth": {"api_key": "fake"}},
            call_model_fn=call_model,
            search_github_repos_fn=search_fn,
            expand_and_collect_candidates_fn=expand_fn,
            fetch_candidate_materials_fn=Mock(side_effect=lambda c, **kw: (True, {c.path: "emotional support"}, None)),
            max_clarification_turns=0,
            limit=1,
            log=lambda *a: None,
            sleep=lambda *a: None,
            enable_active_reflection=kwargs.pop("enable_active_reflection", False),
        )
        args.update(kwargs)
        return execute_find_skill("emotional support", **args)

    def test_interleaved_pending_repo_expansion_between_batches(self):
        """测试在首批评估（12个）间隙，系统自动穿插展开 pending 队列中的高相关仓库。"""
        # 初始搜索发现 20 个仓库
        repos = [make_repo(i, desc="generic tool" if i != 15 else "cbt emotional support companion") for i in range(20)]
        search_fn = Mock(return_value=(True, repos, None))
        expand_fn = Mock(side_effect=lambda repo_list, **kw: ([make_candidate(int(r["repo"][1:])) for r in repo_list], []))

        # 批次 1 处理 12 个候选，间隙展开更多 pending 仓库，批次 2 命中 strong
        model_calls = [mock_response(PLAN)] + [mock_evaluated("none") for _ in range(12)] + [mock_evaluated("strong")]

        r = self.run_find(
            model_calls,
            search_fn,
            expand_fn,
            limit=1,
            max_rounds=1,
        )

        self.assertEqual(r["stop_reason"], "target_reached")
        history = r["search"]["rounds_history"]
        self.assertTrue(len(history) >= 1)
        self.assertGreaterEqual(history[0].get("batch_index", 0), 1)

    def test_active_reflection_triggers_on_two_consecutive_zero_batches(self):
        """测试连续 2 批（共 24 次尝试）均为 none 时，若开启 active_reflection，主动触发反思拓词。"""
        def search_fn(query, **kw):
            if "wellness" in query or "companion" in query:
                return True, [make_repo(99, desc="newly reflected cbt")], None
            return True, [make_repo(i) for i in range(30)], None

        expand_fn = Mock(side_effect=lambda repo_list, **kw: ([make_candidate(int(r["repo"][1:])) for r in repo_list], []))

        reflection_result = {"queries": ["cbt wellness companion", "empathy listener skill", "mindfulness companion"]}

        def model_call(cfg, system, user, **kw):
            if "prompt generator" in system or "规划" in system:
                return mock_response(PLAN)
            if "新短语1" in system or "检索词" in system or "previous_queries" in user:
                return mock_response(reflection_result)
            if "r99" in user:
                return mock_evaluated("strong")
            return mock_evaluated("none")

        r = self.run_find(
            model_call,
            search_fn,
            expand_fn,
            enable_active_reflection=True,
            limit=1,
            max_rounds=2,
        )

        self.assertEqual(r["stop_reason"], "target_reached")
        history = r["search"]["rounds_history"]
        self.assertEqual(history[0].get("active_reflections", 0), 1)

    def test_active_reflection_disabled_preserves_original_continuation(self):
        """测试关闭 active_reflection 时（默认），即使连续 2 批为 none 也不触发中间反思，继续评估既有候选。"""
        repos = [make_repo(i) for i in range(26)]
        search_fn = Mock(return_value=(True, repos, None))
        expand_fn = Mock(side_effect=lambda repo_list, **kw: ([make_candidate(int(r["repo"][1:])) for r in repo_list], []))

        model_calls = (
            [mock_response(PLAN)]
            + [mock_evaluated("none") for _ in range(24)]
            + [mock_evaluated("strong")]
        )

        r = self.run_find(
            model_calls,
            search_fn,
            expand_fn,
            enable_active_reflection=False,
            limit=1,
            max_rounds=1,
        )

        self.assertEqual(r["stop_reason"], "target_reached")
        history = r["search"]["rounds_history"]
        self.assertEqual(history[0].get("active_reflections", 0), 0)
        self.assertEqual(r["evaluated_count"], 25)

    def test_resume_preserves_batch_metrics(self):
        """测试中断后断点续跑能完整恢复 batch_index 与 consecutive_zero_batches 指标。"""
        repos = [make_repo(i) for i in range(15)]
        search_fn = Mock(return_value=(True, repos, None))
        expand_fn = Mock(side_effect=lambda repo_list, **kw: ([make_candidate(int(r["repo"][1:])) for r in repo_list], []))

        model_calls = [mock_response(PLAN)] + [mock_evaluated("none") for _ in range(12)]

        r1 = self.run_find(
            model_calls,
            search_fn,
            expand_fn,
            max_evaluations=12,
            limit=1,
        )
        self.assertEqual(r1["stop_reason"], "evaluation_limit")
        history1 = r1["search"]["rounds_history"][0]
        self.assertEqual(history1.get("batch_index"), 1)
        self.assertEqual(history1.get("consecutive_zero_batches"), 1)

        resume_dir = Path(r1["report_paths"]["json"]).parent
        model_calls_resume = [mock_evaluated("strong")]
        r2 = execute_find_skill(
            "emotional support",
            root_dir=self.root,
            resume_dir=resume_dir,
            model_cfg={"endpoint": "https://fake", "model": "fake", "auth": {"api_key": "fake"}},
            call_model_fn=Mock(side_effect=model_calls_resume),
            search_github_repos_fn=search_fn,
            expand_and_collect_candidates_fn=expand_fn,
            fetch_candidate_materials_fn=Mock(side_effect=lambda c, **kw: (True, {c.path: "emotional support"}, None)),
            max_evaluations=20,
            limit=1,
            log=lambda *a: None,
            sleep=lambda *a: None,
        )
        self.assertEqual(r2["stop_reason"], "target_reached")
        history2 = r2["search"]["rounds_history"][0]
        self.assertGreaterEqual(history2.get("batch_index"), 2)


if __name__ == "__main__":
    unittest.main()
