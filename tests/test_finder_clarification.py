"""测试定向查找阶段一：多轮人机交互澄清轮询（Interactive Clarification Loop）。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.finder.config import (
    CLARIFICATION_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_CLARIFICATION_TURNS,
)
from src.finder.plan import (
    build_clarification_question_prompt,
    build_interactive_plan_prompt,
    build_plan_prompt,
    parse_clarification_question,
    parse_query_plan,
)
from src.finder.run import (
    FinderRunState,
    STATUS_COMPLETED,
    _run_planning_phase,
    execute_find_skill,
    main,
)
from src.infra.llm import ModelCallResult


class TestClarificationPromptsAndParsing(unittest.TestCase):
    """测试多轮澄清 Prompt 构造与解析。"""

    def test_build_clarification_prompt_turn_1(self) -> None:
        system, user = build_clarification_question_prompt("生成高质量 Prompt", [], turn=1, max_turns=3)
        self.assertIn("生成 1 个最关键的定向提问", system)
        self.assertIn("第 1 轮侧重", system)
        self.assertIn("生成高质量 Prompt", user)
        self.assertIn("首轮澄清沟通", user)

    def test_build_clarification_prompt_turn_2_with_history(self) -> None:
        history = [
            {
                "turn": "1",
                "focus": "应用场景",
                "question": "主要用于什么场景？",
                "answer": "Python 代码重构",
            }
        ]
        system, user = build_clarification_question_prompt("生成高质量 Prompt", history, turn=2, max_turns=3)
        self.assertIn("第 2/3 轮", system)
        self.assertIn("Python 代码重构", user)
        self.assertIn("主要用于什么场景？", user)

    def test_parse_clarification_question_valid_json(self) -> None:
        sample_json = json.dumps(
            {
                "focus": "技术栈确认",
                "question": "是否需要支持特定语言？",
                "options": ["仅 Python", "TypeScript 与 React", "通用无限制"],
                "summary": "已确认用于代码场景",
            }
        )
        res = parse_clarification_question(sample_json)
        self.assertEqual(res["focus"], "技术栈确认")
        self.assertEqual(res["question"], "是否需要支持特定语言？")
        self.assertEqual(len(res["options"]), 3)
        self.assertEqual(res["options"][0], "仅 Python")

    def test_parse_clarification_question_markdown_fence(self) -> None:
        sample_fenced = "```json\n" + json.dumps({
            "focus": "场景细化",
            "question": "请问是个人还是团队？",
            "options": ["个人", "团队"],
        }) + "\n```"
        res = parse_clarification_question(sample_fenced)
        self.assertEqual(res["question"], "请问是个人还是团队？")
        self.assertEqual(res["options"], ["个人", "团队"])

    def test_parse_clarification_question_fallback_plain_text(self) -> None:
        raw_text = "请问具体需要针对哪个版本的框架？"
        res = parse_clarification_question(raw_text)
        self.assertEqual(res["question"], raw_text)
        self.assertEqual(res["options"], [])

    def test_build_interactive_plan_prompt_with_history(self) -> None:
        history = [
            {"turn": "1", "question": "用于什么场景？", "answer": "代码优化与重构"},
            {"turn": "2", "question": "是否需要自动化评估？", "answer": "需要包含基准测试指标"},
        ]
        system, user = build_interactive_plan_prompt("提示词工程", history)
        self.assertIn("多轮交互澄清记录", system)
        self.assertIn("用于什么场景？", user)
        self.assertIn("代码优化与重构", user)
        self.assertIn("需要包含基准测试指标", user)


class TestInteractivePlanningExecution(unittest.TestCase):
    """测试阶段一多轮交互澄清问答执行流程。"""

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

    def test_full_3_turns_interactive_clarification(self) -> None:
        """测试完整的 3 轮人机问答澄清流，最终收敛为包含澄清历史的 QueryPlan。"""
        q1_json = json.dumps({
            "focus": "场景细化",
            "question": "应用场景？",
            "options": ["代码生成", "日常写作"],
        })
        q2_json = json.dumps({
            "focus": "约束澄清",
            "question": "语言要求？",
            "options": ["Python", "TypeScript"],
        })
        q3_json = json.dumps({
            "focus": "功能偏好",
            "question": "是否需要评测工具？",
            "options": ["需要", "不需要"],
        })
        final_plan_json = json.dumps({
            "intent": "针对 Python 代码生成且包含评测的高质量提示词工具",
            "queries": ["python prompt generator", "prompt optimizer", "代码提示词"],
            "criteria": [
                {"id": "code_prompt", "kind": "required", "description": "支持根据代码需求生成提示词"},
                {"id": "benchmark_eval", "kind": "quality_signal", "description": "包含评测与指标说明"},
            ],
        })

        mock_call = MagicMock()
        mock_call.side_effect = [
            ModelCallResult(ok=True, content=q1_json, usage={"total_tokens": 100}),
            ModelCallResult(ok=True, content=q2_json, usage={"total_tokens": 120}),
            ModelCallResult(ok=True, content=q3_json, usage={"total_tokens": 110}),
            ModelCallResult(ok=True, content=final_plan_json, usage={"total_tokens": 200}),
        ]

        # 模拟用户在各轮的回答：第 1 轮选 "1" (代码生成)，第 2 轮输入 "Python"，第 3 轮选 "1" (需要)
        user_inputs = iter(["1", "Python", "1"])
        mock_input = lambda prompt="": next(user_inputs)

        run_dir = self.root / "run_test"
        run_dir.mkdir(parents=True)
        state = FinderRunState("生成高质量 Prompt", {"limit": 5, "max_evaluations": 20, "max_tokens": 200000}, run_dir)

        plan, error = _run_planning_phase(
            state,
            "生成高质量 Prompt",
            cfg={"endpoint": "https://fake", "model": "fake-model"},
            api_key="fake-key",
            transport=mock_call,
            sleep=lambda s: None,
            max_turns=3,
            input_fn=mock_input,
            log=lambda *a, **k: None,
        )

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["clarification_turns"], 3)
        self.assertEqual(len(plan["clarification_history"]), 3)
        self.assertEqual(plan["clarification_history"][0]["answer"], "代码生成")
        self.assertEqual(plan["clarification_history"][1]["answer"], "Python")
        self.assertEqual(plan["clarification_history"][2]["answer"], "需要")
        self.assertEqual(mock_call.call_count, 4)
        self.assertEqual(state.usage.total_tokens, 530)

    def test_early_exit_on_empty_input(self) -> None:
        """测试在第 1 轮用户直接按回车（空输入），立即提前早退并生成最终规划。"""
        q1_json = json.dumps({
            "focus": "场景细化",
            "question": "应用场景？",
            "options": ["代码生成", "日常写作"],
        })
        final_plan_json = json.dumps({
            "intent": "通用高质量提示词优化技能",
            "queries": ["prompt generator", "prompt optimizer"],
            "criteria": [
                {"id": "prompt_gen", "kind": "required", "description": "支持生成提示词"},
            ],
        })

        mock_call = MagicMock()
        mock_call.side_effect = [
            ModelCallResult(ok=True, content=q1_json, usage={"total_tokens": 100}),
            ModelCallResult(ok=True, content=final_plan_json, usage={"total_tokens": 180}),
        ]

        user_inputs = iter([""])  # 用户直接回车跳过
        mock_input = lambda prompt="": next(user_inputs)

        run_dir = self.root / "run_test_skip"
        run_dir.mkdir(parents=True)
        state = FinderRunState("提示词优化", {"limit": 5, "max_evaluations": 20, "max_tokens": 200000}, run_dir)

        plan, error = _run_planning_phase(
            state,
            "提示词优化",
            cfg={"endpoint": "https://fake", "model": "fake-model"},
            api_key="fake-key",
            transport=mock_call,
            sleep=lambda s: None,
            max_turns=3,
            input_fn=mock_input,
            log=lambda *a, **k: None,
        )

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        # 澄清未输入有效答案，不计入 history
        self.assertEqual(plan.get("clarification_turns", 0), 0)
        self.assertEqual(mock_call.call_count, 2)  # 1 次提问 + 1 次最终规划

    def test_zero_turns_direct_planning(self) -> None:
        """测试 max_turns=0 时的直接规划：仅发生 1 次规划调用，无澄清问答。"""
        final_plan_json = json.dumps({
            "intent": "快速规划测试",
            "queries": ["prompt generator"],
            "criteria": [{"id": "prompt_gen", "kind": "required", "description": "生成提示词"}],
        })

        mock_call = MagicMock()
        mock_call.return_value = ModelCallResult(ok=True, content=final_plan_json, usage={"total_tokens": 150})

        run_dir = self.root / "run_test_no_interactive"
        run_dir.mkdir(parents=True)
        state = FinderRunState("快速需求", {"limit": 5, "max_evaluations": 20, "max_tokens": 200000}, run_dir)

        plan, error = _run_planning_phase(
            state,
            "快速需求",
            cfg={"endpoint": "https://fake", "model": "fake-model"},
            api_key="fake-key",
            transport=mock_call,
            sleep=lambda s: None,
            max_turns=0,
            log=lambda *a, **k: None,
        )

        self.assertIsNone(error)
        self.assertIsNotNone(plan)
        self.assertNotIn("clarification_history", plan)
        self.assertEqual(mock_call.call_count, 1)

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_execute_find_skill_with_interactive_flag(
        self, mock_call, mock_search, mock_expand, mock_fetch
    ) -> None:
        """集成测试：execute_find_skill 支持 max_clarification_turns 与 input_fn 参数。"""
        q1_json = json.dumps({
            "focus": "场景",
            "question": "应用场景？",
            "options": ["代码开发"],
        })
        final_plan_json = json.dumps({
            "intent": "代码开发提示词技能",
            "queries": ["code prompt"],
            "criteria": [{"id": "code_prompt", "kind": "required", "description": "生成提示词"}],
        })

        mock_call.side_effect = [
            ModelCallResult(ok=True, content=q1_json, usage={"total_tokens": 80}),
            ModelCallResult(ok=True, content=final_plan_json, usage={"total_tokens": 150}),
        ]
        mock_search.return_value = (True, [], None)
        mock_expand.return_value = ([], [])

        user_inputs = iter(["skip"])
        mock_input = lambda prompt="": next(user_inputs)

        report = execute_find_skill(
            "测试需求",
            root_dir=self.root,
            max_clarification_turns=2,
            input_fn=mock_input,
            log=lambda *a, **k: None,
        )

        self.assertEqual(report["status"], STATUS_COMPLETED)
        self.assertEqual(report["plan"]["intent"], "代码开发提示词技能")
        self.assertEqual(mock_call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
