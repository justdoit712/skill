"""阶段一流水线：发现、预筛并在调用前将配额预留写入磁盘（§7.2 步骤 1~3）。

产物包含预留账本与 data/queue.json，必须先提交推送，之后才可付费调用。
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any

from src.infra.files import read_json, write_json_atomic
from src.infra.http import fetch_text, REASON_UPSTREAM_GONE
from src.shared.identity import content_fingerprint
from src.shared.runtime import now_local
from .store import catalog_task
from .budget import BudgetLedger, QuotaExceeded
from .dedupe import dedupe
from .discovery import discover
from .evaluation import evaluation_id
from .models import Candidate, PrescreenResult
from .overrides import get_manual_picks
from .prescreen import DECISION_QUEUED, prescreen
from .snooze import get_active_snoozed

from .config import load_all_config, precheck
from .queue import (
    QUEUE_FILENAME,
    candidate_from_payload,
    candidate_payload,
    catalogued_entries as _catalogued_entries,
    ordered_pending as _ordered_pending,
    prescreen_from_payload,
    queue_pending as _queue_pending,
    read_queue as _read_queue,
    settled_for as _settled_for,
    skill_of as _skill_of,
    write_queue as _write_queue,
)

DEFAULT_LIMIT_EVALUATIONS = 50
TEXTS_DIRNAME = "texts"
MAX_RECHECKS_PER_RUN = 10


def _stamp() -> str:
    return now_local().replace(microsecond=0).isoformat()


def _has_previous_catalog(data_dir: Path) -> bool:
    path = data_dir / "catalog.json"
    if not path.exists():
        return False
    try:
        return bool(read_json(path, default={}).get("entries"))
    except (OSError, json.JSONDecodeError):
        return False


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


def _accumulate_plan(
    first_pass: list[tuple[Candidate, PrescreenResult]],
    previous: list[dict],
    *,
    catalogued: dict,
    catalogued_review: dict,
    source_types: dict,
    cfg: dict | None = None,
    ledger: BudgetLedger | None = None,
    cap: int | None = None,
) -> dict:
    previous_by_skill = {_skill_of(item): item for item in previous if _skill_of(item)}
    stamped = _stamp()

    def known_fingerprint(skill_id: str, *fallbacks: str | None) -> str | None:
        return catalogued.get(skill_id) or next((fp for fp in fallbacks if fp), None)

    def settled(skill_id: str, fingerprint: str | None) -> bool:
        return bool(
            ledger is not None
            and fingerprint
            and _settled_for(ledger, skill_id, fingerprint, cfg or {})
        )

    manual_picks_dict = get_manual_picks((cfg or {}).get("overrides") or {})
    manual_picks_set = set(manual_picks_dict.keys())
    max_manual_checks = int(((cfg or {}).get("rules") or {}).get("run_limits", {}).get("max_manual_checks_per_run", 10) or 10)

    fresh: dict[str, dict] = {}
    manual_recheck: list[str] = []
    recheck: list[str] = []
    rechecked: set[str] = set()
    for index, (candidate, prescreen_result) in enumerate(first_pass):
        if prescreen_result.decision != DECISION_QUEUED:
            continue
        old = previous_by_skill.get(candidate.skill_id) or {}
        baseline = known_fingerprint(
            candidate.skill_id, candidate.content_fingerprint, old.get("baseline_fingerprint")
        )
        if settled(candidate.skill_id, baseline):
            if candidate.skill_id in manual_picks_set:
                if len(manual_recheck) >= max_manual_checks:
                    continue
                manual_recheck.append(candidate.skill_id)
                rechecked.add(candidate.skill_id)
            else:
                if len(recheck) >= MAX_RECHECKS_PER_RUN:
                    continue
                recheck.append(candidate.skill_id)
                rechecked.add(candidate.skill_id)
        item = candidate_payload(
            candidate, prescreen_result,
            {"ok": False, "bytes": 0, "reason_code": None, "skipped": "尚未抓取"},
        )
        item["baseline_fingerprint"] = baseline
        item["content_fingerprint"] = old.get("content_fingerprint")
        item["candidate"]["content_fingerprint"] = item["content_fingerprint"]
        item["recheck"] = candidate.skill_id in rechecked
        item["manual_pick"] = candidate.skill_id in manual_picks_set
        item["seq"] = old.get("seq", len(previous) + index)
        item["first_queued_at"] = old.get("first_queued_at") or stamped
        item["last_attempted_at"] = old.get("last_attempted_at")
        item["needs_review"] = bool(catalogued_review.get(candidate.skill_id))
        item["pending"] = True
        fresh[candidate.skill_id] = item

    regular = [item for item in fresh.values() if not item.get("recheck")]
    ordered_regular = _ordered_pending(
        regular, catalogued=catalogued, source_types=source_types, manual_picks=manual_picks_set
    )
    rechecks = sorted(rechecked)
    if cap is not None and len(ordered_regular) >= cap:
        for skill_id in rechecks:
            fresh.pop(skill_id, None)
        rechecks = []

    kept = [
        item
        for skill_id, item in previous_by_skill.items()
        if skill_id and skill_id not in fresh
    ]
    for item in kept:
        item["pending"] = True
        item.setdefault("first_queued_at", stamped)
        item.setdefault("baseline_fingerprint", catalogued.get(_skill_of(item)))

    queue = _ordered_pending(
        kept + list(fresh.values()), catalogued=catalogued,
        source_types=source_types, recheck=rechecked,
    )
    return {
        "queue": queue,
        "queue_pending": len(queue),
        "carried_over": sum(1 for item in queue if _skill_of(item) not in fresh),
        "needs_review": sum(1 for item in queue if item.get("content_changed")),
        "recheck": sorted(rechecked),
    }


def prepare(
    cfg: dict,
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
    expand: bool = True,
    expand_limit: int | None = None,
    state_dir: Path | None = None,
    data_dir: Path | None = None,
    pending: list[dict] | None = None,
    ledger: BudgetLedger | None = None,
    sleep=time.sleep,
    discover_fn=discover,
    fetch_fn=fetch_text,
) -> dict:
    """发现 → 无内容预筛 → 按上限抓取 → 指纹 → 带内容复筛。"""
    candidates, outcomes = discover_fn(
        cfg["searches"],
        sources=cfg["sources"],
        expand=expand,
        expand_limit=expand_limit,
        max_queries=limit_queries,
        sleep=sleep,
    )
    merged = dedupe(candidates)
    first_pass = [(candidate, prescreen(candidate, cfg["prescreen"], None)) for candidate in merged]

    source_types = cfg.get("source_types") or {}
    cat_entries = _catalogued_entries(Path(data_dir)) if data_dir else {}
    catalogued = {skill_id: entry.get("content_fingerprint") for skill_id, entry in cat_entries.items()}
    catalogued_review = {skill_id: bool(entry.get("needs_review")) for skill_id, entry in cat_entries.items()}

    for item in pending or []:
        skill = _skill_of(item)
        if skill and item.get("content_fingerprint"):
            catalogued.setdefault(skill, item["content_fingerprint"])

    cap_estimate = min(limit_evaluations, len(first_pass) + len(pending or []))
    cap = cap_estimate if limit_fetches is None else max(0, limit_fetches)

    plan = _accumulate_plan(
        first_pass, list(pending or []), catalogued=catalogued,
        catalogued_review=catalogued_review,
        source_types=source_types,
        cfg=cfg, ledger=ledger, cap=cap,
    )
    slots = min(limit_evaluations, len(plan["queue"]))

    staged: dict[str, str] = {}
    texts_dir = (state_dir / TEXTS_DIRNAME) if state_dir else None
    if texts_dir:
        texts_dir.mkdir(parents=True, exist_ok=True)

    enriched: list[tuple[Candidate, PrescreenResult, dict]] = []
    fetched_count = 0
    for item in plan["queue"]:
        if fetched_count >= cap:
            break
        candidate = candidate_from_payload(item["candidate"])

        fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
        text = fetched.text if (fetched.ok and fetched.text) else None
        note = {"ok": bool(text), "bytes": fetched.bytes_read, "reason_code": fetched.reason_code}
        if not fetched.ok:
            note["upstream_gone"] = fetched.reason_code == REASON_UPSTREAM_GONE
        if text:
            fresh_fingerprint = content_fingerprint(text)
            baseline = item.get("baseline_fingerprint")
            note["content_changed"] = bool(baseline and baseline != fresh_fingerprint)
            note["truncated"] = fetched.truncated
            item["content_fingerprint"] = fresh_fingerprint
            item["candidate"]["content_fingerprint"] = fresh_fingerprint
            item["content_changed"] = note["content_changed"]
            item["last_attempted_at"] = _stamp()
            fetched_count += 1
            candidate.content_fingerprint = fresh_fingerprint
            if texts_dir is not None:
                staged[candidate.skill_id] = text
        item["fetch"] = note
        item["prescreen"] = asdict(prescreen(candidate, cfg["prescreen"], text))
        enriched.append((candidate, prescreen_from_payload(item["prescreen"]), note))

    for item in plan["queue"][cap:]:
        candidate = candidate_from_payload(item["candidate"])
        item["fetch"] = {
            "ok": False, "bytes": 0, "reason_code": None,
            "skipped": "超出本次抓取上限，留待后续运行",
        }
        enriched.append((candidate, prescreen_from_payload(item["prescreen"]), item["fetch"]))

    for candidate, result in first_pass:
        if result.decision != DECISION_QUEUED:
            enriched.append(
                (candidate, result, {"ok": False, "bytes": 0, "reason_code": None, "skipped": "预筛排除，未抓取"})
            )

    if texts_dir is not None and staged:
        write_json_atomic(texts_dir / "staged.json", staged)

    active_snoozed_dict = get_active_snoozed((cfg or {}).get("snoozed") or {})
    active_snoozed_set = set(active_snoozed_dict.keys())
    queued = [(c, p, f) for c, p, f in enriched if p.decision == DECISION_QUEUED]
    excluded = [(c, p, f) for c, p, f in enriched if p.decision != DECISION_QUEUED]
    batch = [
        (c, p, f)
        for c, p, f in queued
        if c.content_fingerprint and c.skill_id not in active_snoozed_set
    ]

    return {
        "outcomes": outcomes,
        "queued": queued,
        "batch": batch,
        "excluded": excluded,
        "queue": plan["queue"],
        "queue_pending": plan["queue_pending"],
        "carried_over": plan["carried_over"],
        "needs_review": plan["needs_review"],
        "discovery_total": len(candidates),
        "discovery_failed": sum(1 for o in outcomes if not o.ok),
        "fetch_cap": cap,
        "fetched": fetched_count,
        "evaluation_slots": min(slots, len(batch)),
    }


@catalog_task
def phase_reserve(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    state_dir: str | Path | None = None,
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
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

    fetch_limit = limit_fetches
    if fetch_limit is None:
        fetch_limit = (cfg["rules"].get("run_limits") or {}).get("max_fetches_per_run")

    pending = _queue_pending(_read_queue(state_path) if (state_path / QUEUE_FILENAME).exists() else None)
    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    ledger.rollover()
    ledger.mark_in_progress_as_needs_recovery(started)

    plan = prepare(
        cfg, config_dir=config_dir, limit_queries=limit_queries,
        limit_evaluations=limit_evaluations, limit_fetches=fetch_limit,
        expand=expand, expand_limit=expand_limit,
        state_dir=state_path, data_dir=data_path, pending=pending, ledger=ledger,
        sleep=sleep, discover_fn=discover_fn, fetch_fn=fetch_fn,
    )

    if plan["discovery_total"] == 0 and _has_previous_catalog(data_path):
        return {
            "ok": False,
            "stage": "discover",
            "error": "本轮未发现任何候选且既有索引非空，中止以避免清空目录",
            "discovery_failed": plan["discovery_failed"],
            "outcomes": _outcome_records(plan["outcomes"]),
        }

    batch = plan["batch"][: plan["evaluation_slots"]]
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

    write_queue_payload = {
        "week": ledger.week,
        "excluded": plan["excluded"],
        "outcomes": _outcome_records(plan["outcomes"]),
        "evaluation_slots": plan["evaluation_slots"],
    }
    _write_queue(
        state_path,
        write_queue_payload,
        generated_at=started.replace(microsecond=0).isoformat(),
        pending=plan["queue"],
    )

    return {
        "ok": True,
        "phase": "reserve",
        "week": ledger.week,
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "fetched": plan["fetched"],
        "fetch_cap": plan["fetch_cap"],
        "evaluation_slots": plan["evaluation_slots"],
        "reserved": len(reserved),
        "queue_pending": plan["queue_pending"],
        "carried_over": plan["carried_over"],
        "needs_review_backlog": plan["needs_review"],
        "quota": ledger.snapshot(),
        "discovery_failed": plan["discovery_failed"],
        "state_dir": str(state_path),
    }


def dry_run(
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
    expand: bool = True,
    expand_limit: int | None = None,
    sleep=time.sleep,
) -> dict:
    """只验证配置与计算计划：不调用模型、不写账本、不提交、不部署（§7.3）。"""
    from .budget import week_id
    from .evaluation import resolve_api_key

    cfg = load_all_config(config_dir)
    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems, "dry_run": True}

    fetch_limit = limit_fetches
    if fetch_limit is None:
        fetch_limit = (cfg["rules"].get("run_limits") or {}).get("max_fetches_per_run")

    plan = prepare(
        cfg,
        config_dir=config_dir,
        limit_queries=limit_queries,
        limit_evaluations=limit_evaluations,
        limit_fetches=fetch_limit,
        expand=expand,
        expand_limit=expand_limit,
        state_dir=None,
        sleep=sleep,
    )
    return {
        "ok": True,
        "dry_run": True,
        "week": week_id(),
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "fetched": plan["fetched"],
        "fetch_cap": plan["fetch_cap"],
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


__all__ = [
    "DEFAULT_LIMIT_EVALUATIONS",
    "TEXTS_DIRNAME",
    "MAX_RECHECKS_PER_RUN",
    "prepare",
    "phase_reserve",
    "dry_run",
]
