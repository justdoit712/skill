"""技能目录核心单元测试：入库与发布、周配额上限、投影崩溃恢复、并发计费一致性与单条失败隔离。

保留 5 项核心行为：
1. 入库与发布（mutate_catalog 同步更新主数据与公开投影）。
2. 预算上限（周配额账本超额时严格抛出 QuotaExceeded）。
3. 崩溃恢复（recover_catalog_projections 在前端丢失时自动重建）。
4. 并行计费（多线程评估中未知用量不会污染或错误回退已结算的对等条目）。
5. 失败隔离（单条候选处理失败记为 blocked，不导致全局任务崩溃中止）。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.catalog.budget import BudgetLedger, QuotaExceeded
from src.catalog.config import load_all_config
from src.catalog.index import build_page_data
from src.catalog.local import run_local
from src.catalog.local_candidate import _record_request_usage, process_candidate
from src.catalog.models import Candidate
from src.catalog.pool import CandidatePool, PoolItem, create_pool_from_candidates, load_pool, save_pool
from src.catalog.prescreen import PrescreenConfig
from src.catalog.store import mutate_catalog, recover_catalog_projections
from src.infra.files import read_json, write_json_atomic
from src.infra.llm import ModelCallResult
from tests import smoke

ROOT = Path(__file__).resolve().parents[1]


class CatalogTest(unittest.TestCase):
    """技能目录核心契约测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.state = self.root / "data/local/state"
        self.state.mkdir(parents=True, exist_ok=True)

        self.initial_cat = {
            "catalog_version": "1.0.0",
            "updated_at": "2026-09-30T00:00:00Z",
            "counts": {"total": 1, "recommended": 1, "candidate": 0},
            "entries": [
                {
                    "skill_id": "github:test/first-skill",
                    "name": "First Skill",
                    "url": "https://github.com/test/first-skill",
                    "repo_url": "https://github.com/test/first-skill",
                    "author": "test",
                    "status": "recommended",
                    "summary_zh": "测试技能",
                    "skill_type": "tool",
                    "example_requests": ["测试"],
                    "key_features": ["测试"],
                    "main_category": {"id": "dev", "name": "开发"},
                    "candidate_domains": [],
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

    @smoke
    def test_mutate_catalog_updates_both_data_and_public(self):
        """入库与发布：数据变更原子同步更新主数据与公开页面投影。"""
        def add(cat):
            new_entry = dict(cat["entries"][0])
            new_entry["skill_id"] = "github:test/second-skill"
            cat["entries"].append(new_entry)
            return cat

        updated = mutate_catalog(self.root, add)
        self.assertEqual(len(updated["entries"]), 2)
        public_data = read_json(self.root / "public/data/catalog.json")
        self.assertEqual(public_data["counts"]["recommended"], 2)

    def test_reservation_and_quota_cap(self):
        """预算上限：周额度预留超额时严格抛出 QuotaExceeded 异常。"""
        cap = 5
        led = BudgetLedger.load(self.state, cap)
        items = [{"evaluation_id": f"item{i}", "skill_id": f"s{i}"} for i in range(cap)]
        led.reserve(items)
        self.assertEqual(led.reserved_count, cap)
        self.assertEqual(led.remaining, 0)
        with self.assertRaises(QuotaExceeded):
            led.reserve([{"evaluation_id": "overflow", "skill_id": "so"}])

    def test_recover_catalog_projections_when_public_missing(self):
        """崩溃恢复：公开目录文件缺失时能够从内部主索引原子恢复。"""
        pub = self.root / "public/data/catalog.json"
        pub.unlink()
        self.assertFalse(pub.exists())

        recovered = recover_catalog_projections(self.root)
        self.assertTrue(recovered)
        self.assertTrue(pub.exists())

    def test_parallel_unknown_usage_does_not_mark_a_settled_peer_for_recovery(self):
        """并行计费一致性：某请求未知用量停止时，不影响已成功结算的对等评估。"""
        cfg = load_all_config(ROOT / "config")
        cfg["model"] = {
            "provider": "dashscope",
            "auth": {"api_key": "fake"},
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "models": ["model-a", "model-b"],
        }
        write_json_atomic(self.root / "config/models/model.json", cfg["model"])
        candidates = [
            Candidate(skill_id=f"test/{name}:SKILL.md", owner="test", repo=name, path="SKILL.md", url=f"https://fake/{name}")
            for name in ("known", "unknown")
        ]
        pool_path = self.root / "data/local/pool.json"
        save_pool(pool_path, create_pool_from_candidates(candidates))
        barrier, unknown_recorded = threading.Barrier(2), threading.Event()

        def model(cfg, system, user, **kwargs):
            barrier.wait(timeout=5)
            if "marker_known" in user:
                if not unknown_recorded.wait(timeout=5):
                    raise RuntimeError("peer usage callback did not run")
                return ModelCallResult(http_status=200, attempts=1, reason_code="RESPONSE_EMPTY", content="", usage={"total_tokens": 10})
            return ModelCallResult(http_status=400, attempts=1, reason_code="MODEL_ERROR", error="HTTP 400")

        def record_usage(state, call, *, retryable):
            _record_request_usage(state, call, retryable=retryable)
            if call.http_status == 400:
                unknown_recorded.set()

        options = {
            "target_recommended": 2,
            "max_total_tokens": 1000000,
            "max_consecutive_failures": 3,
            "pool_watermark": 1,
            "parallel_evaluation": True,
        }
        from src.infra.http import FetchResult
        sample_text = "---\nname: audit\ndescription: 审查代码\n---\n定位错误，给出修复建议及可重复验证的测试。\n无外部工具依赖。"

        with patch("src.catalog.evaluation.call_model", side_effect=model), \
             patch("src.catalog.local_candidate._record_request_usage", side_effect=record_usage):
            report = run_local(
                self.root,
                options,
                cfg=cfg,
                discover_fn=lambda *args, **kwargs: ([], []),
                fetch_fn=lambda url, **kwargs: FetchResult(
                    url=url,
                    ok=True,
                    text=sample_text + ("\nmarker_known" if "known" in url.split("/") else "\nmarker_unknown"),
                ),
                sleep=lambda _: None,
                log=lambda _: None,
            )

        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        records = [read_json(path) for path in (self.root / "data/local/state/evaluations").glob("*.json")]
        known = next(record for record in records if record["skill_id"] == candidates[0].skill_id)
        self.assertEqual(known["status"], "reserved", str(known["requests"]))
        self.assertEqual(known["requests"][0]["usage"]["total_tokens"], 10)
        self.assertEqual(load_pool(pool_path).items[0].status, "pending")

    def test_candidate_failure_isolates_and_does_not_halt_pool_run(self):
        """失败隔离：单条候选校验失败持久化为 blocked，不中止后续候选处理流程。"""
        local = self.root / "candidate_isolation"
        local.mkdir(parents=True, exist_ok=True)
        pool_path = local / "pool.json"
        c1 = Candidate(skill_id="o/r:a", owner="o", repo="r", path="a/SKILL.md", url="https://github.com/o/r/blob/HEAD/a/SKILL.md", name="s1")
        c2 = Candidate(skill_id="o/r:b", owner="o", repo="r", path="b/SKILL.md", url="https://github.com/o/r/blob/HEAD/b/SKILL.md", name="s2")
        item1 = PoolItem(candidate=c1, seq=1, status="pending")
        item2 = PoolItem(candidate=c2, seq=2, status="pending")
        pool = CandidatePool(items=[item1, item2])
        save_pool(pool_path, pool)

        ledger = BudgetLedger.load(local / "state", cap=1, max_attempts=2)
        state = SimpleNamespace(
            root=self.root,
            local=local,
            pool=pool,
            pool_path=pool_path,
            ledger=ledger,
            owned_ids=set(),
            skipped_owned_ids=set(),
            active_snoozed=set(),
            manual_exclusions=set(),
            manual_picks=set(),
            settings={"target_recommended": 10, "max_total_tokens": 100000, "max_format_failures_without_valid_result": 5, "max_consecutive_failures": 5},
            report={"new_recommended": 0, "budget_tokens": 0, "evaluations": 0, "checked": 0,
                    "not_skill_files": 0, "prescreen_excluded": 0, "fetch_failed": 0, "static_skipped": 0,
                    "cached": 0, "failed_evaluations": 0, "skipped_output_format": 0, "skipped_length_exceeded": 0,
                    "blocked_records": 0, "blocked_new": 0, "calls": [], "models_used": [], "stop_causes": [], "stop_reason": None},
            cfg={
                "model": {"provider": "dashscope", "endpoint": "https://example.test", "model": "test-m", "request": {"max_attempts": 2}, "auth": {"api_key": "fake"}},
                "rules": {"rules_version": "v1"},
                "taxonomy": {},
                "prescreen": PrescreenConfig(domain_names={"dev": "开发"}, manual_exclusions=set()),
            },
            stop_causes=set(),
            consecutive_failures=0,
            format_failures=0,
            max_format_failures=5,
            max_attempts=2,
            active_eid=None,
            active_call=None,
            unknown_reserve=0,
            pending_items=[item1, item2],
            evaluated_skill_ids=set(),
            entries={},
            fetch_fn=lambda url, **kw: SimpleNamespace(ok=True, text="---\nname: skill\n---\nbody", truncated=False),
            log=lambda msg: None,
            save=lambda: None,
            sleep=lambda s: None,
            model_pool=Mock(),
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

        call_mock = SimpleNamespace(
            ok=False,
            requested_model="test-m",
            billing_state=None,
            attempts=1,
            usage={"total_tokens": 50},
            reason_code="OUTPUT_FORMAT_INVALID",
            http_status=200,
            error="Invalid format",
            error_type=None,
            latency_ms=10,
        )
        state.evaluate_fn = Mock(return_value={
            "ok": False,
            "evaluation": None,
            "call": call_mock,
            "calls": [call_mock],
            "stage": "assessment",
            "reason_code": "OUTPUT_FORMAT_INVALID",
            "error_kind": "OUTPUT_FORMAT_INVALID",
            "error": "Invalid format",
        })

        cont = process_candidate(state, item1)
        self.assertTrue(cont, "单条候选失败不得中止后续候选处理")
        self.assertIsNone(state.report.get("stop_reason"), "单条候选失败不得设置全局 stop_reason")
        self.assertEqual(item1.status, "blocked", "格式校验失败候选应持久化为 blocked")
        self.assertEqual(state.report["skipped_output_format"], 1)


if __name__ == "__main__":
    unittest.main()
