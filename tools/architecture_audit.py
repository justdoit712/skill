"""Reproduce architecture-review measurements without network or real run data.

Usage: python tools/architecture_audit.py --output docs/evidence/architecture-audit.json
This is a diagnostic snapshot, not an acceptance test: probes report current behavior.
Only an explicit --output path is written outside TemporaryDirectory.
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inventory():
    paths = sorted((ROOT / "src").rglob("*.py"))
    names = {".".join(p.relative_to(ROOT).with_suffix("").parts): p for p in paths}
    graphs, modules, functions = {}, {}, []
    for name, path in names.items():
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        dependencies = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(a.name for a in node.names if a.name in names)
            elif isinstance(node, ast.ImportFrom):
                parent = name.split(".")[:-1]
                prefix = parent[:len(parent) - node.level + 1] if node.level else []
                base = ".".join(prefix + ([node.module] if node.module else []))
                if base in names:
                    dependencies.add(base)
                dependencies.update(f"{base}.{a.name}" for a in node.names if f"{base}.{a.name}" in names)
        graphs[name] = sorted(dependencies)
        modules[name] = {"lines": len(source.splitlines()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        def visit(node, scope=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f"{scope}.{child.name}".strip(".")
                    # Count only this function's decisions; nested functions have their own row.
                    own = []
                    def collect(n):
                        for c in ast.iter_child_nodes(n):
                            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                                continue
                            own.append(c)
                            collect(c)
                    collect(child)
                    decisions = sum(isinstance(n, (ast.If, ast.IfExp, ast.For, ast.AsyncFor,
                                                  ast.While, ast.ExceptHandler, ast.comprehension)) for n in own)
                    decisions += sum(len(n.values) - 1 for n in own if isinstance(n, ast.BoolOp))
                    functions.append({"module": name, "name": qualified, "line": child.lineno,
                                      "span": child.end_lineno - child.lineno + 1,
                                      "decision_points": decisions})
                    visit(child, qualified)
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{scope}.{child.name}".strip("."))
                else:
                    visit(child, scope)
        visit(tree)
    for name, info in modules.items():
        reached, pending = set(), list(graphs[name])
        while pending:
            dep = pending.pop()
            if dep in reached:
                continue
            reached.add(dep)
            pending.extend(graphs.get(dep, []))
        info.update(imports=graphs[name], fan_out=len(graphs[name]),
                    imported_by=[n for n in graphs if name in graphs[n]],
                    transitive_imports=sorted(reached), cyclic=name in reached)
    tests = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        tests[path.name] = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in ast.walk(tree))
    return {"method": "Static imports including function-local imports; no dynamic import resolution. Decision points are a branch proxy, not McCabe complexity or coverage. Function span includes nested definitions.",
            "modules": modules, "total_lines": sum(m["lines"] for m in modules.values()),
            "largest_functions": sorted(functions, key=lambda f: f["span"], reverse=True)[:12],
            "test_methods_by_file": tests}


def probes():
    from src.budget import BudgetLedger, week_id
    from src.dedupe import candidate_from_repo, content_fingerprint
    from src.evaluate import ModelCallResult, evaluation_id
    from src.fetch import FetchResult
    from src.find_evaluate import verify_and_adjust_evaluation
    from src.index import CatalogContext, build_catalog, write_catalog
    import src.index as index_module
    from src.skill_finder import fetch_candidate_materials
    from src.usage import UsageTotals

    results = {}
    totals = UsageTotals()
    totals.add(ModelCallResult(ok=False, attempts=0))
    results["A1_zero_attempt_usage"] = {"attempts_supplied": 0, "observed": totals.snapshot()}

    raw = {"match": "strong", "limitations": [], "criteria_results": [
        {"criterion_id": "r1", "status": "supported", "explanation": "claim", "evidence": []}]}
    before = deepcopy(raw)
    criteria = [{"id": "r1", "kind": "required"}]
    verify_and_adjust_evaluation(raw, {}, criteria)
    verify_and_adjust_evaluation(raw, {}, criteria)
    results["A2_verifier_input_mutation"] = {
        "input_changed": before != raw, "limitations_after_two_calls": len(raw["limitations"]),
        "explanation_changed": before["criteria_results"][0]["explanation"] != raw["criteria_results"][0]["explanation"]}

    candidate = candidate_from_repo("example", "repo", path="skills/demo/SKILL.md",
                                    url="https://github.com/example/repo/blob/fixed-revision/skills/demo/SKILL.md")
    urls = []
    def material_fetch(ref_text):
        def fetch(url, **kwargs):
            urls.append(url)
            text = "# Demo\n[Details](details.md)\n" if url.endswith("SKILL.md") else ref_text
            return FetchResult(url=url, ok=True, text=text)
        return fetch
    ok_a, materials_a, _ = fetch_candidate_materials(candidate, fetch_fn=material_fetch("Reference A"))
    fingerprint_a = candidate.content_fingerprint
    ok_b, materials_b, _ = fetch_candidate_materials(candidate, fetch_fn=material_fetch("Reference B"))
    results["A3_material_identity"] = {"both_reads_ok": ok_a and ok_b,
        "reference_changed": materials_a != materials_b,
        "candidate_fingerprint_unchanged": fingerprint_a == candidate.content_fingerprint, "requested_urls": urls}

    rules = {"rules_version": "v1"}
    first = {"model": "model-A", "model_config_version": "v1"}
    second = {"model": "model-B", "model_config_version": "v1"}
    results["A4_evaluation_key"] = {"changed_model_without_version_bump_same_id":
                                    evaluation_id(candidate, first, rules) == evaluation_id(candidate, second, rules)}

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        old = datetime(2026, 9, 14, tzinfo=timezone.utc)
        new = datetime(2026, 9, 21, tzinfo=timezone.utc)
        BudgetLedger.load(state, cap=50, moment=old).save(old)
        before_files = sorted(p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file())
        BudgetLedger.load(state, cap=50, moment=new)
        after_files = sorted(p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file())
        results["A5_load_has_writes"] = {"before": before_files, "after": after_files,
                                          "expected_archive_week": week_id(old)}

    with tempfile.TemporaryDirectory() as tmp:
        data, public = Path(tmp) / "catalog.json", Path(tmp) / "page.json"
        write_catalog(build_catalog([], context=CatalogContext(generated_at="old")), data_path=data, public_path=public)
        original = index_module._write_json
        def fail_page(path, payload):
            if path == public:
                raise OSError("injected page write failure")
            return original(path, payload)
        error = None
        with patch.object(index_module, "_write_json", side_effect=fail_page):
            try:
                write_catalog(build_catalog([], context=CatalogContext(generated_at="new")), data_path=data, public_path=public)
            except OSError as exc:
                error = str(exc)
        results["A6_projection_write_failure"] = {"error": error,
            "catalog_generation": json.loads(data.read_text(encoding="utf-8"))["generated_at"],
            "page_generation": json.loads(public.read_text(encoding="utf-8"))["generated_at"]}

    # Exercise both real pipeline phases with isolated public config and fake external calls.
    from src.pipeline import phase_reserve, phase_evaluate
    fixture = json.loads((ROOT / "tests/fixtures/evaluations.json").read_text(encoding="utf-8"))
    evaluation = next(deepcopy(c["evaluation"]) for c in fixture["cases"] if c["id"] == "in_scope_normal")
    old_text = "---\nname: demo\ndescription: 代码审查与测试\n---\n" + "代码审查步骤与示例。\n" * 40
    new_text = old_text + "New revision\n"
    calls = []
    def fetch_text(text):
        return lambda url, **kw: FetchResult(url=url, ok=True, text=text, bytes_read=len(text.encode()))
    def evaluate(cand, text, **kw):
        calls.append({"declared_fingerprint": cand.content_fingerprint, "actual_fingerprint": content_fingerprint(text)})
        ev = deepcopy(evaluation)
        ev["source_fingerprint"] = cand.content_fingerprint
        return {"ok": True, "evaluation": ev, "call": ModelCallResult(usage={"total_tokens": 1}, attempts=1)}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = base / "config"
        cfg.mkdir()
        for name in ("model.example.json", "rules.json", "taxonomy.json", "sources.json", "searches.json", "overrides.json", "snoozed.json"):
            (cfg / name).write_bytes((ROOT / "config" / name).read_bytes())
        cand = candidate_from_repo("example", "demo", path="SKILL.md", name="代码审查",
                                   url="https://github.com/example/demo/blob/HEAD/SKILL.md")
        reserve = phase_reserve(config_dir=cfg, data_dir=base / "data", limit_evaluations=1,
                                discover_fn=lambda *a, **kw: ([cand], []), fetch_fn=fetch_text(old_text), sleep=lambda _: None)
        assert reserve["ok"] and reserve["reserved"] == 1, reserve
        (base / "data/state/texts/staged.json").unlink()
        outcome = phase_evaluate(config_dir=cfg, data_dir=base / "data", public_dir=base / "public",
                                 fetch_fn=fetch_text(new_text), evaluate_fn=evaluate, sleep=lambda _: None)
        fingerprint_mismatch_prevented = len(calls) == 0 and outcome.get("skipped", 0) >= 1
        results["A7_refetch_identity"] = {
            "evaluated": outcome["evaluated"],
            "skipped": outcome.get("skipped", 0),
            "calls": calls,
            "fingerprint_mismatch_prevented": fingerprint_mismatch_prevented,
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # Unexpected network access is an audit error, including accidental production defaults.
    with patch("requests.sessions.Session.request", side_effect=AssertionError("network prohibited during audit")):
        result = {"schema_version": 1, "python": platform.python_version(),
                  "inventory": inventory(), "probes": probes()}
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.output}: {len(result['inventory']['modules'])} modules, {len(result['probes'])} probes")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
