"""Production-path regressions for baseline metrics and planning audit."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.finder.run import execute_find_skill
from src.finder.report import rebuild_find_report, sanitize_report_for_public
from src.finder.terminology import detect_terminology_gaps
from src.infra.llm import ModelCallResult
from src.shared.metrics import build_run_metrics, PrescreenMetricFacts
from src.shared.versions import check_evaluation_record_compatibility, parse_version_tuple


class ContractRepairs(unittest.TestCase):
    def test_missing_and_changed_versions_reject_reuse(self):
        for key in ('rules_version', 'model_config_version', 'output_contract_version'):
            with self.subTest(key=key):
                expected = {'expected_' + key: '1.0.0'}
                self.assertFalse(check_evaluation_record_compatibility({}, **expected)[0])
                self.assertFalse(check_evaluation_record_compatibility({key: '1.1.0'}, **expected)[0])
                self.assertTrue(check_evaluation_record_compatibility({key: '1.0.0'}, **expected)[0])
        for version in ('1.0oops.0', '1.0.0-beta', '1.0', '1.x.0'):
            self.assertEqual(parse_version_tuple(version), ())

    def test_sampling_denominator_and_missing_sample_size(self):
        facts = PrescreenMetricFacts(skipped_count=100, sample_audit_count=10, sample_audit_false_positives=2)
        self.assertEqual(facts.false_positive_ratio().value, 20)
        self.assertEqual(PrescreenMetricFacts(skipped_count=100).false_positive_ratio().status, 'unknown')

    def test_completely_uncovered_concept_is_reported(self):
        gaps = detect_terminology_gaps('网页爬取与数据清洗', '', ['网页爬取'])
        gap = next(g for g in gaps if g['concept_id'] == 'data_cleaning')
        self.assertEqual(gap['covered_terms'], [])
        gaps = detect_terminology_gaps('PDF 提取', '', ['pdf'])
        self.assertEqual(next(g for g in gaps if g['concept_id'] == 'pdf_extraction')['covered_terms'], [])

    def test_metrics_recompute_without_inventing_legacy_counts(self):
        self.assertIsNone(build_run_metrics({}, kind='finder')['model']['requests'])
        report = {'usage': {'requests': 3, 'total_tokens': 120, 'unknown_usage_requests': 1},
                  'search': {'queries_executed': [
                      {'attempt': 1, 'repos_returned': 0, 'error': 'http_error'},
                      {'attempt': 2, 'repos_returned': 2, 'error': None}],
                      'repos_discovered': 2, 'candidates_found': 4},
                  'shortlist_count': 1, 'metrics': {'model': {'requests': 999}}}
        metrics = build_run_metrics(report, kind='finder')
        self.assertEqual(metrics['model']['requests'], 3)
        self.assertEqual(metrics['model']['unknown_usage_requests'], 1)
        self.assertEqual(metrics['search']['http_requests'], 2)
        self.assertEqual(metrics['search']['retries'], 1)
        self.assertEqual(metrics['search']['conversion_ratio']['value'], 25)
        self.assertEqual(metrics, build_run_metrics(report, kind='finder'))
        self.assertEqual(sanitize_report_for_public(report)['metrics'], metrics)


class PlanningAuditRepairs(unittest.TestCase):
    def test_normal_and_interrupted_planning_have_identical_audit(self):
        plan = {'intent': '网页爬取与数据清洗',
                'queries': ['网页爬取 in:readme', 'WEB SCRAPING', 'web scraping'],
                'criteria': [{'id': 'extract', 'kind': 'required', 'description': '提取内容'}]}
        content = '```json\n' + json.dumps(plan) + '\n```'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = Mock(return_value=ModelCallResult(ok=True, content=content, attempts=1,
                         usage={'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}))
            search = Mock(return_value=(True, [], None))
            kwargs = dict(root_dir=root, model_cfg={'endpoint': 'https://fake.invalid', 'model': 'fake', 'auth': {'api_key': 'fake'}},
                          call_model_fn=model, search_github_repos_fn=search,
                          max_clarification_turns=0, max_rounds=1, sleep=lambda _: None, log=lambda *_: None)
            normal = execute_find_skill('网页爬取与数据清洗', **kwargs)
            self.assertEqual(normal['terminology_observation']['raw_queries'], plan['queries'])
            self.assertEqual(normal['terminology_observation']['queries'], ['网页爬取', 'WEB SCRAPING'])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(search.call_count, 2)
            with patch('src.finder.run.parse_plan_with_observation', side_effect=KeyboardInterrupt):
                interrupted = execute_find_skill('网页爬取与数据清洗', **kwargs)
            paid_calls = model.call_count
            resumed = execute_find_skill('网页爬取与数据清洗', resume_dir=Path(interrupted['report_paths']['json']).parent, **kwargs)
            self.assertEqual(model.call_count, paid_calls)
            self.assertEqual(resumed['terminology_observation'], normal['terminology_observation'])
            self.assertEqual(resumed['metrics']['model']['requests'], 1)
            self.assertEqual(resumed['schema_version'], '1.1.0')
            directory = Path(resumed['report_paths']['json']).parent
            rebuilt = rebuild_find_report(directory)
            self.assertEqual(rebuilt['metrics'], resumed['metrics'])
            legacy = dict(resumed, schema_version='1.0.0')
            (directory / 'report.json').write_text(json.dumps(legacy), encoding='utf-8')
            self.assertEqual(rebuild_find_report(directory)['schema_version'], '1.0.0')
