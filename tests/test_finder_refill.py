"""多轮检索与补仓核心单元测试：达标停止、预算控制、未知计费保护、断点恢复不重复扣费与有限重试。

保留 5 项核心行为：
1. 达标停止（满足目标后立即停止派发）。
2. 预算停止（规划或评估耗尽预算后阻止新检索）。
3. 计费未知停止（LLM 未返回有效 token 用量时立即中止并保护资金）。
4. 恢复不重复付费（断点恢复已完成的评估，严禁重复扣费重评）。
5. 有限重试（HTTP 故障按退避策略重试达到上限后继续推进）。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.finder.run import execute_find_skill
from src.infra.llm import ModelCallResult
from src.shared.models import Candidate
from tests import smoke

PLAN = {
    "intent": "organize",
    "queries": ["organize"],
    "criteria": [{"id": "organize", "kind": "required", "description": "organize"}],
}


def response(data, tokens=100):
    return ModelCallResult(
        ok=True,
        content=json.dumps(data),
        attempts=1,
        usage={"prompt_tokens": tokens - 20, "completion_tokens": 20, "total_tokens": tokens},
    )


def evaluated(match="none", path="SKILL.md", documentation="clear", tokens=100, criterion_id="organize", quote="organize"):
    return response(
        {
            "match": match,
            "documentation": documentation,
            "summary_zh": "organize",
            "criteria_results": [
                {
                    "criterion_id": criterion_id,
                    "status": "supported" if match == "strong" else "unsupported",
                    "evidence": [{"source_path": path, "start_line": 1, "end_line": 1, "quote": quote}]
                    if match == "strong"
                    else [],
                }
            ],
        },
        tokens,
    )


def repo(n):
    return {"owner": "owner", "repo": f"r{n}", "url": f"https://github.com/owner/r{n}", "description": ""}


def candidate(n, path="SKILL.md", owner="owner", desc=""):
    return Candidate(
        skill_id=f"{owner}/r{n}:{path}",
        owner=owner,
        repo=f"r{n}",
        path=path,
        name=f"r{n}",
        description=desc,
        url=f"https://github.com/{owner}/r{n}/blob/HEAD/{path}",
        repo_url=f"https://github.com/{owner}/r{n}",
    )


class FinderRefillTest(unittest.TestCase):
    """多轮检索补仓、预算高压线与恢复契约测试。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = Mock(side_effect=[response(PLAN), evaluated("strong")])
        self.search = Mock(return_value=(True, [repo(0)], None))
        self.expand = Mock(side_effect=lambda repos, **kw: ([candidate(int(r["repo"][1:])) for r in repos], []))
        self.fetch = Mock(side_effect=lambda c, **kw: (True, {c.path: "organize"}, None))

    def run_find(self, **kwargs):
        args = dict(
            root_dir=self.root,
            model_cfg={"endpoint": "https://fake", "model": "fake", "auth": {"api_key": "fake"}},
            call_model_fn=self.model,
            search_github_repos_fn=self.search,
            expand_and_collect_candidates_fn=self.expand,
            fetch_candidate_materials_fn=self.fetch,
            max_clarification_turns=0,
            limit=1,
            log=lambda *a: None,
            sleep=lambda *a: None,
        )
        args.update(kwargs)
        return execute_find_skill("organize", **args)

    def resume(self, report, **kwargs):
        return self.run_find(resume_dir=Path(report["report_paths"]["json"]).parent, **kwargs)

    @smoke
    def test_target_stops_before_next_fetch_and_wins_known_budget_tie(self):
        """达到推荐目标数量时立即停止，不再发起后续抓取或模型评估。"""
        self.search.return_value = True, [repo(0), repo(1)], None
        self.model.side_effect = [response(PLAN, 900), evaluated("strong", tokens=100)]
        with patch("src.finder.session.estimate_request_token_bound", side_effect=[900, 100]):
            r = self.run_find(max_tokens=1000, max_evaluations=1)
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(r["evaluation_attempts"], 1)

    def test_budget_after_planning_prevents_search(self):
        """规划阶段已耗尽 Token 预算时，严格阻断并禁止发起 GitHub 检索。"""
        self.model.side_effect = [response(PLAN, 1000)]
        with patch("src.finder.session.estimate_request_token_bound", return_value=200):
            r = self.run_find(max_tokens=1000)
        self.assertEqual(r["stop_reason"], "token_limit")
        self.search.assert_not_called()

    def test_unknown_usage_beats_target_and_never_calls_next_candidate(self):
        """模型返回未知 Token 用量时，立即以 usage_unknown 终止，绝不评估下一个候选。"""
        strong = evaluated("strong")
        strong.usage = {}
        self.model.side_effect = [response(PLAN), strong]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "usage_unknown")
        self.assertEqual(r["shortlist_count"], 1)

    def test_received_evaluation_is_recovered_without_refetch_or_new_paid_call(self):
        """中断恢复时已就绪的评估结果直接复用，不重复发起网络抓取或模型付费调用。"""
        with patch("src.finder.candidates._record_evaluation", side_effect=KeyboardInterrupt):
            r = self.run_find(max_evaluations=1)
        self.assertEqual(r["stop_reason"], "interrupted")
        self.assertIn("pending_evaluation", r)
        self.model.reset_mock()
        self.fetch.reset_mock()
        self.search.reset_mock()
        resumed = self.resume(r, max_evaluations=1)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.assertEqual(resumed["evaluation_attempts"], 1)
        self.assertEqual(resumed["usage"]["total_tokens"], 200)
        self.model.assert_not_called()
        self.fetch.assert_not_called()
        self.search.assert_not_called()

    def test_failed_page_retries_with_backoff_then_advances_once(self):
        """检索分页失败在上限内以退避方式重试，成功后页码正常递增。"""
        self.search.side_effect = [
            (False, [], "HTTP 503"),
            (False, [], "HTTP 503"),
            (True, [repo(0)], None),
        ]
        sleep = Mock()
        with patch("src.finder.refill.time.time", return_value=100):
            r = self.run_find(sleep=sleep)
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual([c.kwargs["page"] for c in self.search.call_args_list], [1, 1, 1])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])
        self.assertEqual(r["search"]["query_cursors"]["organize"]["next_page"], 2)


if __name__ == "__main__":
    unittest.main()
