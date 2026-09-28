"""Offline integration tests for the fixed two-candidate runner."""
import json
import threading
import time
import unittest

from tests import test_local_run as fixtures


class ParallelCollectionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LocalRunTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Exercise the production default, not a test-only scheduler entry.
        self.fixture.settings.pop('parallel_evaluation')

    def records(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in
                (self.fixture.root / 'data/local/state/evaluations').glob('*.json')]

    def test_two_overlap_and_callbacks_remain_attached_to_candidate(self):
        f = self.fixture
        f.settings['target_recommended'] = 4
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        active = peak = 0

        def evaluate(candidate, text, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                callback = kwargs['on_request']
                callback('before', 'assessment', None)
                barrier.wait()
                result = f.evaluate(candidate, text, **kwargs)
                result['call'].ok = True
                callback('after', 'assessment', result['call'])
                self.assertTrue(callback('before', 'review', None))
                barrier.wait()
                callback('after', 'review', result['call'])
                return result
            finally:
                with lock:
                    active -= 1

        report = f.collect(count=8, evaluate_fn=evaluate)
        self.assertEqual(report['stop_reason'], 'target_reached')
        self.assertEqual(peak, 2)
        self.assertEqual(report['new_recommended'], 4)
        self.assertEqual(report['usage']['total_tokens'], 800)
        records = self.records()
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertEqual(record['status'], 'completed')
            self.assertEqual([r['stage'] for r in record['requests']], ['assessment', 'review'])
            self.assertTrue(all(r['skill_id'] == record['skill_id'] for r in record['requests']))
        # Restart must not incur another paid evaluation for completed records.
        f.settings['target_recommended'] = 1
        f.settings['refresh_pool'] = True
        report = f.collect(count=4, evaluate_fn=lambda *a, **k: self.fail('unexpected model call'))
        self.assertEqual(report['evaluations'], 0)

    def test_tight_budget_waits_for_inflight_usage(self):
        f = self.fixture
        f.settings['max_total_tokens'] = 100

        def evaluate(candidate, text, **kwargs):
            time.sleep(0.05)
            return f.evaluate(candidate, text, **kwargs)

        report = f.collect(evaluate_fn=evaluate)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(report['evaluations'], 1)
        self.assertEqual(report['usage']['total_tokens'], 100)
        self.assertEqual(report['stop_reason'], 'token_limit')

    def test_one_remaining_target_does_not_start_second_request(self):
        f = self.fixture
        f.settings['target_recommended'] = 1
        report = f.collect()
        self.assertEqual(report['new_recommended'], 1)
        self.assertEqual(len(f.calls), 1)

    def test_interrupt_preserves_other_inflight_result_and_recovery(self):
        f = self.fixture
        barrier = threading.Barrier(2, timeout=5)

        def evaluate(candidate, text, **kwargs):
            kwargs['on_request']('before', 'assessment', None)
            barrier.wait()
            if candidate.name == 'tool-0':
                raise KeyboardInterrupt()
            time.sleep(0.05)
            result = f.evaluate(candidate, text, **kwargs)
            kwargs['on_request']('after', 'assessment', result['call'])
            return result

        report = f.collect(evaluate_fn=evaluate)
        self.assertEqual(report['stop_reason'], 'interrupted')
        self.assertEqual(report['usage']['total_tokens'], 100)
        self.assertGreater(report['unknown_usage_reserved_tokens'], 0)
        self.assertCountEqual([r['status'] for r in self.records()], ['needs_recovery', 'completed'])

    def test_evaluation_cap_is_shared(self):
        f = self.fixture
        f.settings['max_evaluations'] = 1
        def evaluate(candidate, text, **kwargs):
            callback = kwargs['on_request']
            callback('before', 'assessment', None)
            time.sleep(0.05)
            result = f.evaluate(candidate, text, **kwargs)
            callback('after', 'assessment', result['call'])
            self.assertTrue(callback('before', 'review', None))
            callback('after', 'review', result['call'])
            return result
        report = f.collect(evaluate_fn=evaluate)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(report['stop_reason'], 'evaluation_limit')

    def test_unknown_usage_stops_new_work_but_saves_other_result(self):
        f = self.fixture
        f.settings['target_recommended'] = 4
        barrier = threading.Barrier(2, timeout=5)
        def evaluate(candidate, text, **kwargs):
            callback = kwargs['on_request']
            callback('before', 'assessment', None)
            barrier.wait()
            result = f.evaluate(candidate, text, **kwargs)
            if candidate.name == 'tool-0':
                result['call'].usage = None
            else:
                time.sleep(0.05)
            callback('after', 'assessment', result['call'])
            return result
        report = f.collect(evaluate_fn=evaluate)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(report['stop_reason'], 'usage_unknown')
        self.assertEqual(report['usage']['total_tokens'], 100)
        self.assertEqual(len(self.records()), 2)
