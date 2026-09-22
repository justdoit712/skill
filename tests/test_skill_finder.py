"""测试定向查找调度层：用量熔断 (T01)、名额记账 (T02)、中断保留短名单 (T04)、伪材料拦截 (T05)、轮转调度 (T06)、参数防御 (T07) 与 A7 两阶段指纹保护。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from src.dedupe import candidate_from_repo, content_fingerprint
from src.evaluate import ModelCallResult
from src.models import Candidate
from src.pipeline import phase_evaluate
from src.skill_finder import (
    DEFAULT_LIMIT,
    STATUS_EVALUATION_LIMIT,
    STATUS_INTERRUPTED,
    STATUS_STOPPED,
    STATUS_USAGE_UNKNOWN,
    _interleave_paths,
    _parse_int_val,
    _round_robin_merge_repos,
    execute_find_skill,
    fetch_candidate_materials,
    main,
)


class TestT01UsageUnknown(unittest.TestCase):
    """T01: 未知用量零容忍熔断测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.skill_finder.call_model")
    def test_usage_unknown_in_planning_stops_immediately(self, mock_call) -> None:
        """规划阶段缺失 usage，立即熔断停机，后续不发生任何模型调用，状态为 usage_unknown。"""
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage=None,
        )

        report = execute_find_skill("测试需求", root_dir=self.root)
        self.assertEqual(report["status"], STATUS_STOPPED)
        self.assertEqual(report["stop_reason"], STATUS_USAGE_UNKNOWN)
        self.assertEqual(mock_call.call_count, 1)

    @patch("src.skill_finder.fetch_candidate_materials")
    @patch("src.skill_finder.expand_and_collect_candidates")
    @patch("src.skill_finder.search_github_repos_for_query")
    @patch("src.skill_finder.call_model")
    def test_usage_unknown_in_candidate_evaluation_stops_immediately(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """候选评估阶段缺失 usage，保留已有进度立即熔断停机，状态为 usage_unknown。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        eval_unknown = ModelCallResult(
            ok=True,
            content=json.dumps({"match": "strong", "criteria_results": []}),
            usage=None,
        )
        mock_call.side_effect = [plan_res, eval_unknown]

        mock_search.return_value = (True, [{"owner": "o1", "repo": "r1", "url": "u1", "description": "d1"}], None)
        fake_cand = candidate_from_repo(
            owner="o1",
            repo="r1",
            path="skills/test/SKILL.md",
            url="https://github.com/o1/r1/blob/HEAD/skills/test/SKILL.md",
            repo_url="https://github.com/o1/r1",
            name="test",
            description="",
            discovered_at="2026-01-01T00:00:00Z",
        )
        mock_expand.return_value = ([fake_cand], [])
        mock_fetch.return_value = (True, {"skills/test/SKILL.md": "some text"}, None)

        report = execute_find_skill("测试需求", root_dir=self.root)
        self.assertEqual(report["status"], STATUS_STOPPED)
        self.assertEqual(report["stop_reason"], STATUS_USAGE_UNKNOWN)
        self.assertEqual(mock_call.call_count, 2)


class TestT02AccountingAndAttempts(unittest.TestCase):
    """T02: 名额记账与 evaluation_attempts 独立性测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.skill_finder.fetch_candidate_materials")
    @patch("src.skill_finder.expand_and_collect_candidates")
    @patch("src.skill_finder.search_github_repos_for_query")
    @patch("src.skill_finder.call_model")
    def test_failed_call_consumes_evaluation_attempt(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """max_evaluations=2 时，第 1 次返回无效 JSON，第 2 次成功，总 evaluation_attempts 正好为 2 次，evaluated_count 为 1。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        eval_fail = ModelCallResult(
            ok=True,
            content="NOT A VALID JSON",
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        )
        eval_succ = ModelCallResult(
            ok=True,
            content=json.dumps({
                "match": "strong",
                "summary_zh": "成功简述",
                "documentation": "clear",
                "criteria_results": [
                    {
                        "criterion_id": "c1",
                        "status": "supported",
                        "evidence": [
                            {"source_path": "skills/cand2/SKILL.md", "start_line": 1, "end_line": 1, "quote": "Valid text line"}
                        ],
                    }
                ],
            }),
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        )
        mock_call.side_effect = [plan_res, eval_fail, eval_succ]

        mock_search.return_value = (True, [{"owner": "o1", "repo": "r1", "url": "u1", "description": "d1"}], None)

        cand1 = candidate_from_repo(owner="o1", repo="r1", path="skills/cand1/SKILL.md", url="u1", repo_url="ru1", name="c1", description="", discovered_at="2026-01-01T00:00:00Z")
        cand2 = candidate_from_repo(owner="o1", repo="r1", path="skills/cand2/SKILL.md", url="u2", repo_url="ru1", name="c2", description="", discovered_at="2026-01-01T00:00:00Z")
        cand3 = candidate_from_repo(owner="o1", repo="r1", path="skills/cand3/SKILL.md", url="u3", repo_url="ru1", name="c3", description="", discovered_at="2026-01-01T00:00:00Z")
        mock_expand.return_value = ([cand1, cand2, cand3], [])

        def fake_fetch(c, sleep=None):
            return True, {c.path: "Valid text line\n"}, None

        mock_fetch.side_effect = fake_fetch

        report = execute_find_skill("测试需求", limit=1, max_evaluations=2, root_dir=self.root)
        self.assertEqual(report["stop_reason"], STATUS_EVALUATION_LIMIT)
        self.assertEqual(report["evaluation_attempts"], 2)
        self.assertEqual(report["evaluated_count"], 1)
        self.assertEqual(mock_call.call_count, 3)


class TestT04InterruptAndErrorPreservation(unittest.TestCase):
    """T04: 中断或异常时统一收尾、保留短名单且 rank_find_results 传入 plan。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.skill_finder.fetch_candidate_materials")
    @patch("src.skill_finder.expand_and_collect_candidates")
    @patch("src.skill_finder.search_github_repos_for_query")
    @patch("src.skill_finder.call_model")
    def test_interrupt_preserves_shortlist_and_passes_plan(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """评估 1 条成功后发生 KeyboardInterrupt，报告中包含已评估条目，状态为 interrupted。"""
        plan_res = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        eval_succ = ModelCallResult(
            ok=True,
            content=json.dumps({
                "match": "strong",
                "summary_zh": "成功评估",
                "documentation": "clear",
                "criteria_results": [
                    {
                        "criterion_id": "c1",
                        "status": "supported",
                        "evidence": [{"source_path": "skills/c1/SKILL.md", "start_line": 1, "end_line": 1, "quote": "Valid text"}],
                    }
                ],
            }),
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        )

        def mock_call_fn(*args, **kwargs):
            if mock_call_fn.count == 0:
                mock_call_fn.count += 1
                return plan_res
            elif mock_call_fn.count == 1:
                mock_call_fn.count += 1
                return eval_succ
            else:
                raise KeyboardInterrupt()

        mock_call_fn.count = 0
        mock_call.side_effect = mock_call_fn

        mock_search.return_value = (True, [{"owner": "o1", "repo": "r1", "url": "u1", "description": "d1"}], None)
        c1 = candidate_from_repo(owner="o1", repo="r1", path="skills/c1/SKILL.md", url="u1", repo_url="ru1", name="c1", description="", discovered_at="2026-01-01T00:00:00Z")
        c2 = candidate_from_repo(owner="o1", repo="r1", path="skills/c2/SKILL.md", url="u2", repo_url="ru1", name="c2", description="", discovered_at="2026-01-01T00:00:00Z")
        mock_expand.return_value = ([c1, c2], [])
        mock_fetch.return_value = (True, {"skills/c1/SKILL.md": "Valid text\n", "skills/c2/SKILL.md": "Valid text\n"}, None)

        report = execute_find_skill("测试需求", root_dir=self.root)
        self.assertEqual(report["status"], STATUS_INTERRUPTED)
        self.assertEqual(report["stop_reason"], STATUS_INTERRUPTED)
        self.assertEqual(len(report["shortlist"]), 1)
        self.assertEqual(report["shortlist"][0]["candidate"]["name"], "c1")


class TestT05RejectNotSkillAndHtml(unittest.TestCase):
    """T05: 拒绝非 SKILL.md 文件与 HTML 错误页材料。"""

    def test_reject_non_skill_md_basename(self) -> None:
        cand = candidate_from_repo(
            owner="o",
            repo="r",
            path="skills/foo/NOT_SKILL.md",
            url="https://github.com/o/r/blob/HEAD/skills/foo/NOT_SKILL.md",
            repo_url="https://github.com/o/r",
            name="foo",
            description="",
            discovered_at="2026-01-01T00:00:00Z",
        )
        ok, mats, err = fetch_candidate_materials(cand)
        self.assertFalse(ok)
        self.assertEqual(err, "NOT_A_SKILL_MD")

    def test_reject_html_content(self) -> None:
        cand = candidate_from_repo(
            owner="o",
            repo="r",
            path="skills/foo/SKILL.md",
            url="https://github.com/o/r/blob/HEAD/skills/foo/SKILL.md",
            repo_url="https://github.com/o/r",
            name="foo",
            description="",
            discovered_at="2026-01-01T00:00:00Z",
        )
        fake_fetch_fn = Mock()
        fake_fetch_fn.return_value = Mock(
            ok=True,
            text="<!DOCTYPE html><html><head><title>Sign in</title></head><body><form></form></body></html>",
            truncated=False,
            reason_code=None,
        )
        ok, mats, err = fetch_candidate_materials(cand, fetch_fn=fake_fetch_fn)
        self.assertFalse(ok)
        self.assertEqual(err, "HTML_CONTENT_REJECTED")


class TestT06RoundRobinAndInterleaving(unittest.TestCase):
    """T06: 多查询轮转去重与合集路径交织提取测试。"""

    def test_round_robin_merge_repos(self) -> None:
        q1_repos = [
            {"owner": "o1", "repo": "r1"},
            {"owner": "o1", "repo": "r2"},
            {"owner": "o1", "repo": "r3"},
        ]
        q2_repos = [
            {"owner": "o2", "repo": "r1"},
            {"owner": "o1", "repo": "r1"},  # 与 q1 重复
            {"owner": "o2", "repo": "r2"},
        ]
        q3_repos = [
            {"owner": "o3", "repo": "r1"},
        ]

        merged = _round_robin_merge_repos([q1_repos, q2_repos, q3_repos], max_repos=10)
        repo_names = [f"{r['owner']}/{r['repo']}" for r in merged]
        # 轮转交织：depth 0 (o1/r1, o2/r1, o3/r1) -> depth 1 (o1/r2, q2重复跳过) -> depth 2 (o1/r3, o2/r2)
        self.assertEqual(
            repo_names,
            ["o1/r1", "o2/r1", "o3/r1", "o1/r2", "o1/r3", "o2/r2"],
        )

    def test_interleave_paths_two_related_one_generic(self) -> None:
        related = [f"skills/prompt_{i}/SKILL.md" for i in range(1, 6)]
        generic = [f"skills/generic_{i}/SKILL.md" for i in range(1, 6)]

        interleaved = _interleave_paths(related, generic, max_count=6)
        expected = [
            "skills/prompt_1/SKILL.md",
            "skills/prompt_2/SKILL.md",
            "skills/generic_1/SKILL.md",
            "skills/prompt_3/SKILL.md",
            "skills/prompt_4/SKILL.md",
            "skills/generic_2/SKILL.md",
        ]
        self.assertEqual(interleaved, expected)


class TestT07ConfigValidationAndExitCode(unittest.TestCase):
    """T07: 参数防御性校验与退出码测试。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "find-skill.json").write_text(json.dumps({}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_int_val_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            _parse_int_val("20m", default=10, field_name="test")
        with self.assertRaises(ValueError):
            _parse_int_val("oops", default=10, field_name="test")
        with self.assertRaises(ValueError):
            _parse_int_val(True, default=10, field_name="test")
        with self.assertRaises(ValueError):
            _parse_int_val(-5, default=10, field_name="test")

    def test_parse_int_val_supports_delimiters(self) -> None:
        self.assertEqual(_parse_int_val("20 000 000", default=10), 20000000)
        self.assertEqual(_parse_int_val("20,000,000", default=10), 20000000)
        self.assertEqual(_parse_int_val("20_000_000", default=10), 20000000)

    def test_invalid_param_does_not_create_run_dir(self) -> None:
        data_dir = self.root / "data" / "local" / "find-skills"
        with self.assertRaises(ValueError):
            execute_find_skill("测试需求", limit=-1, root_dir=self.root)
        self.assertFalse(data_dir.exists())

    def test_cli_main_exit_code_2_on_invalid_argument(self) -> None:
        code = main(["测试需求", "--limit", "20m"], root=self.root)
        self.assertEqual(code, 2)


from tests.test_pipeline import ROOT, PipelineHarness, fake_evaluate, fake_fetch, one_candidate


class TestA7TwoPhaseFingerprintProtection(PipelineHarness):
    """A7: 两阶段恢复抓取时内容指纹核验保护。"""

    def test_two_phase_fingerprint_mismatch_skips_and_does_not_bind(self) -> None:
        """阶段二恢复执行时，若内容指纹发生变化，跳过该条评估并标为 pending，禁止将新材料强行绑定旧指纹。"""
        old_text = "---\nname: widget\ndescription: 一个通用小工具\n---\n" + "旧内容行\n" * 40
        new_text = "---\nname: widget\ndescription: 一个通用小工具\n---\n" + "新内容行被更新\n" * 40
        cand = one_candidate("widget")

        # 阶段一：预留阶段，记录初始文本及其指纹
        self.reserve([cand], text=old_text)

        # 移除阶段一本地暂存的 staged 文本，强制阶段二发起重新抓取
        staged_path = self.state / "texts" / "staged.json"
        if staged_path.exists():
            staged_path.unlink()

        eval_fn = fake_evaluate()
        # 阶段二：重新抓取返回了变更后的 new_text
        res = phase_evaluate(
            config_dir=ROOT / "config",
            data_dir=self.data,
            public_dir=self.public,
            state_dir=self.state,
            evaluate_fn=eval_fn,
            fetch_fn=fake_fetch(text=new_text),
        )

        # 校验：指纹变化条目被跳过，绝未发起模型评估调用
        self.assertEqual(res.get("skipped"), 1)
        self.assertEqual(len(eval_fn.calls), 0)

        # 校验 queue.json 中条目状态被置为 pending 且 content_changed 标记为 True
        queue = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        item = queue["pending"][0]
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item.get("content_changed"))


if __name__ == "__main__":
    unittest.main()
