"""测试 Phase 5：备选判定收紧与核心相关性门槛。

核心约束：
1. partial 必须至少有一项 required 准则具备有效证据支持；
2. 若仅满足 quality_signal（加分/质量项），强制保持或降级为 none，坚决不进入备选池；
3. 模型原判为 none 时，即便核验发现存在支持项，坚决维持 none 并记录 audit_conflict，杜绝擅自批量升级；
4. 存在明确 unsupported 必需能力项时，强制为 none；
5. documentation == "insufficient" 时剥夺 strong 资格，降级为 partial 进入备选；
6. limitations 明确列出缺失的核心能力，且总数受限不超过 5 项；
7. none 判定时清空 why_consider。
"""

from __future__ import annotations

import unittest
from typing import Any

from src.finder.evaluation import (
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
    rank_find_results,
    verify_and_adjust_evaluation,
)

SAMPLE_MATERIALS = {
    "SKILL.md": (
        "# Comforting Companion Skill\n"
        "Provides empathetic emotional soothing and active listening techniques.\n"
        "Includes rich dialogue examples for anxiety and stress relief.\n"
        "Notice: This tool is strictly non-clinical.\n"
    )
}


class TestFinderAlternativeThresholds(unittest.TestCase):
    """测试阶段 5 核心相关性门槛与备选判定收紧。"""

    def setUp(self) -> None:
        self.materials = SAMPLE_MATERIALS
        self.plan_criteria = [
            {
                "id": "emotional_comfort",
                "kind": KIND_REQUIRED,
                "description": "具备情绪安抚与心理疏导能力",
            },
            {
                "id": "active_listening",
                "kind": KIND_REQUIRED,
                "description": "具备主动倾听与共情回应技巧",
            },
            {
                "id": "dialogue_examples",
                "kind": KIND_QUALITY_SIGNAL,
                "description": "包含丰富的情境对话示例",
            },
        ]

    def test_quality_signal_only_downgraded_to_none_never_partial(self) -> None:
        """若仅满足 quality_signal（例如仅有示例），必需能力全为 unknown/unsupported，强制降级为 none，坚决不进入备选池。"""
        raw_eval = {
            "match": MATCH_PARTIAL,
            "documentation": DOC_CLEAR,
            "summary_zh": "仅包含对话格式示例的技能",
            "why_consider": "提供了很多对话样例",
            "criteria_results": [
                {
                    "criterion_id": "emotional_comfort",
                    "status": STATUS_UNKNOWN,
                    "explanation": "未提及具体安抚手法",
                    "evidence": [],
                },
                {
                    "criterion_id": "active_listening",
                    "status": STATUS_UNKNOWN,
                    "explanation": "未提及共情回应",
                    "evidence": [],
                },
                {
                    "criterion_id": "dialogue_examples",
                    "status": STATUS_SUPPORTED,
                    "explanation": "包含丰富样例",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 3,
                            "end_line": 3,
                            "quote": "Includes rich dialogue examples for anxiety and stress relief.",
                        }
                    ],
                },
            ],
            "limitations": [],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, self.plan_criteria)

        # 强制降级为 none，why_consider 被清空
        self.assertEqual(adjusted["match"], MATCH_NONE)
        self.assertEqual(adjusted["why_consider"], "")
        self.assertIn("缺少必需能力", adjusted.get("downgrade_reason", ""))

        # 验证排序投影：绝不进入 shortlist 也绝不进入 alternatives
        item = {"candidate": {"skill_id": "demo/cand1:SKILL.md"}, "evaluation": adjusted}
        shortlist, alternatives = rank_find_results([item], plan={"criteria": self.plan_criteria})
        self.assertEqual(len(shortlist), 0)
        self.assertEqual(len(alternatives), 0)

    def test_partial_requires_at_least_one_verified_required_criterion(self) -> None:
        """至少有 1 项 required 准则经核验有效支持时，才允许保留为 partial 并列入备选池。"""
        raw_eval = {
            "match": MATCH_STRONG,  # 模型声称 strong
            "documentation": DOC_CLEAR,
            "summary_zh": "支持安抚但在倾听方面缺少证据",
            "why_consider": "客观材料具备核心情绪安抚能力",
            "criteria_results": [
                {
                    "criterion_id": "emotional_comfort",
                    "status": STATUS_SUPPORTED,
                    "explanation": "材料明确说明提供安抚",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Provides empathetic emotional soothing and active listening techniques.",
                        }
                    ],
                },
                {
                    "criterion_id": "active_listening",
                    "status": STATUS_UNKNOWN,  # 倾听项缺少直接证据
                    "explanation": "材料中倾听细节不足",
                    "evidence": [],
                },
                {
                    "criterion_id": "dialogue_examples",
                    "status": STATUS_UNKNOWN,
                    "evidence": [],
                },
            ],
            "limitations": [],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, self.plan_criteria)

        # 降级为 partial
        self.assertEqual(adjusted["match"], MATCH_PARTIAL)
        self.assertIn("缺少必需能力支持", " ".join(adjusted.get("limitations", [])))

        # 验证排序投影：成功进入 alternatives
        item = {"candidate": {"skill_id": "demo/cand2:SKILL.md"}, "evaluation": adjusted}
        shortlist, alternatives = rank_find_results([item], plan={"criteria": self.plan_criteria})
        self.assertEqual(len(shortlist), 0)
        self.assertEqual(len(alternatives), 1)
        self.assertEqual(alternatives[0]["candidate"]["skill_id"], "demo/cand2:SKILL.md")

    def test_unsupported_required_criterion_downgrades_to_none(self) -> None:
        """存在明确不支持的 required 项时，即使另一项 required 支持，也强制降级为 none。"""
        raw_eval = {
            "match": MATCH_PARTIAL,
            "documentation": DOC_CLEAR,
            "summary_zh": "测试",
            "criteria_results": [
                {
                    "criterion_id": "emotional_comfort",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Provides empathetic emotional soothing and active listening techniques.",
                        }
                    ],
                },
                {
                    "criterion_id": "active_listening",
                    "status": STATUS_UNSUPPORTED,  # 明确不支持
                    "explanation": "明确声明不提供共情倾听",
                    "evidence": [],
                },
            ],
            "limitations": [],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, self.plan_criteria)
        self.assertEqual(adjusted["match"], MATCH_NONE)
        self.assertIn("不支持", adjusted.get("downgrade_reason", ""))

    def test_strong_downgraded_to_partial_when_documentation_insufficient(self) -> None:
        """所有必需准则全部满足，但 documentation 为 insufficient 时，剥夺 strong 降级为 partial。"""
        raw_eval = {
            "match": MATCH_STRONG,
            "documentation": DOC_INSUFFICIENT,  # 文档严重不足
            "summary_zh": "全满足但无使用说明",
            "why_consider": "核心功能齐全",
            "criteria_results": [
                {
                    "criterion_id": "emotional_comfort",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Provides empathetic emotional soothing and active listening techniques.",
                        }
                    ],
                },
                {
                    "criterion_id": "active_listening",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Provides empathetic emotional soothing and active listening techniques.",
                        }
                    ],
                },
            ],
            "limitations": [],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, self.plan_criteria)
        self.assertEqual(adjusted["match"], MATCH_PARTIAL)
        self.assertIn("insufficient", adjusted.get("downgrade_reason", ""))

        item = {"candidate": {"skill_id": "demo/cand3:SKILL.md"}, "evaluation": adjusted}
        shortlist, alternatives = rank_find_results([item], plan={"criteria": self.plan_criteria})
        self.assertEqual(len(shortlist), 0)
        self.assertEqual(len(alternatives), 1)

    def test_raw_none_with_verified_evidence_retains_none_and_records_audit_conflict(self) -> None:
        """模型原判为 none 时，即使客观核验发现命中引文，绝不擅自自动升级为 partial/strong，记录 audit_conflict。"""
        raw_eval = {
            "match": MATCH_NONE,
            "documentation": DOC_CLEAR,
            "summary_zh": "模型认为完全不符合",
            "why_consider": "",
            "criteria_results": [
                {
                    "criterion_id": "emotional_comfort",
                    "status": STATUS_SUPPORTED,
                    "explanation": "核验器发现文本确实存在引文",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 2,
                            "end_line": 2,
                            "quote": "Provides empathetic emotional soothing and active listening techniques.",
                        }
                    ],
                }
            ],
            "limitations": [],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, self.plan_criteria)
        self.assertEqual(adjusted["match"], MATCH_NONE)
        self.assertIsNotNone(adjusted.get("audit_conflict"))
        self.assertIn("维持原判 none", adjusted["audit_conflict"])

    def test_limitations_bounded_to_max_five_items(self) -> None:
        """测试缺失必需能力时，limitations 即使添加补充说明也不会超过 5 项限制。"""
        many_criteria = [
            {"id": f"req_{i}", "kind": KIND_REQUIRED, "description": f"必需能力 #{i}"}
            for i in range(10)
        ]
        raw_eval = {
            "match": MATCH_PARTIAL,
            "documentation": DOC_CLEAR,
            "summary_zh": "部分满足",
            "criteria_results": [
                {
                    "criterion_id": "req_0",
                    "status": STATUS_SUPPORTED,
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 1,
                            "end_line": 1,
                            "quote": "# Comforting Companion Skill",
                        }
                    ],
                }
            ],
            "limitations": ["用户既有限制A", "用户既有限制B", "用户既有限制C"],
        }

        adjusted = verify_and_adjust_evaluation(raw_eval, self.materials, many_criteria)
        self.assertLessEqual(len(adjusted.get("limitations", [])), 5)


if __name__ == "__main__":
    unittest.main()
