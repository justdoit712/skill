"""决策逻辑：用固定评估输出验证 §5.2 的九类样本与边界情形。

不调用模型。样本类别与 config/rules.json 的 validation_samples 一一对应。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.decide import (
    DECISION_CANDIDATE,
    DECISION_EXCLUDED,
    DECISION_PROCESSING_FAILURE,
    DECISION_RECOMMENDED,
    decide,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "evaluations.json").read_text(encoding="utf-8"))
RULES = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))


class ValidationSampleTest(unittest.TestCase):
    """§5.2 列出的九类样本，必须落到预期决策。"""

    def test_fixture_covers_the_nine_samples(self) -> None:
        self.assertEqual(len(FIXTURE["cases"]), 9)
        configured = {s["id"] for s in RULES["validation_samples"]}
        self.assertEqual({c["id"] for c in FIXTURE["cases"]}, configured)

    def test_each_sample_matches_expected_decision(self) -> None:
        for case in FIXTURE["cases"]:
            with self.subTest(sample=case["id"]):
                result = decide(case["evaluation"], RULES)
                self.assertEqual(result["decision"], case["expected_decision"], result["notes"])

    def test_expected_reason_codes_are_reported(self) -> None:
        for case in FIXTURE["cases"]:
            for code in case.get("expected_reason_codes", []):
                with self.subTest(sample=case["id"], code=code):
                    self.assertIn(code, decide(case["evaluation"], RULES)["reason_codes"])


class BoundaryTest(unittest.TestCase):
    def test_no_evidence_blocks_recommendation(self) -> None:
        """§5.2：不得用模型自报置信度替代证据。"""
        case = next(c for c in FIXTURE["extra_cases"] if c["id"] == "no_evidence")
        result = decide(case["evaluation"], RULES)
        self.assertEqual(result["decision"], DECISION_CANDIDATE)
        self.assertIn("scope_match", result["blocking_checks"])

    def test_processing_failure_is_not_a_quality_judgement(self) -> None:
        case = next(c for c in FIXTURE["extra_cases"] if c["id"] == "processing_failure")
        result = decide(case["evaluation"], RULES)
        self.assertEqual(result["decision"], DECISION_PROCESSING_FAILURE)
        self.assertNotEqual(result["decision"], DECISION_EXCLUDED)

    def test_invalid_structure_is_processing_failure(self) -> None:
        case = next(c for c in FIXTURE["extra_cases"] if c["id"] == "structure_invalid")
        self.assertEqual(decide(case["evaluation"], RULES)["decision"], DECISION_PROCESSING_FAILURE)

    def test_not_applicable_domain_check_does_not_block(self) -> None:
        """§5.2：没有回测功能时该检查明确记为不适用，而非 fail。"""
        case = next(c for c in FIXTURE["extra_cases"] if c["id"] == "backtest_not_applicable")
        self.assertEqual(decide(case["evaluation"], RULES)["decision"], DECISION_RECOMMENDED)

    def test_unknown_check_goes_to_candidate_not_excluded(self) -> None:
        evaluation = dict(FIXTURE["cases"][0]["evaluation"])
        evaluation["risk_review"] = {"value": "unknown", "evidence": "无法判定"}
        result = decide(evaluation, RULES)
        self.assertEqual(result["decision"], DECISION_CANDIDATE)
        self.assertNotEqual(result["decision"], DECISION_EXCLUDED)


if __name__ == "__main__":
    unittest.main()
