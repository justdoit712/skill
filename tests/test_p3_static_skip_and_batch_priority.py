from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from src.catalog.config import load_all_config
from src.catalog.dedupe import candidate_from_repo
from src.catalog.local import run_local
from src.infra.http import FetchResult
from src.infra.llm import ModelCallResult

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

        # 8. Issue 6 回归测试：包含参考入口的短文档绝不可判为空壳跳过
        doc_with_ref = "# [Instructions](references/guide.md)\n"
        c_ref = Candidate(skill_id="test/repo:SKILL.md", owner="test", repo="repo", path="SKILL.md", name="ref-skill")
        obs_ref = analyze_static_tier(c_ref, doc_with_ref)
        self.assertNotEqual(obs_ref.get("tier"), TIER_CLEAR_PLACEHOLDER)
        self.assertFalse(should_static_skip(obs_ref, enabled=True))

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

    def test_prioritize_pending_batch_with_tier_map_and_anti_starvation_quota(self):
        """Issue 7 回归测试：基于正文静态分级排序，且 20% 防饥饿配额保证普通条目不被完全推至末尾。"""
        # 构造一个 20 条的单批次
        items = []
        tier_map = {}
        for i in range(20):
            c = Candidate(
                skill_id=f"test/repo:skills/tool_{i}/SKILL.md",
                owner="test",
                repo="repo",
                path=f"skills/tool_{i}/SKILL.md",
                name=f"tool_{i}",
                description="desc",
            )
            items.append(PoolItem(seq=i, candidate=c, status=STATUS_PENDING))
            # 设定 18 个为高质量正文 (tier_normal)，2 个为包含参考入口未读取 (tier_unassessed，如 seq=3, seq=10)
            if i in (3, 10):
                tier_map[c.skill_id] = "tier_unassessed"
            else:
                tier_map[c.skill_id] = "tier_normal"

        # 1. 默认关闭时，严格保持原 seq
        res_default = prioritize_pending_batch(items, batch_size=20, tier_map=tier_map, enabled=False)
        self.assertEqual([it.seq for it in res_default], list(range(20)))

        # 2. 开启时，按正文分级排序并应用 20% 配额（20 * 0.2 = 4，保证未提权项进入前 18 位而不会被所有 18 个提权项压到第 19、20 位）
        res_prioritized = prioritize_pending_batch(items, batch_size=20, tier_map=tier_map, enabled=True, anti_starvation_ratio=0.2)
        prioritized_seqs = [it.seq for it in res_prioritized]

        # 验证防饥饿：seq=3 与 seq=10 获得了保留配额，位置在前 18 位（具体在第 16、17 位），第 19、20 位是多出的 2 个提权项
        self.assertIn(prioritized_seqs[16], (3, 10))
        self.assertIn(prioritized_seqs[17], (3, 10))
        # 最后的第 18、19 位不应是未提权项（未提权项已在配额内处理完毕）
        self.assertNotIn(prioritized_seqs[18], (3, 10))
        self.assertNotIn(prioritized_seqs[19], (3, 10))

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


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/evaluations.json").read_text(encoding="utf-8"))
PASS = next(c["evaluation"] for c in FIXTURES["cases"] if c["id"] == "low_star_complete")


class TestLocalCollectMultiBatchPrioritization(unittest.TestCase):
    """Issue 1 回归测试：验证 local_collect 中 analyze_static_tier 导入正确性及跨批次正文分级排序。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        for path in (ROOT / "config").rglob("*.json"):
            if not path.name.endswith(".local.json"):
                rel = path.relative_to(ROOT / "config")
                dest = self.root / "config" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        self.cfg = load_all_config(self.root / "config")
        self.cfg["model"].update(endpoint="https://fake.invalid/v1/chat/completions", model="test-model")
        self.cfg["model"]["auth"] = {"api_key": "test-key", "api_key_env": "SKILL_TEST_KEY"}
        self.settings = {
            "target_recommended": 10,
            "max_total_tokens": 1000000,
            "max_evaluations": None,
            "limit_queries": 0,
            "batch_size": 2,
            "enable_batch_prioritization": True,
            "max_consecutive_failures": 3,
            "max_retries": 0,
        }
        self.calls = []
        self.fetch_calls = []

    def test_multi_batch_static_tier_and_prioritization(self):
        # 4 个候选跨越 2 个批次（batch_size=2）：
        # 批次 0：
        # tool-0: 占位空壳正文（tier_clear_placeholder）
        # tool-1: 丰富有效正文（tier_normal）
        # 批次 1：
        # tool-2: 占位空壳正文（tier_clear_placeholder）
        # tool-3: 丰富有效正文（tier_normal）
        candidates = [
            candidate_from_repo(
                "example",
                "skills",
                path=f"skills/tool-{i}/SKILL.md",
                url=f"https://github.com/example/skills/blob/HEAD/skills/tool-{i}/SKILL.md",
                name=f"tool-{i}",
                description="tool description",
            )
            for i in range(4)
        ]

        def mock_fetch(url, **kwargs):
            self.fetch_calls.append(url)
            if "tool-0" in url or "tool-2" in url:
                # 占位空壳
                content = "---\nname: placeholder\ndescription: demo\n---\n# Coming soon\nTODO: add content\n"
            else:
                # 丰富正文
                content = "---\nname: rich\ndescription: demo\n---\n# Rich Skill\n" + "Useful content for coding.\n" * 30
            return FetchResult(url=url, ok=True, text=content)

        def mock_evaluate(candidate, text, **kwargs):
            self.calls.append(candidate.name)
            ev = deepcopy(PASS)
            ev.update(
                main_category="programming",
                summary_zh="测试",
                rules_version=self.cfg["rules"]["rules_version"],
                source_fingerprint=candidate.content_fingerprint,
            )
            return {
                "ok": True,
                "evaluation": ev,
                "call": ModelCallResult(
                    usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    attempts=1,
                ),
            }

        report = run_local(
            self.root,
            self.settings,
            cfg=self.cfg,
            discover_fn=lambda *a, **kw: (candidates, []),
            fetch_fn=mock_fetch,
            evaluate_fn=mock_evaluate,
            log=lambda *a: None,
            sleep=lambda n: None,
        )

        # 批次 0 (tool-0, tool-1)：tool-1 (tier_normal) 应该被排在 tool-0 (tier_clear_placeholder) 前面评估！
        # 批次 1 (tool-2, tool-3)：tool-3 (tier_normal) 应该被排在 tool-2 (tier_clear_placeholder) 前面评估！
        self.assertEqual(self.calls, ["tool-1", "tool-0", "tool-3", "tool-2"])
        # 验证每个候选只被抓取了一次（预抓取存入了 state.batch_materials，process_candidate 直接复用）
        self.assertEqual(len(self.fetch_calls), 4)


if __name__ == "__main__":
    unittest.main()
