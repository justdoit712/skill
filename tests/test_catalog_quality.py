"""生产评估器的单轮筛选、证据检查和用量边界；模型响应全部离线替换。"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.catalog.config import load_all_config
from src.catalog.decide import decide
from src.catalog.evaluation import evaluate, parse_evaluation
from src.catalog.models import Candidate
from src.catalog.quality import check_quality, verified_citations
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
    result = {**{c["id"]: deepcopy(item) for c in rules["checks"]},
            "domain_checks": {}, "reason_codes": [], "main_category": "dev",
            "summary_zh": "用于审查代码。"}
    for key, value in result.items():
        if key != "evidence_traceability" and isinstance(value, dict):
            value.pop("citations", None)
    return result


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

    def test_single_pass_recommends_with_one_request(self):
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")
        self.assertEqual(result["evaluation"]["quality_audit"]["review_status"], "single_pass")
        self.assertEqual(sum(c.total_tokens for c in result["calls"]), 100)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["stage"], "assessment")

    def test_fabricated_quote_stays_candidate(self):
        self.raw["evidence_traceability"]["citations"][0]["quote"] = "并不存在的原文"
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(model.call_count, 1)
        self.assertIn("evidence_traceability", result["evaluation"]["quality_audit"]["invalid_citations"])

    def test_vague_role_prompt_has_no_recommendation_value(self):
        self.raw["purpose_clarity"].update(value="fail", evidence="只有顶级作家的角色设定，没有实质方法")
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.assertEqual(model.call_count, 1)

    def test_missing_verification_does_not_block_useful_guidance(self):
        self.raw["verification_note"] = "给出改稿方法，但未提供验收清单。"
        result, model = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")
        self.assertEqual(result["evaluation"]["verification_note"], self.raw["verification_note"])
        self.assertNotIn("quality_checks", result["evaluation"])
        self.assertEqual(model.call_count, 1)

    def test_unknown_usage_preserves_completed_assessment(self):
        result, model = self.run_evaluation([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertTrue(result["ok"])
        self.assertNotIn("pending_evaluation", result)
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")

    def test_only_assessment_callbacks_are_emitted(self):
        events = []
        result, model = self.run_evaluation([response(self.raw)],
            on_request=lambda event, stage, call: events.append((event, stage)))
        self.assertTrue(result["ok"])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(events, [("before", "assessment"), ("after", "assessment")])

    def test_valid_pending_assessment_finishes_without_another_request(self):
        pending = parse_evaluation(json.dumps(self.raw), self.rules,
                                   self.candidate.content_fingerprint, self.taxonomy)
        result, model = self.run_evaluation([], pending_evaluation=pending)
        self.assertTrue(result["ok"])
        self.assertEqual(model.call_count, 0)
        self.assertEqual(result["calls"], [])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")

    def test_invalid_dependency_blocking_type_is_processing_failure(self):
        self.raw["dependency_transparency"].update(value="unknown", blocking="false")
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
        valid = deepcopy(self.raw["evidence_traceability"]["citations"])
        self.assertTrue(verified_citations(valid, TEXT))
        for changes in ({"start_line": True}, {"start_line": 0},
                        {"quote": "定位错误"}, {"quote": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(verified_citations([{**valid[0], **changes}], TEXT))

    def test_relocation_keeps_full_quote_and_records_actual_lines(self):
        self.raw["evidence_traceability"]["citations"][0].update(start_line=2, end_line=2)
        checked = check_quality(self.raw, TEXT, self.rules)
        self.assertEqual(checked["purpose_clarity"]["value"], "pass")
        citation = checked["quality_audit"]["verified_citations"]["evidence_traceability"][0]
        self.assertEqual((citation["start_line"], citation["end_line"], citation["match_method"]), (5, 5, "nearby"))

    def test_six_checks_with_one_core_quote_can_recommend(self):
        result, model = self.run_evaluation([response(self.raw)])
        self.assertTrue(result["ok"])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(set(result["evaluation"]["quality_audit"]["verified_citations"]), {"evidence_traceability"})

    def test_full_quote_limit_and_ambiguous_relocation(self):
        from src.catalog.quality import locate_citations
        text = "\n".join(f"line {i}" for i in range(65))
        quote = "\n".join(text.splitlines()[:60])
        self.assertTrue(verified_citations([{"start_line": 1, "end_line": 60, "quote": quote}], text))
        self.assertFalse(verified_citations([{"start_line": 1, "end_line": 61, "quote": quote + "\nline 60"}], text))
        self.assertIsNone(locate_citations([{"start_line": 50, "end_line": 50, "quote": "repeated"}], "repeated\nx\nrepeated"))

    def test_distant_quote_is_relocated_without_rewriting_model_evidence(self):
        self.raw["evidence_traceability"]["citations"][0].update(start_line=100, end_line=110)
        result = check_quality(self.raw, TEXT, self.rules)
        self.assertEqual(result["purpose_clarity"], self.raw["purpose_clarity"])
        self.assertEqual(result["quality_audit"]["verified_citations"]["evidence_traceability"][0]["match_method"], "relocated")

    def test_missing_critical_reference_remains_candidate(self):
        self.raw["instruction_completeness"].update(value="unknown", evidence="核心做法仅指向未提供的参考文件")
        result, _ = self.run_evaluation([response(self.raw)])
        decision = decide(result["evaluation"], self.rules)
        self.assertEqual(decision["decision"], "candidate")
        self.assertIn("instruction_completeness", decision["blocking_checks"])

    def test_dependency_information_gap_vs_essential_requirement(self):
        from src.catalog.quality import quality_summary
        for value, flag, expected in (("unknown", False, "recommended"),
                                      ("unknown", True, "candidate"),
                                      ("unknown", None, "candidate"),
                                      ("fail", False, "candidate")):
            with self.subTest(value=value, blocking=flag):
                self.raw["dependency_transparency"] = {"value": value, "evidence": "依赖信息尚未说明"}
                if flag is not None:
                    self.raw["dependency_transparency"]["blocking"] = flag
                result, _ = self.run_evaluation([response(self.raw)])
                self.assertEqual(decide(result["evaluation"], self.rules)["decision"], expected)
                summary = quality_summary(result["evaluation"])
                self.assertEqual(summary["checks"]["dependency_transparency"]["informational"], expected == "recommended")
                self.assertEqual(result["evaluation"].get("dependencies_declared", []), [])
                self.assertIsNone(result["evaluation"].get("platform_declared"))

    def test_prompt_merges_quality_and_requests_only_core_citations(self):
        from src.catalog.evaluation import build_prompt
        system, _ = build_prompt(self.candidate, TEXT, self.rules, self.taxonomy)
        example = json.JSONDecoder().raw_decode(system.split("输出 JSON 结构：\n", 1)[1])[0]
        self.assertNotIn("quality_checks", example)
        self.assertIn("verification_note", example)
        self.assertIn("citations", example["evidence_traceability"])
        self.assertNotIn("citations", example["scope_match"])
        self.assertNotIn("citations", example["risk_review"])
        self.assertIn("blocking", example["dependency_transparency"])
        self.assertIn("实际价值并入 purpose_clarity", system)
        self.assertIn("只有宣传", system)
        rules = deepcopy(self.rules)
        rules["quality_review"]["enabled"] = False
        system, _ = build_prompt(self.candidate, TEXT, rules, self.taxonomy)
        self.assertNotIn('"verification_note"', system)

    def test_missing_core_quote_prevents_recommendation(self):
        self.raw["evidence_traceability"].pop("citations")
        result, _ = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")

    def test_domain_checks_and_clear_exclusions_still_apply(self):
        for category, domain in (("finance", "finance"), ("mental_health", "health")):
            for value in ("unknown", "fail", "pass"):
                with self.subTest(category=category, value=value):
                    self.raw["main_category"] = category
                    self.raw["domain_checks"] = {domain: {"value": value, "evidence": "专项依据未核实", "citations": []}}
                    result, _ = self.run_evaluation([response(self.raw)])
                    self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "candidate")
        self.raw["reason_codes"] = ["CREDENTIAL_EXFILTRATION"]
        result, _ = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "excluded")

    def test_optional_domain_without_quote_is_not_a_failure(self):
        self.raw["main_category"] = "finance"
        self.raw["domain_checks"] = {"finance": {"value": "not_applicable", "evidence": "本项不适用"}}
        result, _ = self.run_evaluation([response(self.raw)])
        self.assertEqual(decide(result["evaluation"], self.rules)["decision"], "recommended")


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

    def test_one_request_and_quality_reaches_public_catalog(self):
        report, model = self.collect([response(self.raw)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["new_recommended"], 1)
        self.assertEqual(report["usage"]["requests"], 1)
        self.assertEqual(report["usage"]["total_tokens"], 100)
        self.assertEqual([c["stage"] for c in report["calls"]], ["assessment"])
        self.assertEqual(report["stop_reason"], "target_reached")
        catalog = json.loads((self.harness.root / "public/data/catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["recommended"][0]["quality_summary"]["review_status"], "single_pass")

    def test_navigation_notices_survive_public_catalog_projection(self):
        self.raw["dependency_transparency"].update(value="unknown", blocking=False,
                                                   evidence="材料未声明特殊依赖，不推测平台兼容性")
        self.raw["verification_note"] = "提供具体改稿方法，未提供验收清单。"
        report, model = self.collect([response(self.raw)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["new_recommended"], 1)
        catalog = json.loads((self.harness.root / "public/data/catalog.json").read_text(encoding="utf-8"))
        summary = catalog["recommended"][0]["quality_summary"]
        self.assertEqual(set(summary["checks"]), {c["id"] for c in self.harness.cfg["rules"]["checks"]})
        self.assertTrue(summary["checks"]["dependency_transparency"]["informational"])
        self.assertEqual(summary["verification_note"], self.raw["verification_note"])
        self.assertNotIn("practical_value", summary["checks"])

    def test_budget_keeps_completed_result_and_stops_other_candidates(self):
        self.harness.settings["target_recommended"] = 2
        self.harness.settings["max_total_tokens"] = 100
        report, model = self.collect([response(self.raw)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["stop_reason"], "token_limit")
        self.assertEqual(report["new_recommended"], 1)
        self.assertEqual(report["usage"]["total_tokens"], 100)
        self.assertEqual(report["failed_evaluations"], 0)

    def test_next_run_processes_next_candidate_without_review(self):
        self.harness.settings["target_recommended"] = 2
        self.harness.settings["max_total_tokens"] = 100
        first, _ = self.collect([response(self.raw)])
        self.assertEqual(first["stop_reason"], "token_limit")
        self.harness.settings["max_total_tokens"] = 1000
        resumed, model = self.collect([response(self.raw, 160)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(resumed["new_recommended"], 1)
        self.assertEqual(resumed["usage"]["total_tokens"], 160)
        self.assertEqual([c["stage"] for c in resumed["calls"]], ["assessment"])
        self.assertNotEqual(first["calls"][0]["skill_id"], resumed["calls"][0]["skill_id"])

    def test_missing_usage_does_not_spend_more(self):
        report, model = self.collect([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(report["stop_reason"], "usage_unknown")
        self.assertEqual(report["usage"]["unknown_usage_requests"], 1)
        self.assertGreater(report["unknown_usage_reserved_tokens"], 0)

    def test_interrupted_assessment_marks_unknown_usage(self):
        report, model = self.collect([KeyboardInterrupt()])
        self.assertEqual(report["stop_reason"], "interrupted")
        self.assertEqual(report["usage"]["requests"], 1)
        self.assertEqual(report["usage"]["total_tokens"], 0)
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

    def test_one_request_completes_at_budget_limit(self):
        first, model = self.run_phase([response(self.raw)])
        self.assertEqual(first["tokens_used"], 100)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(first["evaluated"], 1)
        self.assertEqual(first["queue_pending"], 0)
        second, model = self.run_phase([])
        self.assertEqual(model.call_count, 0)
        self.assertEqual(second["tokens_used"], 0)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in (self.harness.state / "evaluations").glob("*.json")]
        self.assertEqual([r["stage"] for r in records[0]["requests"]], ["assessment"])

    def test_unknown_usage_stops_after_preserving_completed_result(self):
        result, model = self.run_phase([response(self.raw, usage=False)])
        self.assertEqual(model.call_count, 1)
        self.assertTrue(result["usage_unknown"])
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["queue_pending"], 0)


if __name__ == "__main__":
    unittest.main()
