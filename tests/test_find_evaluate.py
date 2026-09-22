"""测试定向查找评估层：严格证据核验、防幻觉降级、F6 四级稳定排序与深拷贝保护。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from src.find_evaluate import (
    DOC_CLEAR,
    DOC_INSUFFICIENT,
    DOC_PARTIAL,
    KIND_QUALITY_SIGNAL,
    KIND_REQUIRED,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_STRONG,
    STATUS_SUPPORTED,
    STATUS_UNKNOWN,
    STATUS_UNSUPPORTED,
    parse_query_plan,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
    verify_evidence_snippet,
)


class TestEvidenceVerificationCases(unittest.TestCase):
    """基于 P0 基线固化夹具进行全量证据比对测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "finder_samples"
        cases_file = fixtures_dir / "cases.json"
        with open(cases_file, "r", encoding="utf-8") as f:
            cls.cases_data = json.load(f)

        cls.valid_text = (fixtures_dir / "valid_skill.md").read_text(encoding="utf-8")
        cls.fabricated_text = (fixtures_dir / "fabricated_quote_skill.md").read_text(encoding="utf-8")
        cls.materials = {
            "skills/prompt-craftsman/SKILL.md": cls.valid_text,
            "skills/prompt-helper/SKILL.md": cls.fabricated_text,
        }

    def test_fixture_verification_cases(self) -> None:
        for case in self.cases_data.get("verification_cases", []):
            with self.subTest(case_id=case["id"]):
                ok, reason = verify_evidence_snippet(
                    source_path=case["source_path"],
                    start_line=case["start_line"],
                    end_line=case["end_line"],
                    quote=case["quote"],
                    materials=self.materials,
                )
                self.assertEqual(ok, case["expected_ok"], f"用例 {case['id']} 结果不符合预期: {reason}")
                if not case["expected_ok"] and "expected_error_substr" in case:
                    self.assertIn(
                        case["expected_error_substr"],
                        reason,
                        f"用例 {case['id']} 错误信息缺少预期子串: {reason}",
                    )


class TestStrictEvidenceAndProbeA2(unittest.TestCase):
    """T03 证据降级门槛与 Probe A2 原地修改防御测试。"""

    def setUp(self) -> None:
        self.materials = {
            "skills/prompt-craftsman/SKILL.md": (
                "# Prompt Craftsman\n"
                "A specialized AI Agent skill for designing production prompts.\n"
                "Provides structured templates and constraints.\n"
            )
        }
        self.plan_criteria = [
            {"id": "prompt_design", "kind": KIND_REQUIRED, "description": "具备 Prompt 设计能力"},
            {"id": "evaluation_rubric", "kind": KIND_QUALITY_SIGNAL, "description": "包含评测标准"},
        ]

    def test_probe_a2_evaluation_mutation_protection(self) -> None:
        """Probe A2：verify_and_adjust_evaluation 必须使用 deepcopy，绝不允许原地篡改输入字典。"""
        original_eval = {
            "match": MATCH_STRONG,
            "documentation": DOC_CLEAR,
            "summary_zh": "优秀 Prompt 技能",
            "criteria_results": [
                {
                    "criterion_id": "prompt_design",
                    "status": STATUS_SUPPORTED,
                    "explanation": "原始解释",
                    "evidence": [
                        {
                            "source_path": "skills/prompt-craftsman/SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "A specialized AI Agent skill for designing production prompts.",
                        }
                    ],
                },
                {
                    "criterion_id": "evaluation_rubric",
                    "status": STATUS_SUPPORTED,
                    "explanation": "虚假评估准则",
                    "evidence": [
                        {
                            "source_path": "skills/prompt-craftsman/SKILL.md",
                            "start_line": 99,
                            "end_line": 100,
                            "quote": "Fabricated evaluation rubric quote",
                        }
                    ],
                },
            ],
            "limitations": ["初始限制1"],
        }
        snapshot_before = copy.deepcopy(original_eval)

        adjusted = verify_and_adjust_evaluation(original_eval, self.materials, self.plan_criteria)

        # 校验原始输入字典及其内部列表字典未被任何原地修改
        self.assertEqual(original_eval, snapshot_before)
        self.assertIsNot(adjusted, original_eval)
        self.assertIsNot(adjusted["criteria_results"], original_eval["criteria_results"])
        self.assertIsNot(adjusted["criteria_results"][1], original_eval["criteria_results"][1])

    def test_t03_strict_evidence_verification_and_downgrade(self) -> None:
        """T03: 虚假行号或造假引文导致必需项降级为 unknown，剥夺 strong 资格，无法进入短名单。"""
        fake_eval = {
            "match": MATCH_STRONG,
            "documentation": DOC_CLEAR,
            "summary_zh": "声称完全支持",
            "criteria_results": [
                {
                    "criterion_id": "prompt_design",
                    "status": STATUS_SUPPORTED,
                    "explanation": "声称支持但引文捏造",
                    "evidence": [
                        {
                            "source_path": "skills/prompt-craftsman/SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Fabricated guarantee 100% accuracy",
                        }
                    ],
                }
            ],
            "limitations": [],
        }
        adjusted = verify_and_adjust_evaluation(fake_eval, self.materials, self.plan_criteria)

        # 必需项证据核验失败，强制降级为 unknown，总匹配度被剥夺 strong
        self.assertEqual(adjusted["criteria_results"][0]["status"], STATUS_UNKNOWN)
        self.assertNotEqual(adjusted["match"], MATCH_STRONG)
        self.assertIn(adjusted["match"], (MATCH_PARTIAL, MATCH_NONE))


class TestT21FixedCriteria(unittest.TestCase):
    """T21: 固化准则与规划禁止私自升级。"""

    def test_parse_query_plan_rejects_missing_required(self) -> None:
        """规划中若缺少必需能力 (required criteria)，严禁私自将质量信号升级为必需，必须报错。"""
        plan_json_no_required = json.dumps(
            {
                "intent": "查找 Prompt 设计技能",
                "queries": ["prompt craftsman skill"],
                "criteria": [
                    {"id": "has_examples", "kind": KIND_QUALITY_SIGNAL, "description": "包含丰富样例"}
                ],
            }
        )
        with self.assertRaises(ValueError) as ctx:
            parse_query_plan(plan_json_no_required)
        self.assertIn("缺少必需能力", str(ctx.exception))

    def test_parse_query_plan_preserves_criteria(self) -> None:
        """正常规划保留 criteria 与 required 标记。"""
        plan_json = json.dumps(
            {
                "intent": "查找 Prompt 设计技能",
                "queries": ["prompt craftsman skill", "ai prompt engineering"],
                "criteria": [
                    {"id": "prompt_core", "kind": KIND_REQUIRED, "description": "核心 Prompt 设计能力"},
                    {"id": "doc_examples", "kind": KIND_QUALITY_SIGNAL, "description": "包含代码示例"},
                ],
            }
        )
        plan = parse_query_plan(plan_json)
        self.assertEqual(len(plan["criteria"]), 2)
        self.assertEqual(plan["criteria"][0]["kind"], KIND_REQUIRED)
        self.assertEqual(plan["criteria"][1]["kind"], KIND_QUALITY_SIGNAL)


class TestT22F6FourTierStableRanking(unittest.TestCase):
    """T22: F6 四级稳定排序规范测试。"""

    def setUp(self) -> None:
        self.plan = {
            "criteria": [
                {"id": "core_req", "kind": KIND_REQUIRED},
                {"id": "qs_1", "kind": KIND_QUALITY_SIGNAL},
                {"id": "qs_2", "kind": KIND_QUALITY_SIGNAL},
            ]
        }

    def test_f6_four_tier_stable_ranking(self) -> None:
        """验证四级排序：
        1. match (strong > partial > none)
        2. quality signal count (descending)
        3. documentation (clear > partial > insufficient)
        4. stable skill_id (ascending)
        """
        item_a = {
            "candidate": {"skill_id": "owner/repo-b:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_STRONG,
                "documentation": DOC_CLEAR,
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_1", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_2", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                ],
            },
        }
        item_b = {
            "candidate": {"skill_id": "owner/repo-a:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_STRONG,
                "documentation": DOC_CLEAR,
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_1", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_2", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                ],
            },
        }
        item_c = {
            "candidate": {"skill_id": "owner/repo-c:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_STRONG,
                "documentation": DOC_CLEAR,
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_1", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_2", "status": STATUS_UNSUPPORTED, "evidence": []},
                ],
            },
        }
        item_d = {
            "candidate": {"skill_id": "owner/repo-d:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_STRONG,
                "documentation": DOC_PARTIAL,
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                    {"criterion_id": "qs_1", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                ],
            },
        }
        item_e = {
            "candidate": {"skill_id": "owner/repo-e:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_STRONG,
                "documentation": DOC_INSUFFICIENT,  # documentation 为 insufficient，不得进短名单！
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_SUPPORTED, "evidence": [{"ok": 1}]},
                ],
            },
        }
        item_f = {
            "candidate": {"skill_id": "owner/repo-f:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_PARTIAL,
                "documentation": DOC_CLEAR,
                "criteria_results": [
                    {"criterion_id": "core_req", "status": STATUS_UNKNOWN, "evidence": []},
                ],
            },
        }
        item_g = {
            "candidate": {"skill_id": "owner/repo-g:skills/SKILL.md"},
            "evaluation": {
                "match": MATCH_NONE,
                "documentation": DOC_CLEAR,
                "criteria_results": [],
            },
        }

        # 乱序输入
        evaluated_items = [item_g, item_d, item_b, item_f, item_a, item_e, item_c]
        shortlist, alternatives = rank_find_results(evaluated_items, plan=self.plan, limit=4)

        # 1. repo-a 与 repo-b 在 match、qs_count(2)、doc(clear) 均相同时，按 skill_id 字典序排序：repo-a 在前
        self.assertEqual(shortlist[0]["candidate"]["skill_id"], "owner/repo-a:skills/SKILL.md")
        self.assertEqual(shortlist[1]["candidate"]["skill_id"], "owner/repo-b:skills/SKILL.md")
        # 2. repo-c 的 qs_count(1) 大于 repo-d 的 doc(partial)
        self.assertEqual(shortlist[2]["candidate"]["skill_id"], "owner/repo-c:skills/SKILL.md")
        self.assertEqual(shortlist[3]["candidate"]["skill_id"], "owner/repo-d:skills/SKILL.md")

        # 3. repo-e 因为 doc 为 insufficient，不得进入短名单，必须降入备选
        alt_ids = [it["candidate"]["skill_id"] for it in alternatives]
        self.assertIn("owner/repo-e:skills/SKILL.md", alt_ids)
        self.assertIn("owner/repo-f:skills/SKILL.md", alt_ids)
        # match 为 none 的条目不进入 alternatives
        self.assertNotIn("owner/repo-g:skills/SKILL.md", alt_ids)


class TestT23MaterialsSemanticsDiscrimination(unittest.TestCase):
    """T23: 语义区隔能力测试（生成 Prompt vs 只列链接 vs 泛泛提及）。"""

    def test_materials_semantics_discrimination(self) -> None:
        plan_criteria = [
            {"id": "prompt_generator", "kind": KIND_REQUIRED, "description": "具备直接生成 Prompt 模板能力"},
        ]
        materials = {
            "cand1/SKILL.md": (
                "# Prompt Engine\n"
                "Generates complete production system prompt templates directly for agents.\n"
            ),
            "cand2/SKILL.md": (
                "# Awesome Links\n"
                "Here is a collection of useful external links to prompt engineering blogs.\n"
            ),
            "cand3/SKILL.md": (
                "# General Tool\n"
                "A general purpose utility mentions prompt once without implementation.\n"
            ),
        }

        # 1. cand1 有真实证据支持
        eval_cand1 = {
            "match": MATCH_STRONG,
            "documentation": DOC_CLEAR,
            "criteria_results": [
                {
                    "criterion_id": "prompt_generator",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "cand1/SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Generates complete production system prompt templates directly for agents.",
                        }
                    ],
                }
            ],
        }
        res1 = verify_and_adjust_evaluation(eval_cand1, materials, plan_criteria)
        self.assertEqual(res1["match"], MATCH_STRONG)

        # 2. cand2 只有外部链接，模型捏造了支持引文
        eval_cand2 = {
            "match": MATCH_STRONG,
            "documentation": DOC_PARTIAL,
            "criteria_results": [
                {
                    "criterion_id": "prompt_generator",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "cand2/SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Generates complete prompt templates directly",
                        }
                    ],
                }
            ],
            "limitations": [],
        }
        res2 = verify_and_adjust_evaluation(eval_cand2, materials, plan_criteria)
        # 核验失败降级为 unknown，强匹配被剥夺
        self.assertNotEqual(res2["match"], MATCH_STRONG)

        # 3. cand3 判定为 unsupported
        eval_cand3 = {
            "match": MATCH_NONE,
            "documentation": DOC_CLEAR,
            "criteria_results": [
                {
                    "criterion_id": "prompt_generator",
                    "status": STATUS_UNSUPPORTED,
                    "evidence": [],
                }
            ],
        }
        res3 = verify_and_adjust_evaluation(eval_cand3, materials, plan_criteria)
        self.assertEqual(res3["match"], MATCH_NONE)


if __name__ == "__main__":
    unittest.main()
