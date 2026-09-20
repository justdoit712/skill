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


class DomainCheckEnforcementTest(unittest.TestCase):
    """§5.2 要求"相关领域检查全部通过"才可推荐，空对象不能算通过。"""

    def base(self, category: str, domain_checks: dict | None = None) -> dict:
        evaluation = json.loads(json.dumps(FIXTURE["cases"][0]["evaluation"]))
        evaluation["main_category"] = category
        evaluation["domain_checks"] = domain_checks if domain_checks is not None else {}
        return evaluation

    def test_finance_without_domain_check_is_not_recommended(self) -> None:
        result = decide(self.base("finance"), RULES)
        self.assertEqual(result["decision"], DECISION_CANDIDATE)
        self.assertIn("finance", result["blocking_checks"])

    def test_health_check_required_for_both_health_categories(self) -> None:
        for category in ("mental_health", "physical_health"):
            with self.subTest(category=category):
                result = decide(self.base(category), RULES)
                self.assertEqual(result["decision"], DECISION_CANDIDATE)
                self.assertIn("health", result["blocking_checks"])

    def test_finance_with_passing_domain_check_is_recommended(self) -> None:
        evaluation = self.base("finance", {"finance": {"value": "pass", "evidence": "来源与时间已记录"}})
        self.assertEqual(decide(evaluation, RULES)["decision"], DECISION_RECOMMENDED)

    def test_not_applicable_still_counts_as_present(self) -> None:
        evaluation = self.base("finance", {"finance": {"value": "not_applicable", "evidence": "无回测功能"}})
        self.assertEqual(decide(evaluation, RULES)["decision"], DECISION_RECOMMENDED)

    def test_non_domain_category_needs_no_domain_check(self) -> None:
        self.assertEqual(decide(self.base("dev"), RULES)["decision"], DECISION_RECOMMENDED)

    def test_domain_checks_not_a_dict_does_not_bypass(self) -> None:
        evaluation = self.base("finance")
        evaluation["domain_checks"] = []
        self.assertEqual(decide(evaluation, RULES)["decision"], DECISION_CANDIDATE)


if __name__ == "__main__":
    unittest.main()
