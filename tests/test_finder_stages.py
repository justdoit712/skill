"""阶段与判定逻辑测试（Stage & Decision Contracts）。

归并自 test_discover.py, test_dedupe.py, test_decide.py,
test_finder_clarification.py, test_clarification_choice.py,
test_finder_relevance.py, test_finder_alternative_thresholds.py, test_p2_query_terminology.py。
所有测试均为纯内存无副作用单元测试。
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests import smoke
from src.catalog.discovery import (
    _skill_name_from_path,
    candidates_from_sources,
)
from src.catalog.dedupe import (
    candidate_from_repo,
    content_fingerprint,
    dedupe,
    make_skill_id,
    parse_github_url,
)
from src.catalog.decide import (
    DECISION_CANDIDATE,
    DECISION_EXCLUDED,
    DECISION_PROCESSING_FAILURE,
    DECISION_RECOMMENDED,
    decide,
)
from src.finder.plan import (
    build_clarification_question_prompt,
    parse_clarification_question,
    parse_query_plan,
)
from src.finder.run import parse_clarification_choice
from src.finder.relevance import (
    extract_relevance_terms,
    score_candidate_relevance,
    score_repository_relevance,
)
from src.finder.evaluation import (
    DOC_CLEAR,
    DOC_INSUFFICIENT,
    KIND_QUALITY_SIGNAL,
    KIND_REQUIRED,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_STRONG,
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    verify_and_adjust_evaluation,
)
from src.finder.terminology import (
    normalize_query,
    has_search_operator,
    strip_search_operators,
    deduplicate_queries,
)
from src.shared.models import Candidate

ROOT = Path(__file__).resolve().parents[1]


def _load_json(rel_path: str):
    p = ROOT / rel_path
    return json.loads(p.read_text(encoding="utf-8"))


# =====================================================================
# 1. 发现层测试 (Discovery)
# =====================================================================

@smoke
class DiscoveryStageTest(unittest.TestCase):
    """候选展开与来源种子提取。"""
    @classmethod
    def setUpClass(cls):
        cls.sources = _load_json("config/discovery/sources.json")

    def test_skill_name_from_path(self):
        self.assertEqual(_skill_name_from_path("skills/market-news-analyst/SKILL.md", "repo"), "market-news-analyst")
        self.assertEqual(_skill_name_from_path("SKILL.md", "repo"), "repo")
        self.assertEqual(_skill_name_from_path("a/b/c/SKILL.md", "repo"), "c")

    def test_seeds_include_official_repositories(self):
        seeds = candidates_from_sources(self.sources)
        ids = {c.skill_id for c in seeds}
        for expected in ("anthropics/skills", "cloudflare/skills", "trailofbits/skills"):
            self.assertIn(expected, ids, f"官方来源 {expected} 未进入发现种子")

    def test_excluded_sources_are_skipped(self):
        seeds = candidates_from_sources(self.sources)
        for cand in seeds:
            self.assertNotEqual(cand.owner, "huggingface", "被排除的来源不得作为种子")


# =====================================================================
# 2. 去重与规范化测试 (Deduplication)
# =====================================================================

@smoke
class DedupeStageTest(unittest.TestCase):
    """GitHub URL 解析、稳定 ID 与候选去重。"""
    def test_parse_github_url_variants(self):
        self.assertEqual(parse_github_url("https://github.com/anthropics/skills"), ("anthropics", "skills", "", "repo"))
        self.assertEqual(parse_github_url("https://github.com/anthropics/skills/tree/main/skills/pdf"), ("anthropics", "skills", "skills/pdf", "tree"))
        self.assertEqual(parse_github_url("https://github.com/tradermonty/claude-trading-skills/blob/main/skills/market-news-analyst/SKILL.md"),
                         ("tradermonty", "claude-trading-skills", "skills/market-news-analyst/SKILL.md", "blob"))
        self.assertEqual(parse_github_url("https://raw.githubusercontent.com/thousandcents/futures-research/main/SKILL.md"),
                         ("thousandcents", "futures-research", "SKILL.md", "raw"))
        self.assertEqual(parse_github_url("https://gitlab.com/x/y"), ("", "", "", "unknown"))

    def test_case_normalised_and_git_suffix(self):
        owner, repo, _, _ = parse_github_url("https://github.com/Anthropics/SKILLS.git")
        self.assertEqual((owner, repo), ("anthropics", "skills"))
        self.assertEqual(make_skill_id(*parse_github_url("https://github.com/a/b.git")[:2]), "a/b")

    def test_dedupe_prefers_active_upstream_and_stable_order(self):
        c1 = candidate_from_repo("a", "b", url="url", description="desc")
        c2 = candidate_from_repo("a", "b", url="url", description="desc")
        self.assertEqual(len(dedupe([c1, c2])), 1)


# =====================================================================
# 3. 决策逻辑测试 (Decision Logic)
# =====================================================================

@smoke
class DecideStageTest(unittest.TestCase):
    """验证 §5.2 的九类基准样本与边界决策。"""
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_json("tests/fixtures/evaluations.json")
        rules_path = "config/standards/rules.json" if (ROOT / "config/standards/rules.json").exists() else "config/rules.json"
        cls.rules = _load_json(rules_path)

    def test_each_validation_sample_matches_expected_decision(self):
        for case in self.fixture["cases"]:
            with self.subTest(sample=case["id"]):
                result = decide(case["evaluation"], self.rules)
                self.assertEqual(result["decision"], case["expected_decision"], result["notes"])

    def test_no_evidence_blocks_recommendation(self):
        case = next(c for c in self.fixture["extra_cases"] if c["id"] == "no_evidence")
        result = decide(case["evaluation"], self.rules)
        self.assertEqual(result["decision"], DECISION_CANDIDATE)
        self.assertIn("scope_match", result["blocking_checks"])


# =====================================================================
# 4. 澄清轮询与选项解析测试 (Clarification)
# =====================================================================

@smoke
class ClarificationStageTest(unittest.TestCase):
    """需求澄清 Prompt 构造与多选交互解析。"""
    def setUp(self):
        self.options = [
            "用于 Python / TypeScript 代码生成与单元测试重构",
            "用于日常技术文档与中英文文章润色写作",
            "通用的 LLM 提示词模板设计与效果评估",
            "面向特定业务领域（如法律/金融）的问答微调",
        ]

    def test_parse_clarification_choice_single_and_multi(self):
        res1 = parse_clarification_choice("2", self.options)
        self.assertEqual(res1["input_type"], "single_choice")
        self.assertEqual(res1["selected_indices"], [2])
        self.assertEqual(res1["answer"], self.options[1])

        res2 = parse_clarification_choice("1, 3", self.options)
        self.assertEqual(res2["input_type"], "multiple_choice")
        self.assertEqual(res2["selected_indices"], [1, 3])
        self.assertEqual(res2["answer"], f"{self.options[0]}；{self.options[2]}")

    def test_parse_clarification_question_json(self):
        sample = json.dumps({"focus": "技术栈", "question": "是否需要特定语言？", "options": ["Python", "Rust"]})
        parsed = parse_clarification_question(sample)
        self.assertEqual(parsed["focus"], "技术栈")
        self.assertEqual(parsed["options"], ["Python", "Rust"])


# =====================================================================
# 5. 相关性与打分测试 (Relevance)
# =====================================================================

class RelevanceStageTest(unittest.TestCase):
    """关键词提取、领域词加权与仓库相关性打分。"""
    def setUp(self):
        self.plan = {
            "intent": "寻找针对情绪支持、共情倾听的提示词",
            "queries": ["emotional support prompt", "cbt companion skill"],
            "criteria": [
                {"name": "共情与情绪支持", "description": "必须具备共情倾听", "kind": "required"}
            ],
        }

    def test_extract_relevance_terms_and_scoring(self):
        terms = extract_relevance_terms("情绪安抚", self.plan)
        self.assertIn("emotional", terms)
        self.assertGreaterEqual(terms["emotional"], 2.0)
        self.assertIn("cbt", terms)

        # 仓库打分
        repo = {"owner": "test", "repo": "cbt-companion", "description": "emotional support bot"}
        res = score_repository_relevance(repo, terms)
        self.assertGreater(res["score"], 0)
        self.assertTrue(len(res["matched_terms"]) > 0)


# =====================================================================
# 6. 准则核验与备选门槛测试 (Alternative Thresholds)
# =====================================================================

class AlternativeThresholdsTest(unittest.TestCase):
    """备选判定门槛与质量项/必选项约束。"""
    def test_partial_requires_valid_evidence_for_required_criteria(self):
        materials = {"SKILL.md": "# Prompt Skill\nA tool for prompts."}
        plan_criteria = [
            {"id": "req_feature", "kind": KIND_REQUIRED, "description": "核心功能"},
            {"id": "quality_sig", "kind": KIND_QUALITY_SIGNAL, "description": "质量信号"},
        ]
        # 仅有 quality_signal 满足时不得判定为 partial
        eval_dict = {
            "match": MATCH_PARTIAL,
            "documentation": DOC_CLEAR,
            "criteria_results": [
                {"criterion_id": "req_feature", "status": STATUS_UNSUPPORTED, "evidence": []},
                {"criterion_id": "quality_sig", "status": STATUS_SUPPORTED,
                 "evidence": [{"source_path": "SKILL.md", "start_line": 1, "end_line": 1, "quote": "Prompt"}]},
            ]
        }
        res = verify_and_adjust_evaluation(eval_dict, materials, plan_criteria)
        self.assertEqual(res["match"], MATCH_NONE)


# =====================================================================
# 7. 查询规范化与术语测试 (Query & Terminology)
# =====================================================================

@smoke
class QueryTerminologyTest(unittest.TestCase):
    """查询规范化、操作符清洗与去重。"""
    def test_normalize_and_strip_operators(self):
        self.assertEqual(normalize_query("  k8s   pod   monitor  "), "k8s pod monitor")
        self.assertTrue(has_search_operator("stars:>100"))
        self.assertFalse(has_search_operator("prompt generator"))
        self.assertEqual(strip_search_operators("web scraping stars:>50 tool"), "web scraping tool")

    def test_deduplicate_queries(self):
        queries = ["k8s pod monitor", "K8S POD MONITOR", "  k8s   pod   monitor  ", "docker"]
        deduped = deduplicate_queries(queries)
        self.assertEqual(deduped, ["k8s pod monitor", "docker"])


if __name__ == "__main__":
    unittest.main()
