"""本地按推荐数量收集；复用采集、筛选、决策与索引，不改 Actions 周额度。

拆解为模块化协同：
- local_state: LocalCollection 运行状态、检查点落盘与报告渲染
- local_candidate: 单候选预筛、材料抓取、缓存复用与受控模型评估
- local: 发现补水编排、仓库批次调度与 CLI 入口
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from src.infra.files import write_json_atomic, write_text_atomic
from src.infra.http import fetch_text
from src.shared.materials import validate_document
from src.shared.model_config import material_fetch_limit
from src.shared.owned import is_skill_owned
from src.shared.runtime import is_test_environment, now_local
from src.shared.usage import UsageTotals
from src.shared.versions import STATIC_HEURISTIC_VERSION, build_config_fingerprint
from .batch import (
    DEFAULT_EXCLUDE_TERMS,
    DEFAULT_QUERY_TEMPLATE,
    DiscoveryStopped,
    acquire_next_repo_batch,
    build_expansion_prompt,
    build_query_string,
    expand_batch_skills,
    init_or_migrate_discovery_state,
    load_discovery_state,
    parse_expansion_response,
    reconcile_batch_and_repositories,
    save_discovery_state,
)
from .budget import BudgetLedger
from .config import load_all_config, precheck
from .discovery import discover, github_search
from .entry_state import STATUS_RECOMMENDED
from .failure_policy import (
    STOP_CANDIDATES_EXHAUSTED,
    STOP_EVALUATION_LIMIT,
    STOP_INTERRUPTED,
    STOP_LABELS,
    STOP_TARGET_REACHED,
    STOP_TOKEN_LIMIT,
    STOP_USAGE_UNKNOWN,
    resolve_primary_stop_reason,
)
from .index import CatalogContext, build_catalog, index_by_id
from .local_candidate import (
    _evaluate_with_pool,
    _evaluate_with_retries,
    _record_request_usage,
    _retryable,
    _topic_evaluation_options,
    _update_blocked,
    find_normalized_candidate_record,
    process_candidate,
)
from .local_state import (
    DEFAULT_BATCH_REPO_LIMIT,
    LocalCollection,
    _read,
    _record_static_observation,
    _unknown_usage_reserve,
    _valid_settings,
    apply_result,
    init_local_state,
    save_and_render,
)
from .overrides import apply_manual_overrides_to_entry, get_manual_exclusions, get_manual_picks
from .parallel import TwoCandidateScheduler
from .pool import (
    CandidatePool,
    PoolItem,
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_LENGTH_EXCEEDED,
    STATUS_PENDING as POOL_STATUS_PENDING,
    append_new_candidates,
    classify_pending_candidate,
    create_pool_from_candidates,
    is_pool_expired,
    load_pool,
    prioritize_pending_batch,
    save_pool,
    update_candidate_status,
)
from .prescreen import analyze_static_tier
from .report import build_report, write_report
from .snooze import get_active_snoozed
from .store import catalog_task, mutate_catalog


def prepare_pool(
    root: Path,
    local: Path,
    cfg: dict,
    settings: dict,
    old_recommended: set[str],
    *,
    discover_fn: Callable = discover,
    sleep: Callable = time.sleep,
    log: Callable = print,
    report: dict | None = None,
) -> Any:
    """管道步骤 1：候选池准备，检查水位线并按需增量搜索补水。"""
    active_snoozed = get_active_snoozed(cfg.get("snoozed") or {})
    manual_exclusions = get_manual_exclusions(cfg.get("overrides") or {})
    owned_cfg = cfg.get("owned") or {}
    owned_ids = {it["skill_id"] for it in owned_cfg.get("items", [])}
    pool_path = local / "pool.json"
    force_refresh = settings.get("refresh_pool", False)
    watermark = settings.get("pool_watermark", 20)
    max_age_days = settings.get("pool_max_age_days", 7)
    pool = None
    previous_pool = load_pool(pool_path)
    length_skips = {it.candidate.skill_id: it for it in previous_pool.items
                    if it.status == STATUS_LENGTH_EXCEEDED} if previous_pool else {}
    blocked_skips = {it.candidate.skill_id: it for it in previous_pool.items
                     if it.status == STATUS_BLOCKED} if previous_pool else {}

    def count_actionable(p) -> int:
        return sum(
            1
            for it in p.items
            if it.status == POOL_STATUS_PENDING
            and it.candidate.skill_id not in active_snoozed
            and it.candidate.skill_id not in manual_exclusions
            and not is_skill_owned(it.candidate.skill_id, owned_ids)
        )

    if not force_refresh:
        pool = previous_pool
        if pool is not None and is_pool_expired(pool, max_age_days=max_age_days):
            log(f"本地候选池已超过 {max_age_days} 天有效期，重新运行发现并重建候选池...")
            pool = None

    if pool is None:
        if force_refresh:
            log("已指定 --refresh-pool，重新运行网络搜索并重建候选池，保留超长跳过标记...")
        else:
            log("未检测到有效本地候选池，正在首次搜索并展开真实 SKILL.md...")
        log("搜索阶段不调用模型。")
        candidates, outcomes = discover_fn(
            cfg["searches"],
            sources=cfg["sources"],
            expand=True,
            max_queries=settings.get("limit_queries"),
            expand_limit=settings.get("expand_limit"),
            sleep=sleep,
            progress=log,
        )
        if report is not None:
            report["discovery_failures"] = sum(not item.ok for item in outcomes)
        pool = create_pool_from_candidates(
            list(candidates) + [it.candidate for it in length_skips.values()]
            + [it.candidate for it in blocked_skips.values()],
            old_recommended, cfg["source_types"])
        for item in pool.items:
            previous_skip = length_skips.get(item.candidate.skill_id)
            if previous_skip:
                update_candidate_status(pool, item.seq, STATUS_LENGTH_EXCEEDED,
                                        checked_at=previous_skip.checked_at)
                continue
            previous_blocked = blocked_skips.get(item.candidate.skill_id)
            if previous_blocked:
                item.block_info = previous_blocked.block_info
                update_candidate_status(pool, item.seq, STATUS_BLOCKED,
                                        checked_at=previous_blocked.checked_at)
        save_pool(pool_path, pool)
        log(f"候选池已构建并保存至 {pool_path}，共 {len(pool)} 条候选，待处理 {pool.pending_count} 条。")
    else:
        actionable_count = count_actionable(pool)
        pending_count = pool.pending_count
        log(f"加载已有候选池：共 {len(pool)} 条，待处理 {pending_count} 条（可处理 {actionable_count} 条）。")
        if actionable_count < watermark:
            log(f"可处理候选数量 ({actionable_count}) 低于水位线 ({watermark})，正在增量搜索补水...")
            candidates, outcomes = discover_fn(
                cfg["searches"],
                sources=cfg["sources"],
                expand=True,
                max_queries=settings.get("limit_queries"),
                expand_limit=settings.get("expand_limit"),
                sleep=sleep,
                progress=log,
            )
            if report is not None:
                report["discovery_failures"] = sum(not item.ok for item in outcomes)
            added = append_new_candidates(pool, candidates, old_recommended, cfg["source_types"])
            save_pool(pool_path, pool)
            actionable_count = count_actionable(pool)
            log(f"增量补水完成，新增 {added} 条候选入池，当前池总量 {len(pool)} 条，可处理候选 {actionable_count} 条。")
        else:
            log(f"可处理候选充足（{actionable_count} >= 水位线 {watermark}），跳过网络搜索，秒级启动。")
            if report is not None:
                report["discovery_failures"] = 0

    return pool


def _evaluate_candidate_items(state: LocalCollection, items: list[PoolItem]) -> bool:
    """按小批次切分并在批内排序，调度并发或串行评估条目。返回 True 表示该组处理完毕，False 表示触发停止原因。"""
    if not items:
        return True
    enable_batch_prioritization = bool(
        state.settings.get('enable_batch_prioritization', False)
        or state.settings.get('enable_static_skip', False)
    )
    batch_size = int(state.settings.get('batch_size', 20) or 20)

    for chunk_start in range(0, len(items), batch_size):
        chunk = items[chunk_start : chunk_start + batch_size]
        tier_map: dict[str, str] = {}
        if enable_batch_prioritization:
            for it in chunk:
                cand = it.candidate
                if cand.skill_id not in state.batch_materials and cand.path.split('/')[-1] == 'SKILL.md':
                    if '/blob/' not in cand.url:
                        cand.url = f'https://github.com/{cand.owner}/{cand.repo}/blob/HEAD/{cand.path}'
                    u = cand.url.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1).replace('/blob/', '/', 1)
                    try:
                        fetched = state.fetch_fn(u, sleep=state.sleep, max_bytes=material_fetch_limit(state.cfg['model']))
                        state.batch_materials[cand.skill_id] = fetched
                        if fetched.ok and fetched.text and not fetched.truncated:
                            obs = analyze_static_tier(cand, fetched.text)
                            tier_map[cand.skill_id] = obs.get("tier")
                    except (IOError, OSError, TimeoutError, ValueError):
                        pass
            prioritized_chunk = prioritize_pending_batch(
                chunk,
                batch_size=batch_size,
                manual_picks=state.manual_picks,
                tier_map=tier_map,
                enabled=True,
            )
        else:
            prioritized_chunk = chunk

        if state.settings.get('parallel_evaluation', True):
            stopped = not TwoCandidateScheduler(state, process_candidate).run(prioritized_chunk)
        else:
            stopped = False
            for item in prioritized_chunk:
                state.pending_items.append(item)
                if not process_candidate(state, item):
                    stopped = True
                    break
        if stopped or state.report.get('stop_reason'):
            return False
    return True


def _expand_search_queries(state: LocalCollection) -> list[dict[str, str]]:
    """扩词请求逐次预记账、结算；收到的响应可离线恢复，不重复付费。"""
    from src.catalog.evaluation import call_model, resolve_api_key
    from src.infra.model_pool import PoolStopped
    from src.shared.output_contracts import resolve_response_format
    from .failure_policy import classify_result

    ds = state.discovery_state
    path = state.local / "state" / "discovery.json"
    taxonomy = state.cfg.get("taxonomy") or {}
    allowed = {d["id"] for d in taxonomy.get("main_categories", [])}
    if not allowed:
        raise DiscoveryStopped("search_plan_invalid")
    searches = state.cfg.get("searches", {})
    template = (searches.get("file_constraint") or {}).get("query_template", DEFAULT_QUERY_TEMPLATE)
    excludes = tuple((searches.get("global_exclusions") or {}).get("query_terms", DEFAULT_EXCLUDE_TERMS))
    existing = [c.get("term", "") for c in ds.query_cursors.values()]
    seen = {t.casefold() for t in existing}
    if ds.pending_expansion is None:
        ds.pending_expansion = {"task_id": f"expansion-round-{len(ds.expansion_history) + 1}",
                                "attempt": 1, "requests": []}
    pending = ds.pending_expansion
    accepted, rejected = [], []

    def persist():
        try:
            save_discovery_state(path, ds)
            state.save()
        except OSError as exc:
            raise PoolStopped('storage_error', str(exc)) from exc

    def invoke(model_cfg, fmt, context):
        if state.report.get("stop_reason"):
            raise PoolStopped(state.report["stop_reason"])
        if len(pending["requests"]) >= state.max_attempts:
            raise PoolStopped("retry_exhausted")
        reserve = (len(system.encode("utf-8")) + len(user.encode("utf-8"))
                   + len(json.dumps(fmt).encode("utf-8"))
                   + int((model_cfg.get("limits") or {}).get("max_output_tokens", 4000)) + 1024)
        if state.report["budget_tokens"] + reserve > state.settings["max_total_tokens"]:
            raise PoolStopped("token_limit")
        row = {**context, "logical_task_id": pending["task_id"], "run_id": state.run_id,
               "stage": "expansion", "state": "started", "status": "in_progress",
               "reserved_tokens": reserve, "reservation_state": "active", "usage": None}
        pending["requests"].append(row)
        state.report["calls"].append(row)
        persist()
        cfg = deepcopy(model_cfg)
        cfg.setdefault("request", {})["max_attempts"] = 1
        try:
            result = call_model(cfg, system, user, api_key=resolve_api_key(cfg),
                                response_format=fmt, sleep=state.sleep)
        except BaseException:
            state.usage.record_unknown_request()
            state.report["unknown_usage_reserved_tokens"] += reserve
            row.update(state="unknown", status="unknown", reservation_state="unknown",
                       unknown_usage_reserved_tokens=reserve)
            persist()
            raise
        previous_call, previous_reserve = state.active_call, state.unknown_reserve
        state.active_call, state.unknown_reserve = row, reserve
        try:
            decision = classify_result({"ok": result.ok, "call": result})
            _record_request_usage(state, result, retryable=decision.retryable)
        finally:
            state.active_call, state.unknown_reserve = previous_call, previous_reserve
        known = row["usage"]["total_tokens"] is not None or result.billing_state == "rejected_before_inference"
        row.update(state="received" if result.ok else "error", status="completed" if result.ok else "failed",
                   reservation_state="settled" if known else "unknown", raw_usage=result.usage,
                   response={k: getattr(result, k, None) for k in ("ok", "content", "reason_code", "error")})
        if not result.ok and result.billing_state != "rejected_before_inference":
            state.report["failed_requests"] += 1
        used = state.report.setdefault("models_used", [])
        model = getattr(result, "requested_model", None) or model_cfg.get("model")
        if model and model not in used:
            used.append(model)
        pending["response"] = row["response"]
        persist()
        if state.report.get("stop_reason"):
            raise PoolStopped(state.report["stop_reason"])
        return result

    while pending["attempt"] <= 2:
        system, user = build_expansion_prompt(taxonomy, existing)
        if pending["attempt"] > 1:
            user += "\n上一轮词汇重复或无效，请严格使用给出的分类 ID，并提供不同的检索词。"
        response = pending.get("response")
        if not response or not response.get("ok"):
            if getattr(state, "model_pool", None) is not None:
                result, _ = state.model_pool.run(system, user, "catalog_expansion", invoke,
                                                max_attempts=state.max_attempts, sleep=state.sleep)
            else:
                while True:
                    result = invoke(state.cfg["model"], resolve_response_format(state.cfg["model"], "catalog_expansion"),
                                    {"request_id": uuid4().hex, "requested_model": state.cfg["model"].get("model")})
                    decision = classify_result({"ok": result.ok, "call": result})
                    if result.ok or not decision.retryable:
                        break
                    state.sleep(1)
            if not result.ok:
                decision = classify_result({"ok": result.ok, "call": result})
                raise PoolStopped(decision.stop_cause or "search_plan_invalid")
            response = pending["response"]
        for item in parse_expansion_response(response.get("content") or ""):
            term, domain = item["term"], item["domain_id"]
            if domain not in allowed or term.casefold() in seen:
                rejected.append(item)
                continue
            domain_excludes = tuple((searches.get("per_domain", {}).get(domain) or {}).get("exclude_terms", []))
            ds.query_cursors[f"{domain}:{term}"] = {
                "domain_id": domain, "term": term,
                "q": build_query_string(term, template, excludes + domain_excludes),
                "source": "expansion", "next_page": 1, "exhausted": False,
                "page_attempts": 0, "last_error": None, "retry_at": None, "total_count": None,
            }
            accepted.append(item)
            seen.add(term.casefold())
        if accepted:
            break
        pending["attempt"] += 1
        pending.pop("response", None)
        persist()
    ds.expansion_history.append({**pending, "at": now_local().isoformat(),
        "input_basis": {"taxonomy_domains": len(allowed), "existing_terms_count": len(existing)},
        "accepted_count": len(accepted), "accepted_queries": accepted, "rejected_queries": rejected})
    ds.pending_expansion = None
    persist()
    return accepted
    return accepted


def _collect(
    root: Path,
    local: Path,
    settings: dict,
    cfg: dict,
    *,
    discover_fn: Callable = discover,
    fetch_fn: Callable = fetch_text,
    evaluate_fn: Callable = None,
    log: Callable = print,
    sleep: Callable = time.sleep,
    search_fn: Any = None,
    expand_fn: Any = None,
) -> dict:
    """本地收集主管道编排：初始化状态、候选池与批次调度、受控评估并收尾。"""
    if evaluate_fn is None:
        from .evaluation import evaluate
        evaluate_fn = evaluate

    run_id = uuid4().hex[:6]
    state = init_local_state(
        root=root, local=local, settings=settings, cfg=cfg, run_id=run_id,
        discover_fn=discover_fn, fetch_fn=fetch_fn, evaluate_fn=evaluate_fn,
        log=log, sleep=sleep, search_fn=search_fn, expand_fn=expand_fn,
    )
    pool_path = state.pool_path
    discovery_path = local / 'state' / 'discovery.json'

    try:
        from src.infra.model_pool import ModelPool, PoolStopped
        state.model_pool = None
        if 'models' in cfg['model']:
            state.model_pool = ModelPool(cfg['model'], root, log=log)
            state.model_pool.start()
            if STOP_USAGE_UNKNOWN in state.stop_causes:
                raise PoolStopped(STOP_USAGE_UNKNOWN, '存在未知结果请求，停止自动重发')
            history_path = local / 'model-pool-config-history.json'
            history = _read(history_path, {'changes': []})
            fingerprint = state.report['config_fingerprint']
            if history.get('current') != fingerprint:
                change = {'previous': history.get('current'), 'current': fingerprint,
                          'at': now_local().isoformat(), 'run_id': run_id}
                history['changes'].append(change)
                history['current'] = fingerprint
                write_json_atomic(history_path, history)
                state.report['config_changes'] = [change]

        state.report['evaluation_threads'] = 2 if settings.get('parallel_evaluation', True) else 1
        state.save()
        state.log(f"目标：新增 {state.settings['target_recommended']} 个推荐技能；上限 {state.settings['max_total_tokens']:,} Token。")
        state.log(f"评估并发：{state.report['evaluation_threads']} 个候选；账本与结果串行写入。")

        if pool_path.exists():
            state.pool = load_pool(pool_path)
        else:
            state.pool = CandidatePool(items=[])
            save_pool(pool_path, state.pool)

        state.discovery_state = init_or_migrate_discovery_state(
            discovery_path, state.pool, state.cfg.get('searches', {}), state.cfg.get('sources'),
        )
        pending_expansion = state.discovery_state.pending_expansion or {}
        if any(r.get('reservation_state') in ('active', 'unknown')
               for r in pending_expansion.get('requests', [])):
            raise PoolStopped(STOP_USAGE_UNKNOWN, '扩词请求结果或用量未确认，禁止自动重复付费请求')

        queried_keys, expanded_keys = set(), set()
        if settings.get("refresh_pool"):
            state.log("已指定 --refresh-pool，重置搜索游标以重新扫描...")
            for c in state.discovery_state.query_cursors.values():
                c["exhausted"] = False
                c["next_page"] = 1
                c["page_attempts"] = 0
            save_discovery_state(discovery_path, state.discovery_state)

        state.report['discovered'] = len(state.pool)
        state.report['blocked_total'] = state.pool.stats().get('blocked', 0)
        state.ledger.cap = state.ledger.reserved_count + max(1, len(state.pool))
        state.ledger.save()
        state.save()

        batch_repo_limit = int(state.settings.get("batch_repo_limit", 1000) or 1000)
        target_recommended = int(state.settings.get("target_recommended", 500) or 500)
        max_total_tokens = int(state.settings.get("max_total_tokens", 100000000) or 100000000)
        max_evaluations = state.settings.get("max_evaluations")

        while True:
            # 1. 运行级停止边界检查
            if state.report.get("stop_reason"):
                break
            if state.report["new_recommended"] >= target_recommended:
                state.stop_causes.add(STOP_TARGET_REACHED)
                state.report["stop_reason"] = resolve_primary_stop_reason(state.stop_causes)
                break
            if state.report["budget_tokens"] >= max_total_tokens:
                state.stop_causes.add(STOP_TOKEN_LIMIT)
                state.report["stop_reason"] = resolve_primary_stop_reason(state.stop_causes)
                break
            if max_evaluations and state.report["evaluations"] >= max_evaluations:
                state.stop_causes.add(STOP_EVALUATION_LIMIT)
                state.report["stop_reason"] = resolve_primary_stop_reason(state.stop_causes)
                break

            # 2. 如果当前没有活动批次，优先消耗已有候选池中的存量可处理候选
            if state.discovery_state.active_batch is None:
                actionable_items = [
                    it for it in state.pool.items
                    if it.status == POOL_STATUS_PENDING
                    and classify_pending_candidate(
                        it.candidate.skill_id,
                        state.active_snoozed,
                        state.manual_exclusions,
                        state.owned_ids,
                    )[0]
                ]
                if actionable_items:
                    state.log(f"发现已有候选池中有 {len(actionable_items)} 个可处理候选，优先评估存量工作...")
                    completed = _evaluate_candidate_items(state, actionable_items)
                    state.save()
                    if not completed or state.report.get("stop_reason"):
                        break
                    continue

                # 存量候选处理完毕，获取下一批仓库
                state.log(f"存量工作已就绪，获取下一批新仓库（上限 {batch_repo_limit} 个）...")
                batch = acquire_next_repo_batch(
                    state.discovery_state,
                    discovery_path,
                    batch_repo_limit,
                    state.cfg.get("searches", {}),
                    state.cfg.get("sources"),
                    search_fn=state.search_fn,
                    sleep=state.sleep,
                    log=state.log,
                    max_attempts=state.max_attempts,
                    limit_queries=settings.get('limit_queries'),
                    queried_keys=queried_keys,
                )

                if batch is not None:
                    allocated_repos = len(batch.get("repositories", []))
                    if allocated_repos < batch_repo_limit:
                        state.log(f"批次 #{batch['batch_id']}：发现 {allocated_repos}/{batch_repo_limit} 个新仓库；当前查询扫描完成，先评估这 {allocated_repos} 个")
                    else:
                        state.log(f"批次 #{batch['batch_id']}：分配 {allocated_repos}/{batch_repo_limit} 个新仓库进入展开与评估")
                else:
                    state.log("现有查询无法提供新仓库，正在分类范围内扩展关键词...")
                    new_queries = _expand_search_queries(state)
                    if new_queries:
                        state.log(f"扩词成功，新增 {len(new_queries)} 个新查询，继续搜索下一批...")
                        continue
                    else:
                        if not state.report.get("stop_reason"):
                            state.stop_causes.add(STOP_CANDIDATES_EXHAUSTED)
                            state.report["stop_reason"] = resolve_primary_stop_reason(state.stop_causes)
                        break

            # 3. 处理当前活动批次
            active = state.discovery_state.active_batch
            if active is not None:
                active_id = active.get("batch_id")
                stage = active.get("stage")
                if stage in ("discovering", "expanding"):
                    state.log(f"展开批次 #{active_id} 的仓库 SKILL.md...")
                    expand_batch_skills(
                        state.discovery_state,
                        discovery_path,
                        state.pool,
                        state.pool_path,
                        expand_repo_fn=state.expand_fn,
                        old_recommended=state.old_recommended,
                        source_types=state.cfg.get("source_types"),
                        sleep=state.sleep,
                        log=state.log,
                        max_attempts=state.max_attempts,
                        expand_limit=settings.get('expand_limit'),
                        expanded_keys=expanded_keys,
                    )
                    state.report['discovered'] = len(state.pool)
                    state.ledger.cap = state.ledger.reserved_count + max(1, len(state.pool))
                    state.ledger.save()
                    state.save()

                batch_skill_ids = set(active.get("skill_ids") or [])
                batch_actionable = [
                    it for it in state.pool.items
                    if it.candidate.skill_id in batch_skill_ids
                    and it.status == POOL_STATUS_PENDING
                    and classify_pending_candidate(
                        it.candidate.skill_id,
                        state.active_snoozed,
                        state.manual_exclusions,
                        state.owned_ids,
                    )[0]
                ]
                if batch_actionable:
                    state.log(f"批次 #{active_id}：评估 {len(batch_actionable)} 个可处理 Skill（本次新增推荐 {state.report['new_recommended']}/{target_recommended}）...")
                    completed = _evaluate_candidate_items(state, batch_actionable)
                else:
                    completed = True

                batch_finished = reconcile_batch_and_repositories(
                    state.discovery_state,
                    discovery_path,
                    state.pool,
                    state.active_snoozed,
                    state.manual_exclusions,
                    state.owned_ids,
                )
                state.save()
                if batch_finished:
                    state.log(f"批次 #{active_id} 已完成，开始获取下一批新仓库")

                if not completed or state.report.get("stop_reason"):
                    break

        if state.report['new_recommended'] >= state.settings['target_recommended']:
            state.stop_causes.add(STOP_TARGET_REACHED)
        elif state.report['budget_tokens'] >= state.settings['max_total_tokens']:
            state.stop_causes.add(STOP_TOKEN_LIMIT)
        elif state.settings.get('max_evaluations') and state.report['evaluations'] >= state.settings['max_evaluations']:
            state.stop_causes.add(STOP_EVALUATION_LIMIT)
        elif not state.stop_causes:
            state.stop_causes.add(STOP_CANDIDATES_EXHAUSTED)
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
    except (PoolStopped, DiscoveryStopped) as exc:
        state.stop_causes.add(exc.reason)
        state.report['error_message'] = str(exc)
    except KeyboardInterrupt:
        state.stop_causes.add(STOP_INTERRUPTED)
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
    except Exception as exc:
        import traceback
        state.stop_causes.add('error')
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes) or 'error'
        state.report['error_type'] = type(exc).__name__
        state.report['error_message'] = str(exc)
        state.report['error_traceback'] = traceback.format_exc()
        state.log(f"[系统异常] {type(exc).__name__}: {exc}")
    finally:
        if state.pool is not None:
            save_pool(state.pool_path, state.pool)
        if getattr(state, "discovery_state", None) is not None:
            save_discovery_state(local / "state" / "discovery.json", state.discovery_state)
        if state.active_eid:
            state.ledger.mark_needs_recovery(state.active_eid, '运行中断或异常，禁止自动重复付费请求')
            if state.active_call and state.active_call.get('usage') is None:
                state.usage.add(None)
                state.report['unknown_usage_reserved_tokens'] += state.unknown_reserve
                state.active_call['status'] = 'unknown'
        state.report['blocked_total'] = state.pool.stats().get('blocked', 0) if state.pool else 0
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes) or state.report.get('stop_reason')
        state.report['status'] = 'completed' if state.report['stop_reason'] == 'target_reached' else 'stopped'
        state.save()
        if state.dirty:
            changes = build_report(
                build_catalog(list(state.entries.values()), context=state.context, overrides=state.cfg.get('overrides'), snoozed=state.cfg.get('snoozed'), owned=state.cfg.get('owned')),
                previous_catalog=state.baseline,
                run_meta={'usage': state.usage.snapshot(), 'run_id': state.run_id},
            )
            write_report(changes, json_path=state.run_dir / 'changes.json', markdown_path=state.run_dir / 'changes.md')

    return state.report


@catalog_task
def run_local(
    root: Path,
    settings: dict,
    *,
    cfg: dict | None = None,
    discover_fn: Callable = discover,
    fetch_fn: Callable = fetch_text,
    evaluate_fn: Callable = None,
    log: Callable = print,
    sleep: Callable = time.sleep,
    search_fn: Any = None,
    expand_fn: Any = None,
) -> dict:
    """本地收集核心业务函数，受 catalog_task 会话级文件锁保护。"""
    cfg = cfg or load_all_config(root / 'config')
    _valid_settings(settings)
    local = root / 'data' / 'local'
    (local / 'runs').mkdir(parents=True, exist_ok=True)
    (local / 'state').mkdir(parents=True, exist_ok=True)

    return _collect(
        root, local, settings, cfg,
        discover_fn=discover_fn, fetch_fn=fetch_fn, evaluate_fn=evaluate_fn,
        log=log, sleep=sleep, search_fn=search_fn, expand_fn=expand_fn,
    )


def main(argv=None) -> int:
    """本地收集命令行入口。"""
    parser = argparse.ArgumentParser(description='本地收集技能')
    parser.add_argument('--check', action='store_true', help='仅预检')
    parser.add_argument('--refresh-pool', action='store_true', help='重建候选池')
    parser.add_argument('--batch-repos', type=int, help='每批最多分配的新仓库数')
    parser.add_argument('--target', type=int, help='新增推荐目标')
    parser.add_argument('--max-tokens', type=int, help='Token 上限')
    parser.add_argument('--max-evals', type=int, help='评估上限')
    parser.add_argument('--limit-queries', type=int, help='查询上限')
    parser.add_argument('--expand-limit', type=int, help='单仓库展开上限')
    parser.add_argument('--sync-config', action='store_true', help='同步本地配置')
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    cfg = load_all_config(root / 'config')

    if args.sync_config:
        print("正在同步本地配置...")
        baseline = _read(root / 'data' / 'catalog.json', {'entries': []})
        entries = index_by_id(baseline.get('entries') or [])
        manual_picks = get_manual_picks(cfg.get('favorites') or cfg.get('overrides') or {})
        manual_exclusions = get_manual_exclusions(cfg.get('overrides') or {})
        active_snoozed = get_active_snoozed(cfg.get('snoozed') or {})
        for e in entries.values():
            apply_manual_overrides_to_entry(e, manual_picks, manual_exclusions)
        apply_snooze_overrides(list(entries.values()), active_snoozed)
        context = CatalogContext(rules_version=cfg['rules']['rules_version'],
                                 domain_names=cfg['prescreen'].domain_names,
                                 source_types=cfg['source_types'])
        mutate_catalog(root, lambda _: build_catalog(list(entries.values()), context=context,
                                                    favorites=cfg.get("favorites"),
                                                    overrides=cfg.get("overrides"),
                                                    snoozed=cfg.get("snoozed"),
                                                    owned=cfg.get("owned")))
        print("本地配置同步完成。")
        return 0

    pre = precheck(cfg)
    if not pre.ok:
        for err in pre.errors:
            print(f"[错误] {err}")
        return 1

    model_display = '模型队列' if 'models' in cfg['model'] else cfg['model'].get('model', '未配置')
    print(f"预检通过；模型 {model_display}；API Key 已配置（不显示密钥）。")
    if args.check:
        return 0

    run_config = _read(root / 'config' / 'runners' / 'local-run.json', {})
    settings = dict(run_config.get('settings', {}))
    if args.batch_repos is not None:
        settings['batch_repo_limit'] = args.batch_repos
    if args.target is not None:
        settings['target_recommended'] = args.target
    if args.max_tokens is not None:
        settings['max_total_tokens'] = args.max_tokens
    if args.max_evals is not None:
        settings['max_evaluations'] = args.max_evals
    if args.limit_queries is not None:
        settings['limit_queries'] = args.limit_queries
    if args.expand_limit is not None:
        settings['expand_limit'] = args.expand_limit
    if args.refresh_pool:
        settings['refresh_pool'] = True

    try:
        _valid_settings(settings)
    except ValueError as exc:
        print(f"[配置错误] {exc}")
        return 1

    print(f"目标 {settings['target_recommended']} 个新增推荐，上限 {settings['max_total_tokens']:,} Token，每批仓库上限 {settings.get('batch_repo_limit', DEFAULT_BATCH_REPO_LIMIT)} 个。")
    print(f"网络及临时 HTTP 错误最多重连 {settings.get('max_retries', 5)} 次，尝试次数会保存。")
    if not os.environ.get('GITHUB_TOKEN'):
        print("未设置 GITHUB_TOKEN；GitHub 限流可能导致本轮候选不足，可在 PyCharm 的环境变量中设置。")

    report = run_local(root, settings, cfg=cfg)

    primary = report.get('stop_reason')
    if primary:
        msg = {
            'target_reached': '已达到推荐目标',
            'token_limit': '已达到 Token 上限',
            'evaluation_limit': '已达到评估次数上限',
            'candidates_exhausted': '候选池已全部处理完毕',
            'model_failures': '模型连续失败次数达到上限',
            'retry_exhausted': '单条候选重试次数已达上限',
            'format_failures': '模型输出格式异常达到上限',
            'access_denied': '访问凭据失效或权限不足',
            'resume_state_invalid': '恢复状态冲突',
            'request_config_error': '模型请求配置错误',
            'usage_unknown': '响应用量未知，停止自动重试',
            'interrupted': '用户中断运行',
            'storage_error': '持久化存储故障',
        }.get(primary, f'未知停止原因: {primary}')
        print(f"\n{msg}")

    print(f"本次新增推荐：{report['new_recommended']}/{settings['target_recommended']}；评估 {report['evaluations']} 次。")
    if 'pool_stats' in report:
        st = report['pool_stats']
        print(f"候选池状态：总计 {st['total']} 条，待处理 {st['pending']} 条，已完成 {st['done']} 条，排除 {st['excluded']} 条。")
    u = report.get('usage', {})
    print(f"请求 {u.get('requests', 0)} 次（含重试），失败请求 {report.get('failed_requests', 0)} 次。")
    print(f"已知输入 {u.get('prompt_tokens', 0):,} / 输出 {u.get('completion_tokens', 0):,} / 合计 {u.get('total_tokens', 0):,} Token。")
    print(f"其中推理 {u.get('reasoning_tokens', 0):,}（已含在输出中）；用量未知请求 {u.get('unknown_usage_requests', 0)}。")
    if report.get('unknown_usage_reserved_tokens', 0):
        print(f"未知用量预留预算 {report['unknown_usage_reserved_tokens']:,} Token（估算）；预算占用合计 {report.get('budget_tokens', 0):,}。")
    if u.get('incomplete_breakdown_requests'):
        print("部分响应未返回完整输入/输出明细，分项数值仅包含已返回的部分。")
    print(f"完整报告与本次推荐链接：{report['report_path']}")
    print(f"可读报告：{Path(report['report_path']).with_suffix('.md')}")
    print("查看目录：运行 scripts/preview.ps1，或 python -m http.server 8000 --directory public")

    return 0 if report.get('stop_reason') in ('target_reached', 'candidates_exhausted', None) else 1


if __name__ == '__main__':
    raise SystemExit(main())
