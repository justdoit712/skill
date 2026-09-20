"""运行入口：把发现、预筛、额度、评估、索引与报告串成一次运行（§7.2）。

设计要点：
- `--dry-run` 只验证配置、读取与计算计划，**不调用模型、不写账本、不提交、不部署**（§7.3）
- 额度在调用任何模型之前先预留并落盘（§7.2 步骤 3）
- 依赖注入 sleep / session，便于测试与限流
- 所有输出路径可指定，因此演练可以写到临时目录
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .budget import BudgetLedger, QuotaExceeded, now_local, week_id
from .decide import decide
from .dedupe import dedupe
from .discover import discover, load_searches
from .evaluate import evaluate, evaluation_id, resolve_api_key
from .fetch import fetch_text
from .index import CatalogContext, build_catalog, build_entry, write_catalog
from .prescreen import DECISION_QUEUED, load_config, prescreen
from .report import build_report, write_report

DEFAULT_LIMIT_EVALUATIONS = 50


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
        "model": model_cfg,
        "source_types": {
            s["id"]: s.get("source_type")
            for s in _load_json(base / "sources.json").get("sources", [])
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


def plan_run(cfg: dict, *, limit_queries: int | None, limit_evaluations: int) -> dict:
    """发现 + 去重 + 预筛，算出本批要评估什么。此步骤不调用模型。"""
    candidates, outcomes = discover(cfg["searches"], max_queries=limit_queries)
    merged = dedupe(candidates)

    queued, excluded = [], []
    for candidate in merged:
        result = prescreen(candidate, cfg["prescreen"])
        (excluded if result.excluded else queued).append((candidate, result))

    return {
        "candidates": merged,
        "queued": queued,
        "excluded": excluded,
        "discovery_outcomes": [
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
        ],
        "evaluation_slots": min(limit_evaluations, len(queued)),
    }


def run(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    public_dir: str | Path = "public",
    state_dir: str | Path | None = None,
    dry_run: bool = False,
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    fetch_texts: bool = True,
    api_key: str | None = None,
    sleep=time.sleep,
) -> dict:
    """执行一次运行。返回结果摘要。"""
    started = now_local()
    cfg = load_all_config(config_dir)
    data_path, public_path = Path(data_dir), Path(public_dir)
    state_path = Path(state_dir) if state_dir else data_path / "state"

    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems, "dry_run": dry_run}

    plan = plan_run(cfg, limit_queries=limit_queries, limit_evaluations=limit_evaluations)

    if dry_run:
        # §7.3：dry_run 不调用模型、不写账本、不提交、不部署
        return {
            "ok": True,
            "dry_run": True,
            "week": week_id(started),
            "candidates": len(plan["candidates"]),
            "queued": len(plan["queued"]),
            "excluded": len(plan["excluded"]),
            "evaluation_slots": plan["evaluation_slots"],
            "quota_would_be_used": plan["evaluation_slots"],
            "model": cfg["model"].get("model"),
            "credentials_present": bool(api_key or resolve_api_key(cfg["model"])),
            "discovery_outcomes": plan["discovery_outcomes"],
            "excluded_reasons": [
                {"skill_id": c.skill_id, "reason_codes": r.reason_codes} for c, r in plan["excluded"]
            ],
            "notes": ["dry_run：只验证配置与计算计划，未调用模型、未写账本、未提交、未部署"],
        }

    cap = int(cfg["rules"].get("weekly_quota") or plan["evaluation_slots"] or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    ledger.mark_in_progress_as_needs_recovery(started)

    batch = plan["queued"][: plan["evaluation_slots"]]
    entries_to_reserve = [
        {
            "evaluation_id": evaluation_id(candidate, cfg["model"], cfg["rules"]),
            "skill_id": candidate.skill_id,
            "content_fingerprint": candidate.content_fingerprint,
            "rules_version": cfg["rules"].get("rules_version"),
            "model_config_version": cfg["model"].get("model_config_version"),
        }
        for candidate, _ in batch
    ]

    try:
        reserved = ledger.reserve(entries_to_reserve, started)
    except QuotaExceeded as exc:
        return {"ok": False, "stage": "reserve", "error": str(exc), "quota": ledger.snapshot()}

    # 预留已落盘，此后才可以付费调用
    results: dict[str, dict] = {}
    for candidate, pre in batch:
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
        if eid not in reserved:
            existing = ledger.get(eid) or {}
            if existing.get("status") == "completed":
                results[candidate.skill_id] = existing.get("outcome") or {}
            continue

        allowed, reason = ledger.can_attempt(eid)
        if not allowed:
            results[candidate.skill_id] = {"status": "skipped", "note": reason}
            continue

        text = None
        if fetch_texts:
            fetched = fetch_text(candidate.url or candidate.repo_url, sleep=sleep)
            if fetched.ok:
                text = fetched.text
        if text is None:
            ledger.fail(eid, "NETWORK_ERROR", "抓取失败或未取得内容", started)
            results[candidate.skill_id] = {"status": "failed", "note": "抓取失败"}
            continue

        ledger.begin_attempt(eid, started)
        outcome = evaluate(
            candidate, text, model_cfg=cfg["model"], rules=cfg["rules"], taxonomy=cfg["taxonomy"],
            api_key=api_key, sleep=sleep,
        )
        if outcome["ok"]:
            decision = decide(outcome["evaluation"], cfg["rules"])
            ledger.complete(eid, {"decision": decision["decision"], "reason_codes": decision["reason_codes"]}, started)
            results[candidate.skill_id] = {"status": "completed", "decision": decision["decision"]}
        else:
            ledger.fail(eid, outcome["reason_code"], outcome["error"] or "", started)
            results[candidate.skill_id] = {"status": "failed", "note": outcome["error"]}

    context = CatalogContext(
        rules_version=cfg["rules"].get("rules_version"),
        generated_at=started.replace(microsecond=0).isoformat(),
        domain_names=cfg["prescreen"].domain_names,
        source_types=cfg["source_types"],
    )

    entries = []
    for candidate, pre in plan["queued"] + plan["excluded"]:
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
        record = ledger.get(eid) or {}
        outcome = record.get("outcome") or {}
        entries.append(
            build_entry(
                candidate,
                prescreen_result=pre,
                decision={"decision": outcome["decision"], "reason_codes": outcome.get("reason_codes", [])}
                if outcome.get("decision")
                else None,
                evaluation=None,
                context=context,
                first_seen=candidate.discovered_at or context.generated_at,
                last_checked=context.generated_at,
            )
        )

    catalog = build_catalog(entries, context=context)
    manifest = write_catalog(
        catalog,
        data_path=data_path / "catalog.json",
        public_path=public_path / "data" / "catalog.json",
    )

    queue = {
        "week": ledger.week,
        "pending_count": max(0, len(plan["queued"]) - len(batch)),
        "pending": [c.skill_id for c, _ in plan["queued"][len(batch):]],
    }
    (data_path / "queue.json").parent.mkdir(parents=True, exist_ok=True)
    (data_path / "queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")

    report = build_report(
        catalog,
        run_meta={"quota": ledger.snapshot(), "dry_run": False},
        outcomes=plan["discovery_outcomes"],
    )
    week = ledger.week
    written = write_report(
        report,
        json_path=data_path / "reports" / f"{week}.json",
        markdown_path=public_path / "reports" / f"{week}.md",
    )

    (state_path / "build-manifest.json").parent.mkdir(parents=True, exist_ok=True)
    (state_path / "build-manifest.json").write_text(
        json.dumps({"week": week, **manifest, **written}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "ok": True,
        "dry_run": False,
        "week": week,
        "reserved": len(reserved),
        "quota": ledger.snapshot(),
        "results": results,
        "manifest": manifest,
        "report_paths": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skills 导航目录：一次完整运行")
    parser.add_argument("--dry-run", action="store_true", help="只验证配置与计算计划，不调用模型、不写账本")
    parser.add_argument("--limit-queries", type=int, default=None, help="限制搜索查询数，便于本地抽样")
    parser.add_argument("--limit-evaluations", type=int, default=DEFAULT_LIMIT_EVALUATIONS)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--public-dir", default="public")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    args = parser.parse_args(argv)

    result = run(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        public_dir=args.public_dir,
        state_dir=args.state_dir,
        dry_run=args.dry_run,
        limit_queries=args.limit_queries,
        limit_evaluations=args.limit_evaluations,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result.get("ok"):
            print(f"运行中止于 {result.get('stage')}：{result.get('problems') or result.get('error')}")
            return 1
        if result.get("dry_run"):
            print(
                f"dry_run 通过：候选 {result['candidates']}、待评估 {result['queued']}、"
                f"预筛排除 {result['excluded']}、本批名额 {result['evaluation_slots']}、"
                f"模型 {result['model']}、凭据 {'已就绪' if result['credentials_present'] else '缺失'}"
            )
        else:
            print(f"运行完成：周 {result['week']}，预留 {result['reserved']}，额度 {result['quota']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
