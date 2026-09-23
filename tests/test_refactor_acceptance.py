"""Production-path regression tests for the refactor acceptance gaps."""
from copy import deepcopy
from pathlib import Path
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from src.finder.run import execute_find_skill, main, FinderRunState
from src.finder.config import _parse_int_val, load_finder_run_config, load_finder_model_config
from src.finder.evaluation import parse_skill_evaluation, verify_and_adjust_evaluation
from src.finder.evidence import verify_evidence_snippet
from src.finder.report import sanitize_report_for_public, rebuild_find_report, render_find_markdown_report
from src.finder.search import fetch_candidate_materials
from src.infra.http import FetchResult, _read_capped
from src.infra.llm import ModelCallResult
from src.shared.identity import candidate_from_repo
from src.shared.materials import DocumentSnapshot, MaterialBundle
from src.shared.usage import UsageTotals
from src.catalog.store import catalog_lock, recover_catalog_projections
from src.catalog.maintenance import enrich_catalog_offline
from tests.test_pipeline import PipelineHarness, one_candidate, fake_discover, fake_fetch, fake_evaluate, passing_evaluation

CRITERIA = [{"id": "c1", "kind": "required", "description": "Generate prompts"}]
PLAN = {"intent": "Generate prompts", "queries": ["prompt"], "criteria": CRITERIA}
EVALUATION = {"match": "strong", "documentation": "clear", "summary_zh": "tool",
    "criteria_results": [{"criterion_id": "c1", "status": "supported", "evidence": [
        {"source_path": "SKILL.md", "start_line": 1, "end_line": 1, "quote": "Generate prompts"}]}]}


class FinderAcceptanceTest(unittest.TestCase):
    def test_failed_start_record_does_not_send_or_count_request(self):
        state = FinderRunState("topic", {}, Path("unused"))
        with patch.object(state, "save", side_effect=OSError("disk full")), patch("src.finder.run.call_model") as model:
            with self.assertRaises(OSError):
                state.call(model, {}, "", "", api_key="fake", sleep=lambda _: None, candidate_id="test")
            model.assert_not_called()
        self.assertEqual(state.report["calls"][0]["state"], "not_sent")
        self.assertEqual(state.report["evaluation_attempts"], 0)
        self.assertEqual(state.usage.snapshot()["requests"], 0)

    def test_markdown_preserves_url_and_unknown_usage(self):
        report = {"usage": {"total_tokens": None}, "shortlist": [{"candidate": {
            "name": "[link]", "url": "https://github.com/a/my_repo/blob/main/(x).md",
            "repo_url": "javascript:alert(1)"}, "evaluation": {}}]}
        rendered = render_find_markdown_report(report)
        self.assertIn("my_repo/blob/main/%28x%29.md", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn("总计 未知", rendered)
        self.assertIn(r"\[link\]", rendered)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = candidate_from_repo(owner="test", repo="sample", path="SKILL.md",
            url="https://github.com/test/sample/blob/HEAD/SKILL.md", repo_url="https://github.com/test/sample",
            name="sample", description="", discovered_at="2026-09-23T00:00:00Z")
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_case(self, mode="success", **overrides):
        self.calls = []
        def model(*args, **kwargs):
            self.calls.append(1)
            if len(self.calls) > 1 and mode == "interrupt":
                raise KeyboardInterrupt()
            return ModelCallResult(ok=True, attempts=1,
                content=json.dumps(PLAN if len(self.calls) == 1 else EVALUATION),
                usage={} if len(self.calls) > 1 and mode == "unknown" else {"total_tokens": 20})
        arguments = dict(root_dir=self.root, model_cfg={"endpoint": "https://fake.invalid", "model": "test", "auth": {"api_key": "fake"}},
            limit=1, max_evaluations=1, log=lambda *args: None, call_model_fn=model,
            search_github_repos_fn=lambda *a, **k: (True, [{"owner": "test", "repo": "sample"}], None),
            expand_and_collect_candidates_fn=lambda *a, **k: ([], [{"ok": False}]) if mode == "expand" else ([self.candidate], []),
            fetch_candidate_materials_fn=lambda *a, **k: (False, {}, "HTTP 429") if mode == "fetch" else (True, {"SKILL.md": "Generate prompts"}, None))
        arguments.update(overrides)
        return execute_find_skill("Generate prompts", **arguments)

    def test_unknown_usage_preserves_current_valid_result(self):
        report = self.run_case("unknown")
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(report["evaluated_count"], 1)
        self.assertEqual(len(report["shortlist"]), 1)
        self.assertEqual(len(self.calls), 2)

    def test_interrupted_transport_records_unknown_fact(self):
        report = self.run_case("interrupt")
        self.assertEqual(report["usage"]["requests"], 2)
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertEqual(report["calls"][-1]["state"], "unknown")

    def test_complete_infrastructure_failure_is_not_empty_success(self):
        for mode, reason in (("expand", "expansion_failed"), ("fetch", "material_failed")):
            with self.subTest(mode=mode):
                report = self.run_case(mode)
                self.assertEqual((report["status"], report["stop_reason"]), ("error", reason))

    def test_public_snapshot_only_written_after_finalization(self):
        from src.finder.report import update_public_snapshot
        states = []
        def publish(report, directory):
            states.append(report["status"])
            return update_public_snapshot(report, directory)
        with patch("src.finder.run.update_public_snapshot", side_effect=publish):
            report = self.run_case()
        self.assertEqual(states, ["completed"])
        self.assertEqual(report["schema_version"], "1.0.0")

    def test_public_failure_is_diagnosed_and_rebuild_is_offline(self):
        with patch("src.finder.run.update_public_snapshot", side_effect=OSError("C:/private/report")):
            report = self.run_case()
        self.assertEqual(report["stop_reason"], "artifact_failed")
        self.assertTrue(report["errors"])
        rebuilt = rebuild_find_report(Path(report["report_paths"]["json"]).parent, self.root / "public" / "data")
        self.assertEqual(len(rebuilt["evaluations"]), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("private", json.dumps(sanitize_report_for_public(report)))

    def test_parser_does_not_coerce_invalid_evidence_or_text(self):
        for value in (True, 1.5, "1"):
            data = deepcopy(EVALUATION)
            data["criteria_results"][0]["evidence"][0]["start_line"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_skill_evaluation(json.dumps(data), CRITERIA)
        for value in ({"text": "fake"}, "x" * 1201):
            data = deepcopy(EVALUATION)
            data["summary_zh"] = value
            with self.assertRaises(ValueError):
                parse_skill_evaluation(json.dumps(data), CRITERIA)
        for values in ([], EVALUATION["criteria_results"] * 2):
            data = dict(EVALUATION, criteria_results=values)
            with self.assertRaises(ValueError):
                parse_skill_evaluation(json.dumps(data), CRITERIA)

    def test_strict_config_and_help_independent_of_broken_config(self):
        with self.assertRaises(ValueError):
            _parse_int_val(1.9)
        config = self.root / "config"
        config.mkdir()
        path = config / "find-skill.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_finder_run_config(config)
        path.write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--help"], root=self.root), 0)
        (config / "model.example.json").write_text(json.dumps({"endpoint": "https://api.example.com", "model": "example-model"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_finder_model_config(config)

    def test_material_bundle_tracks_reference_change_and_revision(self):
        revision = "a" * 40
        self.candidate.url = f"https://github.com/test/sample/blob/{revision}/SKILL.md"
        urls = []
        reference = ["first"]
        def fetch(url, **kwargs):
            urls.append(url)
            return FetchResult(url, ok=True, text="Generate prompts\n[more](guide.md)" if url.endswith("SKILL.md") else reference[0])
        ok, first, _ = fetch_candidate_materials(self.candidate, fetch_fn=fetch)
        reference[0] = "changed"
        _, second, _ = fetch_candidate_materials(self.candidate, fetch_fn=fetch)
        self.assertTrue(ok)
        self.assertIsInstance(first, MaterialBundle)
        self.assertNotEqual(first.bundle_fingerprint, second.bundle_fingerprint)
        self.assertEqual(first.primary_doc.fingerprint, second.primary_doc.fingerprint)
        self.assertTrue(all(revision in u for u in urls))
        self.assertTrue(first.manifest()["revision_consistent"])
        self.assertTrue(verify_evidence_snippet("SKILL.md", 1, 1, "Generate prompts", {"SKILL.md": first.primary_doc})[0])

    def test_exact_limit_and_no_request_accounting(self):
        class Response:
            def iter_content(self, **kwargs):
                yield b"1234"
        self.assertEqual(_read_capped(Response(), 4), (b"1234", False))
        self.assertTrue(_read_capped(Response(), 3)[1])
        totals = UsageTotals()
        totals.add(ModelCallResult(attempts=0))
        self.assertEqual(totals.requests, 0)
        self.assertEqual(totals.unknown_usage_requests, 0)


class CatalogAcceptanceTest(PipelineHarness):
    def test_completed_record_replay_without_source_catalog_or_credentials(self):
        from src.catalog.sync_reserve import phase_reserve
        from src.catalog.sync_evaluate import phase_evaluate
        from src.catalog.maintenance import recover_completed_results
        from src.infra.files import write_json_atomic
        config = self.temp_config()
        phase_reserve(config_dir=config, data_dir=self.data,
                      discover_fn=fake_discover([one_candidate()]), fetch_fn=fake_fetch())
        evaluator = fake_evaluate()
        def fault(path, payload, **kwargs):
            if Path(path) == self.data / "catalog.json":
                raise OSError("injected source write failure")
            return write_json_atomic(path, payload, **kwargs)
        with patch("src.catalog.store.write_json_atomic", side_effect=fault), self.assertRaises(OSError):
            phase_evaluate(config_dir=config, data_dir=self.data, public_dir=self.public, evaluate_fn=evaluator)
        self.assertEqual(len(evaluator.calls), 1)
        (config / "model.local.json").unlink()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("offline recovery")):
            result = recover_completed_results(self.root)
            self.assertEqual(result["restored"], 1)
            self.assertEqual(recover_completed_results(self.root)["restored"], 0)
        self.assertTrue((self.public / "data" / "catalog.json").exists())

    def test_local_and_actions_produce_same_entry_through_real_callers(self):
        from datetime import datetime, timezone
        from src.catalog.sync_reserve import phase_reserve
        from src.catalog.sync_evaluate import phase_evaluate
        from src.catalog.local import run_local
        from src.catalog.config import load_all_config
        fixed = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
        config = self.temp_config()
        candidate = one_candidate(path="skills/widget/SKILL.md")
        candidate.url = "https://github.com/acme/widget/blob/HEAD/skills/widget/SKILL.md"
        evaluator = fake_evaluate(passing_evaluation())
        with patch("src.catalog.sync_reserve.now_local", return_value=fixed), patch("src.catalog.sync_evaluate.now_local", return_value=fixed):
            phase_reserve(config_dir=config, data_dir=self.data, discover_fn=fake_discover([deepcopy(candidate)]), fetch_fn=fake_fetch())
            phase_evaluate(config_dir=config, data_dir=self.data, public_dir=self.public, evaluate_fn=evaluator)
        actions_entry = json.loads((self.data / "catalog.json").read_text(encoding="utf-8"))["entries"][0]
        local_root = self.root / "local-only"
        cfg = load_all_config(config)
        cfg["model"]["auth"] = {"api_key": "fake", "api_key_env": "TEST_UNUSED_KEY"}
        settings = {"target_recommended": 1, "max_total_tokens": 100000, "max_retries": 0,
                    "max_consecutive_failures": 3, "pool_watermark": 20}
        with patch("src.catalog.local.now_local", return_value=fixed):
            run_local(local_root, settings, cfg=cfg, discover_fn=fake_discover([deepcopy(candidate)]),
                      fetch_fn=fake_fetch(), evaluate_fn=evaluator, log=lambda *a: None)
        local_entry = json.loads((local_root / "data" / "catalog.json").read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(actions_entry, local_entry)

    def test_maintenance_honors_same_lock_as_collectors(self):
        with catalog_lock(self.data / ".catalog.lock"):
            with self.assertRaises(RuntimeError):
                enrich_catalog_offline(self.root)

    def test_corrupt_catalog_stops_before_evaluation_and_preserves_source(self):
        from src.catalog.sync_evaluate import phase_evaluate
        self.data.mkdir(parents=True, exist_ok=True)
        source = self.data / "catalog.json"
        source.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            phase_evaluate(config_dir=self.temp_config(), data_dir=self.data, public_dir=self.public,
                           evaluate_fn=lambda *a, **k: self.fail("must not call model"))
        self.assertEqual(source.read_text(encoding="utf-8"), "{broken")

    def test_reserve_handles_fetch_failure_without_name_error(self):
        from src.catalog.sync_reserve import phase_reserve
        for reason in ("UPSTREAM_GONE", "HTTP_ERROR", "NETWORK_ERROR"):
            result = phase_reserve(config_dir=self.temp_config(), data_dir=self.data,
                discover_fn=fake_discover([one_candidate()]), fetch_fn=fake_fetch(ok=False, reason_code=reason), sleep=lambda _: None)
            self.assertTrue(result["ok"])

    def test_completed_evaluation_recovers_after_page_failure_without_model(self):
        from src.catalog.sync_reserve import phase_reserve
        from src.catalog.sync_evaluate import phase_evaluate
        from src.infra.files import write_json_atomic
        config = self.temp_config()
        phase_reserve(config_dir=config, data_dir=self.data, discover_fn=fake_discover([one_candidate()]), fetch_fn=fake_fetch())
        evaluator = fake_evaluate()
        def fault(path, payload, **kwargs):
            if Path(path) == self.public / "data" / "catalog.json":
                raise OSError("injected page failure")
            return write_json_atomic(path, payload, **kwargs)
        with patch("src.catalog.store.write_json_atomic", side_effect=fault), self.assertRaises(OSError):
            phase_evaluate(config_dir=config, data_dir=self.data, public_dir=self.public, evaluate_fn=evaluator)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertTrue(recover_catalog_projections(self.root))
        phase_evaluate(config_dir=config, data_dir=self.data, public_dir=self.public,
                       evaluate_fn=lambda *a, **k: self.fail("completed evaluation must not repeat"))
