import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.catalog.models import Candidate, PrescreenResult
from src.catalog.prescreen import (
    PrescreenConfig,
    analyze_static_tier,
    prescreen,
    should_static_skip,
    TIER_CLEAR_PLACEHOLDER,
    TIER_NORMAL,
    TIER_SUSPECT,
    TIER_PENDING,
    TIER_CONTENT_DEFECT,
    ACTION_SUGGEST_SKIP,
    ACTION_SUGGEST_REVIEW,
    ACTION_SUGGEST_EVALUATE,
    ACTION_SUGGEST_PENDING,
)
from src.catalog.pool import (
    CandidatePool,
    PoolItem,
    STATUS_PENDING,
    STATUS_STATIC_SKIPPED,
    prioritize_pending_batch,
)
from src.catalog.queue import ordered_pending
from src.shared.metrics import build_run_metrics
from src.shared.versions import STATIC_HEURISTIC_VERSION
from tests.fixtures.benchmark_samples import DOCUMENT_BENCHMARK_SAMPLES


class TestStaticSkipAndBatchPriority(unittest.TestCase):
    """Unit 7 / P3.2: 明确空壳跳过与批内排序及防饥饿测试。"""

    def setUp(self):
        self.cfg = PrescreenConfig(
            taxonomy={},
            rules={"rules_version": "1.0.0"},
            self_repos=set(),
            out_of_scope_repos=set(),
            out_of_scope_orgs=set(),
            domain_terms={"tools": ["calc", "sync", "tool", "clean"]},
            domain_names={"tools": "工具类"},
            manual_exclusions=set(),
        )

    def test_should_static_skip_safety_guarantees(self):
        """严格防误拦截：仅在开关明确开启且为纯占位时跳过，其余一律放行。"""
        # 1. 开关关闭（默认观察模式）：任何输入一律不跳过
        obs_placeholder = {
            "tier": TIER_CLEAR_PLACEHOLDER,
            "suggested_action": ACTION_SUGGEST_SKIP,
        }
        self.assertFalse(should_static_skip(obs_placeholder, enabled=False))
        self.assertFalse(should_static_skip(None, enabled=True))

        # 2. 开关开启时：纯占位明确跳过
        self.assertTrue(should_static_skip(obs_placeholder, enabled=True))

        # 3. 短而有效（绝不可跳过）
        sample_short = DOCUMENT_BENCHMARK_SAMPLES["short_and_valid"]
        c_short = Candidate(skill_id="test/repo:" + sample_short["path"], owner="test", repo="repo", path=sample_short["path"], name="mini-calc")
        obs_short = analyze_static_tier(c_short, sample_short["content"])
        self.assertFalse(should_static_skip(obs_short, enabled=True))

        # 4. 缺失 Frontmatter（绝不可跳过）
        sample_no_fm = DOCUMENT_BENCHMARK_SAMPLES["no_frontmatter"]
        c_no_fm = Candidate(skill_id="test/repo:" + sample_no_fm["path"], owner="test", repo="repo", path=sample_no_fm["path"], name="clean-tool")
        obs_no_fm = analyze_static_tier(c_no_fm, sample_no_fm["content"])
        self.assertFalse(should_static_skip(obs_no_fm, enabled=True))

        # 5. 正文包含未来路线图 TODO（绝不可跳过）
        sample_todo = DOCUMENT_BENCHMARK_SAMPLES["body_with_todo"]
        c_todo = Candidate(skill_id="test/repo:" + sample_todo["path"], owner="test", repo="repo", path=sample_todo["path"], name="sync-tool")
        obs_todo = analyze_static_tier(c_todo, sample_todo["content"])
        self.assertFalse(should_static_skip(obs_todo, enabled=True))

        # 6. 敏感合规词（需重点复核，绝不可跳过）
        obs_suspect = {
            "tier": TIER_SUSPECT,
            "suggested_action": ACTION_SUGGEST_REVIEW,
        }
        self.assertFalse(should_static_skip(obs_suspect, enabled=True))

        # 7. 待抓取材料（绝不可跳过）
        obs_pending = {
            "tier": TIER_PENDING,
            "suggested_action": ACTION_SUGGEST_PENDING,
        }
        self.assertFalse(should_static_skip(obs_pending, enabled=True))

    def test_prioritize_pending_batch_and_starvation_prevention(self):
        """测试批内优先级调整及跨批次防饥饿（窗口隔离）。"""
        # 构建 45 个待处理条目：批次 0 (0..19), 批次 1 (20..39), 批次 2 (40..44)
        items = []
        for i in range(45):
            c = Candidate(
                skill_id=f"test/repo:skills/tool_{i}/SKILL.md",
                owner="test",
                repo="repo",
                path=f"skills/tool_{i}/SKILL.md",
                name=f"tool_{i}",
                description="normal description",
            )
            items.append(PoolItem(seq=i, candidate=c, status=STATUS_PENDING))

        # 设定特定条目的特征：
        # 批次 0 中：
        # seq=5 为人工收藏（manual_pick）
        # seq=12 为敏感合规（auto-trade live-trading）
        items[5].candidate.name = "pick-5"
        items[12].candidate.description = "auto-trade place-order live-trading broker-api"

        # 批次 1 中：
        # seq=25 为人工收藏（manual_pick）
        items[25].candidate.name = "pick-25"

        manual_picks = {"test/repo:skills/tool_5/SKILL.md", "test/repo:skills/tool_25/SKILL.md"}

        # 1. 当 enabled=False 时，严格保持原有 seq 顺序
        disabled_order = prioritize_pending_batch(items, batch_size=20, manual_picks=manual_picks, enabled=False)
        self.assertEqual([it.seq for it in disabled_order], list(range(45)))

        # 2. 当 enabled=True 时：
        prioritized = prioritize_pending_batch(items, batch_size=20, manual_picks=manual_picks, enabled=True)
        self.assertEqual(len(prioritized), 45)

        # 验证批次 0 (前 20 条)：
        batch_0_seqs = [it.seq for it in prioritized[:20]]
        # 高优项 seq=5 与 seq=12 排在最前面
        self.assertEqual(batch_0_seqs[0], 5)
        self.assertEqual(batch_0_seqs[1], 12)
        # 其余普通条目按原 seq FIFO 顺序排列
        remaining_b0 = [s for s in batch_0_seqs if s not in (5, 12)]
        expected_b0_remaining = [s for s in range(20) if s not in (5, 12)]
        self.assertEqual(remaining_b0, expected_b0_remaining)

        # 核心防饥饿验证（窗口隔离）：
        # 批次 1 中的高优项 seq=25 绝对不能插队到批次 0 的普通条目（如 seq=0, 1）之前！
        self.assertNotIn(25, batch_0_seqs)

        # 验证批次 1 (20..39 条)：
        batch_1_seqs = [it.seq for it in prioritized[20:40]]
        self.assertEqual(batch_1_seqs[0], 25)  # seq=25 在其自身批次内排在最前
        remaining_b1 = [s for s in batch_1_seqs if s != 25]
        expected_b1_remaining = [s for s in range(20, 40) if s != 25]
        self.assertEqual(remaining_b1, expected_b1_remaining)

    def test_queue_ordered_pending_static_tier_incorporation(self):
        """测试 queue.py 的 ordered_pending 兼容静态分级档位。"""
        items = [
            {
                "candidate": {"skill_id": "c_normal/r:SKILL.md"},
                "prescreen": {"static_observation": {"tier": "tier_normal"}},
                "seq": 0,
            },
            {
                "candidate": {"skill_id": "c_suspect/r:SKILL.md"},
                "prescreen": {"static_observation": {"tier": "tier_suspect"}},
                "seq": 1,
            },
            {
                "candidate": {"skill_id": "c_placeholder/r:SKILL.md"},
                "prescreen": {"static_observation": {"tier": "tier_clear_placeholder"}},
                "seq": 2,
            },
            {
                "candidate": {"skill_id": "c_manual/r:SKILL.md"},
                "manual_pick": True,
                "seq": 3,
            },
        ]

        ordered = ordered_pending(
            items,
            catalogued={},
            source_types={},
            manual_picks={"c_manual/r:SKILL.md"},
        )
        ordered_ids = [it["candidate"]["skill_id"] for it in ordered]
        # manual_pick 和 suspect 排在前两位
        self.assertIn(ordered_ids[0], ("c_manual/r:SKILL.md", "c_suspect/r:SKILL.md"))
        self.assertIn(ordered_ids[1], ("c_manual/r:SKILL.md", "c_suspect/r:SKILL.md"))
        # normal 在第三位
        self.assertEqual(ordered_ids[2], "c_normal/r:SKILL.md")
        # 纯占位排在最后
        self.assertEqual(ordered_ids[3], "c_placeholder/r:SKILL.md")

    def test_static_skip_and_metrics_accounting(self):
        """测试静态跳过事实单独记录并正确沉淀至 build_run_metrics。"""
        report = {
            "run_id": "test_run_skip",
            "prescreen_excluded": 2,
            "static_skipped": 3,
            "static_heuristics": {
                "version": STATIC_HEURISTIC_VERSION,
                "observed_count": 5,
                "skipped_count": 3,
                "tier_counts": {TIER_CLEAR_PLACEHOLDER: 3, TIER_NORMAL: 2},
                "signal_counts": {"EXPLICIT_BOILERPLATE": 3},
                "suggested_actions": {ACTION_SUGGEST_SKIP: 3, ACTION_SUGGEST_EVALUATE: 2},
            },
        }

        metrics = build_run_metrics(report, kind="catalog")
        pres_metrics = metrics["prescreen"]
        # skipped_count = prescreen_excluded (2) + static_skipped (3) = 5
        self.assertEqual(pres_metrics["skipped_count"], 5)
        self.assertEqual(pres_metrics["recommended_actions"][ACTION_SUGGEST_SKIP], 3)
        self.assertEqual(pres_metrics["signal_hits"]["EXPLICIT_BOILERPLATE"], 3)


if __name__ == "__main__":
    unittest.main()
