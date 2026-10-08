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


class CandidatePoolTest(unittest.TestCase):
    """候选池创建、状态更新与读写往返。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_check_validates_run_settings_without_starting_collection(self):
        import io
        from contextlib import redirect_stdout
        from src.catalog import local

        valid = {"target_recommended": 1, "max_total_tokens": 1000,
                 "max_consecutive_failures": 3}
        config_path = self.root / "config/runners/local-run.json"
        cases = [
            (valid, [], 0),
            ({**valid, "target_recommended": 0}, [], 1),
            (valid, ["--target", "0"], 1),
            (valid, ["--max-tokens", "-1"], 1),
            (valid, ["--max-evaluations", "0"], 1),
            ({**valid, "target_recommended": 0}, ["--target", "2"], 0),
        ]
        with patch.object(local, "load_all_config", return_value={"model": {"model": "fake"}}), \
             patch.object(local, "precheck", return_value=[]), \
             patch.object(local, "resolve_api_key", return_value="fake"), \
             patch.object(local, "run_local", side_effect=AssertionError("check started collection")) as run:
            for settings, args, expected in cases:
                with self.subTest(settings=settings, args=args):
                    write_json_atomic(config_path, settings)
                    before = config_path.read_bytes()
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = local.main(["--check", *args], root=self.root)
                    self.assertEqual(code, expected, output.getvalue())
                    self.assertEqual(config_path.read_bytes(), before)
                    self.assertFalse((self.root / "data").exists())
            run.assert_not_called()

    def test_historical_failures_are_reconciled_without_new_blocks_or_requests(self):
        from src.catalog.config import load_all_config
        from src.catalog.evaluation import evaluation_id
        from src.catalog.local_candidate import process_candidate
        from src.catalog.local_state import init_local_state
        from src.infra.http import FetchResult
        from src.shared.identity import content_fingerprint
        from tests.test_catalog_quality import TEXT

        cfg = load_all_config(ROOT / "config")
        cfg["model"] = {"model": "fake", "endpoint": "https://example.invalid/v1/chat/completions",
                        "auth": {"api_key": "fake"}}
        settings = {"target_recommended": 1, "max_total_tokens": 1000000,
                    "max_consecutive_failures": 3}
        for status in ("failed", "needs_recovery"):
            with self.subTest(status=status):
                root = self.root / status
                state = init_local_state(root, root / "data/local", settings, cfg, "review",
                    discover_fn=lambda *a, **kw: self.fail("unexpected discovery"),
                    fetch_fn=lambda *a, **kw: FetchResult(url="fake", ok=True, text=TEXT),
                    evaluate_fn=lambda *a, **kw: self.fail("unexpected model call"),
                    log=lambda _: None, sleep=lambda _: None)
                candidate = Candidate(skill_id="test/audit:SKILL.md", owner="test", repo="audit",
                    path="SKILL.md", content_fingerprint=content_fingerprint(TEXT))
                state.pool = create_pool_from_candidates([candidate])
                state.pending_items = state.pool.items
                save_pool(state.pool_path, state.pool)
                eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
                state.ledger.reserve([{"evaluation_id": eid, "skill_id": candidate.skill_id}])
                state.ledger.fail(eid, "OUTPUT_SCHEMA_INVALID", "historical failure")
                if status == "needs_recovery":
                    state.ledger.mark_needs_recovery(eid, "historical interrupted request")
                before = state.ledger.get(eid)
                state.save()

                self.assertTrue(process_candidate(state, state.pool.items[0]))
                self.assertEqual(state.pool.items[0].status, "blocked")
                self.assertEqual(state.report["blocked_records"], 1)
                self.assertEqual(state.report["reconciled_blocked"], 1)
                self.assertEqual(state.report["blocked_new"], 0)
                self.assertEqual(state.report["evaluations"], 0)
                self.assertEqual(state.report["calls"], [])
                self.assertEqual(state.ledger.get(eid), before)

    @smoke
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

    @smoke
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

    def test_parameter_rejection_rotation_preserves_candidates_budget_and_parallel_accounting(self):
        import json
        from copy import deepcopy
        from unittest.mock import Mock
        from src.catalog.config import load_all_config
        from src.catalog.local import run_local
        from src.infra.http import FetchResult
        from tests.test_catalog_quality import TEXT, assessment

        original = load_all_config(ROOT / 'config')
        original['model'] = {'provider': 'dashscope', 'auth': {'api_key': 'fake'},
            'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
            'models': ['model-a', 'model-b']}
        rejection = {'error': {'code': 'invalid_parameter_error', 'type': 'invalid_request_error',
                              'message': 'parameter.enable_thinking must be set to false for non-streaming calls.'}}
        settings = {'target_recommended': 1, 'max_total_tokens': 1000000,
                    'max_consecutive_failures': 3, 'pool_watermark': 1, 'parallel_evaluation': False}
        for mode in ('rotate', 'parallel', 'all_incompatible', 'generic', 'charged'):
            with self.subTest(mode=mode):
                cfg = deepcopy(original)
                root = self.root / mode
                write_json_atomic(root / 'config/models/model.json', cfg['model'])
                count = 2 if mode == 'parallel' else 1
                candidates = [Candidate(skill_id=f'test/audit-{i}:SKILL.md', owner='test',
                                         repo=f'audit-{i}', path='SKILL.md') for i in range(count)]
                pool_path = root / 'data/local/pool.json'
                save_pool(pool_path, create_pool_from_candidates(candidates))
                session = Mock()
                def post(endpoint, *, data, **kwargs):
                    model = json.loads(data)['model']
                    if model == 'model-a' or mode == 'all_incompatible':
                        body = deepcopy(rejection)
                        if mode == 'generic':
                            body['error']['message'] = 'malformed request'
                        elif mode == 'charged':
                            body['usage'] = {'total_tokens': 9}
                        return Mock(status_code=400, text=json.dumps(body), json=Mock(return_value=body))
                    return Mock(status_code=200, json=Mock(return_value={
                        'usage': {'prompt_tokens': 80, 'completion_tokens': 20, 'total_tokens': 100},
                        'choices': [{'finish_reason': 'stop', 'message': {
                            'content': json.dumps(assessment(cfg['rules']), ensure_ascii=False)}}]}))
                session.post.side_effect = post
                options = {**settings, 'parallel_evaluation': mode == 'parallel', 'target_recommended': count}
                def collect():
                    return run_local(root, options, cfg=cfg,
                        discover_fn=lambda *args, **kwargs: ([], []),
                        fetch_fn=lambda *args, **kwargs: FetchResult(url='fake', ok=True, text=TEXT),
                        sleep=lambda _: None, log=lambda _: None)
                with patch('src.infra.llm.requests.Session', return_value=session):
                    report = collect()
                if mode in ('rotate', 'parallel'):
                    self.assertEqual((report['stop_reason'], report['new_recommended'],
                                      report['usage']['unknown_usage_requests'], report['budget_tokens']),
                                     ('target_reached', count, 0, count * 100))
                    self.assertTrue(all(item.status == 'done' for item in load_pool(pool_path).items))
                    self.assertTrue(any(call['switch_reason'] == 'request_incompatible' for call in report['calls']))
                elif mode == 'all_incompatible':
                    record = read_json(next((root / 'data/local/state/evaluations').glob('*.json')))
                    self.assertEqual((report['stop_reason'], load_pool(pool_path).items[0].status,
                                      record['status'], report['budget_tokens'], report['usage']['requests']),
                                     ('models_incompatible', 'pending', 'reserved', 0, 2))
                    with patch('src.infra.llm.requests.Session', return_value=session):
                        resumed = collect()
                    self.assertEqual((resumed['stop_reason'], session.post.call_count), ('models_incompatible', 2))
                else:
                    self.assertEqual((report['stop_reason'], session.post.call_count), ('request_config_error', 1))
                    self.assertEqual(report['usage']['unknown_usage_requests'], int(mode == 'generic'))
                    self.assertEqual(report['usage']['total_tokens'], 9 if mode == 'charged' else 0)

    def test_parallel_unknown_usage_does_not_mark_a_settled_peer_for_recovery(self):
        import threading
        from src.catalog.config import load_all_config
        from src.catalog.local import run_local
        from src.catalog.local_candidate import _record_request_usage
        from src.infra.http import FetchResult
        from src.infra.llm import ModelCallResult
        from tests.test_catalog_quality import TEXT

        cfg = load_all_config(ROOT / 'config')
        cfg['model'] = {'provider': 'dashscope', 'auth': {'api_key': 'fake'},
            'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
            'models': ['model-a', 'model-b']}
        write_json_atomic(self.root / 'config/models/model.json', cfg['model'])
        candidates = [Candidate(skill_id=f'test/{name}:SKILL.md', owner='test', repo=name,
                                 path='SKILL.md', url=f'https://fake/{name}') for name in ('known', 'unknown')]
        pool_path = self.root / 'data/local/pool.json'
        save_pool(pool_path, create_pool_from_candidates(candidates))
        barrier, unknown_recorded = threading.Barrier(2), threading.Event()
        def model(cfg, system, user, **kwargs):
            barrier.wait(timeout=5)
            if 'marker_known' in user:
                if not unknown_recorded.wait(timeout=5):
                    raise RuntimeError('peer usage callback did not run')
                return ModelCallResult(http_status=200, attempts=1, reason_code='RESPONSE_EMPTY',
                                       content='', usage={'total_tokens': 10})
            return ModelCallResult(http_status=400, attempts=1, reason_code='MODEL_ERROR', error='HTTP 400')
        def record_usage(state, call, *, retryable):
            _record_request_usage(state, call, retryable=retryable)
            if call.http_status == 400:
                unknown_recorded.set()
        options = {'target_recommended': 2, 'max_total_tokens': 1000000,
                   'max_consecutive_failures': 3, 'pool_watermark': 1, 'parallel_evaluation': True}
        with patch('src.catalog.evaluation.call_model', side_effect=model), \
                patch('src.catalog.local_candidate._record_request_usage', side_effect=record_usage):
            report = run_local(self.root, options, cfg=cfg,
                discover_fn=lambda *args, **kwargs: ([], []),
                fetch_fn=lambda url, **kwargs: FetchResult(url=url, ok=True,
                    text=TEXT + ('\nmarker_known' if 'known' in url.split('/') else '\nmarker_unknown')),
                sleep=lambda _: None, log=lambda _: None)
        self.assertEqual(report['usage']['unknown_usage_requests'], 1)
        records = [read_json(path) for path in (self.root / 'data/local/state/evaluations').glob('*.json')]
        known = next(record for record in records if record['skill_id'] == candidates[0].skill_id)
        self.assertEqual(known['status'], 'reserved', str(known['requests']))
        self.assertEqual(known['requests'][0]['usage']['total_tokens'], 10)
        self.assertEqual(load_pool(pool_path).items[0].status, 'pending')


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

    def test_classify_candidate_no_capable_model(self):
        from src.catalog.failure_policy import update_failure_counters
        result = {"ok": False, "reason_code": "CANDIDATE_NO_CAPABLE_MODEL", "error": "超出上限"}
        decision = classify_result(result)
        self.assertEqual(decision.category, "candidate_no_capable_model")
        self.assertEqual(decision.block_reason, "CANDIDATE_NO_CAPABLE_MODEL")
        self.assertFalse(decision.is_service_failure)
        self.assertFalse(decision.retryable)
        cons, fmt = update_failure_counters(result, decision, 2, 0)
        self.assertEqual(cons, 2)  # 不增加服务故障计数，不触发整轮停止


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

    def test_recover_settled_pool_pauses_preserves_accounting_and_real_blockers(self):
        from copy import deepcopy
        from src.catalog.budget import BudgetLedger
        ledger = BudgetLedger.load(self.state, 20, max_attempts=4)
        empty = {'state': 'error', 'reservation_state': 'settled',
                 'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10, 'attempts': 1},
                 'response': {'ok': False, 'http_status': 200, 'content': '', 'reason_code': 'MODEL_ERROR'}}
        variants = {'safe': {}, 'unknown': {'usage': {'attempts': 1}},
                    'active': {'state': 'started', 'reservation_state': 'active'},
                    'success': {'response': {'ok': True, 'content': '{"ok":true}', 'http_status': 200}},
                    'generic_400': {'response': {'ok': False, 'http_status': 400, 'reason_code': 'MODEL_ERROR'}},
                    'aggregated': {'usage': {**empty['usage'], 'attempts': 2}},
                    'conflicting': {'usage': {**empty['usage'], 'total_tokens': 11}}, 'storage': {}}
        ledger.reserve([{'evaluation_id': name, 'skill_id': name} for name in variants])
        for name, changes in variants.items():
            record = ledger.get(name)
            record.update(status='needs_recovery', attempts=1,
                          pause_reason='storage_error' if name == 'storage' else 'request_config_error',
                          requests=[{**deepcopy(empty), **deepcopy(changes)}])
            ledger.save_record(name, record)
        before = deepcopy(ledger.get('safe'))
        self.assertEqual(ledger.recover_settled_pool_pauses(dry_run=True), ['safe'])
        self.assertEqual(ledger.get('safe'), before)
        self.assertEqual(ledger.recover_settled_pool_pauses(), ['safe'])
        after = ledger.get('safe')
        self.assertEqual((after['status'], after['requests'], after['attempts'], after['max_attempts']),
                         ('reserved', before['requests'], before['attempts'], before['max_attempts']))
        self.assertEqual(len(after['recovery_history']), 1)
        self.assertEqual(ledger.recover_settled_pool_pauses(), [])
        self.assertTrue(all(ledger.get(name)['status'] == 'needs_recovery' for name in variants if name != 'safe'))


class RepoBatchAndCheckpointRecoveryTest(unittest.TestCase):
    """分批仓库抓取、游标断点恢复与待处理有效性过滤长效契约。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "data" / "local" / "state").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    @smoke
    def test_settings_validation_batch_repo_limit(self):
        from src.catalog.local import _valid_settings, DEFAULT_BATCH_REPO_LIMIT
        settings = {"target_recommended": 5, "max_total_tokens": 1000, "max_consecutive_failures": 3}
        _valid_settings(settings)
        self.assertEqual(settings["batch_repo_limit"], DEFAULT_BATCH_REPO_LIMIT)

        for bad in (0, -1, True, False, 1.5, "100"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _valid_settings({**settings, "batch_repo_limit": bad})

    def test_batch_repo_overflow_and_unassigned_queue(self):
        from src.catalog.batch import (
            DiscoveryState,
            acquire_next_repo_batch,
            save_discovery_state,
        )
        state_path = self.root / "data" / "local" / "state" / "discovery.json"
        state = DiscoveryState(
            search_config_fingerprint="test-fp",
            query_cursors={
                "domain1:term1": {
                    "domain_id": "domain1",
                    "term": "term1",
                    "q": "dummy query",
                    "source": "searches",
                    "next_page": 1,
                    "exhausted": False,
                    "page_attempts": 0,
                    "last_error": None,
                    "retry_at": None,
                    "total_count": None,
                }
            },
        )
        save_discovery_state(state_path, state)

        # Mock search returning 5 repositories
        def mock_search(q, per_page=30, page=1, **kwargs):
            return [
                {"full_name": f"owner/repo-{i}", "html_url": f"https://github.com/owner/repo-{i}"}
                for i in range(1, 6)
            ], 5

        # Request batch with N=2
        batch = acquire_next_repo_batch(
            state,
            state_path,
            batch_repo_limit=2,
            searches_cfg={},
            search_fn=mock_search,
            sleep=lambda _: None,
            log=lambda _: None,
        )
        self.assertIsNotNone(batch)
        self.assertEqual(batch["repo_limit"], 2)
        self.assertEqual(len(batch["repositories"]), 2)
        self.assertEqual(batch["repositories"], ["owner/repo-1", "owner/repo-2"])
        # Remaining 3 repos must be saved in unassigned_repositories
        self.assertEqual(len(state.unassigned_repositories), 3)
        self.assertEqual(
            [f"{r['owner']}/{r['repo']}" for r in state.unassigned_repositories],
            ["owner/repo-3", "owner/repo-4", "owner/repo-5"],
        )

        # Mark first batch as completed
        state.active_batch = None
        save_discovery_state(state_path, state)

        # Acquire next batch with N=2, must consume unassigned_repositories without calling search
        search_called = []
        def guarded_search(*args, **kwargs):
            search_called.append(True)
            return [], 0

        batch2 = acquire_next_repo_batch(
            state,
            state_path,
            batch_repo_limit=2,
            searches_cfg={},
            search_fn=guarded_search,
            sleep=lambda _: None,
            log=lambda _: None,
        )
        self.assertFalse(search_called, "Should consume unassigned repos before querying search API")
        self.assertEqual(len(batch2["repositories"]), 2)
        self.assertEqual(batch2["repositories"], ["owner/repo-3", "owner/repo-4"])
        self.assertEqual(len(state.unassigned_repositories), 1)

    @smoke
    def test_pending_breakdown_and_actionable_classification(self):
        from src.catalog.pool import (
            CandidatePool,
            PoolItem,
            classify_pending_candidate,
            REASON_SNOOZED,
            REASON_MANUAL_EXCLUDED,
            REASON_OWNED,
        )
        cand_active = Candidate("user/repo1:SKILL.md", "user", "repo1", path="SKILL.md")
        cand_snoozed = Candidate("user/repo2:SKILL.md", "user", "repo2", path="SKILL.md")
        cand_excluded = Candidate("user/repo3:SKILL.md", "user", "repo3", path="SKILL.md")
        cand_owned = Candidate("user/repo4:SKILL.md", "user", "repo4", path="SKILL.md")

        active_snoozed = {cand_snoozed.skill_id}
        manual_exclusions = {cand_excluded.skill_id}
        owned_ids = {cand_owned.skill_id}

        self.assertEqual(classify_pending_candidate(cand_active, active_snoozed, manual_exclusions, owned_ids), (True, None))
        self.assertEqual(classify_pending_candidate(cand_snoozed, active_snoozed, manual_exclusions, owned_ids), (False, REASON_SNOOZED))
        self.assertEqual(classify_pending_candidate(cand_excluded, active_snoozed, manual_exclusions, owned_ids), (False, REASON_MANUAL_EXCLUDED))
        self.assertEqual(classify_pending_candidate(cand_owned, active_snoozed, manual_exclusions, owned_ids), (False, REASON_OWNED))

        pool = CandidatePool(items=[
            PoolItem(1, cand_active),
            PoolItem(2, cand_snoozed),
            PoolItem(3, cand_excluded),
            PoolItem(4, cand_owned),
        ])
        actionable_count, breakdown = pool.count_actionable(
            active_snoozed=active_snoozed,
            manual_exclusions=manual_exclusions,
            owned_ids=owned_ids,
        )
        self.assertEqual(actionable_count, 1)
        self.assertEqual(breakdown["actionable"], 1)
        self.assertEqual(breakdown["snoozed"], 1)
        self.assertEqual(breakdown["manual_excluded"], 1)
        self.assertEqual(breakdown["owned"], 1)

    def test_continuous_batch_run_with_custom_n_and_m(self):
        import json
        from src.catalog.config import load_all_config
        from src.catalog.local import run_local
        from src.infra.http import FetchResult
        from src.infra.llm import ModelCallResult
        from tests.test_catalog_quality import TEXT, assessment

        cfg = load_all_config(ROOT / "config")
        cfg["model"] = {
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "mock-llm",
            "auth": {"api_key": "fake-key"},
        }
        cfg["sources"] = {"sources": []}
        cfg["searches"]["per_domain"] = {"dev": {"en": ["test"]}}
        write_json_atomic(self.root / "config/models/model.json", cfg["model"])
        searched = []

        # N = 2, M = 3
        # Search returns 2 repos on page 1, 2 repos on page 2
        def mock_search(q, per_page=30, page=1, **kwargs):
            searched.append(page)
            if page == 1:
                return [
                    {"full_name": "alpha/repo1", "html_url": "https://github.com/alpha/repo1"},
                    {"full_name": "alpha/repo2", "html_url": "https://github.com/alpha/repo2"},
                ], 4
            elif page == 2:
                return [
                    {"full_name": "beta/repo3", "html_url": "https://github.com/beta/repo3"},
                    {"full_name": "beta/repo4", "html_url": "https://github.com/beta/repo4"},
                ], 4
            return [], 4

        def mock_expand(owner, repo, sleep=None):
            return [f"skills/{repo}/SKILL.md"], None

        def mock_fetch(url, **kwargs):
            return FetchResult(
                url=url,
                ok=True,
                text=TEXT,
            )

        # Mock model output recommending the skill
        rec_content = json.dumps(assessment(cfg["rules"]), ensure_ascii=False)
        def mock_call_model(model_cfg, system, user, **kwargs):
            return ModelCallResult(
                ok=True,
                http_status=200,
                content=rec_content,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )

        options = {
            "batch_repo_limit": 2,
            "target_recommended": 3,
            "max_total_tokens": 1000000,
            "max_consecutive_failures": 3,
            "parallel_evaluation": False,
        }

        from src.catalog.batch import acquire_next_repo_batch
        def acquire(*args, **kwargs):
            return acquire_next_repo_batch(*args, **kwargs, per_page=2)
        with patch("src.catalog.evaluation.call_model", side_effect=mock_call_model), patch(
                "src.catalog.local.acquire_next_repo_batch", side_effect=acquire):
            report = run_local(
                self.root,
                options,
                cfg=cfg,
                search_fn=mock_search,
                expand_fn=mock_expand,
                fetch_fn=mock_fetch,
                sleep=lambda _: None,
                log=lambda _: None,
            )

        self.assertEqual(searched, [1, 2])
        self.assertEqual(report["stop_reason"], "target_reached")
        self.assertEqual(report["new_recommended"], 3)
        self.assertEqual(report["evaluations"], 3)

        # Verify discovery.json state persistence
        disc_state = read_json(self.root / "data" / "local" / "state" / "discovery.json")
        self.assertIsNotNone(disc_state)
        # Batch 1 was completed (2 repos evaluated -> 2 recommendations)
        self.assertTrue(len(disc_state["completed_batches"]) >= 1)
        # Active batch was Batch 2, stopped when target was reached (3 recommendations total)
        self.assertIsNotNone(disc_state["active_batch"])
        self.assertEqual(disc_state["active_batch"]["batch_seq"], 2)

        # 用独立目录重放同一流水线，首个候选连续失败两次，其余三个成功。
        isolated = self.root / "failure-case"
        write_json_atomic(isolated / "config/models/model.json", cfg["model"])
        failure = ModelCallResult(ok=False, reason_code="NETWORK_ERROR", attempts=1,
                                  usage={"total_tokens": 10})
        success = ModelCallResult(ok=True, http_status=200, content=rec_content,
                                  usage={"total_tokens": 150})
        with patch("src.catalog.evaluation.call_model", side_effect=[failure, failure, success, success, success]), patch(
                "src.catalog.local.acquire_next_repo_batch", side_effect=acquire):
            report = run_local(isolated, {**options, "max_retries": 1}, cfg=cfg,
                               search_fn=mock_search, expand_fn=mock_expand, fetch_fn=mock_fetch,
                               sleep=lambda _: None, log=lambda _: None)
        self.assertEqual(report["stop_reason"], "target_reached", report.get("error_message"))
        self.assertEqual(report["new_recommended"], 3)
        self.assertEqual(report["blocked_new"], 1)
        self.assertEqual(report["usage"]["total_tokens"], 470)
        # 已完成页恢复后不再检索；限额不能触发额外付费扩词。
        limit_root = self.root / "query-limit"
        write_json_atomic(limit_root / "config/models/model.json", cfg["model"])
        with patch("src.catalog.evaluation.call_model", side_effect=AssertionError("unexpected model request")):
            report = run_local(limit_root, {**options, "limit_queries": 0}, cfg=cfg,
                search_fn=lambda *a, **kw: self.fail("query limit ignored"),
                expand_fn=mock_expand, sleep=lambda _: None, log=lambda _: None)
        self.assertEqual(report["stop_reason"], "discovery_limit")
        outage_root = self.root / "search-outage"
        write_json_atomic(outage_root / "config/models/model.json", cfg["model"])
        with patch("src.catalog.evaluation.call_model", side_effect=AssertionError("outage triggered paid expansion")):
            report = run_local(outage_root, {**options, "max_retries": 1}, cfg=cfg,
                search_fn=lambda *a, **kw: (False, [], 0, "NETWORK_ERROR"),
                expand_fn=mock_expand, sleep=lambda _: None, log=lambda _: None)
        self.assertEqual(report["stop_reason"], "search_failed")


    def test_expand_error_isolation_and_not_marked_completed(self):
        from src.catalog.batch import (
            DiscoveryState,
            expand_batch_skills,
            reconcile_batch_and_repositories,
            save_discovery_state,
        )
        from src.catalog.pool import CandidatePool
        state_path = self.root / "data" / "local" / "state" / "discovery.json"
        pool_path = self.root / "data" / "local" / "pool.json"
        state = DiscoveryState(
            search_config_fingerprint="test-fp",
            repository_index={
                "owner/failed-repo": {
                    "owner": "owner",
                    "repo": "failed-repo",
                    "url": "https://github.com/owner/failed-repo",
                    "expanded": False,
                    "expand_error": None,
                    "skill_paths": [],
                    "has_skills": False,
                    "status": "pending",
                    "processed": False,
                    "batch_id": "batch-1",
                },
                "owner/ok-repo": {
                    "owner": "owner",
                    "repo": "ok-repo",
                    "url": "https://github.com/owner/ok-repo",
                    "expanded": False,
                    "expand_error": None,
                    "skill_paths": [],
                    "has_skills": False,
                    "status": "pending",
                    "processed": False,
                    "batch_id": "batch-1",
                },
            },
            active_batch={
                "batch_id": "batch-1",
                "batch_seq": 1,
                "repo_limit": 2,
                "stage": "expanding",
                "repositories": ["owner/failed-repo", "owner/ok-repo"],
                "skill_ids": [],
                "summary": {},
            },
        )
        save_discovery_state(state_path, state)
        pool = CandidatePool(items=[])

        def mock_expand(owner, repo, **kwargs):
            if repo == "failed-repo":
                return [], "HTTP_500: Internal Server Error"
            return [], None  # Confirmed no skills

        added, expanded = expand_batch_skills(
            state, state_path, pool, pool_path,
            expand_repo_fn=mock_expand,
        )
        self.assertEqual(expanded, 1)
        failed_info = state.repository_index["owner/failed-repo"]
        self.assertFalse(failed_info["expanded"])
        self.assertEqual(failed_info["expand_error"], "HTTP_500: Internal Server Error")
        self.assertEqual(failed_info["status"], "expand_failed")
        self.assertFalse(failed_info["processed"])

        ok_info = state.repository_index["owner/ok-repo"]
        self.assertTrue(ok_info["expanded"])
        self.assertTrue(ok_info["processed"])
        self.assertEqual(ok_info["status"], "completed")

        # Now reconcile
        reconcile_batch_and_repositories(
            state, state_path, pool,
            active_snoozed=set(), manual_exclusions=set(), owned_ids=set(),
        )
        # failed-repo MUST NOT be marked processed=True
        self.assertFalse(state.repository_index["owner/failed-repo"]["processed"])
        self.assertEqual(state.repository_index["owner/failed-repo"]["status"], "expand_failed")
        self.assertEqual(state.active_batch["stage"], "expanding")
        from src.catalog.batch import load_discovery_state
        state = load_discovery_state(state_path)
        self.assertEqual(state.repository_index["owner/failed-repo"]["expand_attempts"], 1)
        expand_batch_skills(state, state_path, pool, pool_path,
                            expand_repo_fn=lambda *a, **kw: ([], None))
        self.assertTrue(reconcile_batch_and_repositories(state, state_path, pool, set(), set(), set()))
        self.assertTrue(state.repository_index["owner/failed-repo"]["processed"])


    def test_expansion_accounting_budget_and_recovery(self):
        import json
        from types import SimpleNamespace
        from src.catalog.batch import DiscoveryState, load_discovery_state
        from src.catalog.local import _expand_search_queries
        from src.infra.llm import ModelCallResult
        from src.infra.model_pool import PoolStopped
        from src.shared.usage import UsageTotals, recompute_usage_from_calls

        for mode in ("single", "pool", "rotation", "budget", "interrupt", "unknown", "resume"):
            with self.subTest(mode=mode):
                local = self.root / mode
                state = SimpleNamespace(
                    cfg={"taxonomy": {"main_categories": [{"id": "dev", "name": "开发", "scope": "测试"}]},
                         "searches": {"per_domain": {"dev": {"exclude_terms": ["-private"]}}},
                         "model": {"model": "mock", "auth": {"api_key": "fake"}}},
                    local=local, discovery_state=DiscoveryState(), usage=UsageTotals(),
                    report={"calls": [], "budget_tokens": 0, "unknown_usage_reserved_tokens": 0, "failed_requests": 0},
                    settings={"max_total_tokens": 100000}, run_id="run", max_attempts=3,
                    active_call=None, unknown_reserve=0, stop_causes=set(), sleep=lambda _: None,
                    model_pool=None)
                state.save = lambda: state.report.update(budget_tokens=state.usage.total_tokens + state.report["unknown_usage_reserved_tokens"])
                path = local / "state/discovery.json"
                content = json.dumps({"queries": [{"domain_id": "dev", "term": "pytest"},
                    {"domain_id": "invented", "term": "bad"}, {"domain_id": "dev", "term": ["bad"]}]})
                result = ModelCallResult(ok=True, content=content, attempts=1, usage={"total_tokens": 150})
                observed = []
                def call(cfg, system, user, **kw):
                    checkpoint = load_discovery_state(path).pending_expansion
                    self.assertEqual(checkpoint["requests"][-1]["state"], "started")
                    self.assertIn("领域ID: dev", user)
                    self.assertIn("测试", user)
                    self.assertEqual(cfg["request"]["max_attempts"], 1)
                    observed.append(True)
                    if mode == "rotation" and len(observed) == 1:
                        return ModelCallResult(ok=False, http_status=403, attempts=1,
                            reason_code="QUOTA_EXHAUSTED", billing_state="rejected_before_inference")
                    if mode == "interrupt":
                        raise KeyboardInterrupt()
                    if mode == "unknown":
                        return ModelCallResult(ok=True, content=content, attempts=1)
                    return result
                class Pool:
                    def run(self, system, user, stage, invoke, **kw):
                        first = invoke(state.cfg["model"], {"type": "json_object"}, {})
                        if mode == "budget":
                            # 只允许已发生的第一次请求；第二次回调必须先拦截。
                            state.settings["max_total_tokens"] = state.report["budget_tokens"]
                            invoke(state.cfg["model"], {"type": "json_object"}, {})
                        return first, [first]
                if mode in ("pool", "budget"):
                    state.model_pool = Pool()
                if mode == "rotation":
                    from src.infra.model_pool import ModelPool
                    queue = {"provider": "dashscope", "endpoint": "https://example.test/chat/completions",
                             "models": ["first", "second"], "auth": {"api_key": "fake"}}
                    write_json_atomic(local / "config/models/model.json", queue)
                    state.model_pool = ModelPool(queue, local, log=lambda _: None)
                    state.model_pool.start()
                if mode == "resume":
                    state.discovery_state.pending_expansion = {"task_id": "saved", "attempt": 1,
                        "requests": [], "response": {"ok": True, "content": content}}
                with patch("src.catalog.evaluation.call_model", side_effect=call):
                    if mode in ("budget", "unknown"):
                        with self.assertRaises(PoolStopped) as caught:
                            _expand_search_queries(state)
                        self.assertEqual(caught.exception.reason, "token_limit" if mode == "budget" else "usage_unknown")
                    elif mode == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            _expand_search_queries(state)
                    else:
                        self.assertEqual(_expand_search_queries(state), [{"domain_id": "dev", "term": "pytest"}])
                        self.assertIn("-private", state.discovery_state.query_cursors["dev:pytest"]["q"])
                self.assertEqual(len(observed), 0 if mode == "resume" else 2 if mode == "rotation" else 1)
                self.assertEqual(state.usage.total_tokens, 0 if mode in ("unknown", "interrupt", "resume") else 150)
                self.assertEqual(recompute_usage_from_calls(state.report["calls"]).total_tokens, state.usage.total_tokens)
                saved = load_discovery_state(path)
                if mode in ("interrupt", "unknown"):
                    self.assertEqual(saved.pending_expansion["requests"][-1]["reservation_state"], "unknown")
                if mode == "budget":
                    # 已收到响应的请求在新 run 恢复时不得重新收费。
                    state.discovery_state = saved
                    with patch("src.catalog.evaluation.call_model", side_effect=AssertionError("duplicate payment")):
                        self.assertEqual(len(_expand_search_queries(state)), 1)

    def test_discovery_failures_limits_and_seed_paths(self):
        from src.catalog.batch import (DiscoveryStopped, acquire_next_repo_batch, expand_batch_skills,
            init_or_migrate_discovery_state, load_discovery_state, reconcile_batch_and_repositories)
        from src.catalog.pool import CandidatePool
        searches = {"per_domain": {"dev": {"en": ["one", "two"]}}}
        path = self.root / "guard/discovery.json"
        ds = init_or_migrate_discovery_state(path, None, searches)
        calls = []
        def failed(q, **kw):
            calls.append(kw["page"])
            self.assertEqual(kw["max_attempts"], 1)
            self.assertGreater(load_discovery_state(path).query_cursors[f"dev:{q.term}"]["page_attempts"], 0)
            return False, [], 0, "NETWORK_ERROR"
        with self.assertRaises(DiscoveryStopped) as caught:
            acquire_next_repo_batch(ds, path, search_fn=failed, limit_queries=0)
        self.assertEqual(caught.exception.reason, "discovery_limit")
        self.assertEqual(calls, [])
        with self.assertRaises(DiscoveryStopped) as caught:
            acquire_next_repo_batch(ds, path, search_fn=failed, max_attempts=2, sleep=lambda _: None)
        self.assertEqual(caught.exception.reason, "search_failed")
        self.assertEqual(calls, [1, 1, 1, 1])
        ds = load_discovery_state(path)
        with self.assertRaises(DiscoveryStopped):
            acquire_next_repo_batch(ds, path, search_fn=failed, max_attempts=2)
        self.assertEqual(len(calls), 4, "重启不得重置失败页尝试次数")
        self.assertTrue(all(not c["exhausted"] for c in ds.query_cursors.values()))

        seeds = {"sources": [{"id": "seed", "url": "https://github.com/owner/repo",
                               "skill_paths": ["a/SKILL.md", "b/SKILL.md"]},
                              {"id": "other", "url": "https://github.com/owner/other"}]}
        path = self.root / "seeds/discovery.json"
        ds = init_or_migrate_discovery_state(path, None, {}, seeds)
        acquire_next_repo_batch(ds, path, batch_repo_limit=2, sources_cfg=seeds)
        self.assertEqual(ds.repository_index["owner/repo"]["skill_paths"], ["a/SKILL.md", "b/SKILL.md"])
        self.assertFalse(ds.repository_index["owner/repo"]["expanded"])
        expanded = []
        def expand(owner, repo, **kw):
            expanded.append(repo)
            return ["c/SKILL.md"] if repo == "repo" else [], None
        pool = CandidatePool(items=[])
        pool_path = self.root / "seeds/pool.json"
        expand_batch_skills(ds, path, pool, pool_path, expand_repo_fn=expand, expand_limit=1)
        self.assertEqual(expanded, ["repo"])
        self.assertEqual(len(pool.items), 3)
        for item in pool.items:
            item.status = "done"
        with self.assertRaises(DiscoveryStopped) as caught:
            reconcile_batch_and_repositories(ds, path, pool, set(), set(), set())
        self.assertEqual(caught.exception.reason, "discovery_limit")
        self.assertEqual(ds.active_batch["stage"], "expanding")
        ds = load_discovery_state(path)
        expand_batch_skills(ds, path, pool, pool_path, expand_repo_fn=expand, expand_limit=1)
        self.assertEqual(expanded, ["repo", "other"])
        self.assertTrue(reconcile_batch_and_repositories(ds, path, pool, set(), set(), set()))

    def test_repair_stale_running_reports_fixes_interrupted_run(self):
        import json
        from src.catalog.local_state import repair_stale_running_reports
        from src.infra.files import write_json_atomic
        local = self.root / "stale_report_test"
        local.mkdir(parents=True, exist_ok=True)
        run_dir = local / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        rep = {"run_id": "test-run", "status": "running", "report_path": str(run_dir / "report.json")}
        write_json_atomic(local / "latest-run.json", rep)
        write_json_atomic(run_dir / "report.json", rep)

        repair_stale_running_reports(local)

        fixed_latest = json.loads((local / "latest-run.json").read_text(encoding="utf-8"))
        self.assertEqual(fixed_latest["status"], "interrupted")
        self.assertEqual(fixed_latest["stop_reason"], "interrupted")

        fixed_rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(fixed_rep["status"], "interrupted")
        self.assertEqual(fixed_rep["stop_reason"], "interrupted")

    def test_candidate_failure_isolates_and_does_not_halt_pool_run(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.catalog.local_candidate import process_candidate
        from src.catalog.pool import CandidatePool, PoolItem, save_pool
        from src.catalog.models import Candidate
        from src.catalog.prescreen import PrescreenConfig
        from src.infra.files import write_json_atomic
        from src.catalog.budget import BudgetLedger

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
            root=self.root, local=local, pool=pool, pool_path=pool_path,
            ledger=ledger, owned_ids=set(), skipped_owned_ids=set(),
            active_snoozed=set(), manual_exclusions=set(), manual_picks=set(),
            settings={"target_recommended": 10, "max_total_tokens": 100000, "max_format_failures_without_valid_result": 5, "max_consecutive_failures": 5},
            report={"new_recommended": 0, "budget_tokens": 0, "evaluations": 0, "checked": 0,
                    "not_skill_files": 0, "prescreen_excluded": 0, "fetch_failed": 0, "static_skipped": 0,
                    "cached": 0, "failed_evaluations": 0, "skipped_output_format": 0, "skipped_length_exceeded": 0,
                    "blocked_records": 0, "blocked_new": 0, "calls": [], "models_used": [], "stop_causes": [], "stop_reason": None},
            cfg={"model": {"provider": "dashscope", "endpoint": "https://example.test", "model": "test-m",
                           "request": {"max_attempts": 2}, "auth": {"api_key": "fake"}},
                 "rules": {"rules_version": "v1"}, "taxonomy": {}, "prescreen": PrescreenConfig(domain_names={"dev": "开发"}, manual_exclusions=set())},
            stop_causes=set(), consecutive_failures=0, format_failures=0, max_format_failures=5, max_attempts=2,
            active_eid=None, active_call=None, unknown_reserve=0, pending_items=[item1, item2],
            evaluated_skill_ids=set(), entries={},
            fetch_fn=lambda url, **kw: SimpleNamespace(ok=True, text="---\nname: skill\n---\nbody", truncated=False),
            log=lambda msg: None, save=lambda: None, sleep=lambda s: None,
            model_pool=Mock(),
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

        call_mock = SimpleNamespace(ok=False, requested_model="test-m", billing_state=None,
                                    attempts=1, usage={"total_tokens": 50}, reason_code="OUTPUT_FORMAT_INVALID",
                                    http_status=200, error="Invalid format", error_type=None, latency_ms=10)
        state.evaluate_fn = Mock(return_value={
            "ok": False, "evaluation": None, "call": call_mock, "calls": [call_mock],
            "stage": "assessment", "reason_code": "OUTPUT_FORMAT_INVALID",
            "error_kind": "OUTPUT_FORMAT_INVALID", "error": "Invalid format",
        })

        cont = process_candidate(state, item1)
        self.assertTrue(cont, "单条候选失败不得中止后续候选处理")
        self.assertIsNone(state.report.get("stop_reason"), "单条候选失败不得设置全局 stop_reason")
        self.assertEqual(item1.status, "blocked", "格式校验失败候选应持久化为 blocked")
        self.assertEqual(state.report["skipped_output_format"], 1)


if __name__ == "__main__":
    unittest.main()
