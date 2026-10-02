"""技能目录评估流水线核心单元测试：条目状态机、候选池调度、持久化协调、流水线与容错恢复。

合并收敛自 test_entry_state.py, test_pool.py, test_store.py, test_failure_policy.py,
test_fault_recovery.py, test_pipeline.py, test_local_run.py, test_catalog_parallel.py,
test_p3_static_skip_and_batch_priority.py, test_p3_static_tiers_observation.py 与 test_p4_normalized_cache.py。
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.catalog.dedupe import candidate_from_repo
from src.catalog.entry_state import EntryUpdateEvent, STATUS_RECOMMENDED, update_entry
from src.catalog.failure_policy import (
    ACTION_DONE,
    ACTION_LENGTH_EXCEEDED,
    REASON_LENGTH_EXCEEDED,
    classify_result,
)
from src.catalog.index import CatalogContext, build_page_data
from src.catalog.models import Candidate
from src.catalog.pool import STATUS_DONE, create_pool_from_candidates, load_pool, save_pool, update_candidate_status
from src.catalog.store import mutate_catalog, recover_catalog_projections
from src.infra.files import read_json, write_json_atomic
from tests import smoke

ROOT = Path(__file__).resolve().parents[1]


@smoke
class EntryStateMachineTest(unittest.TestCase):
    """条目状态转移与元数据保留契约。"""
    def setUp(self):
        self.context = CatalogContext(
            rules_version="1.0.1",
            domain_names={"programming": "编程开发"},
            source_types={"official": "官方"},
            generated_at="2026-09-23T12:00:00",
        )

    def test_same_fingerprint_preserves_summary_and_category(self):
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:1111",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "既有中文摘要",
            "main_category": {"id": "programming", "name": "编程开发"},
            "first_seen": "2026-09-01T00:00:00",
            "last_checked": "2026-09-20T00:00:00",
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            name="widget",
            content_fingerprint="sha256:1111",
        )
        event = EntryUpdateEvent(
            kind="no_evaluation",
            fetched_fingerprint="sha256:1111",
            rules_version="1.0.1",
        )
        updated = update_entry(previous, cand, event, self.context)
        self.assertEqual(updated["status"], STATUS_RECOMMENDED)
        self.assertEqual(updated["summary_zh"], "既有中文摘要")
        self.assertEqual(updated["main_category"], {"id": "programming", "name": "编程开发"})


@smoke
class CandidatePoolTest(unittest.TestCase):
    """候选池创建、状态更新与读写往返。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pool_save_and_load(self):
        cand = candidate_from_repo("test-owner", "tool-repo", path="skills/tool/SKILL.md")
        cand.content_fingerprint = "sha256:abcd"
        pool = create_pool_from_candidates([cand])
        pool_file = self.root / "data/state/candidate_pool.json"

        save_pool(pool_file, pool)
        self.assertTrue(pool_file.exists())

        loaded = load_pool(pool_file)
        self.assertEqual(len(loaded.items), 1)
        self.assertEqual(loaded.items[0].candidate.skill_id, cand.skill_id)

    def test_update_candidate_status(self):
        cand = candidate_from_repo("test-owner", "tool-repo", path="skills/tool/SKILL.md")
        pool = create_pool_from_candidates([cand])
        self.assertEqual(pool.items[0].status, "pending")

        update_candidate_status(pool, 0, STATUS_DONE, checked_at="2026-09-30T10:00:00")
        self.assertEqual(pool.items[0].status, STATUS_DONE)
        self.assertEqual(pool.items[0].checked_at, "2026-09-30T10:00:00")

    def test_empty_responses_pause_without_blocking_or_losing_usage(self):
        from src.catalog.config import load_all_config
        from src.catalog.local import run_local
        from src.infra.http import FetchResult
        from src.infra.llm import ModelCallResult
        from tests.test_catalog_quality import TEXT

        cfg = load_all_config(ROOT / 'config')
        cfg['model'] = {'provider': 'dashscope', 'auth': {'api_key': 'fake'},
            'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
            'models': ['model-a', 'model-b']}
        settings = {'target_recommended': 1, 'max_total_tokens': 1000000,
                    'max_consecutive_failures': 3, 'pool_watermark': 1, 'parallel_evaluation': False}
        for tokens, calls, stop, status in ((53, 4, 'models_cooling_down', 'reserved'),
                                            (None, 1, 'usage_unknown', 'needs_recovery')):
            with self.subTest(tokens=tokens):
                root = self.root / str(tokens)
                write_json_atomic(root / 'config/models/model.json', cfg['model'])
                candidate = Candidate(skill_id='test/audit:SKILL.md', owner='test', repo='audit', path='SKILL.md')
                save_pool(root / 'data/local/pool.json', create_pool_from_candidates([candidate]))
                empty = ModelCallResult(http_status=200, attempts=1, reason_code='RESPONSE_EMPTY',
                                        usage={'total_tokens': tokens} if tokens is not None else {})
                with patch('src.catalog.evaluation.call_model', return_value=empty):
                    report = run_local(root, settings, cfg=cfg,
                        discover_fn=lambda *args, **kwargs: ([], []),
                        fetch_fn=lambda *args, **kwargs: FetchResult(url='fake', ok=True, text=TEXT),
                        sleep=lambda _: None, log=lambda _: None)
                record = read_json(next((root / 'data/local/state/evaluations').glob('*.json')))
                self.assertEqual((report['stop_reason'], load_pool(root / 'data/local/pool.json').items[0].status,
                                  record['status']), (stop, 'pending', status))
                self.assertEqual((len(record['requests']), report['usage']['total_tokens']),
                                 (calls, calls * (tokens or 0)))


@smoke
class CatalogStoreTest(unittest.TestCase):
    """目录修改事务、文件锁互斥与投影恢复。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir(parents=True)
        (self.root / "public/data").mkdir(parents=True)
        self.initial_cat = {
            "catalog_version": "1.0.0",
            "rules_version": "1.0.0",
            "generated_at": "2026-09-30T10:00:00",
            "counts": {"recommended": 1},
            "entries": [
                {
                    "skill_id": "test/tool:SKILL.md",
                    "name": "tool",
                    "url": "https://github.com/test/tool",
                    "author": "test",
                    "summary_zh": "测试工具",
                    "status": "recommended",
                    "main_category": {"id": "dev", "name": "开发"},
                    "tags": ["utility"],
                    "needs_review": False,
                    "review_note": None,
                    "pending_review": None,
                    "reason_codes": [],
                    "limitations": None,
                    "first_seen": "2026-09-01",
                    "last_checked": "2026-09-30",
                    "content_changed_at": None,
                    "upstream_status": "ok",
                    "license": "MIT",
                    "source_type": "official",
                    "dependencies_declared": [],
                    "platform_declared": None,
                }
            ],
        }
        write_json_atomic(self.root / "data/catalog.json", self.initial_cat)
        write_json_atomic(self.root / "public/data/catalog.json", build_page_data(self.initial_cat))

    def tearDown(self):
        self.tmp.cleanup()

    def test_mutate_catalog_updates_both_data_and_public(self):
        def add(cat):
            new_entry = dict(cat["entries"][0])
            new_entry["skill_id"] = "github:test/second-skill"
            cat["entries"].append(new_entry)
            return cat

        updated = mutate_catalog(self.root, add)
        self.assertEqual(len(updated["entries"]), 2)
        public_data = read_json(self.root / "public/data/catalog.json")
        self.assertEqual(public_data["counts"]["recommended"], 2)

    def test_recover_catalog_projections_when_public_missing(self):
        pub = self.root / "public/data/catalog.json"
        pub.unlink()
        self.assertFalse(pub.exists())

        recovered = recover_catalog_projections(self.root)
        self.assertTrue(recovered)
        self.assertTrue(pub.exists())


@smoke
class FailurePolicyTest(unittest.TestCase):
    """异常分类与重试策略契约。"""
    def test_classify_success(self):
        result = {"ok": True, "evaluation": {"some": "data"}, "stage": "review"}
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_DONE)
        self.assertEqual(decision.category, "success")
        self.assertFalse(decision.is_format_error)
        self.assertFalse(decision.is_length_exceeded)
        self.assertFalse(decision.is_service_failure)

    def test_classify_length_exceeded(self):
        from src.infra.llm import ModelCallResult
        call = ModelCallResult(finish_reason="length", reason_code=REASON_LENGTH_EXCEEDED)
        result = {"ok": False, "reason_code": REASON_LENGTH_EXCEEDED, "call": call}
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_LENGTH_EXCEEDED)
        self.assertTrue(decision.is_length_exceeded)


@smoke
class BudgetLedgerTest(unittest.TestCase):
    """周额度账本与生命周期契约。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_week_id_format(self):
        from src.catalog.budget import week_id
        self.assertRegex(week_id(), r"^\d{4}-W\d{2}$")

    def test_reservation_and_quota_cap(self):
        from src.catalog.budget import BudgetLedger, QuotaExceeded
        cap = 5
        led = BudgetLedger.load(self.state, cap)
        items = [{"evaluation_id": f"item{i}", "skill_id": f"s{i}"} for i in range(cap)]
        led.reserve(items)
        self.assertEqual(led.reserved_count, cap)
        self.assertEqual(led.remaining, 0)
        with self.assertRaises(QuotaExceeded):
            led.reserve([{"evaluation_id": "overflow", "skill_id": "so"}])

    def test_reservation_survives_crash_and_recovers(self):
        from src.catalog.budget import BudgetLedger, STATUS_NEEDS_RECOVERY
        led = BudgetLedger.load(self.state, 10)
        led.reserve([{"evaluation_id": "c1", "skill_id": "s1"}])
        led.begin_attempt("c1")
        # Reload fresh instance
        fresh = BudgetLedger.load(self.state, 10)
        self.assertEqual(fresh.reserved_count, 1)
        recovered = fresh.mark_in_progress_as_needs_recovery()
        self.assertEqual(recovered, ["c1"])
        self.assertEqual(fresh.get("c1")["status"], STATUS_NEEDS_RECOVERY)


if __name__ == "__main__":
    unittest.main()
