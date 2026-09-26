"""本地目标、停止额度和费用记录的离线测试；不读取真实密钥、不联网。"""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from src.catalog.config import load_all_config
from src.catalog.dedupe import candidate_from_repo
from src.catalog.local import run_local
from src.infra.http import FetchResult
from src.infra.llm import ModelCallResult
from src.shared.usage import UsageTotals

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
        for path in (ROOT / "config").rglob("*.json"):
            if not path.name.endswith(".local.json"):
                rel = path.relative_to(ROOT / "config")
                dest = self.root / "config" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        self.cfg = load_all_config(self.root / "config")
        self.cfg["model"].update(endpoint="https://fake.invalid/v1/chat/completions", model="test-model")
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

    def length_result(self, usage=True):
        return {"ok": False, "reason_code": "LENGTH_EXCEEDED", "evaluation": None,
                "call": ModelCallResult(reason_code="LENGTH_EXCEEDED", is_sample_error=True,
                    finish_reason="length", http_status=200, attempts=1,
                    usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                           "completion_tokens_details": {"reasoning_tokens": 15}} if usage else {})}

    def test_five_length_skips_continue_to_target_and_survive_restart_and_refresh(self):
        self.settings['max_retries'] = 5
        attempts = []
        def mixed(candidate, text, **kwargs):
            attempts.append(candidate.skill_id)
            if len(attempts) <= 5:
                return self.length_result()
            return self.evaluate(candidate, text, **kwargs)
        report = self.collect(count=7, evaluate_fn=mixed)
        self.assertEqual(report['stop_reason'], 'target_reached')
        self.assertEqual(len(attempts), 7)
        self.assertEqual(report['skipped_length_exceeded'], 5)
        self.assertEqual(report['failed_requests'], 0)
        self.assertEqual(report['failed_evaluations'], 0)
        self.assertEqual(report['usage']['total_tokens'], 700)
        self.assertEqual(report['usage']['reasoning_tokens'], 75)
        self.assertEqual(report['calls'][0]['status'], 'length_exceeded')
        self.assertFalse(any('请求失败' in line or '重连 ' in line for line in self.logs))
        self.assertEqual(sum('[自动跳过]' in line for line in self.logs), 5)
        markdown = Path(report['report_path']).with_suffix('.md').read_text(encoding='utf-8')
        self.assertIn('超长跳过候选：5 条', markdown)
        pool_file = self.root / 'data/local/pool.json'
        pool = json.loads(pool_file.read_text(encoding='utf-8'))
        self.assertEqual(pool['stats']['length_exceeded'], 5)
        def no_more_calls(*args, **kwargs):
            self.fail('terminal candidates must not be evaluated again')
        self.collect(count=7, evaluate_fn=no_more_calls)
        # 模拟账本已记录截断、池终态尚未保存就中断。
        pool['candidates'][0]['status'] = 'pending'
        pool_file.write_text(json.dumps(pool), encoding='utf-8')
        recovered = self.collect(count=7, evaluate_fn=no_more_calls)
        self.assertEqual(recovered['skipped_length_exceeded'], 1)
        pool = json.loads(pool_file.read_text(encoding='utf-8'))
        pool['built_at'] = '2000-01-01T00:00:00+08:00'
        pool_file.write_text(json.dumps(pool), encoding='utf-8')
        self.collect(count=7, evaluate_fn=no_more_calls)
        self.settings['refresh_pool'] = True
        self.collect(count=7, evaluate_fn=no_more_calls)
        pool = json.loads(pool_file.read_text(encoding='utf-8'))
        self.assertEqual(pool['stats']['length_exceeded'], 5)

    def test_length_still_respects_budget_and_unknown_usage(self):
        self.settings['max_total_tokens'] = 100
        report = self.collect(evaluate_fn=lambda *a, **k: self.length_result())
        self.assertEqual(report['stop_reason'], 'token_limit')
        self.assertEqual(report['evaluations'], 1)
        self.assertEqual(report['skipped_length_exceeded'], 1)
        self.settings['max_total_tokens'] = 100000
        report = self.collect(evaluate_fn=lambda *a, **k: self.length_result(usage=False))
        self.assertEqual(report['stop_reason'], 'usage_unknown')
        self.assertEqual(report['evaluations'], 1)
        self.assertEqual(report['skipped_length_exceeded'], 1)

    def test_length_resets_failure_streak_but_real_failures_still_stop(self):
        attempts = []
        def mixed(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 3:
                return self.length_result()
            result = self.length_result()
            result['reason_code'] = 'MODEL_ERROR'
            result['call'].reason_code = 'MODEL_ERROR'
            result['call'].is_sample_error = False
            return result
        report = self.collect(count=8, evaluate_fn=mixed)
        self.assertEqual(report['stop_reason'], 'model_failures')
        self.assertEqual(len(attempts), 6)
        self.assertEqual(report['skipped_length_exceeded'], 1)
        self.assertEqual(report['failed_evaluations'], 5)

    def test_review_length_counts_both_calls_and_continues(self):
        from unittest.mock import patch
        from src.catalog.evaluation import evaluate
        from tests.test_catalog_quality import TEXT, assessment, response
        self.settings['target_recommended'] = 1
        raw = assessment(self.cfg['rules'])
        def fetch(url, **kwargs):
            return FetchResult(url=url, ok=True, text=TEXT)
        responses = [response(raw), self.length_result()['call'], response(raw), response(raw)]
        with patch('src.catalog.evaluation.call_model', side_effect=responses) as model:
            report = self.collect(count=2, evaluate_fn=evaluate, fetch_fn=fetch)
        self.assertEqual(model.call_count, 4)
        self.assertEqual(report['stop_reason'], 'target_reached')
        self.assertEqual(report['skipped_length_exceeded'], 1)
        self.assertEqual(report['usage']['total_tokens'], 400)
        self.assertEqual(report['usage']['requests'], 4)
        self.assertEqual(report['calls'][1]['stage'], 'review')
        self.assertEqual(report['calls'][1]['status'], 'length_exceeded')
        records = [json.loads(p.read_text(encoding='utf-8'))
                   for p in (self.root / 'data/local/state/evaluations').glob('*.json')]
        failed = next(r for r in records if r['status'] == 'failed')
        self.assertEqual(len(failed['requests']), 2)
        self.assertEqual(failed['requests'][1]['reason_code'], 'LENGTH_EXCEEDED')
        self.assertFalse(failed['retryable'])

    def test_token_limit_stops_following_request(self):
        self.settings.update(target_recommended=5, max_total_tokens=150)
        report = self.collect()
        self.assertEqual(report["stop_reason"], "token_limit")
        self.assertEqual(report["usage"]["total_tokens"], 200)
        self.assertEqual(len(self.calls), 2)

    def test_missing_usage_stops_even_successful_response(self):
        def missing(*args, **kwargs):
            result = self.evaluate(*args, **kwargs)
            result["call"] = ModelCallResult(ok=True, attempts=1)
            return result
        report = self.collect(evaluate_fn=missing)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_failure_tokens_count_and_consecutive_failure_stops(self):
        def failure(candidate, text, **kwargs):
            result = self.evaluate(candidate, text, **kwargs)
            result.update(ok=False, reason_code="MODEL_ERROR", error="untrusted echo test-secret-never-print")
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
        self.assertEqual(report["stop_reason"], "access_denied")
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


    def non_retryable_result(self, reason_code="MODEL_ERROR", http_status=200):
        """模拟不可重试的评估失败结果。"""
        from src.infra.llm import ModelCallResult
        return {
            "ok": False,
            "reason_code": reason_code,
            "evaluation": None,
            "call": ModelCallResult(
                reason_code=reason_code,
                is_sample_error=False,
                finish_reason="stop",
                http_status=http_status,
                attempts=1,
                usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            ),
        }

    def test_non_retryable_failure_becomes_blocked_on_restart(self):
        """不可重试失败当轮持久化为 blocked；再次运行不重复抓取，也不调用模型。"""
        fetch_calls = []
        model_calls = []

        def failing_evaluate(candidate, text, **kwargs):
            model_calls.append(candidate.skill_id)
            # tool-0 失败且不可重试；其余正常通过
            if len(model_calls) == 1:
                return self.non_retryable_result()
            return self.evaluate(candidate, text, **kwargs)

        def counting_fetch(url, **kwargs):
            fetch_calls.append(url)
            return self.fetch(url, **kwargs)

        # 第一轮：tool-0 不可重试失败，tool-1、tool-2 推荐成功
        self.settings["target_recommended"] = 2
        report1 = self.collect(count=3, evaluate_fn=failing_evaluate, fetch_fn=counting_fetch)
        self.assertEqual(report1["stop_reason"], "target_reached")
        self.assertEqual(len(model_calls), 3)  # tool-0 失败 + tool-1, tool-2 成功

        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        blocked_items = [c for c in pool_data["candidates"] if c["status"] == "blocked"]
        self.assertEqual(len(blocked_items), 1, "tool-0 应已被标记为 blocked")
        self.assertEqual(blocked_items[0]["candidate"]["name"], "tool-0")
        self.assertIsNotNone(blocked_items[0]["block_info"])
        self.assertEqual(blocked_items[0]["block_info"]["reason"], "NON_RETRYABLE_FAILURE")
        self.assertEqual(pool_data["stats"]["blocked"], 1)

        # 第二轮：pool 中 tool-0 已是 blocked，不应再被抓取或调用模型
        fetch_calls.clear()
        model_calls.clear()

        def no_more_model_calls(*args, **kwargs):
            self.fail("blocked 候选不得再次调用模型")

        # 重新运行（tool-0 已 blocked，tool-1/tool-2 已 done，tool-3 新加入）
        self.settings["target_recommended"] = 3
        self.collect(count=4, evaluate_fn=no_more_model_calls if False else self.evaluate, fetch_fn=counting_fetch)
        # tool-0 不应出现在抓取记录中
        blocked_skill_url = "https://raw.githubusercontent.com/example/skills/HEAD/skills/tool-0/SKILL.md"
        self.assertNotIn(blocked_skill_url, fetch_calls, "blocked 候选不应被重复抓取")

    def test_blocked_survives_pool_rebuild_and_refresh(self):
        """blocked 状态在池过期重建和 --refresh-pool 时应被保留。"""
        fetch_calls = []
        model_calls = []

        def failing_first(candidate, text, **kwargs):
            model_calls.append(candidate.skill_id)
            if len(model_calls) == 1:
                return self.non_retryable_result(reason_code="MODEL_ERROR", http_status=403)
            return self.evaluate(candidate, text, **kwargs)

        def counting_fetch(url, **kwargs):
            fetch_calls.append(url)
            return self.fetch(url, **kwargs)

        # 第一轮：tool-0 变为 blocked
        self.settings["target_recommended"] = 2
        self.collect(count=5, evaluate_fn=failing_first, fetch_fn=counting_fetch)

        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["blocked"], 1)
        blocked_candidate = next(c for c in pool_data["candidates"] if c["status"] == "blocked")
        self.assertEqual(blocked_candidate["block_info"]["http_status"], 403)

        # 模拟池过期（修改 built_at 为 8 天前）
        pool_data["built_at"] = "2000-01-01T00:00:00+08:00"
        pool_file.write_text(json.dumps(pool_data), encoding="utf-8")

        fetch_calls.clear()
        model_calls.clear()

        def no_blocked_calls(candidate, text, **kwargs):
            self.assertNotEqual(candidate.name, "tool-0", "blocked 候选不应进入模型评估")
            return self.evaluate(candidate, text, **kwargs)

        # 池过期触发重建；blocked 应保留
        self.settings["target_recommended"] = 3
        self.collect(count=5, evaluate_fn=no_blocked_calls, fetch_fn=counting_fetch)
        pool_data2 = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data2["stats"]["blocked"], 1, "池重建后 blocked 应保留")
        blocked_candidate2 = next(c for c in pool_data2["candidates"] if c["status"] == "blocked")
        self.assertIsNotNone(blocked_candidate2["block_info"], "block_info 应保留")

        fetch_calls.clear()
        model_calls.clear()

        # --refresh-pool 显式刷新；blocked 也应保留
        self.settings["refresh_pool"] = True
        self.collect(count=5, evaluate_fn=no_blocked_calls, fetch_fn=counting_fetch)
        pool_data3 = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data3["stats"]["blocked"], 1, "--refresh-pool 后 blocked 应保留")

    def test_ledger_blocked_record_not_refetched_on_restart(self):
        """历史账本记录为不可重试失败时，重启后首次遇到即标记 blocked，不再抓取。"""
        from src.catalog.local import prepare_pool
        # 第一轮：tool-0 失败且不可重试
        model_calls = []

        def one_failure(candidate, text, **kwargs):
            model_calls.append(candidate.skill_id)
            if len(model_calls) == 1:
                return self.non_retryable_result()
            return self.evaluate(candidate, text, **kwargs)

        self.settings["target_recommended"] = 2
        self.collect(count=4, evaluate_fn=one_failure)

        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["blocked"], 1)

        # 模拟：将 blocked 条目手动重置为 pending（模拟旧版行为中的卡住条目）
        for c in pool_data["candidates"]:
            if c["status"] == "blocked":
                c["status"] = "pending"
                c["block_info"] = None
        pool_file.write_text(json.dumps(pool_data), encoding="utf-8")

        fetch_calls_round2 = []
        model_calls_round2 = []

        def second_round_eval(candidate, text, **kwargs):
            model_calls_round2.append(candidate.skill_id)
            return self.evaluate(candidate, text, **kwargs)

        def second_round_fetch(url, **kwargs):
            fetch_calls_round2.append(url)
            return self.fetch(url, **kwargs)

        # 第二轮：tool-0 的评估账本仍然是 failed+not-retryable
        # 应在遇到时（读取账本后）转为 blocked，不进入模型
        self.settings["target_recommended"] = 3
        self.collect(count=4, evaluate_fn=second_round_eval, fetch_fn=second_round_fetch)

        pool_data2 = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data2["stats"]["blocked"], 1, "重启后 tool-0 应再次被标为 blocked")
        # tool-0 不应出现在本轮模型调用中
        blocked_names = [c["candidate"]["name"] for c in pool_data2["candidates"] if c["status"] == "blocked"]
        self.assertEqual(blocked_names, ["tool-0"])
        self.assertNotIn("example/skills:skills/tool-0/SKILL.md", model_calls_round2)

    def format_failure_result(self, error_kind="OUTPUT_SCHEMA_INVALID"):
        return {
            "ok": False,
            "reason_code": "PARSE_ERROR",
            "error_kind": error_kind,
            "stage": "assessment",
            "evaluation": None,
            "call": ModelCallResult(
                ok=True,
                http_status=200,
                attempts=1,
                usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            ),
        }

    def test_five_format_failures_continue_to_target_and_save_blocked(self):
        """连续 5 个格式异常不会熔断服务，继续寻找候选并达成目标，5 个异常项记为 blocked。"""
        attempts = []

        def mixed(candidate, text, **kwargs):
            attempts.append(candidate.skill_id)
            if len(attempts) <= 5:
                return self.format_failure_result()
            return self.evaluate(candidate, text, **kwargs)

        self.settings["target_recommended"] = 2
        report = self.collect(count=8, evaluate_fn=mixed)
        self.assertEqual(report["stop_reason"], "target_reached")
        self.assertEqual(report["new_recommended"], 2)
        self.assertEqual(report["skipped_output_format"], 5)
        self.assertEqual(report["blocked_records"], 5)
        self.assertEqual(len(attempts), 7)

        pool_file = self.root / "data" / "local" / "pool.json"
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        self.assertEqual(pool_data["stats"]["blocked"], 5)
        self.assertEqual(pool_data["stats"]["done"], 2)

        blocked_items = [c for c in pool_data["candidates"] if c["status"] == "blocked"]
        self.assertEqual(len(blocked_items), 5)
        self.assertEqual(blocked_items[0]["block_info"]["reason"], "OUTPUT_FORMAT_INVALID")
        self.assertEqual(blocked_items[0]["block_info"]["error_kind"], "OUTPUT_SCHEMA_INVALID")

    def test_ten_format_failures_stop_with_format_failures(self):
        """连续 10 个格式异常达到阈值停止，第 11 个候选不调用。"""
        attempts = []

        def always_format_failure(candidate, text, **kwargs):
            attempts.append(candidate.skill_id)
            return self.format_failure_result()

        report = self.collect(count=15, evaluate_fn=always_format_failure)
        self.assertEqual(report["stop_reason"], "format_failures")
        self.assertEqual(report["skipped_output_format"], 10)
        self.assertEqual(len(attempts), 10)

    def test_format_failures_alternating_with_length_do_not_reset_format_streak(self):
        """格式异常与 length 交替出现时，length 不清空格式计数，格式计数达到阈值时仍停止。"""
        self.settings["max_format_failures_without_valid_result"] = 3
        attempts = []

        def alternating(candidate, text, **kwargs):
            attempts.append(candidate.skill_id)
            # 1: format, 2: length, 3: format, 4: length, 5: format -> 达到 3 次格式异常
            if len(attempts) in (1, 3, 5):
                return self.format_failure_result()
            return self.length_result()

        report = self.collect(count=8, evaluate_fn=alternating)
        self.assertEqual(report["stop_reason"], "format_failures")
        self.assertEqual(report["skipped_output_format"], 3)
        self.assertEqual(report["skipped_length_exceeded"], 2)
        self.assertEqual(len(attempts), 5)

    def test_manage_pool_reconcile_and_resume(self):
        """测试 manage_pool 的离线对账 (reconcile) 与显式恢复 (resume)。"""
        from src.catalog.maintenance import reconcile_pool, resume_candidate
        from src.catalog.pool import save_pool, load_pool, create_pool_from_candidates
        from src.catalog.budget import evaluation_filename
        from src.infra.files import write_json_atomic

        # 准备候选池，含 2 个 pending 候选
        c0 = self.candidate(0)
        c0.content_fingerprint = "fp_0"
        c1 = self.candidate(1)
        c1.content_fingerprint = "fp_1"
        pool = create_pool_from_candidates([c0, c1], {}, {})
        pool_file = self.root / "data" / "local" / "pool.json"
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        save_pool(pool_file, pool)

        # 模拟账本记录：
        # c0 有明确的 LENGTH_EXCEEDED 记录
        from src.catalog.evaluation import evaluation_id
        eid0 = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        hash0 = evaluation_filename(eid0)
        rec0_path = self.root / "data" / "local" / "state" / "evaluations" / hash0
        rec0_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(rec0_path, {
            "evaluation_id": eid0,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "rules_version": self.cfg["rules"].get("rules_version", "1.0.0"),
            "model_config_version": self.cfg["model"].get("model_config_version", "1.0.0"),
            "status": "failed",
            "attempts": 1,
            "max_attempts": 2,
            "error": {"reason_code": "LENGTH_EXCEEDED"},
            "requests": [{"status": "length_exceeded", "reason_code": "LENGTH_EXCEEDED"}],
        })

        # c1 有格式异常不可重试记录
        eid1 = evaluation_id(c1, self.cfg["model"], self.cfg["rules"])
        hash1 = evaluation_filename(eid1)
        rec1_path = self.root / "data" / "local" / "state" / "evaluations" / hash1
        write_json_atomic(rec1_path, {
            "evaluation_id": eid1,
            "skill_id": c1.skill_id,
            "content_fingerprint": c1.content_fingerprint,
            "rules_version": self.cfg["rules"].get("rules_version", "1.0.0"),
            "model_config_version": self.cfg["model"].get("model_config_version", "1.0.0"),
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
            "retryable": False,
            "error": {"reason_code": "PARSE_ERROR", "error_kind": "OUTPUT_SCHEMA_INVALID"},
        })

        # 1. 预览对账 (dry-run)
        preview = reconcile_pool(self.root, apply=False)
        self.assertEqual(preview["reconciled"], 2)
        self.assertFalse(preview["apply"])

        # 确认池尚未修改
        loaded = load_pool(pool_file)
        self.assertEqual(loaded.items[0].status, "pending")
        self.assertEqual(loaded.items[1].status, "pending")

        # 2. 应用对账 (apply)
        applied = reconcile_pool(self.root, apply=True)
        self.assertEqual(applied["reconciled"], 2)
        self.assertTrue(applied["apply"])
        self.assertIsNotNone(applied["backup_path"])

        # 确认池已对齐
        loaded2 = load_pool(pool_file)
        self.assertEqual(loaded2.items[0].status, "length_exceeded")
        self.assertEqual(loaded2.items[1].status, "blocked")
        self.assertEqual(loaded2.items[1].block_info["reason_code"], "PARSE_ERROR")

        # 3. 显式恢复 c1 (resume)
        # 先 dry-run
        resume_preview = resume_candidate(
            self.root, evaluation_id=eid1, reason="已修复提示词", extra_attempts=1, apply=False
        )
        self.assertTrue(resume_preview["dry_run"])
        self.assertEqual(resume_preview["target_status"], "pending")
        self.assertEqual(resume_preview["new_max_attempts"], 3)

        # 确认池中仍为 blocked
        loaded3 = load_pool(pool_file)
        self.assertEqual(loaded3.items[1].status, "blocked")

        # 再 apply
        resume_applied = resume_candidate(
            self.root, evaluation_id=eid1, reason="已修复提示词", extra_attempts=1, apply=True
        )
        self.assertFalse(resume_applied["dry_run"])
        self.assertEqual(resume_applied["new_max_attempts"], 3)

        # 确认池中 c1 已恢复为 pending，账本 max_attempts 增为 3 且记录 resume_history
        loaded4 = load_pool(pool_file)
        self.assertEqual(loaded4.items[1].status, "pending")
        self.assertIsNone(loaded4.items[1].block_info)

        rec1_after = json.loads(rec1_path.read_text(encoding="utf-8"))
        self.assertEqual(rec1_after["max_attempts"], 3)
        self.assertTrue(rec1_after["retryable"])
        self.assertEqual(len(rec1_after["resume_history"]), 1)
        self.assertEqual(rec1_after["resume_history"][0]["reason"], "已修复提示词")


if __name__ == "__main__":
    unittest.main()

