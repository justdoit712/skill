"""Fixed two-candidate scheduler; all shared mutations are serialized.

Only the evaluator runs outside the state lock. Its request callbacks re-enter
the lock before touching accounting or checkpoints. No background work survives
the catalog session, including on interruption.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context
from threading import Condition

from .failure_policy import STOP_INTERRUPTED, STOP_TOKEN_LIMIT


class _CandidateState:
    _private = frozenset({"_shared", "_scheduler", "active_eid", "active_call",
                          "unknown_reserve"})

    def __init__(self, shared, scheduler):
        self._shared = shared
        self._scheduler = scheduler
        self.active_eid = None
        self.active_call = None
        self.unknown_reserve = 0

    def __getattr__(self, name):
        return getattr(self._shared, name)

    def __setattr__(self, name, value):
        if name in self._private:
            object.__setattr__(self, name, value)
        else:
            setattr(self._shared, name, value)

    def wait_for_budget(self):
        scheduler = self._scheduler
        while True:
            if self.report.get("stop_reason"):
                return False
            if self.report['budget_tokens'] >= self.settings['max_total_tokens']:
                self.stop_causes.add(STOP_TOKEN_LIMIT)
                self.report['stop_reason'] = STOP_TOKEN_LIMIT
                return False
            others = sum(value for worker, value in scheduler.reservations.items()
                         if worker is not self)
            # Preserve the serial runner's last-request policy. Additional
            # concurrent work must fit alongside the estimated in-flight cost.
            if not others or (self.report['budget_tokens'] + others + self.unknown_reserve
                              <= self.settings['max_total_tokens']):
                return True
            scheduler.condition.wait()

    def evaluate_fn(self, *args, **kwargs):
        scheduler = self._scheduler
        callback = kwargs['on_request']

        def synchronized_callback(*args):
            with scheduler.condition:
                if args[0] == 'before' and isinstance(args[2], dict):
                    reserve = args[2].get('reserved_tokens', 0)
                    while not self.report.get('stop_reason'):
                        others = sum(c.get('reserved_tokens', 0) for c in self.report['calls']
                                     if c.get('reservation_state') == 'active')
                        if (not others or self.report['budget_tokens'] + reserve > self.settings['max_total_tokens']
                                or self.report['budget_tokens'] + others + reserve <= self.settings['max_total_tokens']):
                            break
                        scheduler.condition.wait()
                try:
                    return callback(*args)
                finally:
                    scheduler.condition.notify_all()

        kwargs['on_request'] = synchronized_callback
        scheduler.reservations[self] = self.unknown_reserve
        scheduler.condition.release()
        try:
            return self._shared.evaluate_fn(*args, **kwargs)
        finally:
            scheduler.condition.acquire()
            scheduler.reservations.pop(self, None)
            scheduler.condition.notify_all()


class TwoCandidateScheduler:
    def __init__(self, state, process):
        self.state = state
        self.process = process
        self.condition = Condition()
        self.reservations = {}
        self.active = set()

    def _run_one(self, item):
        with self.condition:
            state = self.state
            # Near the target wait for the other result before spending more.
            while (self.active and not state.report.get('stop_reason') and (
                   state.report['new_recommended'] + len(self.active)
                   >= state.settings['target_recommended'] or
                   (state.settings.get('max_evaluations') and
                    state.report['evaluations'] >= state.settings['max_evaluations']))):
                self.condition.wait()
            if state.report.get('stop_reason'):
                return False
            worker = _CandidateState(state, self)
            self.active.add(worker)
            state.pending_items.append(item)
            try:
                return self.process(worker, item)
            except BaseException as exc:
                self.stop(STOP_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else 'error')
                if worker.active_eid:
                    state.ledger.mark_needs_recovery(worker.active_eid,
                        '并行评估异常中断，禁止自动重复付费请求')
                    if worker.active_call and worker.active_call.get('usage') is None:
                        state.usage.add(None)
                        state.report['unknown_usage_reserved_tokens'] += worker.unknown_reserve
                        worker.active_call['status'] = 'unknown'
                state.save()
                raise
            finally:
                self.active.discard(worker)
                self.condition.notify_all()

    def stop(self, reason=STOP_INTERRUPTED):
        with self.condition:
            self.state.stop_causes.add(reason)
            self.state.report['stop_reason'] = reason
            self.condition.notify_all()

    def run(self, items):
        iterator = iter(items)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='catalog-eval') as executor:
            pending = set()
            stopped = False
            try:
                for _ in range(2):
                    item = next(iterator, None)
                    if item is not None:
                        pending.add(executor.submit(copy_context().run, self._run_one, item))
                while pending:
                    done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        if not future.result():
                            stopped = True
                    with self.condition:
                        stopped = stopped or bool(self.state.report.get('stop_reason'))
                    if not stopped:
                        for _ in range(len(done)):
                            item = next(iterator, None)
                            if item is not None:
                                pending.add(executor.submit(copy_context().run, self._run_one, item))
                return not stopped
            except BaseException as exc:
                self.stop(STOP_INTERRUPTED if isinstance(exc, KeyboardInterrupt) else 'error')
                for future in pending:
                    future.cancel()
                raise
