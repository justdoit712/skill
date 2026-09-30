"""Offline regressions for request admission, recovery and usage reconciliation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.finder.run import (
    FinderRunState, RunStopped, _evaluate_candidates_concurrent,
    estimate_request_token_bound, execute_find_skill,
)
from src.infra.llm import ModelCallResult
from src.shared.models import Candidate
from src.shared.usage import recompute_usage_from_calls


PLAN = {"intent": "comfort", "queries": ["comfort"], "criteria": [
    {"id": "comfort", "kind": "required", "description": "comfort"},
]}
CFG = {"endpoint": "https://fake.invalid", "model": "fake", "auth": {"api_key": "fake"},
       "limits": {"max_output_tokens": 100}}


def candidate(n):
    return Candidate(f"owner/repo{n}:SKILL.md", "owner", f"repo{n}", "SKILL.md", name=f"comfort{n}")


def result(match="strong", tokens=20):
    return ModelCallResult(ok=True, attempts=1, finish_reason="stop", usage={
        "prompt_tokens": tokens - 10, "completion_tokens": 10, "total_tokens": tokens,
    }, content=json.dumps({"match": match, "documentation": "clear", "criteria_results": [
        {"criterion_id": "comfort", "status": "supported" if match == "strong" else "unsupported",
         "evidence": [{"source_path": "SKILL.md", "start_line": 1, "end_line": 1,
                       "quote": "comfort"}] if match == "strong" else []},
    ]}))


class FinderRequestAdmissionTest(unittest.TestCase):
    def state(self, **params):
        state = FinderRunState("comfort", {"limit": 1, "max_tokens": 100000, "max_evaluations": 2, **params})
        state.report["plan"] = deepcopy(PLAN)
        return state

    def call(self, state, transport, sid="candidate", cfg=None, user="user"):
        return state.call(transport, cfg or CFG, "system", user, api_key="fake",
                          sleep=lambda _: None, candidate_id=sid)

    def test_last_slot_executes_and_reports_target_in_concurrent_runner(self):
        state = self.state(max_evaluations=1)
        transport = Mock(return_value=result())
        reason = _evaluate_candidates_concurrent(
            state, [candidate(1), candidate(2)], CFG, "fake", transport,
            lambda *a, **k: (True, {"SKILL.md": "comfort"}, None), lambda _: None,
            log=lambda *a: None,
        )
        self.assertEqual(reason, "target_reached")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(state.report["evaluation_attempts"], 1)
        self.assertEqual(len(state.report["evaluations"]), 1)
        self.assertEqual(state.reserved_tokens, 0)

    def test_exact_token_and_attempt_boundary_admits_request_once(self):
        bound = estimate_request_token_bound(CFG, "system", "user")
        state = self.state(max_tokens=bound, max_evaluations=1)
        transport = Mock(return_value=result(tokens=bound))
        self.call(state, transport)
        self.assertEqual(state.usage.total_tokens, bound)
        with self.assertRaises(RunStopped):
            self.call(state, transport, "another")
        self.assertEqual(transport.call_count, 1)

    def test_input_and_output_and_schema_all_contribute_to_reservation(self):
        fmt = {"type": "json_schema", "schema": "字段" * 200}
        config = deepcopy(CFG)
        config["request"] = {"response_format": fmt}
        expected = estimate_request_token_bound(config, "system", "中文" * 100, fmt)
        self.assertGreaterEqual(expected, 600 + 100 + len(json.dumps(fmt, ensure_ascii=False).encode("utf-8")))
        state = self.state(max_tokens=expected - 1)
        transport = Mock()
        with self.assertRaises(RunStopped) as stopped:
            self.call(state, transport, cfg=config, user="中文" * 100)
        self.assertEqual(stopped.exception.reason, "token_limit")
        transport.assert_not_called()
        self.assertEqual(state.report["calls"], [])

    def test_two_inflight_requests_have_distinct_persisted_reservations(self):
        bound = estimate_request_token_bound(CFG, "system", "user")
        state = self.state(max_tokens=bound * 2, max_evaluations=2)
        barrier = threading.Barrier(2)
        observed = []

        def transport(*args, **kwargs):
            barrier.wait(timeout=5)
            with state._lock:
                observed.append(deepcopy(state.report["calls"]))
            barrier.wait(timeout=5)
            return result(tokens=bound)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.call, state, transport, str(i)) for i in range(2)]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(state.report["evaluation_attempts"], 2)
        self.assertEqual(state.usage.total_tokens, bound * 2)
        self.assertEqual(len({c["request_id"] for c in observed[0]}), 2)
        self.assertTrue(all(c["reservation_state"] == "active" for c in observed[0]))
        self.assertEqual(sum(c["reserved_tokens"] for c in observed[0]), bound * 2)
        self.assertEqual(state.reserved_tokens, 0)
        self.assertEqual(state.report["usage_audit"]["status"], "matched")

    def test_unknown_usage_keeps_reservation_and_blocks_further_calls(self):
        state = self.state()
        transport = Mock(side_effect=TimeoutError("read timed out"))
        with self.assertRaises(TimeoutError):
            self.call(state, transport)
        self.assertGreater(state.reserved_tokens, 0)
        self.assertEqual(state.report["calls"][0]["reservation_state"], "unknown")
        with self.assertRaises(RunStopped) as stopped:
            self.call(state, transport, "another")
        self.assertEqual(stopped.exception.reason, "usage_unknown")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(state.report["usage_audit"]["status"], "matched")

    def test_failed_presend_write_does_not_send_or_hold_reservation(self):
        state = self.state()
        transport = Mock()
        with patch.object(state, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.call(state, transport)
        transport.assert_not_called()
        self.assertEqual(state.reserved_tokens, 0)
        self.assertEqual(state.report["evaluation_attempts"], 0)
        state.save()
        self.assertEqual(state.report["usage_audit"]["status"], "matched")

    def test_not_sent_result_does_not_become_an_unknown_billed_request(self):
        state = self.state()
        self.call(state, Mock(return_value=ModelCallResult(ok=False, attempts=0, error="local validation")))
        self.assertEqual(state.usage.requests, 0)
        self.assertEqual(state.reserved_tokens, 0)
        self.assertEqual(recompute_usage_from_calls(state.report["calls"]).requests, 0)
        self.assertEqual(state.report["usage_audit"]["status"], "matched")

    def test_success_replaces_stale_audit_and_started_is_not_unknown(self):
        state = self.state()
        state.report["usage_audit"] = {"status": "discrepancy_detected"}

        def transport(*args, **kwargs):
            self.assertEqual(state.report["calls"][0]["state"], "started")
            self.assertEqual(state.report["usage_audit"]["status"], "matched")
            return result()

        self.call(state, transport)
        self.assertEqual(state.report["usage_audit"]["status"], "matched")
        self.assertEqual(state.usage.unknown_usage_requests, 0)

    def test_concurrent_worker_exception_is_not_reported_as_exhausted(self):
        state = self.state()
        with self.assertRaisesRegex(OSError, "fetch failed"):
            _evaluate_candidates_concurrent(
                state, [candidate(1)], CFG, "fake", Mock(),
                Mock(side_effect=OSError("fetch failed")), lambda _: None, log=lambda *a: None)

    def test_plain_403_keeps_unknown_usage_protection(self):
        state = self.state()
        transport = Mock(return_value=ModelCallResult(
            ok=False, attempts=1, http_status=403, error="Free quota exhausted",
        ))
        reason = _evaluate_candidates_concurrent(
            state, [candidate(1)], CFG, "fake", transport,
            lambda *a, **k: (True, {"SKILL.md": "comfort"}, None), lambda _: None,
            log=lambda *a: None)
        self.assertEqual(reason, "usage_unknown")
        self.assertEqual(state.usage.unknown_usage_requests, 1)
        self.assertGreater(state.reserved_tokens, 0)


class FinderMultiRequestRecoveryTest(unittest.TestCase):
    def test_all_received_checkpoints_replay_locally_and_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "data" / "local" / "find-skills" / "saved"
            run_dir.mkdir(parents=True)
            state = FinderRunState("comfort", {"limit": 2, "max_tokens": 100000,
                                               "max_evaluations": 2, "concurrency": 2}, run_dir)
            state.report.update(plan=deepcopy(PLAN), run_id="saved",
                                report_paths={"json": str(run_dir / "report.json")})
            for n in (1, 2):
                cand = candidate(n)
                response = result()
                request_id = f"request-{n}"
                state.report["calls"].append({
                    "request_id": request_id, "stage": "evaluation", "skill_id": cand.skill_id,
                    "state": "received", "usage": state.usage.add(response),
                    "response": {k: getattr(response, k) for k in
                                 ("ok", "content", "error", "reason_code", "finish_reason")},
                    "reserved_tokens": 10000, "reservation_state": "settled",
                })
                state.report["pending_evaluations"][cand.skill_id] = {
                    "request_id": request_id, "candidate": asdict(cand),
                    "materials": {"SKILL.md": "comfort"}, "manifest": {},
                }
            state.report["evaluation_attempts"] = 2
            state.save()
            forbidden = Mock(side_effect=AssertionError("Recovery must not access network"))
            for attempt in range(2):
                report = execute_find_skill(
                    root_dir=root, resume_dir=run_dir, model_cfg=CFG, owned_ids=set(),
                    max_clarification_turns=0, call_model_fn=forbidden,
                    search_github_repos_fn=forbidden, expand_and_collect_candidates_fn=forbidden,
                    fetch_candidate_materials_fn=forbidden, log=lambda *a: None)
                self.assertEqual(report["stop_reason"], "target_reached")
                self.assertEqual(report["evaluated_count"], 2)
                self.assertEqual(report["evaluation_attempts"], 2)
                self.assertEqual(report["usage"]["total_tokens"], 40)
                self.assertEqual(report["pending_evaluations"], {})
                self.assertEqual(report["usage_audit"]["status"], "matched")
            forbidden.assert_not_called()

    def test_resume_after_settled_error_or_unknown_usage_does_not_lock_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "data" / "local" / "find-skills" / "saved_timeout"
            run_dir.mkdir(parents=True)
            state = FinderRunState("comfort", {"limit": 1, "max_tokens": 100000,
                                               "max_evaluations": 2, "concurrency": 1}, run_dir)
            state.report.update(plan=deepcopy(PLAN), run_id="saved_timeout",
                                report_paths={"json": str(run_dir / "report.json")})
            # 候选 1 发生了网络超时报错 (已结算但无用量数据，曾导致未知用量熔断)
            cand1 = candidate(1)
            err_resp = ModelCallResult(ok=False, attempts=1, error="Read timed out")
            state.report["calls"].append({
                "request_id": "req-timeout", "stage": "evaluation", "skill_id": cand1.skill_id,
                "state": "error", "usage": state.usage.add(err_resp),
                "response": {"ok": False, "error": "Read timed out", "reason_code": "NETWORK_ERROR"},
                "reserved_tokens": 10000, "reservation_state": "settled",
            })
            state.report["search"]["processed_skill_ids"] = [cand1.skill_id]
            state.report["status"] = "stopped"
            state.report["stop_reason"] = "usage_unknown"
            state.save()
            self.assertEqual(state.usage.unknown_usage_requests, 1)

            # 续跑时，不应在开局被上一轮已终态的报错锁死，应能正常调度候选 2
            cand2 = candidate(2)
            model_mock = Mock(return_value=result())
            search_mock = Mock(return_value=(True, [{"owner": "owner", "repo": "repo2", "url": "https://github.com/owner/repo2"}], None))
            expand_mock = Mock(return_value=([cand2], []))
            fetch_mock = Mock(return_value=(True, {"SKILL.md": "comfort"}, None))

            report = execute_find_skill(
                root_dir=root, resume_dir=run_dir, model_cfg=CFG, owned_ids=set(),
                max_clarification_turns=0, call_model_fn=model_mock,
                search_github_repos_fn=search_mock, expand_and_collect_candidates_fn=expand_mock,
                fetch_candidate_materials_fn=fetch_mock, log=lambda *a: None,
                enable_active_reflection=False)
            
            self.assertEqual(report["stop_reason"], "target_reached")
            self.assertEqual(report["evaluated_count"], 1)
            self.assertEqual(len(report["shortlist"]), 1)
            self.assertEqual(model_mock.call_count, 1)



if __name__ == "__main__":
    unittest.main()
