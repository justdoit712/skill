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
        self.tmp = tempfile.TemporaryDirectory()
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
                         "expand_limit": None, "max_consecutive_failures": 3}
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

    def collect(self, count=6, evaluate_fn=None, fetch_fn=None, candidates=None):
        return run_local(self.root, self.settings, cfg=self.cfg,
                         discover_fn=lambda *a, **kw: (candidates if candidates is not None else [self.candidate(i) for i in range(count)], []),
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


if __name__ == "__main__":
    unittest.main()
