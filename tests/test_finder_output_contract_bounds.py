from __future__ import annotations

import json
import unittest

from src.finder.evaluation import build_evaluation_prompt, parse_skill_evaluation
from src.shared.models import Candidate
from src.shared.output_contracts import FINDER_EVALUATION_CONTRACT

PLAN_CRITERIA = [
    {"id": "support", "kind": "required", "description": "emotional support"},
]


class TestFinderOutputContractBounds(unittest.TestCase):
    """测试阶段 4：输出契约定义、Prompt 精简指令与解析器优雅降级。"""

    def test_output_contract_schema_bounds(self):
        """测试 FINDER_EVALUATION_CONTRACT 中 dependencies, limitations, quote 的硬约束。"""
        schema = FINDER_EVALUATION_CONTRACT["json_schema"]["schema"]
        props = schema["properties"]

        # dependencies 约束
        deps = props["dependencies"]
        self.assertEqual(deps["type"], "array")
        self.assertEqual(deps["maxItems"], 5)
        self.assertEqual(deps["items"]["type"], "string")
        self.assertEqual(deps["items"]["maxLength"], 100)

        # limitations 约束
        limits = props["limitations"]
        self.assertEqual(limits["type"], "array")
        self.assertEqual(limits["maxItems"], 5)
        self.assertEqual(limits["items"]["type"], "string")
        self.assertEqual(limits["items"]["maxLength"], 100)

        # evidence quote 约束
        cr_items = props["criteria_results"]["items"]
        ev_items = cr_items["properties"]["evidence"]["items"]
        quote_prop = ev_items["properties"]["quote"]
        self.assertEqual(quote_prop["type"], "string")
        self.assertEqual(quote_prop["maxLength"], 1200)

    def test_build_evaluation_prompt_contains_bounds_and_none_instructions(self):
        """测试 build_evaluation_prompt 明确声明长度上限及 none 判定精简指令。"""
        cand = Candidate(
            skill_id="test/repo:SKILL.md",
            owner="test",
            repo="repo",
            path="SKILL.md",
            name="repo",
            description="desc",
            url="url",
            repo_url="repo_url",
        )
        plan = {
            "intent": "emotional support",
            "criteria": PLAN_CRITERIA,
        }
        system, user = build_evaluation_prompt(cand, {"SKILL.md": "content"}, plan)

        self.assertIn("dependencies 与 limitations 数组最多 5 项", system)
        self.assertIn("每项不超过 100 字符", system)
        self.assertIn("quote 最多 1200 字符", system)
        self.assertIn("极简不相关输出约束", system)

    def test_parser_graceful_truncation_for_dependencies_limitations_and_evidence(self):
        """测试解析器对超出的数组项数和字符长度执行优雅截断，避免抛出硬异常。"""
        long_quote = "A" * 1500
        long_dep = "B" * 150
        long_limit = "C" * 180

        raw_payload = {
            "match": "strong",
            "documentation": "clear",
            "summary_zh": "测试摘要",
            "usage_zh": "测试调用指引",
            "why_consider": "测试推荐理由",
            "dependencies": [long_dep, "dep2", "dep3", "dep4", "dep5", "dep6_extra", "dep7_extra"],
            "limitations": [long_limit, "lim2", "lim3", "lim4", "lim5", "lim6_extra"],
            "criteria_results": [
                {
                    "criterion_id": "support",
                    "status": "supported",
                    "explanation": "匹配成功",
                    "evidence": [
                        {"source_path": "SKILL.md", "start_line": 1, "end_line": 2, "quote": long_quote},
                        {"source_path": "SKILL.md", "start_line": 3, "end_line": 4, "quote": "ev2"},
                        {"source_path": "SKILL.md", "start_line": 5, "end_line": 6, "quote": "ev3"},
                        {"source_path": "SKILL.md", "start_line": 7, "end_line": 8, "quote": "ev4_extra"},
                    ],
                }
            ],
        }

        res = parse_skill_evaluation(json.dumps(raw_payload), PLAN_CRITERIA)

        # dependencies 截断至最多 5 项，首项截断至 100 字符
        self.assertEqual(len(res["dependencies"]), 5)
        self.assertEqual(len(res["dependencies"][0]), 100)
        self.assertEqual(res["dependencies"][0], "B" * 100)

        # limitations 截断至最多 5 项，首项截断至 100 字符
        self.assertEqual(len(res["limitations"]), 5)
        self.assertEqual(len(res["limitations"][0]), 100)
        self.assertEqual(res["limitations"][0], "C" * 100)

        # evidence 截断至最多 3 项，首项 quote 截断至 1200 字符
        ev_list = res["criteria_results"][0]["evidence"]
        self.assertEqual(len(ev_list), 3)
        self.assertEqual(len(ev_list[0]["quote"]), 1200)
        self.assertEqual(ev_list[0]["quote"], "A" * 1200)

    def test_parser_preserves_strict_type_checks(self):
        """测试解析器对根本性类型错误（非数组、非文本、行号非整型）仍严格拦截。"""
        # dependencies 不是数组
        bad_deps = {
            "match": "none",
            "documentation": "insufficient",
            "summary_zh": "测试",
            "dependencies": "not-a-list",
            "criteria_results": [{"criterion_id": "support", "status": "unsupported", "explanation": "no", "evidence": []}],
        }
        with self.assertRaises(ValueError):
            parse_skill_evaluation(json.dumps(bad_deps), PLAN_CRITERIA)

        # dependencies 元素不是文本
        bad_dep_item = {
            "match": "none",
            "documentation": "insufficient",
            "summary_zh": "测试",
            "dependencies": [123],
            "criteria_results": [{"criterion_id": "support", "status": "unsupported", "explanation": "no", "evidence": []}],
        }
        with self.assertRaises(ValueError):
            parse_skill_evaluation(json.dumps(bad_dep_item), PLAN_CRITERIA)

        # quote 不是文本
        bad_quote = {
            "match": "none",
            "documentation": "insufficient",
            "summary_zh": "测试",
            "criteria_results": [
                {
                    "criterion_id": "support",
                    "status": "unsupported",
                    "explanation": "no",
                    "evidence": [{"source_path": "SKILL.md", "start_line": 1, "end_line": 2, "quote": 12345}],
                }
            ],
        }
        with self.assertRaises(ValueError):
            parse_skill_evaluation(json.dumps(bad_quote), PLAN_CRITERIA)


if __name__ == "__main__":
    unittest.main()
