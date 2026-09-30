"""阶段二流水线：评估预留项、调用决策机、统一条目状态转移并输出目录产物（§7.2 步骤 4~7）。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import time
from typing import Any

from src.infra.files import read_json, write_json_atomic
from src.infra.http import fetch_text
from src.shared.identity import content_fingerprint
from src.shared.runtime import now_local
from src.shared.materials import validate_document, primary_material_bundle
from src.shared.usage import UsageTotals
from .store import catalog_task
from .budget import BudgetLedger
from .decide import decide
from .prescreen import should_static_skip
from .evaluation import evaluate, evaluation_id
from .filter_rules import eligible_for_topic_filter, filter_new_evaluation, successful_evaluation_skill_ids
from .models import Candidate, PrescreenResult
from .overrides import apply_manual_overrides, get_manual_exclusions, get_manual_picks
from .report import build_report, write_report
from .snooze import apply_snooze_overrides, get_active_snoozed
from src.shared.owned import is_skill_owned

from .config import load_all_config, precheck
from .entry_state import (
    EntryUpdateEvent,
    UPSTREAM_GONE,
    UPSTREAM_OK,
    update_entry,
)
from .index import CatalogContext, build_catalog, index_by_id, merge_entries
from .store import write_catalog
from .queue import (
    QUEUE_FILENAME,
    candidate_from_payload,
    mark_settled,
    prescreen_from_payload,
    read_queue,
    settled_pending,
    skill_of,
)
from .sync_reserve import DEFAULT_LIMIT_EVALUATIONS, TEXTS_DIRNAME


def _previous_entries(data_dir: Path) -> list[dict]:
    path = data_dir / "catalog.json"
    if not path.exists():
        return []
    catalog = read_json(path)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("entries"), list):
        raise ValueError("主索引结构无效；停止评估以保留原文件")
    return catalog["entries"]


def _evaluate_queue(queue, cfg, ledger, staged, started, token_cap,
                    fetch_fn, evaluate_fn, api_key, sleep, model_pool_root=None,
                    previous_entries=None):
    from src.infra.model_pool import ModelPool, PoolStopped, PoolReselect
    pool = None
    pool_stop = None
    if 'models' in cfg['model']:
        pool = ModelPool(cfg['model'], model_pool_root or ledger.state_dir.parent.parent)
        try:
            pool.start()
        except PoolStopped as exc:
            return {'results': {}, 'evaluated': 0, 'skipped': 0, 'settled_items': [],
                    'tokens_used': 0, 'token_stopped': [], 'usage_unknown': False, 'stop_reason': exc.reason}
    reserved_ids = set(ledger.reserved)
    tokens_used = 0
    token_stopped = []
    results: dict[str, dict] = {}
    evaluated = 0
    skipped = 0
    settled_items: list[str] = []
    usage_unknown = False

    manual_picks_dict = get_manual_picks((cfg or {}).get("favorites") or (cfg or {}).get("overrides") or {})
    manual_picks_set = set(manual_picks_dict.keys())
    manual_exclusions_dict = get_manual_exclusions((cfg or {}).get("overrides") or {})
    manual_exclusions_set = manual_exclusions_dict
    active_snoozed_dict = get_active_snoozed((cfg or {}).get("snoozed") or {})
    active_snoozed_set = set(active_snoozed_dict.keys())
    owned_cfg = (cfg or {}).get("owned") or {}
    owned_ids = {it["skill_id"] for it in owned_cfg.get("items", [])}
    previous_by_id = index_by_id(previous_entries or [])
    evaluated_skill_ids = set()
    filter_rules = cfg.get("filter_rules")
    if filter_rules and filter_rules.has_evaluation_rules:
        directories = [ledger.evaluations_dir]
        if model_pool_root is not None:
            directories.append(Path(model_pool_root) / "data" / "local" / "state" / "evaluations")
        evaluated_skill_ids = successful_evaluation_skill_ids(*directories)

    for item in queue.get("pending", []):
        candidate = candidate_from_payload(item["candidate"])
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
        if is_skill_owned(candidate.skill_id, owned_ids):
            results[candidate.skill_id] = {"status": "skipped", "note": "已收录条目直接跳过"}
            skipped += 1
            continue

        if candidate.skill_id in manual_exclusions_set:
            results[candidate.skill_id] = {"status": "skipped", "note": "人工排除黑名单条目直接跳过"}
            skipped += 1
            continue

        if candidate.skill_id in manual_picks_set:
            results[candidate.skill_id] = {"status": "skipped", "note": "人工收藏条目不调用模型重新评估"}
            skipped += 1
            continue

        if candidate.skill_id in active_snoozed_set:
            results[candidate.skill_id] = {"status": "skipped", "note": "临时冷冻条目直接跳过"}
            skipped += 1
            continue

        enable_static_skip = bool(
            (cfg.get("rules", {}).get("static_heuristics") or {}).get("enable_skip")
            or cfg.get("enable_static_skip")
        )
        pres_obs = (item.get("prescreen") or {}).get("static_observation")
        if should_static_skip(pres_obs, enable_static_skip):
            results[candidate.skill_id] = {"status": "skipped", "note": "静态规则明确空壳占位，跳过评估"}
            skipped += 1
            continue

        if mark_settled(item, cfg, ledger):
            settled_items.append(skill_of(item))
            continue

        if eid not in reserved_ids:
            continue

        existing = ledger.get(eid) or {}
        if existing.get("status") == "completed":
            skipped += 1
            continue

        allowed, reason = ledger.can_attempt(eid)
        if not allowed:
            results[candidate.skill_id] = {"status": "skipped", "note": reason}
            skipped += 1
            continue

        if usage_unknown or (token_cap and tokens_used >= token_cap):
            token_stopped.append(candidate.skill_id)
            results[candidate.skill_id] = {
                "status": "stopped",
                "note": "请求用量未知，本轮停止调用模型" if usage_unknown else f"已达单次运行 token 上限 {token_cap}，本轮不再调用模型",
            }
            continue

        text = staged.get(candidate.skill_id)
        if text is None:
            fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
            if not fetched.ok or not fetched.text or fetched.truncated:
                ledger.fail(eid, fetched.reason_code or "NETWORK_ERROR", fetched.error or "抓取失败", started)
                results[candidate.skill_id] = {"status": "failed", "note": "抓取失败"}
                continue
            text = fetched.text
            fresh = content_fingerprint(text)
            if candidate.content_fingerprint and fresh != candidate.content_fingerprint:
                results[candidate.skill_id] = {
                    "status": "skipped",
                    "fingerprint_changed": True,
                    "note": f"上游内容已变化（预留: {candidate.content_fingerprint} vs 当前: {fresh}），跳过本次评估，待重新排队",
                }
                skipped += 1
                item["status"] = "pending"
                item["content_changed"] = True
                continue

        valid, material_error = validate_document(candidate.path or "SKILL.md", text)
        if not valid or content_fingerprint(text) != candidate.content_fingerprint:
            results[candidate.skill_id] = {"status": "skipped", "note": material_error or "material_identity_changed"}
            skipped += 1
            continue
        materials = primary_material_bundle(candidate, text, started.isoformat())
        if pool is None:
            ledger.begin_attempt(eid, started)
        request_records = list(existing.get("requests") or [])
        observed_requests = 0

        def on_request(event, stage, request_call):
            nonlocal tokens_used, usage_unknown, observed_requests
            if event == "before":
                if pool:
                    if not pool.model_available(request_call['requested_model']):
                        raise PoolReselect()
                    if usage_unknown:
                        raise PoolStopped('usage_unknown')
                    if token_cap and tokens_used + request_call['reserved_tokens'] > token_cap:
                        raise PoolStopped('token_limit')
                if stage == "review" and (usage_unknown or (token_cap and tokens_used >= token_cap)):
                    return False
                request_records.append({"stage": stage, "state": "started", "status": "in_progress", "usage": None, **(request_call or {})})
            else:
                usage = UsageTotals()
                observed_requests += 1
                summary = usage.add(request_call)
                tokens_used += usage.total_tokens
                usage_unknown = usage_unknown or usage.unknown_usage_requests > 0
                request_records[-1].update(status="completed" if request_call.ok else "failed", usage=summary,
                    state='received' if request_call.ok else 'error', raw_usage=request_call.usage,
                    billing_state=getattr(request_call, 'billing_state', None),
                    provider_error_code=getattr(request_call, 'provider_error_code', None),
                    response={k: getattr(request_call, k, None) for k in ('ok', 'content', 'reason_code', 'http_status')})
            record = ledger.get(eid)
            record["requests"] = request_records
            if pool:
                record["status"] = "reserved" if event == "after" and getattr(request_call, "billing_state", None) == "rejected_before_inference" else "in_progress"
            ledger.save_record(eid, record, started)
            return True

        topic_options = {}
        if eligible_for_topic_filter(
            filter_rules, previous_by_id.get(candidate.skill_id),
            previously_evaluated=candidate.skill_id in evaluated_skill_ids,
        ):
            topic_options["filter_rules"] = filter_rules
        outcome = evaluate_fn(
            candidate, text, model_cfg=cfg["model"], rules=cfg["rules"],
            taxonomy=cfg["taxonomy"], api_key=api_key, sleep=sleep, on_request=on_request,
            pending_evaluation=existing.get("pending_evaluation"),
            **({"model_pool": pool} if pool else {}),
            **topic_options,
        )
        if outcome.get('pool_stop'):
            pool_stop = outcome['pool_stop']
            checkpoint = ledger.get(eid)
            checkpoint.update(status='needs_recovery' if usage_unknown or pool_stop == 'quota_response_conflict' else 'reserved', pause_reason=pool_stop)
            ledger.save_record(eid, checkpoint, started)
            results[candidate.skill_id] = {'status': 'stopped', 'note': pool_stop}
            break
        call = outcome.get("call")
        if call is not None and not observed_requests:
            tokens_used += int(getattr(call, "total_tokens", 0) or 0)
        if not outcome["ok"]:
            if outcome.get("pending_evaluation"):
                checkpoint = ledger.get(eid)
                checkpoint.update(status="reserved", pending_evaluation=outcome["pending_evaluation"],
                                  max_attempts=int(checkpoint.get("max_attempts") or 1) + 1)
                ledger.save_record(eid, checkpoint, started)
                results[candidate.skill_id] = {"status": "review_pending", "note": outcome["error"]}
                continue
            ledger.fail(eid, outcome["reason_code"], outcome["error"] or "", started)
            results[candidate.skill_id] = {"status": "failed", "note": outcome["error"]}
            continue

        evaluation = outcome["evaluation"]
        decision = filter_new_evaluation(
            decide(evaluation, cfg["rules"]), evaluation, filter_rules,
            previous_by_id.get(candidate.skill_id),
            previously_evaluated=candidate.skill_id in evaluated_skill_ids,
        )
        ledger.complete(
            eid,
            {
                **decision,
                "main_category": evaluation.get("main_category"),
                "evaluation": evaluation,
                "model": getattr(call, "requested_model", None),
                "model_config_fingerprint": getattr(call, "model_config_fingerprint", None),
                "materials": materials.manifest(),
                "request_usage": request_records,
                "candidate": asdict(candidate),
                "prescreen": item["prescreen"],
                "evaluated_at": started.replace(microsecond=0).isoformat(),
            },
            started,
        )
        results[candidate.skill_id] = {"status": "completed", "decision": decision["decision"]}
        evaluated_skill_ids.add(candidate.skill_id)
        evaluated += 1

    return {"results": results, "evaluated": evaluated, "skipped": skipped, "settled_items": settled_items, "tokens_used": tokens_used, "token_stopped": token_stopped, "usage_unknown": usage_unknown, "stop_reason": pool_stop}


def _build_evaluated_catalog(previous_entries, queue, cfg, ledger, context):
    manual_picks_dict = get_manual_picks((cfg or {}).get("favorites") or (cfg or {}).get("overrides") or {})
    manual_exclusions_dict = get_manual_exclusions(cfg.get("overrides") or {})
    active_snoozed_set = set(get_active_snoozed(cfg.get("snoozed") or {}))
    owned_cfg = (cfg or {}).get("owned") or {}
    owned_ids = {it["skill_id"] for it in owned_cfg.get("items", [])}
    previous_by_id = index_by_id(previous_entries)

    fresh_entries: list[dict] = []
    for item in queue.get("pending", []):
        cand = candidate_from_payload(item["candidate"])
        if cand.skill_id in active_snoozed_set or is_skill_owned(cand.skill_id, owned_ids):
            continue
        pres = prescreen_from_payload(item["prescreen"])
        fetch_note = item.get("fetch") or {}
        eid = evaluation_id(cand, cfg["model"], cfg["rules"])
        record = ledger.get(eid) or {}
        outcome = record.get("outcome") or {}
        evaluation = outcome.get("evaluation")
        decision = (
            {"decision": outcome["decision"], "reason_codes": outcome.get("reason_codes", [])}
            if outcome.get("decision")
            else None
        )
        previous = previous_by_id.get(cand.skill_id)
        if evaluation is None and previous and cand.skill_id in manual_picks_dict:
            # 收藏跳过评估时，保留已提交的条目，不能用无结果事件改成 pending。
            continue
        event_kind = "cached_evaluation" if evaluation is not None else "no_evaluation"
        upstream_status = UPSTREAM_GONE if fetch_note.get("upstream_gone") else UPSTREAM_OK

        event = EntryUpdateEvent(
            kind=event_kind,
            prescreen_result=pres,
            evaluation=evaluation,
            decision=decision,
            upstream_status=upstream_status,
            fetched_fingerprint=cand.content_fingerprint,
            rules_version=cfg["rules"].get("rules_version", "1.0.1"),
            model_config_version=cfg["model"].get("model_config_version", "1.0.0"),
            evaluation_id=eid,
            evaluated_at=outcome.get("evaluated_at"),
        )
        fresh_entries.append(update_entry(previous, cand, event, context))

    for item in queue.get("excluded", []):
        cand = candidate_from_payload(item["candidate"])
        if cand.skill_id in active_snoozed_set or is_skill_owned(cand.skill_id, owned_ids):
            continue
        pres = prescreen_from_payload(item["prescreen"])
        previous = previous_by_id.get(cand.skill_id)
        event = EntryUpdateEvent(
            kind="no_evaluation",
            prescreen_result=pres,
            evaluation=None,
            decision=None,
            upstream_status=UPSTREAM_OK,
            fetched_fingerprint=cand.content_fingerprint,
            rules_version=cfg["rules"].get("rules_version", "1.0.1"),
            model_config_version=cfg["model"].get("model_config_version", "1.0.0"),
        )
        fresh_entries.append(update_entry(previous, cand, event, context))

    merged = merge_entries(previous_entries, fresh_entries)
    apply_manual_overrides(merged, manual_picks_dict, manual_exclusions_dict)
    apply_snooze_overrides(merged, (cfg or {}).get("snoozed"))
    catalog = build_catalog(
        merged,
        context=context,
        favorites=(cfg or {}).get("favorites"),
        overrides=(cfg or {}).get("overrides"),
        snoozed=(cfg or {}).get("snoozed"),
        owned=(cfg or {}).get("owned"),
    )
    return catalog


@catalog_task
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

    previous_entries = _previous_entries(data_path)
    queue = read_queue(state_path)
    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    ledger.rollover()
    token_cap = int((cfg["model"].get("limits") or {}).get("max_total_tokens_per_run") or 0)

    staged: dict[str, str] = {}
    staged_path = state_path / TEXTS_DIRNAME / "staged.json"
    if staged_path.exists():
        try:
            staged = read_json(staged_path, default={})
        except (OSError, json.JSONDecodeError):
            staged = {}

    batch = _evaluate_queue(queue, cfg, ledger, staged, started, token_cap,
                            fetch_fn, evaluate_fn, api_key, sleep,
                            model_pool_root=Path(config_dir).resolve().parent,
                            previous_entries=previous_entries)
    results, evaluated, skipped = batch["results"], batch["evaluated"], batch["skipped"]
    settled_items = batch["settled_items"]
    tokens_used, token_stopped = batch["tokens_used"], batch["token_stopped"]

    context = CatalogContext(
        rules_version=cfg["rules"].get("rules_version"),
        generated_at=started.replace(microsecond=0).isoformat(),
        domain_names=cfg["prescreen"].domain_names,
        source_types=cfg["source_types"],
    )

    catalog = _build_evaluated_catalog(previous_entries, queue, cfg, ledger, context)
    merged = catalog["entries"]
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
    write_json_atomic(state_path / "build-manifest.json", build_manifest)

    queued_before = list(queue.get("pending") or [])
    remaining = settled_pending(queued_before, cfg, ledger)
    queue["pending"] = remaining
    queue["generated_at"] = context.generated_at
    write_json_atomic(state_path / QUEUE_FILENAME, queue)

    if staged_path.exists():
        try:
            staged_path.unlink()
        except OSError:
            pass

    return {
        "ok": True,
        "phase": "evaluate",
        "week": week,
        "evaluated": evaluated,
        "skipped": skipped,
        "settled": len(settled_items),
        "tokens_used": tokens_used,
        "token_cap": token_cap or None,
        "token_stopped": token_stopped,
        "usage_unknown": batch["usage_unknown"],
        "stop_reason": batch.get("stop_reason"),
        "results": results,
        "entries_before": len(previous_entries),
        "entries_after": len(merged),
        "queue_pending": len(remaining),
        "queue_dropped": len(queued_before) - len(remaining),
        "quota": ledger.snapshot(),
        "manifest": manifest,
        "report_paths": written,
    }


__all__ = ["phase_evaluate"]
