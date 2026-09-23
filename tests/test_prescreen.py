"""预筛：该拒的必须拒，不该拒的不能误杀。"""

from __future__ import annotations

import unittest
from pathlib import Path

from src.catalog.dedupe import candidate_from_repo
from src.catalog.prescreen import DECISION_EXCLUDED, DECISION_QUEUED, load_config, prescreen

ROOT = Path(__file__).resolve().parents[1]


def make(owner: str, repo: str, **kwargs):
    return candidate_from_repo(owner, repo, **kwargs)


class PrescreenExclusionTest(unittest.TestCase):
    """§5.1：不符合领域、非技能、DSH 插件应在调用 AI 前排除。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(ROOT / "config")

    def assert_excluded(self, candidate, code: str) -> None:
        result = prescreen(candidate, self.cfg)
        self.assertEqual(result.decision, DECISION_EXCLUDED, result.notes)
        self.assertIn(code, result.reason_codes)

    def test_dsh_plugin_excluded(self) -> None:
        self.assert_excluded(
            make("someone", "dsh-ui-skin-switcher", url="https://github.com/someone/dsh-ui-skin-switcher"),
            "DSH_PLUGIN",
        )

    def test_self_repo_excluded(self) -> None:
        self.assert_excluded(make("justdoit712", "skill", url="https://github.com/justdoit712/skill"), "SELF_REPO")

    def test_out_of_scope_source_excluded(self) -> None:
        """Hugging Face 已在 sources.json 标记整库排除（AI 开发类）。"""
        self.assert_excluded(make("huggingface", "skills", url="https://github.com/huggingface/skills"), "NOT_STANDALONE_DIRECTION")

    def test_template_excluded(self) -> None:
        self.assert_excluded(
            make("someone", "x", path="_template", url="https://github.com/someone/x/tree/main/_template"),
            "NOT_A_SKILL",
        )

    def test_placeholder_name_excluded(self) -> None:
        self.assert_excluded(make("someone", "your-skill"), "NOT_A_SKILL")

    def test_exclusion_reason_codes_are_defined(self) -> None:
        defined = set(self.cfg.rules["reason_codes"]["exclusion"])
        used = {"DSH_PLUGIN", "SELF_REPO", "NOT_A_SKILL", "NOT_STANDALONE_DIRECTION"}
        self.assertTrue(used <= defined, f"未定义：{used - defined}")


class PrescreenNoFalseKillTest(unittest.TestCase):
    """误杀不可逆，因此不确定的一律排队。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(ROOT / "config")

    def test_in_scope_skill_queued_with_domain(self) -> None:
        candidate = make(
            "tradermonty",
            "claude-trading-skills",
            path="skills/market-news-analyst",
            url="https://github.com/tradermonty/claude-trading-skills"
            "/blob/main/skills/market-news-analyst/SKILL.md",
            name="market-news-analyst",
            description="政策、地缘事件、能源与贵金属关联",
        )
        result = prescreen(candidate, self.cfg)
        self.assertEqual(result.decision, DECISION_QUEUED)
        self.assertIn("finance", result.domains)

    def test_no_domain_match_is_not_excluded(self) -> None:
        result = prescreen(make("acme", "widget", description="一个通用小工具"), self.cfg)
        self.assertEqual(result.decision, DECISION_QUEUED)
        self.assertIn("NO_DOMAIN_TERM_MATCH", result.flags)

    def test_repo_root_flagged_for_expansion(self) -> None:
        result = prescreen(make("acme", "collection", description="视频创作技能合集"), self.cfg)
        self.assertIn("COLLECTION_NEEDS_EXPANSION", result.flags)

    def test_live_trading_is_flagged_not_excluded(self) -> None:
        """实盘下单属硬拒绝项，但需读内容才能判定，预筛只落 flag。"""
        result = prescreen(
            make("acme", "trader", name="auto-trading-bot", description="支持实盘自动下单"), self.cfg
        )
        self.assertEqual(result.decision, DECISION_QUEUED)
        self.assertIn("LIVE_TRADING_SUSPECT", result.flags)

    def test_clinical_decision_is_flagged_not_excluded(self) -> None:
        result = prescreen(
            make("acme", "med", name="clinical-decision-helper", description="辅助临床诊断与治疗决策"), self.cfg
        )
        self.assertEqual(result.decision, DECISION_QUEUED)
        self.assertIn("CLINICAL_DECISION_SUSPECT", result.flags)

    def test_short_content_flagged(self) -> None:
        candidate = make("acme", "tiny", description="内容创作")
        result = prescreen(candidate, self.cfg, text="---\nname: x\n---\n")
        self.assertIn("CONTENT_MAY_BE_EMPTY", result.flags)


if __name__ == "__main__":
    unittest.main()
