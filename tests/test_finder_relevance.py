from __future__ import annotations

import unittest
from dataclasses import asdict

from src.shared.models import Candidate
from src.finder.relevance import (
    GENERIC_STOP_WORDS,
    extract_relevance_terms,
    score_candidate_relevance,
    score_repository_relevance,
    rank_pending_repositories,
    schedule_candidates_by_relevance_and_fairness,
)
from src.finder.search import schedule_candidates_fairly


class TestFinderRelevance(unittest.TestCase):
    """测试 Finder 领域相关性打分、多源关键词提取与 80/20 混合调度。"""

    def setUp(self):
        self.sample_topic = "情绪安抚与心理疏导"
        self.sample_plan = {
            "intent": "寻找针对情绪支持、共情倾听、心理慰藉的提示词或技能",
            "queries": [
                "emotional support prompt",
                "cbt companion skill",
                "心理 疏导",
            ],
            "criteria": [
                {
                    "name": "共情与情绪支持",
                    "description": "必须具备共情倾听与情绪安抚支持",
                    "kind": "required",
                },
                {
                    "name": "输出结构化建议",
                    "description": "提供结构化心理自助建议",
                    "kind": "nice_to_have",
                },
            ],
        }

    def test_extract_relevance_terms_weights_and_stopwords(self):
        """测试从主题、意图、准则和查询中提取关键词，并验证停用词降权与核心词加权。"""
        terms = extract_relevance_terms(self.sample_topic, self.sample_plan)

        # 核心领域词应被加权
        self.assertIn("emotional", terms)
        self.assertGreaterEqual(terms["emotional"], 2.0)
        self.assertIn("cbt", terms)
        self.assertGreaterEqual(terms["cbt"], 2.0)
        self.assertIn("心理", terms)
        self.assertGreaterEqual(terms["心理"], 1.8)
        self.assertIn("疏导", terms)
        self.assertGreaterEqual(terms["疏导"], 1.8)
        self.assertIn("共情", terms)
        self.assertGreaterEqual(terms["共情"], 2.5)

        # 复合 2-gram 词组应被提取且权重较高
        self.assertIn("emotional support", terms)
        self.assertGreaterEqual(terms["emotional support"], 3.0)
        self.assertIn("cbt companion", terms)
        self.assertGreaterEqual(terms["cbt companion"], 3.0)

        # 通用停用词应被降权至 <= 0.05 或被过滤
        for sw in ["prompt", "skill", "ai", "高质量", "使用"]:
            if sw in terms:
                self.assertLessEqual(terms[sw], 0.05)

    def test_score_candidate_relevance_path_and_description(self):
        """测试候选技能打分：路径/名称主导 (70%) + 仓库描述辅助 (30%)。"""
        terms = extract_relevance_terms(self.sample_topic, self.sample_plan)

        # 路径直接命中的候选
        direct_candidate = Candidate(
            owner="mayimian123",
            repo="lumae",
            path="skills/emotional-support/skill.md",
            name="emotional-support",
            description="A CBT and wellness companion",
            skill_id="mayimian123/lumae/skills/emotional-support/skill.md",
            url="https://github.com/mayimian123/lumae/blob/main/skills/emotional-support/skill.md",
        )
        res_direct = score_candidate_relevance(direct_candidate, terms)
        self.assertGreater(res_direct["score"], 3.0)
        self.assertIn("emotional support", res_direct["matched_terms"])
        self.assertGreater(res_direct["score_breakdown"]["path_score"], 0)

        # 仅仓库描述提及，但路径是纯通用工具的候选
        indirect_candidate = Candidate(
            owner="generic-user",
            repo="ai-companion",
            path="skills/calculator/skill.md",
            name="calculator",
            description="An emotional support tool collection with calc",
            skill_id="generic-user/ai-companion/skills/calculator/skill.md",
            url="https://github.com/generic-user/ai-companion/blob/main/skills/calculator/skill.md",
        )
        res_indirect = score_candidate_relevance(indirect_candidate, terms)
        self.assertEqual(res_indirect["score_breakdown"]["path_score"], 0)
        self.assertGreater(res_indirect["score_breakdown"]["repo_score"], 0)
        # 路径直接命中应明显高于仅仓库描述提及
        self.assertGreater(res_direct["score"], res_indirect["score"])

        # 完全不相关的候选
        irrelevant_candidate = Candidate(
            owner="coder",
            repo="devtools",
            path="skills/docker-deploy/skill.md",
            name="docker-deploy",
            description="Fast docker deployment scripts",
            skill_id="coder/devtools/skills/docker-deploy/skill.md",
            url="https://github.com/coder/devtools/blob/main/skills/docker-deploy/skill.md",
        )
        res_irrelevant = score_candidate_relevance(irrelevant_candidate, terms)
        self.assertEqual(res_irrelevant["score"], 0.0)
        self.assertEqual(res_irrelevant["matched_terms"], [])

    def test_score_candidate_saturation_penalty(self):
        """测试大单体仓库候选累积数量惩罚（饱和衰减）。"""
        terms = {"cbt": 3.0, "support": 2.5}
        cand = Candidate(
            owner="huge-repo",
            repo="monorepo",
            path="cbt/skill.md",
            name="cbt-1",
            description="huge collection",
            skill_id="huge-repo/monorepo/cbt/skill.md",
            url="https://github.com/huge-repo/monorepo",
        )

        res_first = score_candidate_relevance(cand, terms, repo_counts={"huge-repo/monorepo": 1})
        res_many = score_candidate_relevance(cand, terms, repo_counts={"huge-repo/monorepo": 20})

        self.assertGreater(res_first["score"], res_many["score"])
        self.assertEqual(res_first["score_breakdown"]["saturation_factor"], 1.0)
        self.assertLess(res_many["score_breakdown"]["saturation_factor"], 0.5)

    def test_score_candidate_dict_compatibility(self):
        """测试打分函数对字典格式候选的无缝支持。"""
        terms = {"cbt": 3.0}
        cand_dict = {
            "owner": "test-owner",
            "repo": "cbt-repo",
            "path": "cbt/test.md",
            "name": "cbt-helper",
            "description": "cbt assistant",
            "skill_id": "test-owner/cbt-repo/cbt/test.md",
        }
        res = score_candidate_relevance(cand_dict, terms)
        self.assertGreater(res["score"], 0)
        self.assertIn("cbt", res["matched_terms"])

    def test_rank_pending_repositories(self):
        """测试待展开仓库的相关性打分与排序。"""
        terms = extract_relevance_terms(self.sample_topic, self.sample_plan)

        pending_repos = [
            {
                "owner": "unrelated",
                "repo": "k8s-manifests",
                "description": "kubernetes deployment YAMLs",
                "query": "emotional support prompt",
                "url": "https://github.com/unrelated/k8s-manifests",
            },
            {
                "owner": "mayimian123",
                "repo": "lumae",
                "description": "CBT wellness companion and emotional support",
                "query": "cbt companion skill",
                "url": "https://github.com/mayimian123/lumae",
            },
            {
                "owner": "mallalokesh",
                "repo": "antar-canvas",
                "description": "Thoughtful AI companions for self reflection and psychology",
                "query": "cbt companion skill",
                "url": "https://github.com/mallalokesh/antar-canvas",
            },
        ]

        ranked = rank_pending_repositories(pending_repos, terms, max_repos=2)
        self.assertEqual(len(ranked), 2)
        # lumae 和 antar-canvas 应当排在 k8s-manifests 前面
        ranked_repos = [r["repo"] for r in ranked]
        self.assertIn("lumae", ranked_repos)
        self.assertNotIn("k8s-manifests", ranked_repos)

    def test_schedule_candidates_by_relevance_and_fairness_determinism_and_ratio(self):
        """测试 80/20 混合调度器的确定性、不重复性与排序逻辑。"""
        terms = {"emotional": 3.0, "support": 2.5, "cbt": 2.5}

        # 准备 10 个候选：3 个来自高相关仓库，7 个来自普通或无关仓库
        cands = [
            Candidate(owner="irrel1", repo="r1", path="p1.md", name="tool1", description="desc", skill_id="id1", url="u1"),
            Candidate(owner="rel1", repo="lumae", path="emotional-support.md", name="emotional-support", description="cbt", skill_id="id_high1", url="u2"),
            Candidate(owner="irrel2", repo="r2", path="p2.md", name="tool2", description="desc", skill_id="id2", url="u3"),
            Candidate(owner="rel1", repo="lumae", path="cbt-journal.md", name="cbt-journal", description="cbt", skill_id="id_high2", url="u4"),
            Candidate(owner="irrel3", repo="r3", path="p3.md", name="tool3", description="desc", skill_id="id3", url="u5"),
            Candidate(owner="rel2", repo="antar", path="support-chat.md", name="support-chat", description="support", skill_id="id_high3", url="u6"),
            Candidate(owner="irrel4", repo="r4", path="p4.md", name="tool4", description="desc", skill_id="id4", url="u7"),
            Candidate(owner="irrel5", repo="r5", path="p5.md", name="tool5", description="desc", skill_id="id5", url="u8"),
        ]

        scheduled_1 = schedule_candidates_by_relevance_and_fairness(cands, terms)
        scheduled_2 = schedule_candidates_by_relevance_and_fairness(cands, terms)

        # 1. 确定性测试：输入相同，输出顺序必须 100% 一致
        self.assertEqual([c.skill_id for c in scheduled_1], [c.skill_id for c in scheduled_2])

        # 2. 去重测试：结果不重复且数量完整
        self.assertEqual(len(scheduled_1), len(cands))
        self.assertEqual(len(set(c.skill_id for c in scheduled_1)), len(cands))

        # 3. 高相关候选应排在靠前位置（前 4 个 slot 中高相关优先）
        top_4_ids = [c.skill_id for c in scheduled_1[:4]]
        self.assertIn("id_high1", top_4_ids)
        self.assertIn("id_high2", top_4_ids)
        self.assertIn("id_high3", top_4_ids)

    def test_schedule_candidates_fairly_with_term_weights_delegation(self):
        """测试 search.py 中 schedule_candidates_fairly 对 term_weights 的无缝转接与回退。"""
        cands = [
            Candidate(owner="r1", repo="a", path="p1.md", name="apple", description="desc", skill_id="id1", url="u1"),
            Candidate(owner="r2", repo="b", path="p2.md", name="banana", description="desc", skill_id="id2", url="u2"),
        ]

        # 传 term_weights 时应调用新混合调度器
        res_weighted = schedule_candidates_fairly(cands, term_weights={"banana": 3.0})
        # banana 相关性高，应排在第 1
        self.assertEqual(res_weighted[0].skill_id, "id2")

        # 未传 term_weights 时回退原生公平轮转
        res_default = schedule_candidates_fairly(cands)
        self.assertEqual(len(res_default), 2)


if __name__ == "__main__":
    unittest.main()
