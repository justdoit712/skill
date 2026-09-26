"""多轮检索、预算与检查点；所有接口离线替换，文件只写临时目录。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.finder.run import execute_find_skill, main
from src.finder.plan import parse_reflection_queries
from src.finder.search import expand_and_collect_candidates, search_github_repos_for_query
from src.infra.github import search_repositories
from src.infra.llm import ModelCallResult
from src.shared.models import Candidate


PLAN = {"intent": "organize", "queries": ["organize"],
        "criteria": [{"id": "organize", "kind": "required", "description": "organize"}]}


def response(data, tokens=100):
    return ModelCallResult(ok=True, content=json.dumps(data), attempts=1,
                           usage={"prompt_tokens": tokens - 20, "completion_tokens": 20, "total_tokens": tokens})


def evaluated(match="none", path="SKILL.md", documentation="clear", tokens=100):
    return response({"match": match, "documentation": documentation, "summary_zh": "organize",
        "criteria_results": [{"criterion_id": "organize", "status": "supported" if match == "strong" else "unsupported",
        "evidence": [{"source_path": path, "start_line": 1, "end_line": 1, "quote": "organize"}] if match == "strong" else []}]}, tokens)


def repo(n):
    return {"owner": "owner", "repo": f"r{n}", "url": f"https://github.com/owner/r{n}", "description": ""}


def candidate(n, path="SKILL.md"):
    return Candidate(skill_id=f"owner/r{n}:{path}", owner="owner", repo=f"r{n}", path=path,
                     name=f"r{n}", url=f"https://github.com/owner/r{n}/blob/HEAD/{path}", repo_url=repo(n)["url"])


class RefillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = Mock(side_effect=[response(PLAN), evaluated("strong")])
        self.search = Mock(return_value=(True, [repo(0)], None))
        self.expand = Mock(side_effect=lambda repos, **kw: ([candidate(int(r["repo"][1:])) for r in repos], []))
        self.fetch = Mock(side_effect=lambda c, **kw: (True, {c.path: "organize"}, None))

    def run_find(self, **kwargs):
        args = dict(root_dir=self.root, model_cfg={"endpoint": "https://fake", "model": "fake", "auth": {"api_key": "fake"}},
                    call_model_fn=self.model, search_github_repos_fn=self.search, expand_and_collect_candidates_fn=self.expand,
                    fetch_candidate_materials_fn=self.fetch, max_clarification_turns=0, limit=1,
                    log=lambda *a: None, sleep=lambda *a: None)
        args.update(kwargs)
        return execute_find_skill("organize", **args)

    def resume(self, report, **kwargs):
        return self.run_find(resume_dir=Path(report["report_paths"]["json"]).parent, **kwargs)

    def test_target_stops_before_next_fetch_and_wins_known_budget_tie(self):
        self.search.return_value = True, [repo(0), repo(1)], None
        self.model.side_effect = [response(PLAN, 900), evaluated("strong", tokens=100)]
        r = self.run_find(max_tokens=1000, max_evaluations=1)
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(r["evaluation_attempts"], 1)
        self.model.reset_mock(); self.search.reset_mock(); self.fetch.reset_mock()
        again = self.resume(r)
        self.assertEqual(again["stop_reason"], "target_reached")
        self.model.assert_not_called(); self.search.assert_not_called(); self.fetch.assert_not_called()

    def test_insufficient_documentation_does_not_count_towards_target(self):
        self.search.return_value = True, [repo(0), repo(1)], None
        self.model.side_effect = [response(PLAN), evaluated("strong", documentation="insufficient"), evaluated("strong")]
        r = self.run_find()
        self.assertEqual(r["evaluated_count"], 2)
        self.assertEqual(r["shortlist_count"], 1)

    def test_pagination_is_next_round_and_repeated_repositories_are_not_expanded(self):
        self.search.side_effect = [(True, [repo(i) for i in range(20)], None), (True, [repo(0), repo(20)], None)]
        self.model.side_effect = [response(PLAN)] + [evaluated() for _ in range(20)] + [evaluated("strong")]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual(r["search"]["current_round"], 2)
        self.assertEqual([c.kwargs["page"] for c in self.search.call_args_list], [1, 2])
        self.assertEqual(self.expand.call_count, 21)
        self.assertEqual(r["evaluated_count"], 21)

    def test_reflection_changes_queries_only_and_counts_tokens(self):
        self.search.side_effect = [(True, [repo(0)], None), (True, [repo(1)], None), (True, [], None), (True, [], None)]
        self.model.side_effect = [response(PLAN), evaluated(), response({"queries": ["tagging", "sorting", "taxonomy"]}), evaluated("strong")]
        r = self.run_find()
        self.assertEqual(r["plan"], PLAN)
        self.assertEqual(r["usage"]["total_tokens"], 400)
        self.assertEqual(r["evaluation_attempts"], 2)
        self.assertEqual(r["calls"][2]["stage"], "reflection")
        self.assertEqual(r["search"]["rounds_history"][1]["strategy"], "llm_reflection")

    def test_round_limit_is_bounded_even_with_empty_results(self):
        self.search.return_value = True, [], None
        self.model.side_effect = [response(PLAN), response({"queries": ["a", "b", "c"]}), response({"queries": ["d", "e", "f"]})]
        r = self.run_find()
        self.assertEqual((r["status"], r["stop_reason"]), ("stopped", "round_limit"))
        self.assertEqual(r["search"]["current_round"], 3)
        self.assertEqual(self.model.call_count, 3)
        self.assertEqual(self.search.call_count, 7)
        with patch("src.finder.run.execute_find_skill", return_value=r):
            self.assertEqual(main(["organize"], root=self.root), 2)

    def test_budget_after_planning_prevents_search(self):
        self.model.side_effect = [response(PLAN, 1000)]
        r = self.run_find(max_tokens=1000)
        self.assertEqual(r["stop_reason"], "token_limit")
        self.search.assert_not_called()

    def test_budget_after_reflection_prevents_new_search(self):
        self.model.side_effect = [response(PLAN, 400), evaluated(tokens=400), response({"queries": ["a", "b", "c"]}, 200)]
        r = self.run_find(max_tokens=1000)
        self.assertEqual(r["stop_reason"], "token_limit")
        self.assertEqual(self.search.call_count, 1)

    def test_resume_uses_pending_queue_without_repeating_search_or_failed_eval(self):
        self.search.return_value = True, [repo(0), repo(1)], None
        self.model.side_effect = [response(PLAN), response("invalid schema")]
        r = self.run_find(max_evaluations=1)
        self.assertEqual(r["stop_reason"], "evaluation_limit")
        self.search.reset_mock(); self.expand.reset_mock(); self.fetch.reset_mock()
        self.model.side_effect = [evaluated("strong")]
        resumed = self.resume(r, max_evaluations=2)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.search.assert_not_called(); self.expand.assert_not_called()
        self.assertEqual(self.fetch.call_args.args[0].skill_id, candidate(1).skill_id)
        self.assertEqual(resumed["usage"]["total_tokens"], 300)

    def test_interrupt_fetch_resumes_queue(self):
        self.fetch.side_effect = KeyboardInterrupt
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "interrupted")
        self.search.reset_mock(); self.expand.reset_mock()
        self.fetch.side_effect = lambda c, **kw: (True, {c.path: "organize"}, None)
        resumed = self.resume(r)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.search.assert_not_called(); self.expand.assert_not_called()

    def test_unknown_interrupted_paid_call_cannot_be_replayed(self):
        self.model.side_effect = [response(PLAN), KeyboardInterrupt]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "interrupted")
        self.model.reset_mock(); self.search.reset_mock()
        resumed = self.resume(r)
        self.assertEqual(resumed["stop_reason"], "usage_unknown")
        self.model.assert_not_called(); self.search.assert_not_called()

    def test_received_evaluation_is_recovered_without_refetch_or_new_paid_call(self):
        with patch("src.finder.run._record_evaluation", side_effect=KeyboardInterrupt):
            r = self.run_find(max_evaluations=1)
        self.assertEqual(r["stop_reason"], "interrupted")
        self.assertIn("pending_evaluation", r)
        self.model.reset_mock(); self.fetch.reset_mock(); self.search.reset_mock()
        resumed = self.resume(r, max_evaluations=1)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.assertEqual(resumed["evaluation_attempts"], 1)
        self.assertEqual(resumed["usage"]["total_tokens"], 200)
        self.model.assert_not_called(); self.fetch.assert_not_called(); self.search.assert_not_called()

    def test_received_reflection_is_recovered_without_paying_twice(self):
        self.search.side_effect = [(True, [repo(0)], None), (True, [repo(1)], None), (True, [], None), (True, [], None)]
        self.model.side_effect = [response(PLAN), evaluated(), response({"queries": ["a", "b", "c"]}), evaluated("strong")]
        with patch("src.finder.refill.parse_reflection_queries", side_effect=KeyboardInterrupt):
            r = self.run_find()
        self.assertEqual(r["stop_reason"], "interrupted")
        self.model.reset_mock()
        resumed = self.resume(r)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.assertEqual(self.model.call_count, 1)
        self.assertEqual(resumed["usage"]["total_tokens"], 400)

    def test_unknown_usage_beats_target_and_never_calls_next_candidate(self):
        strong = evaluated("strong")
        strong.usage = {}
        self.model.side_effect = [response(PLAN), strong]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "usage_unknown")
        self.assertEqual(r["shortlist_count"], 1)

    def test_last_candidate_uses_budget_before_reflection(self):
        self.model.side_effect = [response(PLAN), evaluated()]
        r = self.run_find(max_evaluations=1)
        self.assertEqual(r["stop_reason"], "evaluation_limit")
        self.assertEqual(self.model.call_count, 2)

    def test_legacy_resume_reconstructs_state_and_skips_existing_results(self):
        r = self.run_find()
        p = Path(r["report_paths"]["json"])
        r["parameters"]["limit"] = 2
        r.pop("evaluation_attempts")
        r.pop("calls")
        r["search"] = {}
        p.write_text(json.dumps(r), encoding="utf-8")
        self.search.return_value = True, [repo(0), repo(1)], None
        self.model.side_effect = [evaluated("strong")]
        self.fetch.reset_mock()
        resumed = self.resume(r, limit=2)
        self.assertEqual(resumed["stop_reason"], "target_reached")
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(resumed["evaluation_attempts"], 2)

    def test_two_none_rounds_trigger_reflection_before_page_three(self):
        self.search.side_effect = [(True, [repo(i) for i in range(20)], None),
                                  (True, [repo(i) for i in range(20, 40)], None),
                                  (True, [repo(40)], None), (True, [], None), (True, [], None)]
        self.model.side_effect = ([response(PLAN)] + [evaluated() for _ in range(40)] +
                                 [response({"queries": ["a", "b", "c"]}), evaluated("strong")])
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual([c.kwargs["page"] for c in self.search.call_args_list], [1, 2, 1, 1, 1])

    def test_search_failure_is_not_mistaken_for_no_results(self):
        self.search.return_value = False, [], "HTTP 429"
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "search_failed")
        self.assertEqual(self.model.call_count, 1)

    def test_invalid_round_count_is_rejected_before_model_call(self):
        with self.assertRaises(ValueError):
            self.run_find(max_rounds=0)
        self.model.assert_not_called()

    def test_cli_resume_does_not_replace_original_topic_with_runner_default(self):
        r = self.run_find()
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        (config / "find-skill.json").write_text(json.dumps({"topic": "a different default"}), encoding="utf-8")
        directory = str(Path(r["report_paths"]["json"]).parent)
        with patch("src.finder.run.execute_find_skill", return_value=r) as execute:
            self.assertEqual(main(["--resume", directory], root=self.root), 0)
        self.assertEqual(execute.call_args.args[0], "")

    def test_resume_rejects_explicitly_changed_topic(self):
        r = self.run_find()
        with self.assertRaisesRegex(ValueError, "原始需求"):
            execute_find_skill("different", resume_dir=Path(r["report_paths"]["json"]).parent, root_dir=self.root)

    def test_over_80_candidates_in_one_repo_are_preserved(self):
        candidates = [candidate(0, f"skills/{i}/SKILL.md") for i in range(81)]
        self.expand.side_effect = None
        self.expand.return_value = candidates, []
        self.model.side_effect = [response(PLAN)] + [evaluated() for _ in range(80)] + [evaluated("strong", candidates[-1].path)]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual(r["evaluated_count"], 81)
        self.assertEqual(self.search.call_count, 1)

    def test_remaining_discovered_repos_are_used_before_another_search(self):
        plan = dict(PLAN, queries=["organize", "sort"])
        self.search.side_effect = [(True, [repo(i) for i in range(20)], None), (True, [repo(i) for i in range(20, 40)], None)]
        self.model.side_effect = [response(plan)] + [evaluated() for _ in range(20)] + [evaluated("strong")]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "target_reached")
        self.assertEqual(self.search.call_count, 2)
        self.assertEqual(r["search"]["current_round"], 1)
        self.assertEqual(r["search"]["repos_discovered"], 40)

    def test_invalid_reflection_stops_without_changing_plan(self):
        self.model.side_effect = [response(PLAN), evaluated(), response({"queries": ["a", "b", "c"], "criteria": []})]
        r = self.run_find()
        self.assertEqual(r["stop_reason"], "reflection_failed")
        self.assertEqual(r["plan"], PLAN)

    def test_reflection_deduplicates_case_and_whitespace(self):
        self.assertEqual(parse_reflection_queries('{"queries": [" Organize ", "SORT", " sort "]}', ["organize"]), ["SORT"])


class SearchAdapterTest(unittest.TestCase):
    def test_page_reaches_http_request(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"items": [], "total_count": 0}
        search_repositories("q", page=3, session=session)
        self.assertEqual(session.get.call_args.kwargs["params"]["page"], 3)
        with patch("src.finder.search.search_repositories", return_value=(True, [], 0, None)) as search:
            search_github_repos_for_query("q", page=4)
            self.assertEqual(search.call_args.kwargs["page"], 4)

    def test_expansion_can_preserve_paths_beyond_ten(self):
        paths = [f"skills/{i}/SKILL.md" for i in range(13)]
        with patch("src.finder.search.expand_repo_skills", return_value=(paths, None)):
            candidates, logs = expand_and_collect_candidates([repo(0)], max_files_per_repo=None)
        self.assertEqual(len(candidates), 13)
        self.assertEqual(logs[0]["omitted_files"], 0)


if __name__ == "__main__":
    unittest.main()
