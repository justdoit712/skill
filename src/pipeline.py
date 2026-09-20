"""运行入口：把发现、预筛、额度、评估、索引与报告串成一次运行（§7.2）。

分成两个阶段，以对应 §7.2 的步骤 3 与 4-5：

- `--phase reserve`：发现 → 抓取 → 内容指纹 → 预筛 → **预留额度**，并把候选与
  已抓取材料落到 `data/state/`。此阶段的产物**必须先提交推送**，否则下次运行读不到
  已消耗额度，会造成重复计费。
- `--phase evaluate`：读取预留、逐条调用模型、**保留完整评估内容**、与既有索引合并、
  生成页面数据与周报。

`--dry-run` 只做发现与计算计划：不调用模型、不写账本、不提交、不部署（§7.3）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .budget import BudgetLedger, QuotaExceeded, now_local, week_id
from .decide import decide
from .dedupe import content_fingerprint, dedupe
from .discover import discover, load_searches
from .evaluate import evaluate, evaluation_id, resolve_api_key
from .fetch import fetch_text
from .index import (
    CatalogContext,
    build_catalog,
    build_entry,
    index_by_id,
    merge_entries,
    write_catalog,
)
from .models import Candidate, PrescreenResult
from .prescreen import DECISION_QUEUED, load_config, prescreen
from .report import build_report, write_report

DEFAULT_LIMIT_EVALUATIONS = 50
TEXTS_DIRNAME = "texts"
QUEUE_FILENAME = "queue.json"
FAILURE_RATE_ABORT = 1.0  # 全部来源失败时中止，不用空结果覆盖既有索引


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_config(config_dir: str | Path = "config") -> dict:
    base = Path(config_dir)
    prescreen_cfg = load_config(base)
    model_path = base / "model.local.json"
    model_cfg = _load_json(model_path) if model_path.exists() else _load_json(base / "model.example.json")
    return {
        "prescreen": prescreen_cfg,
        "taxonomy": prescreen_cfg.taxonomy,
        "rules": prescreen_cfg.rules,
        "searches": load_searches(str(base)),
        "sources": _load_json(base / "sources.json"),
        "model": model_cfg,
        "source_types": {
            s["id"]: s.get("source_type") for s in _load_json(base / "sources.json").get("sources", [])
        },
    }


def precheck(cfg: dict) -> list[str]:
    """预检（§7.2 步骤 1）。返回问题列表；非空时必须中止，不得进入付费调用。"""
    problems: list[str] = []
    if not cfg["prescreen"].domain_names:
        problems.append("taxonomy.json 未加载到任何主分类")
    if not cfg["rules"].get("checks"):
        problems.append("rules.json 未定义检查项")
    if not cfg["searches"].get("per_domain"):
        problems.append("searches.json 未定义任何领域的查询词")
    if not cfg["model"].get("endpoint") or not cfg["model"].get("model"):
        problems.append("模型配置缺 endpoint 或 model")
    return problems


# --------------------------------------------------------------------------
# 准备：发现 → 抓取 → 指纹 → 预筛
# --------------------------------------------------------------------------


def _candidate_payload(candidate: Candidate, pres: PrescreenResult, fetch: dict) -> dict:
    return {
        "candidate": {
            "skill_id": candidate.skill_id,
            "owner": candidate.owner,
            "repo": candidate.repo,
            "path": candidate.path,
            "url": candidate.url,
            "repo_url": candidate.repo_url,
            "name": candidate.name,
            "description": candidate.description,
            "source_ids": candidate.source_ids,
            "discovery_methods": candidate.discovery_methods,
            "search_terms": candidate.search_terms,
            "discovered_at": candidate.discovered_at,
            "content_fingerprint": candidate.content_fingerprint,
        },
        "prescreen": asdict(pres),
        "fetch": fetch,
    }


def prepare(
    cfg: dict,
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    expand: bool = True,
    expand_limit: int | None = None,
    state_dir: Path | None = None,
    sleep=time.sleep,
    discover_fn=discover,
    fetch_fn=fetch_text,
) -> dict:
    """发现、抓取内容、计算指纹并预筛。不写账本、不调用模型。"""
    candidates, outcomes = discover_fn(
        cfg["searches"],
        sources=cfg["sources"],
        expand=expand,
        expand_limit=expand_limit,
        max_queries=limit_queries,
        sleep=sleep,
    )

    # 抓取内容：指纹必须在这里算出来，评估 ID 才能反映内容版本（§7.3）
    staged: dict[str, str] = {}
    texts_dir = (state_dir / TEXTS_DIRNAME) if state_dir else None
    if texts_dir:
        texts_dir.mkdir(parents=True, exist_ok=True)

    enriched: list[tuple[Candidate, PrescreenResult, dict]] = []
    for candidate in dedupe(candidates):
        fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
        note = {"ok": fetched.ok, "bytes": fetched.bytes_read, "reason_code": fetched.reason_code}
        if fetched.ok and fetched.text:
            candidate.content_fingerprint = content_fingerprint(fetched.text)
            note["truncated"] = fetched.truncated
            if texts_dir is not None:
                staged[candidate.skill_id] = fetched.text
        enriched.append((candidate, prescreen(candidate, cfg["prescreen"], fetched.text), note))

    # 暂存材料：仅在同一 runner 内复用，不提交
    if texts_dir is not None and staged:
        payload = json.dumps(staged, ensure_ascii=False)
        (texts_dir / "staged.json").write_text(payload, encoding="utf-8")

    queued = [(c, p, f) for c, p, f in enriched if p.decision == DECISION_QUEUED]
    excluded = [(c, p, f) for c, p, f in enriched if p.decision != DECISION_QUEUED]

    return {
        "outcomes": outcomes,
        "queued": queued,
        "excluded": excluded,
        "discovery_total": len(candidates),
        "discovery_failed": sum(1 for o in outcomes if not o.ok),
        "evaluation_slots": min(limit_evaluations, len(queued)),
    }


def _outcome_records(outcomes) -> list[dict]:
    return [
        {
            "query": o.query.q,
            "domain": o.query.domain_id,
            "ok": o.ok,
            "total_count": o.total_count,
            "candidates": len(o.candidates),
            "reason_code": o.reason_code,
            "error": o.error,
        }
        for o in outcomes
    ]


def _write_queue(plan: dict, state_dir: Path, week: str, generated_at: str) -> None:
    payload = {
        "week": week,
        "generated_at": generated_at,
        "queued": [_candidate_payload(c, p, f) for c, p, f in plan["queued"]],
        "excluded": [_candidate_payload(c, p, f) for c, p, f in plan["excluded"]],
        "outcomes": _outcome_records(plan["outcomes"]),
        "evaluation_slots": plan["evaluation_slots"],
    }
    path = state_dir / QUEUE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_queue(state_dir: Path) -> dict:
    return _load_json(state_dir / QUEUE_FILENAME)


def _candidate_from_payload(payload: dict) -> Candidate:
    return Candidate(**payload)


def _prescreen_from_payload(payload: dict) -> PrescreenResult:
    return PrescreenResult(**payload)


def _has_previous_catalog(data_dir: Path) -> bool:
    path = data_dir / "catalog.json"
    if not path.exists():
        return False
    try:
        return bool(_load_json(path).get("entries"))
    except (OSError, json.JSONDecodeError):
        return False


# --------------------------------------------------------------------------
# 阶段一：预留
# --------------------------------------------------------------------------


def phase_reserve(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    state_dir: str | Path | None = None,
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    expand: bool = True,
    expand_limit: int | None = None,
    sleep=time.sleep,
    discover_fn=discover,
    fetch_fn=fetch_text,
) -> dict:
    """发现、预筛并预留额度。产物必须先提交推送，之后才可付费调用。"""
    started = now_local()
    cfg = load_all_config(config_dir)
    data_path = Path(data_dir)
    state_path = Path(state_dir) if state_dir else data_path / "state"

    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems}

    plan = prepare(
        cfg, config_dir=config_dir, limit_queries=limit_queries,
        limit_evaluations=limit_evaluations, expand=expand, expand_limit=expand_limit,
        state_dir=state_path, sleep=sleep, discover_fn=discover_fn, fetch_fn=fetch_fn,
    )

    # §7：来源失败时保留上次有效数据；全部来源失败时不得用空结果覆盖既有索引
    if plan["discovery_total"] == 0 and _has_previous_catalog(data_path):
        return {
            "ok": False,
            "stage": "discover",
            "error": "本轮未发现任何候选且既有索引非空，中止以避免清空目录",
            "discovery_failed": plan["discovery_failed"],
            "outcomes": _outcome_records(plan["outcomes"]),
        }

    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    ledger.mark_in_progress_as_needs_recovery(started)

    batch = plan["queued"][: plan["evaluation_slots"]]
    entries = [
        {
            "evaluation_id": evaluation_id(candidate, cfg["model"], cfg["rules"]),
            "skill_id": candidate.skill_id,
            "content_fingerprint": candidate.content_fingerprint,
            "rules_version": cfg["rules"].get("rules_version"),
            "model_config_version": cfg["model"].get("model_config_version"),
        }
        for candidate, _, _ in batch
    ]

    try:
        reserved = ledger.reserve(entries, started)
    except QuotaExceeded as exc:
        return {"ok": False, "stage": "reserve", "error": str(exc), "quota": ledger.snapshot()}

    _write_queue(plan, state_path, ledger.week, started.replace(microsecond=0).isoformat())

    return {
        "ok": True,
        "phase": "reserve",
        "week": ledger.week,
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "reserved": len(reserved),
        "quota": ledger.snapshot(),
        "discovery_failed": plan["discovery_failed"],
        "state_dir": str(state_path),
    }


# --------------------------------------------------------------------------
# 阶段二：评估与生成
# --------------------------------------------------------------------------


def phase_evaluate(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    public_dir: str | Path = "public",
    state_dir: str | Path | None = None,
    api_key: str | None = None,
    sleep=time.sleep,
    fetch_fn=fetch_text,
    evaluate_fn=evaluate,
) -> dict:
    """评估已预留的条目，保留完整评估内容，合并既有索引并生成页面数据与周报。"""
    started = now_local()
    cfg = load_all_config(config_dir)
    data_path, public_path = Path(data_dir), Path(public_dir)
    state_path = Path(state_dir) if state_dir else data_path / "state"

    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems}

    queue = _read_queue(state_path)
    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)

    staged: dict[str, str] = {}
    staged_path = state_path / TEXTS_DIRNAME / "staged.json"
    if staged_path.exists():
        try:
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            staged = {}

    results: dict[str, dict] = {}
    evaluated = 0
    skipped = 0

    for item in queue.get("queued", []):
        candidate = _candidate_from_payload(item["candidate"])
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])

        existing = ledger.get(eid) or {}
        if existing.get("status") == "completed":
            skipped += 1
            continue

        allowed, reason = ledger.can_attempt(eid)
        if not allowed:
            results[candidate.skill_id] = {"status": "skipped", "note": reason}
            skipped += 1
            continue

        text = staged.get(candidate.skill_id)
        if text is None:
            fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
            if not fetched.ok or not fetched.text:
                ledger.fail(eid, fetched.reason_code or "NETWORK_ERROR", fetched.error or "抓取失败", started)
                results[candidate.skill_id] = {"status": "failed", "note": "抓取失败"}
                continue
            text = fetched.text
            fresh = content_fingerprint(text)
            if candidate.content_fingerprint and fresh != candidate.content_fingerprint:
                results.setdefault(candidate.skill_id, {})["fingerprint_changed"] = True

        ledger.begin_attempt(eid, started)
        outcome = evaluate_fn(
            candidate, text, model_cfg=cfg["model"], rules=cfg["rules"],
            taxonomy=cfg["taxonomy"], api_key=api_key, sleep=sleep,
        )
        if not outcome["ok"]:
            ledger.fail(eid, outcome["reason_code"], outcome["error"] or "", started)
            results[candidate.skill_id] = {"status": "failed", "note": outcome["error"]}
            continue

        evaluation = outcome["evaluation"]
        decision = decide(evaluation, cfg["rules"])
        # §6：中文简述、分类与依赖必须随索引保留，不能只留决策与原因码
        ledger.complete(
            eid,
            {
                "decision": decision["decision"],
                "reason_codes": decision["reason_codes"],
                "main_category": evaluation.get("main_category"),
                "evaluation": evaluation,
            },
            started,
        )
        results[candidate.skill_id] = {"status": "completed", "decision": decision["decision"]}
        evaluated += 1

    context = CatalogContext(
        rules_version=cfg["rules"].get("rules_version"),
        generated_at=started.replace(microsecond=0).isoformat(),
        domain_names=cfg["prescreen"].domain_names,
        source_types=cfg["source_types"],
    )

    def entry_for(candidate: Candidate, pres: PrescreenResult) -> dict:
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
        record = ledger.get(eid) or {}
        # 完整评估内容随 outcome 一起保存在评估记录里（§6 需要中文简述与分类）
        outcome = record.get("outcome") or {}
        evaluation = outcome.get("evaluation") or {}
        decision = (
            {"decision": outcome["decision"], "reason_codes": outcome.get("reason_codes", [])}
            if outcome.get("decision")
            else None
        )
        return build_entry(
            candidate,
            prescreen_result=pres,
            decision=decision,
            evaluation=evaluation,
            context=context,
            first_seen=candidate.discovered_at or context.generated_at,
            last_checked=context.generated_at,
        )

    fresh: list[dict] = []
    for item in queue.get("queued", []):
        fresh.append(entry_for(_candidate_from_payload(item["candidate"]), _prescreen_from_payload(item["prescreen"])))
    for item in queue.get("excluded", []):
        fresh.append(entry_for(_candidate_from_payload(item["candidate"]), _prescreen_from_payload(item["prescreen"])))

    # §7：本轮未出现的条目保留，不因来源失败而批量删除
    previous_entries = []
    previous_path = data_path / "catalog.json"
    if previous_path.exists():
        try:
            previous_entries = _load_json(previous_path).get("entries") or []
        except (OSError, json.JSONDecodeError):
            previous_entries = []

    merged = merge_entries(previous_entries, fresh)
    catalog = build_catalog(merged, context=context)
    manifest = write_catalog(
        catalog,
        data_path=data_path / "catalog.json",
        public_path=public_path / "data" / "catalog.json",
    )

    report = build_report(
        catalog,
        previous_catalog={"entries": previous_entries},
        run_meta={"quota": ledger.snapshot(), "dry_run": False},
        outcomes=queue.get("outcomes") or [],
    )
    week = queue.get("week") or ledger.week
    written = write_report(
        report,
        json_path=data_path / "reports" / f"{week}.json",
        markdown_path=public_path / "reports" / f"{week}.md",
    )

    build_manifest = {"week": week, **manifest, **written}
    (state_path / "build-manifest.json").parent.mkdir(parents=True, exist_ok=True)
    (state_path / "build-manifest.json").write_text(
        json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "ok": True,
        "phase": "evaluate",
        "week": week,
        "evaluated": evaluated,
        "skipped": skipped,
        "results": results,
        "entries_before": len(previous_entries),
        "entries_after": len(merged),
        "quota": ledger.snapshot(),
        "manifest": manifest,
        "report_paths": written,
    }


# --------------------------------------------------------------------------
# 干跑
# --------------------------------------------------------------------------


def dry_run(
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    expand: bool = True,
    expand_limit: int | None = None,
    sleep=time.sleep,
) -> dict:
    """只验证配置与计算计划：不调用模型、不写账本、不提交、不部署（§7.3）。"""
    cfg = load_all_config(config_dir)
    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems, "dry_run": True}

    plan = prepare(
        cfg, config_dir=config_dir, limit_queries=limit_queries,
        limit_evaluations=limit_evaluations, expand=expand, expand_limit=expand_limit,
        state_dir=None, sleep=sleep,
    )
    return {
        "ok": True,
        "dry_run": True,
        "week": week_id(),
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "evaluation_slots": plan["evaluation_slots"],
        "model": cfg["model"].get("model"),
        "credentials_present": bool(resolve_api_key(cfg["model"])),
        "discovery_failed": plan["discovery_failed"],
        "outcomes": _outcome_records(plan["outcomes"]),
        "excluded_reasons": [
            {"skill_id": c.skill_id, "reason_codes": p.reason_codes} for c, p, _ in plan["excluded"]
        ],
        "notes": ["dry_run：未调用模型、未写账本、未提交、未部署"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skills 导航目录：一次完整运行")
    parser.add_argument("--phase", choices=["reserve", "evaluate", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="只验证配置与计算计划")
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--limit-evaluations", type=int, default=DEFAULT_LIMIT_EVALUATIONS)
    parser.add_argument("--expand-limit", type=int, default=None, help="本轮最多展开多少个仓库")
    parser.add_argument("--no-expand", action="store_true", help="不展开到具体技能（仅调试用）")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--public-dir", default="public")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    common = dict(
        config_dir=args.config_dir, data_dir=args.data_dir, state_dir=args.state_dir,
        limit_queries=args.limit_queries, limit_evaluations=args.limit_evaluations,
        expand=not args.no_expand, expand_limit=args.expand_limit,
    )

    if args.dry_run:
        result = dry_run(**{k: v for k, v in common.items() if k not in ("data_dir", "state_dir")})
    elif args.phase == "reserve":
        result = phase_reserve(**common)
    elif args.phase == "evaluate":
        result = phase_evaluate(
            config_dir=args.config_dir, data_dir=args.data_dir, public_dir=args.public_dir,
            state_dir=args.state_dir,
        )
    else:
        result = phase_reserve(**common)
        if result.get("ok"):
            evaluated = phase_evaluate(
                config_dir=args.config_dir, data_dir=args.data_dir,
                public_dir=args.public_dir, state_dir=args.state_dir,
            )
            result = {"ok": evaluated.get("ok"), "reserve": result, "evaluate": evaluated}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print(f"运行中止于 {result.get('stage')}：{result.get('problems') or result.get('error')}")
    elif result.get("dry_run"):
        print(
            f"dry_run 通过：候选 {result['candidates']}、待评估 {result['queued']}、"
            f"预筛排除 {result['excluded']}、本批名额 {result['evaluation_slots']}、"
            f"采集失败 {result['discovery_failed']}、凭据 "
            f"{'已就绪' if result['credentials_present'] else '缺失'}"
        )
    else:
        print(json.dumps(result, ensure_ascii=False)[:400])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
