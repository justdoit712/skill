"""生产评估器的两轮筛选、证据检查和用量边界；模型响应全部离线替换。"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.catalog.config import load_all_config
from src.catalog.decide import decide
from src.catalog.evaluation import evaluate, parse_evaluation
from src.catalog.models import Candidate
from src.catalog.quality import QUALITY_CHECKS, check_quality, verified_citations
from src.infra.llm import ModelCallResult
from src.infra.http import FetchResult
import tests.test_local_run as local_tests
import tests.test_pipeline as pipeline_tests
from src.catalog.sync_reserve import phase_reserve
from src.catalog.sync_evaluate import phase_evaluate


ROOT = Path(__file__).resolve().parents[1]
TEXT = "---\nname: audit\ndescription: 审查代码\n---\n定位错误，给出修复建议及可重复验证的测试。\n无外部工具依赖。"


def assessment(rules):
    item = {"value": "pass", "evidence": "有具体步骤及验证要求", "citations": [
        {"start_line": 5, "end_line": 5, "quote": TEXT.splitlines()[4]}]}
    return {**{c["id"]: deepcopy(item) for c in rules["checks"]},
            "quality_checks": {key: deepcopy(item) for key in QUALITY_CHECKS},
            "domain_checks": {}, "reason_codes": [], "main_category": "dev",
            "summary_zh": "用于审查代码。"}


def response(payload, tokens=100, usage=True):
    return ModelCallResult(ok=True, content=json.dumps(payload, ensure_ascii=False), attempts=1,
        usage={"prompt_tokens": tokens - 20, "completion_tokens": 20, "total_tokens": tokens} if usage else {})


class QualityTest(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads((ROOT / "config/standards/rules.json").read_text(encoding="utf-8"))
        self.taxonomy = json.loads((ROOT / "config/standards/taxonomy.json").read_text(encoding="utf-8"))
        self.candidate = Candidate(skill_id="test/audit:SKILL.md", owner="test", repo="audit", path="SKILL.md")
        self.raw = assessment(self.rules)

    def run_evaluation(self, responses, **kwargs):
        with patch("src.catalog.evaluation.call_model", side_effect=responses) as model:
            result = evaluate(self.candidate, TEXT, model_cfg={}, rules=self.rules, taxonomy=self.taxonomy, **kwargs)
        return result, model

    def test_only_two_independent_passes_recommend(self):
        result, model = self.run_evaluation([response(self.raw), response(self.raw, 160)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")
        self.assertEqual(result["evaluation"]["quality_audit"]["review_status"], "passed")
        self.assertEqual(sum(c.total_tokens for c in result["calls"]), 260)
        self.assertEqual(model.call_count, 2)
        # 复核只接收相同原文，未接收初评结论。
        self.assertEqual(model.call_args_list[0].args[2], model.call_args_list[1].args[2])

    def test_fabricated_quote_never_reaches_review(self):
        self.raw["purpose_clarity"]["citations"][0]["quote"] = "并不存在的原文"
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(model.call_count, 1)
        self.assertIn("purpose_clarity", result["evaluation"]["quality_audit"]["invalid_citations"])

    def test_vague_skill_fails_quality_even_with_six_passes(self):
        self.raw["quality_checks"]["actionability"].update(value="fail", evidence="只有口号，没有步骤")
        result, model = self.run_evaluation([response(self.raw)])
        self.assertIn("QUALITY_BELOW_BAR", result["evaluation"]["reason_codes"])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(model.call_count, 1)

    def test_disagreement_holds_candidate(self):
        review = deepcopy(self.raw)
        review["quality_checks"]["verification"].update(value="unknown", evidence="缺少验收条件")
        result, _ = self.run_evaluation([response(self.raw), response(review)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(result["evaluation"]["quality_audit"]["review_status"], "disagreed")

    def test_unknown_usage_stops_before_review(self):
        result, model = self.run_evaluation([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "REVIEW_PENDING")
        self.assertIsNotNone(result["pending_evaluation"])

    def test_budget_can_stop_between_rounds(self):
        result, model = self.run_evaluation([response(self.raw)],
            on_request=lambda event, stage, call: not (event == "before" and stage == "review"))
        self.assertEqual(model.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "REVIEW_PENDING")

    def test_review_transport_failure_is_not_quality_failure(self):
        failed = ModelCallResult(ok=False, attempts=1, reason_code="NETWORK_ERROR")
        result, _ = self.run_evaluation([response(self.raw), failed])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["evaluation"])
        self.assertEqual(len(result["calls"]), 2)

    def test_malformed_quality_is_processing_failure(self):
        self.raw["quality_checks"] = []
        result, model = self.run_evaluation([response(self.raw)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "PARSE_ERROR")
        self.assertEqual(model.call_count, 1)

    def test_check_quality_does_not_mutate_input(self):
        raw = parse_evaluation(json.dumps(self.raw), self.rules, None, self.taxonomy)
        before = deepcopy(raw)
        check_quality(raw, TEXT, self.rules)
        self.assertEqual(raw, before)

    def test_citations_reject_booleans_wrong_lines_and_partial_quotes(self):
        valid = deepcopy(self.raw["scope_match"]["citations"])
        self.assertTrue(verified_citations(valid, TEXT))
        for changes in ({"start_line": True}, {"start_line": 0}, {"end_line": 99},
                        {"quote": "定位错误"}, {"quote": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(verified_citations([{**valid[0], **changes}], TEXT))


class LocalQualityIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.harness = local_tests.LocalRunTest()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.harness.settings["target_recommended"] = 1
        self.raw = assessment(self.harness.cfg["rules"])

    def collect(self, responses):
        with patch("src.catalog.evaluation.call_model", side_effect=responses) as model:
            report = self.harness.collect(count=2, evaluate_fn=evaluate,
                fetch_fn=lambda url, **kw: FetchResult(url=url, ok=True, text=TEXT))
        return report, model

    def test_both_requests_count_once_and_quality_reaches_public_catalog(self):
        report, model = self.collect([response(self.raw), response(self.raw, 160)])
        self.assertEqual(report["new_recommended"], 1)
        self.assertEqual(report["usage"]["requests"], 2)
        self.assertEqual(report["usage"]["total_tokens"], 260)
        self.assertEqual([c["stage"] for c in report["calls"]], ["assessment", "review"])
        self.assertEqual(report["stop_reason"], "target_reached")
        catalog = json.loads((self.harness.root / "public/data/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["recommended"][0]["quality_summary"]["review_status"], "passed")

    def test_budget_stops_review_and_other_candidates(self):
        self.harness.settings["max_total_tokens"] = 100
        report, model = self.collect([response(self.raw)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["stop_reason"], "token_limit")
        self.assertEqual(report["new_recommended"], 0)
        self.assertEqual(report["usage"]["total_tokens"], 100)
        self.assertEqual(report["failed_evaluations"], 0)

    def test_next_run_resumes_review_without_repeating_assessment(self):
        self.harness.settings["max_total_tokens"] = 100
        first, _ = self.collect([response(self.raw)])
        self.assertEqual(first["stop_reason"], "token_limit")
        self.harness.settings["max_total_tokens"] = 1000
        resumed, model = self.collect([response(self.raw, 160)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(resumed["new_recommended"], 1)
        self.assertEqual(resumed["usage"]["total_tokens"], 160)
        self.assertEqual([c["stage"] for c in resumed["calls"]], ["review"])

    def test_missing_usage_does_not_spend_more(self):
        report, model = self.collect([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertGreater(report["unknown_usage_reserved_tokens"], 0)

    def test_interrupted_review_keeps_known_first_usage_and_marks_unknown_second(self):
        report, model = self.collect([response(self.raw), KeyboardInterrupt()])
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertEqual(report["usage"]["requests"], 2)
        self.assertEqual(report["usage"]["total_tokens"], 100)
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertEqual(report["calls"][-1]["status"], "unknown")


class ActionsQualityIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.harness = pipeline_tests.PipelineHarness()
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.config = self.harness.temp_config(max_total_tokens_per_run=100)
        self.raw = assessment(load_all_config(self.config)["rules"])
        candidate = pipeline_tests.one_candidate(path="skills/audit/SKILL.md")
        phase_reserve(config_dir=self.config, data_dir=self.harness.data, state_dir=self.harness.state,
            discover_fn=pipeline_tests.fake_discover([candidate]), fetch_fn=pipeline_tests.fake_fetch(TEXT))

    def run_phase(self, responses):
        with patch("src.catalog.evaluation.call_model", side_effect=responses) as model:
            result = phase_evaluate(config_dir=self.config, data_dir=self.harness.data,
                public_dir=self.harness.public, state_dir=self.harness.state,
                evaluate_fn=evaluate, fetch_fn=pipeline_tests.fake_fetch(TEXT))
        return result, model

    def test_budget_checkpoint_resumes_and_keeps_both_request_records(self):
        first, model = self.run_phase([response(self.raw)])
        self.assertEqual(first["tokens_used"], 100)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(first["evaluated"], 0)
        self.assertEqual(first["queue_pending"], 1)
        second, model = self.run_phase([response(self.raw, 150)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(second["tokens_used"], 150)
        self.assertEqual(second["evaluated"], 1)
        self.assertEqual(second["queue_pending"], 0)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (self.harness.state / "evaluations").glob("*.json")]
        self.assertEqual([r["stage"] for r in records[0]["requests"]], ["assessment", "review"])

    def test_unknown_usage_stops_without_recommending(self):
        result, model = self.run_phase([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertTrue(result["usage_unknown"])
        self.assertEqual(result["evaluated"], 0)
        self.assertEqual(result["queue_pending"], 1)


if __name__ == "__main__":
    unittest.main()
