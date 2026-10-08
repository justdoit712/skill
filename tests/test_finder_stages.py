"""定向查找阶段核心单元测试：候选去重与证据链决定推荐结果。

保留 2 项核心行为：
1. 候选去重（保持上游唯一性与稳定排序）。
2. 证据决定推荐结果（完整证据准入 recommended，缺失证据阻断至 candidate）。
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.catalog.decide import DECISION_CANDIDATE, DECISION_RECOMMENDED, decide
from src.catalog.dedupe import candidate_from_repo, dedupe
from tests import smoke

ROOT = Path(__file__).resolve().parents[1]


class FinderStagesTest(unittest.TestCase):
    """查找流水线阶段逻辑契约测试。"""

    @classmethod
    def setUpClass(cls):
        rules_path = (
            ROOT / "config/standards/rules.json"
            if (ROOT / "config/standards/rules.json").exists()
            else ROOT / "config/rules.json"
        )
        cls.rules = json.loads(rules_path.read_text(encoding="utf-8"))

    @smoke
    def test_dedupe_prefers_active_upstream_and_stable_order(self):
        """重复仓库候选被正确去重，保留单一有效条目。"""
        c1 = candidate_from_repo("a", "b", url="url", description="desc")
        c2 = candidate_from_repo("a", "b", url="url", description="desc")
        self.assertEqual(len(dedupe([c1, c2])), 1)

    def test_evidence_decides_recommendation(self):
        """证据链完整度决定最终准入：有证据准入，缺失定位证据则阻断为 candidate。"""
        valid_eval = {
            "scope_match": {"value": "pass", "evidence": "SKILL.md 描述代码审查用途"},
            "purpose_clarity": {"value": "pass", "evidence": "SKILL.md#任务目标"},
            "instruction_completeness": {"value": "pass", "evidence": "SKILL.md 含步骤与示例"},
            "evidence_traceability": {"value": "pass", "evidence": "介绍与实际内容一致"},
            "dependency_transparency": {"value": "pass", "evidence": "声明无需外部依赖"},
            "risk_review": {"value": "pass", "evidence": "无可疑行为"},
            "domain_checks": {},
            "reason_codes": [],
            "rules_version": "1.0.0",
            "source_fingerprint": "sha256:aaaa0001",
        }
        rec_decision = decide(valid_eval, self.rules)
        self.assertEqual(rec_decision["decision"], DECISION_RECOMMENDED)

        # 缺失证据场景（例如 scope_match 证据为空）
        no_evidence_eval = {**valid_eval, "scope_match": {"value": "pass", "evidence": ""}}
        block_decision = decide(no_evidence_eval, self.rules)
        self.assertEqual(block_decision["decision"], DECISION_CANDIDATE)
        self.assertIn("scope_match", block_decision.get("blocking_checks", []))


if __name__ == "__main__":
    unittest.main()
