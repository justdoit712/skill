"""本地目标、停止额度和费用记录的离线测试；不读取真实密钥、不联网。"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from src.dedupe import candidate_from_repo
from src.evaluate import ModelCallResult
from src.fetch import FetchResult
from src.local_run import run_local
from src.pipeline import load_all_config
from src.usage import UsageTotals

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/evaluations.json").read_text(encoding="utf-8"))
PASS = next(c["evaluation"] for c in FIXTURES["cases"] if c["id"] == "low_star_complete")


class UsageTest(unittest.TestCase):
    def test_reasoning_is_subset_and_missing_total_can_be_derived(self):
        totals = UsageTotals()
        totals.add(ModelCallResult(usage={"prompt_tokens": 100, "completion_tokens": 40,
                                         "completion_tokens_details": {"reasoning_tokens": 30}}, attempts=1))
        self.assertEqual(totals.total_tokens, 140)
        self.assertEqual(totals.reasoning_tokens, 30)
        self.assertEqual(totals.unknown_usage_requests, 0)

    def test_retries_and_missing_usage_are_not_reported_as_free(self):
        totals = UsageTotals()
        totals.add(ModelCallResult(usage={"total_tokens": 20}, attempts=2))
        totals.add(None)
        self.assertEqual(totals.total_tokens, 20)
        self.assertEqual(totals.unknown_usage_requests, 2)
        self.assertEqual(totals.requests, 3)
        self.assertEqual(totals.incomplete_breakdown_requests, 2)


class LocalRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        for path in (ROOT / "config").glob("*.json"):
            if not path.name.endswith(".local.json"):
                shutil.copyfile(path, self.root / "config" / path.name)
        self.cfg = load_all_config(self.root / "config")
        self.cfg["model"]["auth"] = {"api_key": "test-secret-never-print", "api_key_env": "SKILL_TEST_UNUSED_KEY"}
        self.settings = {"target_recommended": 2, "max_total_tokens": 100000000,
                         "max_evaluations": None, "limit_queries": 0,
                         "expand_limit": None, "max_consecutive_failures": 3, "max_retries": 0}
        self.calls = []
        self.urls = []
        self.logs = []

    def candidate(self, i):
        path = f"skills/tool-{i}/SKILL.md"
        return candidate_from_repo("example", "skills", path=path,
                                   url=f"https://github.com/example/skills/blob/HEAD/{path}",
                                   name=f"tool-{i}")

    def fetch(self, url, **kwargs):
        self.urls.append(url)
        return FetchResult(url=url, ok=True, text="---\nname: helper\ndescription: 代码审查\n---\n" + "审查代码步骤和示例。\n" * 40)

    def evaluate(self, candidate, text, **kwargs):
        self.assertEqual(kwargs["model_cfg"]["request"]["max_attempts"], 1)
        self.calls.append(candidate.skill_id)
        ev = deepcopy(PASS)
        ev.update(main_category="programming", summary_zh="用于代码审查。",
                  rules_version=self.cfg["rules"]["rules_version"], source_fingerprint=candidate.content_fingerprint)
        return {"ok": True, "evaluation": ev,
                "call": ModelCallResult(usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}, attempts=1)}

    def collect(self, count=6, evaluate_fn=None, fetch_fn=None, candidates=None, discover_fn=None):
        return run_local(self.root, self.settings, cfg=self.cfg,
                         discover_fn=discover_fn or (lambda *a, **kw: (candidates if candidates is not None else [self.candidate(i) for i in range(count)], [])),
                         fetch_fn=fetch_fn or self.fetch, evaluate_fn=evaluate_fn or self.evaluate,
                         log=self.logs.append, sleep=lambda n: None)

    def test_50_recommendations_can_require_more_than_50_evaluations(self):
        self.settings["target_recommended"] = 50
        def mixed(candidate, text, **kwargs):
            result = self.evaluate(candidate, text, **kwargs)
            if len(self.calls) <= 5:
                result["evaluation"]["dependency_transparency"] = {"value": "unknown", "evidence": "未声明"}
            return result
        report = self.collect(count=60, evaluate_fn=mixed)
        self.assertEqual(report["stop_reason"], "target_reached", report)
        self.assertEqual(report["evaluations"], 55)
        self.assertEqual(report["new_recommended"], 50)
        self.assertEqual(report["usage"]["total_tokens"], 5500)
        self.assertFalse((self.root / "data/state/budget.json").exists())
        text = Path(report["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("test-secret-never-print", text)
        self.assertTrue(all(u.startswith("https://raw.githubusercontent.com/") for u in self.urls))

    def test_token_limit_stops_following_request(self):
        self.settings.update(target_recommended=5, max_total_tokens=150)
        report = self.collect()
        self.assertEqual(report["stop_reason"], "token_limit")
        self.assertEqual(report["usage"]["total_tokens"], 200)
        self.assertEqual(len(self.calls), 2)

    def test_missing_usage_stops_even_successful_response(self):
        def missing(*args, **kwargs):
            result = self.evaluate(*args, **kwargs)
            result["call"] = ModelCallResult()
            return result
        report = self.collect(evaluate_fn=missing)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_failure_tokens_count_and_consecutive_failure_stops(self):
        def failure(candidate, text, **kwargs):
            result = self.evaluate(candidate, text, **kwargs)
            result.update(ok=False, reason_code="PARSE_ERROR", error="untrusted echo test-secret-never-print")
            return result
        report = self.collect(evaluate_fn=failure)
        self.assertEqual(report["stop_reason"], "model_failures")
        self.assertEqual(report["usage"]["total_tokens"], 300)
        self.assertEqual(report["failed_evaluations"], 3)
        self.assertNotIn("test-secret-never-print", json.dumps(report))

    def test_network_failure_logs_safe_details_and_stops_on_unknown_usage(self):
        def failure(candidate, text, **kwargs):
            self.calls.append(candidate.skill_id)
            return {"ok": False, "reason_code": "NETWORK_ERROR", "error": "test-secret-never-print",
                    "call": ModelCallResult(error_type="ReadTimeout", latency_ms=30000, attempts=1,
                                            error="test-secret-never-print")}
        report = self.collect(evaluate_fn=failure)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["calls"][0]["diagnostics"]["error_type"], "ReadTimeout")
        self.assertEqual(report["calls"][0]["diagnostics"]["latency_ms"], 30000)
        messages = "\n".join(self.logs)
        self.assertIn("NETWORK_ERROR", messages)
        self.assertIn("ReadTimeout", messages)
        self.assertIn("30.0", messages)
        self.assertNotIn("test-secret-never-print", messages + json.dumps(report))

    def network_failure(self, candidate, text, **kwargs):
        self.calls.append(candidate.skill_id)
        return {"ok": False, "reason_code": "NETWORK_ERROR", "error": "private detail",
                "call": ModelCallResult(error_type="ReadTimeout", attempts=1)}

    def test_five_retries_recover_then_continue_to_next_skill(self):
        self.settings["max_retries"] = 5
        def recover(candidate, text, **kwargs):
            if len(self.calls) < 5:
                return self.network_failure(candidate, text, **kwargs)
            return self.evaluate(candidate, text, **kwargs)
        report = self.collect(count=3, evaluate_fn=recover)
        self.assertEqual(report["stop_reason"], "target_reached", report)
        self.assertEqual(report["new_recommended"], 2)
        self.assertEqual(report["evaluations"], 2)
        self.assertEqual(report["usage"]["requests"], 7)
        self.assertEqual(report["usage"]["total_tokens"], 200)
        self.assertEqual(report["usage"]["unknown_usage_requests"], 5)
        self.assertGreater(report["unknown_usage_reserved_tokens"], 0)
        self.assertEqual(report["budget_tokens"], 200 + report["unknown_usage_reserved_tokens"])
        self.assertIn("重连 5/5", "\n".join(self.logs))
        self.assertEqual([c["attempt"] for c in report["calls"]], [1, 2, 3, 4, 5, 6, 1])

    def test_retry_exhaustion_stops_after_six_requests_and_rerun_does_not_reset(self):
        self.settings["max_retries"] = 5
        first = self.collect(count=1, evaluate_fn=self.network_failure)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(first["stop_reason"], "retry_exhausted")
        self.assertEqual(first["failed_evaluations"], 1)
        self.assertEqual(first["failed_requests"], 6)
        second = self.collect(count=1)
        self.assertEqual(second["evaluations"], 0)
        self.assertEqual(second["blocked_records"], 1)
        self.assertEqual(len(self.calls), 6)

    def test_new_retry_setting_can_resume_previously_failed_request(self):
        first = self.collect(count=1, evaluate_fn=self.network_failure)
        self.assertEqual(first["usage"]["requests"], 1)
        self.settings.update(max_retries=5, target_recommended=1)
        second = self.collect(count=1)
        self.assertEqual(second["stop_reason"], "target_reached")
        self.assertEqual(second["calls"][0]["attempt"], 2)
        self.assertEqual(second["usage"]["requests"], 1)

    def test_unknown_usage_reservation_can_stop_retries_at_budget(self):
        self.settings.update(max_retries=5, max_total_tokens=150)
        report = self.collect(evaluate_fn=self.network_failure)
        self.assertEqual(report["stop_reason"], "token_limit")
        self.assertEqual(len(self.calls), 1)
        self.assertGreaterEqual(report["budget_tokens"], 150)
        self.assertEqual(report["usage"]["total_tokens"], 0)

    def test_non_retryable_http_error_does_not_retry(self):
        self.settings["max_retries"] = 5
        def unauthorized(candidate, text, **kwargs):
            self.calls.append(candidate.skill_id)
            return {"ok": False, "reason_code": "MODEL_ERROR", "error": "private detail",
                    "call": ModelCallResult(http_status=401, attempts=1)}
        report = self.collect(evaluate_fn=unauthorized)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertIn("HTTP 401", "\n".join(self.logs))

    def test_retryable_http_response_counts_all_reported_tokens(self):
        self.settings.update(max_retries=5, target_recommended=1)
        def temporary(candidate, text, **kwargs):
            result = self.evaluate(candidate, text, **kwargs)
            if len(self.calls) == 1:
                result.update(ok=False, reason_code="MODEL_ERROR")
                result["call"].http_status = 503
            return result
        report = self.collect(evaluate_fn=temporary)
        self.assertEqual(report["stop_reason"], "target_reached")
        self.assertEqual(report["usage"]["total_tokens"], 200)
        self.assertEqual(report["usage"]["requests"], 2)
        self.assertEqual(report["unknown_usage_reserved_tokens"], 0)

    def test_rerun_reuses_evaluations_and_counts_only_new_recommendations(self):
        first = self.collect(count=3)
        second = self.collect(count=3)
        self.assertEqual(first["new_recommended"], 2)
        self.assertEqual(second["new_recommended"], 1)
        self.assertEqual(second["evaluations"], 1)
        self.assertEqual(second["usage"]["total_tokens"], 100)
        self.assertEqual(second["stop_reason"], "candidates_exhausted")
        self.assertEqual(len(set(self.calls)), 3)

    def test_interrupt_saves_known_tokens_and_unknown_inflight_and_prevents_retry(self):
        self.settings["target_recommended"] = 5
        def interrupt(*args, **kwargs):
            if self.calls:
                raise KeyboardInterrupt()
            return self.evaluate(*args, **kwargs)
        report = self.collect(count=2, evaluate_fn=interrupt)
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertEqual(report["usage"]["total_tokens"], 100)
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        again = self.collect(count=2)
        self.assertEqual(again["evaluations"], 0)
        self.assertEqual(again["blocked_records"], 1)
        self.assertFalse((self.root / "data/local/run.lock").exists())

    def test_truncated_text_and_unexpanded_repo_never_call_model(self):
        def truncated(url, **kwargs):
            return FetchResult(url=url, ok=True, text="partial", truncated=True)
        report = self.collect(count=2, fetch_fn=truncated)
        self.assertEqual(report["fetch_failed"], 2)
        self.assertEqual(report["evaluations"], 0)
        report = self.collect(candidates=[candidate_from_repo("example", "repo")])
        self.assertEqual(report["not_skill_files"], 1)
        self.assertEqual(report["evaluations"], 0)

    def test_seed_with_known_skill_path_reads_file_not_repository_home(self):
        candidate = self.candidate(0)
        candidate.url = candidate.repo_url
        report = self.collect(candidates=[candidate])
        self.assertEqual(report["new_recommended"], 1)
        self.assertEqual(self.urls, ["https://raw.githubusercontent.com/example/skills/HEAD/skills/tool-0/SKILL.md"])
        self.assertIn("/blob/HEAD/skills/tool-0/SKILL.md", report["recommendations"][0]["url"])

    def test_no_candidates_preserves_existing_catalog(self):
        self.collect(count=2)
        old = (self.root / "data/catalog.json").read_bytes()
        report = self.collect(count=0)
        self.assertEqual(report["stop_reason"], "candidates_exhausted")
        self.assertEqual((self.root / "data/catalog.json").read_bytes(), old)

    def test_optional_evaluation_cap_and_lock(self):
        self.settings.update(target_recommended=5, max_evaluations=1)
        report = self.collect()
        self.assertEqual(report["stop_reason"], "evaluation_limit")
        self.assertEqual(report["evaluations"], 1)
        (self.root / "data/local/run.lock").write_text("active")
        with self.assertRaisesRegex(ValueError, "已有本地任务"):
            self.collect()

    def test_pool_persistence_and_resuming_from_breakpoint(self):
        """验证断点续跑：第一轮处理前 2 个，第二轮直接跳过网络搜索，从第 3 个（tool-2）继续。"""
        # 第一轮：目标 2 个，池子放入 5 个候选
        first = self.collect(count=5)
        self.assertEqual(first["stop_reason"], "target_reached")
        self.assertEqual(first["new_recommended"], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls, [
            "example/skills:skills/tool-0/SKILL.md",
            "example/skills:skills/tool-1/SKILL.md",
        ])

        pool_file = self.root / "data" / "local" / "pool.json"
        self.assertTrue(pool_file.exists())
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["total"], 5)
        self.assertEqual(pool_data["stats"]["done"], 2)
        self.assertEqual(pool_data["stats"]["pending"], 3)

        # 第二轮：设置 watermark=1（pending 3 >= 1，跳过搜索），discover_fn 若被调用直接报错
        self.settings.update(target_recommended=2, pool_watermark=1)
        second_calls = []
        def fail_if_searched(*args, **kwargs):
            raise AssertionError("当待处理候选充足时，不应调用网络搜索！")

        second = run_local(
            self.root, self.settings, cfg=self.cfg,
            discover_fn=fail_if_searched,
            fetch_fn=self.fetch,
            evaluate_fn=self.evaluate,
            log=self.logs.append, sleep=lambda n: None,
        )

        self.assertEqual(second["stop_reason"], "target_reached")
        self.assertEqual(second["new_recommended"], 2)
        # 本轮只应评估 tool-2 与 tool-3，绝不重复评估 tool-0 或 tool-1
        self.assertEqual(self.calls[2:], [
            "example/skills:skills/tool-2/SKILL.md",
            "example/skills:skills/tool-3/SKILL.md",
        ])

        # 核验 pool.json 更新：前 4 个 done，最后 1 个 pending
        pool_data2 = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data2["stats"]["done"], 4)
        self.assertEqual(pool_data2["stats"]["pending"], 1)

    def test_pool_watermark_triggers_refill_when_pending_low(self):
        """当待处理候选低于水位线时，触发增量补水。"""
        # 第一轮：2 个候选，全部处理完毕
        self.collect(count=2)
        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["pending"], 0)

        # 第二轮：待处理为 0 < watermark(20)，提供新候选 tool-2, tool-3
        discovered_flag = []
        def refill_discover(*args, **kwargs):
            discovered_flag.append(True)
            return ([self.candidate(i) for i in range(4)], [])

        self.settings.update(target_recommended=1, pool_watermark=20)
        self.collect(discover_fn=refill_discover)
        self.assertTrue(discovered_flag, "低于水位线时应触发增量补水")

        pool_data2 = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data2["stats"]["total"], 4)
        self.assertEqual(pool_data2["stats"]["done"], 3)  # tool-0, 1, 2 done
        self.assertEqual(pool_data2["stats"]["pending"], 1)  # tool-3 pending

    def test_refresh_pool_forces_rebuild(self):
        """--refresh-pool 强制丢弃旧池并重新构建。"""
        self.collect(count=4)
        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["done"], 2)

        # 设置 refresh_pool 标志
        self.settings["refresh_pool"] = True
        self.calls.clear()
        refreshed = self.collect(count=4)
        self.assertEqual(refreshed["stop_reason"], "target_reached")
        # 刷新后重新排布优先级：已有推荐排在后面，未推荐的 tool-2、tool-3 优先评估并达成目标
        self.assertEqual(refreshed["new_recommended"], 2)
        self.assertEqual(self.calls, [
            "example/skills:skills/tool-2/SKILL.md",
            "example/skills:skills/tool-3/SKILL.md",
        ])
        pool_data2 = json.loads(pool_file.read_text(encoding="utf-8"))
        # 新池优先评估了 tool-2 和 tool-3，因此 done 为 2，已有推荐待处理 pending 为 2
        self.assertEqual(pool_data2["stats"]["done"], 2)
        self.assertEqual(pool_data2["stats"]["pending"], 2)
        self.assertEqual([c["candidate"]["name"] for c in pool_data2["candidates"][:2]], ["tool-2", "tool-3"])


if __name__ == "__main__":
    unittest.main()

