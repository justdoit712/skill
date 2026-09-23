"""Offline acceptance observations; not regression tests. No real API calls.

Run from repository root: python docs/evidence/refactor_acceptance_probes.py
All run data is written inside TemporaryDirectory; JSON observations go to stdout.
"""
from pathlib import Path
import ast
import contextlib
import copy
import io
import json
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.finder.config import _parse_int_val, load_finder_run_config, load_finder_model_config
from src.finder.evaluation import parse_skill_evaluation, verify_and_adjust_evaluation
from src.finder.run import execute_find_skill, main
from src.finder.report import sanitize_report_for_public
from src.finder.evidence import verify_evidence_snippet
from src.infra.http import _read_capped
from src.infra.llm import ModelCallResult
from src.shared.usage import UsageTotals
from src.shared.identity import candidate_from_repo
from src.shared.materials import DocumentSnapshot

criteria = [{"id": "c1", "kind": "required", "description": "Generate prompts"}]
plan = {"intent": "Generate prompts", "queries": ["prompt"], "criteria": criteria}
evaluation = {
    "match": "strong", "documentation": "clear", "summary_zh": "Prompt tool",
    "criteria_results": [{"criterion_id": "c1", "status": "supported", "evidence": [
        {"source_path": "SKILL.md", "start_line": 1, "end_line": 1, "quote": "Generate prompts"}
    ]}],
}
cfg = {"endpoint": "https://fake.invalid", "model": "fake", "auth": {"api_key": "fake"}}
usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
candidate = candidate_from_repo(owner="test", repo="sample", path="SKILL.md",
    url="https://github.com/test/sample/blob/HEAD/SKILL.md",
    repo_url="https://github.com/test/sample", name="sample", description="",
    discovered_at="2026-09-23T00:00:00Z")
observations = {}

def observe(name, fn):
    try:
        observations[name] = fn()
    except Exception as exc:
        observations[name] = {"exception": type(exc).__name__, "message": str(exc)}

def run_case(mode):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        calls = []
        def model(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return ModelCallResult(ok=True, content=json.dumps(plan), usage=usage, attempts=1)
            if mode == "interrupt":
                raise KeyboardInterrupt()
            return ModelCallResult(ok=True, content=json.dumps(evaluation),
                usage={} if mode == "unknown_usage" else usage, attempts=1)
        report = execute_find_skill("Generate prompts", root_dir=root, model_cfg=cfg,
            limit=1, max_evaluations=1, log=lambda *a: None,
            call_model_fn=model,
            search_github_repos_fn=lambda *a, **k: (True, [{"owner": "test", "repo": "sample"}], None),
            expand_and_collect_candidates_fn=lambda *a, **k:
                ([], [{"ok": False, "error": "HTTP 429"}]) if mode == "expansion_failed"
                else ([copy.deepcopy(candidate)], []),
            fetch_candidate_materials_fn=lambda *a, **k:
                (False, {}, "HTTP 429") if mode == "material_failed"
                else (True, {"SKILL.md": "Generate prompts"}, None))
        return {"status": report["status"], "stop_reason": report["stop_reason"],
            "model_calls": len(calls), "evaluated_count": report["evaluated_count"],
            "evaluation_attempts": report["evaluation_attempts"], "usage": report["usage"],
            "local_schema_present": "schema_version" in report}

def parsed_boolean():
    raw = copy.deepcopy(evaluation)
    raw["criteria_results"][0]["evidence"][0].update(start_line=True, end_line=True)
    parsed = parse_skill_evaluation(json.dumps(raw), criteria)
    result = verify_and_adjust_evaluation(parsed, {"SKILL.md": "Generate prompts"}, criteria)
    return {"parsed_line": parsed["criteria_results"][0]["evidence"][0]["start_line"],
            "match": result["match"]}

def overlength():
    raw = copy.deepcopy(evaluation)
    raw["summary_zh"] = "x" * 1201
    return {"accepted_summary_length": len(parse_skill_evaluation(json.dumps(raw), criteria)["summary_zh"])}

def config_case(kind):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        directory = Path(td)
        if kind == "placeholder":
            (directory / "model.example.json").write_text(json.dumps({
                "endpoint": "https://api.example.com/v1/chat/completions", "model": "example-model-name"}), encoding="utf-8")
            return {"accepted_endpoint": load_finder_model_config(directory)["endpoint"]}
        config_dir = directory / "config"
        config_dir.mkdir()
        (config_dir / "find-skill.json").write_text("[]" if kind == "array" else "{broken", encoding="utf-8")
        if kind == "array":
            return load_finder_run_config(config_dir)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return {"help_exit": main(["--help"], root=directory)}

class Response:
    def iter_content(self, **kwargs):
        yield b"1234"

with patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
    for mode in ("success", "unknown_usage", "interrupt", "material_failed", "expansion_failed"):
        observe(mode, lambda mode=mode: run_case(mode))
    observe("boolean_line_through_parser", parsed_boolean)
    observe("overlength_output", overlength)
    observe("decimal_parameter", lambda: _parse_int_val(1.9))
    for kind in ("array", "placeholder", "broken_help"):
        observe(kind, lambda kind=kind: config_case(kind))
    observe("exact_read_limit", lambda: {"truncated": _read_capped(Response(), 4)[1]})
    totals = UsageTotals()
    totals.add(ModelCallResult(attempts=0))
    observations["no_request_usage"] = totals.snapshot()
    observe("snapshot_evidence", lambda: verify_evidence_snippet("SKILL.md", 1, 1, "Generate prompts", {
        "SKILL.md": DocumentSnapshot("SKILL.md", "Generate prompts", "hash", "time", "https://fake.invalid")
    }))
    observe("public_error_path", lambda: sanitize_report_for_public({
        "status": "error", "stop_reason": "OSError: C:\\private\\report.json"})["stop_reason"])

targets = {"mutate_catalog", "recover_catalog_projections", "MaterialBundle", "validate_document", "search_repositories"}
call_sites = {target: [] for target in targets}
long_functions = []
for path in sorted((ROOT / "src").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in targets:
                call_sites[name].append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 > 150:
            long_functions.append({"file": path.relative_to(ROOT).as_posix(), "name": node.name,
                "line": node.lineno, "span": node.end_lineno - node.lineno + 1})
observations["static_named_call_sites"] = call_sites
observations["functions_over_150_lines"] = long_functions
print(json.dumps(observations, ensure_ascii=False, indent=2))
