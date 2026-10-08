"""目录质量评估核心单元测试：单轮推荐、证据伪造阻断与待决恢复不重复调用。

保留 3 项核心行为：
1. 正常推荐（单轮模型请求完整通过并给出 recommended 决策）。
2. 无效证据拒绝推荐（伪造引用或无法在原文定位时阻断在 candidate）。
3. 恢复不重复调用（已有有效 pending_evaluation 恢复时零模型调用）。
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.catalog.decide import decide
from src.catalog.evaluation import evaluate, parse_evaluation
from src.catalog.models import Candidate
from src.infra.llm import ModelCallResult
from tests import smoke

ROOT = Path(__file__).resolve().parents[1]
TEXT = "---\nname: audit\ndescription: 审查代码\n---\n定位错误，给出修复建议及可重复验证的测试。\n无外部工具依赖。"


def assessment(rules):
    item = {
        "value": "pass",
        "evidence": "有具体步骤及验证要求",
        "citations": [{"start_line": 5, "end_line": 5, "quote": TEXT.splitlines()[4]}],
    }
    result = {
        **{c["id"]: deepcopy(item) for c in rules["checks"]},
        "domain_checks": {},
        "reason_codes": [],
        "main_category": "dev",
        "summary_zh": "用于审查代码。",
    }
    for key, value in result.items():
        if key != "evidence_traceability" and isinstance(value, dict):
            value.pop("citations", None)
    return result


def response(payload, tokens=100, usage=True):
    return ModelCallResult(
        ok=True,
        content=json.dumps(payload, ensure_ascii=False),
        attempts=1,
        usage={"prompt_tokens": tokens - 20, "completion_tokens": 20, "total_tokens": tokens} if usage else {},
    )


class QualityTest(unittest.TestCase):
    """目录质量准入与证据检验契约测试。"""

    def setUp(self):
        self.rules = json.loads((ROOT / "config/standards/rules.json").read_text(encoding="utf-8"))
        self.taxonomy = json.loads((ROOT / "config/standards/taxonomy.json").read_text(encoding="utf-8"))
        self.candidate = Candidate(skill_id="test/audit:SKILL.md", owner="test", repo="audit", path="SKILL.md")
        self.raw = assessment(self.rules)

    def run_evaluation(self, responses, **kwargs):
        with patch("src.catalog.evaluation.call_model", side_effect=responses) as model:
            result = evaluate(self.candidate, TEXT, model_cfg={}, rules=self.rules, taxonomy=self.taxonomy, **kwargs)
        return result, model

    @smoke
    def test_single_pass_recommends_with_one_request(self):
        """单轮评估在 6 项检查及真实引用均合格时直接推荐，且仅消耗一次模型请求。"""
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")
        self.assertEqual(result["evaluation"]["quality_audit"]["review_status"], "single_pass")
        self.assertEqual(sum(c.total_tokens for c in result["calls"]), 100)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["stage"], "assessment")

    def test_fabricated_quote_stays_candidate(self):
        """当模型返回的引文在原文中无法找到时，阻断推荐并停留在 candidate。"""
        self.raw["evidence_traceability"]["citations"][0]["quote"] = "并不存在的原文"
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(model.call_count, 1)
        self.assertIn("evidence_traceability", result["evaluation"]["quality_audit"]["invalid_citations"])

    def test_valid_pending_assessment_finishes_without_another_request(self):
        """已有有效待决评估 (pending_evaluation) 时直接复用，不发起任何模型调用。"""
        pending = parse_evaluation(
            json.dumps(self.raw), self.rules, self.candidate.content_fingerprint, self.taxonomy
        )
        result, model = self.run_evaluation([], pending_evaluation=pending)
        self.assertTrue(result["ok"])
        self.assertEqual(model.call_count, 0)
        self.assertEqual(result["calls"], [])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")


if __name__ == "__main__":
    unittest.main()
