"""本地按推荐数量收集；复用采集、筛选、决策与索引，不改 Actions 周额度。

拆解为 4 步管道函数，保持签名与行为 100% 向后兼容：
- Step 1: prepare_pool: 候选池加载、水位线检查与自动补水
- Step 2: process_candidate: 候选预筛、内容抓取、缓存复用与受控模型评估
- Step 3: apply_result: 条目状态机更新（update_entry）与持久化
- Step 4: save_and_render: 运行报告、Markdown 页面与变更记录保存
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, asdict, field
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from src.infra.files import write_json_atomic, write_text_atomic
from src.shared.runtime import now_local
from src.shared.materials import validate_document, primary_material_bundle
from src.shared.owned import is_skill_owned
from .store import catalog_task
from .budget import BudgetLedger, evaluation_filename
from .config import load_all_config, precheck
from .decide import decide
from .dedupe import content_fingerprint, dedupe
from .discovery import discover
from .entry_state import (
    EntryUpdateEvent,
    STATUS_RECOMMENDED,
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    admission_decision,
    review_state,
    update_entry,
)
from .evaluation import (
    RETRYABLE_STATUS,
    build_prompt,
    evaluate,
    evaluation_id,
    resolve_api_key,
    validate_pending_evaluation,
)
from src.infra.http import fetch_text
from src.infra.llm import REASON_LENGTH_EXCEEDED
from src.shared.usage import UsageTotals
from .index import CatalogContext, build_catalog, build_entry, index_by_id
from .store import mutate_catalog
from .overrides import apply_manual_overrides_to_entry, get_manual_exclusions, get_manual_picks
from .snooze import apply_snooze_overrides, get_active_snoozed, load_snooze
from .pool import (
    STATUS_DONE,
    STATUS_EXCLUDED as POOL_STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_LENGTH_EXCEEDED,
    STATUS_BLOCKED,
    STATUS_PENDING as POOL_STATUS_PENDING,
    append_new_candidates,
    create_pool_from_candidates,
    get_pending_candidates,
    is_pool_expired,
    load_pool,
    save_pool,
    update_candidate_status,
)
from .prescreen import prescreen
from .report import build_report, write_report
from .failure_policy import (
    ACTION_BLOCKED,
    ACTION_DONE,
    ACTION_LENGTH_EXCEEDED,
    ACTION_RETRY,
    ERROR_KIND_OUTPUT_JSON_INVALID,
    ERROR_KIND_OUTPUT_SCHEMA_INVALID,
    ERROR_KIND_RESUME_STATE_INVALID,
    REASON_ACCESS_DENIED,
    REASON_REQUEST_CONFIG_ERROR,
    REASON_RESUME_STATE_INVALID,
    STOP_ACCESS_DENIED,
    STOP_CANDIDATES_EXHAUSTED,
    STOP_EVALUATION_LIMIT,
    STOP_FORMAT_FAILURES,
    STOP_INTERRUPTED,
    STOP_LABELS,
    STOP_MODEL_FAILURES,
    STOP_REQUEST_CONFIG_ERROR,
    STOP_RESUME_STATE_INVALID,
    STOP_RETRY_EXHAUSTED,
    STOP_STORAGE_ERROR,
    STOP_TARGET_REACHED,
    STOP_TOKEN_LIMIT,
    STOP_USAGE_UNKNOWN,
    classify_result,
    resolve_primary_stop_reason,
    update_failure_counters,
)


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _valid_settings(settings: dict) -> None:
    retries = settings.get("max_retries", 5)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("max_retries 必须是非负整数")
    for name in ("target_recommended", "max_total_tokens", "max_consecutive_failures"):
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    format_limit = settings.get("max_format_failures_without_valid_result", 10)
    if isinstance(format_limit, bool) or not isinstance(format_limit, int) or format_limit < 1:
        raise ValueError("max_format_failures_without_valid_result 必须是正整数")
    for name in ("max_evaluations", "limit_queries", "expand_limit"):
        value = settings.get(name)
        minimum = 0 if name == "limit_queries" else 1
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
            raise ValueError(f"{name} 必须 >= {minimum}，或设为 null")
    watermark = settings.get("pool_watermark", 20)
    if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
        raise ValueError("pool_watermark 必须是非负整数")
    max_age = settings.get("pool_max_age_days", 7)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 1:
        raise ValueError("pool_max_age_days 必须是正整数")


def _retryable(result: dict) -> bool:
    return not result["ok"] and (
        result.get("reason_code") == "NETWORK_ERROR"
        or getattr(result.get("call"), "http_status", None) in RETRYABLE_STATUS
    )


def _unknown_usage_reserve(candidate, text, cfg) -> int:
    """未返回 usage 的请求按输入 UTF-8 字节数＋最大输出＋消息余量预留预算。

    这是偏保守的估算，不声称是接口的精确分词或账单；与已知 Token 分列。
    """
    system, material = build_prompt(candidate, text, cfg["rules"], cfg["taxonomy"])
    return (
        len(system.encode("utf-8"))
        + len(material.encode("utf-8"))
        + 1024
        + int(cfg["model"].get("limits", {}).get("max_output_tokens", 4000))
    )


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


def apply_result(
    candidate: Any,
    pres: Any,
    outcome: dict | None = None,
    upstream_status: str = "ok",
    *,
    entries: dict[str, dict],
    context: CatalogContext,
    manual_picks: dict,
    manual_exclusions: dict,
    active_snoozed: set[str],
    root: Path,
    cfg: dict,
) -> dict:
    """管道步骤 3：统一条目状态转移并原子持久化到目录。"""
    outcome = outcome or {}
    previous = entries.get(candidate.skill_id)
    current_fp = getattr(candidate, "content_fingerprint", None)

    if not outcome:
        kind = "fetch_failed" if (pres and getattr(pres, "excluded", False)) or upstream_status != "ok" else "no_evaluation"
    elif outcome.get("cached"):
        kind = "cached_evaluation"
    else:
        kind = "fresh_evaluation"

    event = EntryUpdateEvent(
        kind=kind,
        prescreen_result=pres,
        evaluation=outcome.get("evaluation"),
        decision=outcome if outcome.get("decision") else None,
        upstream_status=upstream_status,
        fetched_fingerprint=current_fp,
        rules_version=cfg["rules"]["rules_version"],
        model_config_version=cfg["model"].get("model_config_version", "1.0.0"),
        evaluation_id=evaluation_id(candidate, cfg["model"], cfg["rules"]),
        evaluated_at=outcome.get("evaluated_at"),
    )
    context.generated_at = now_local().isoformat()
    entry = update_entry(
        previous_entry=previous,
        candidate=candidate,
        event=event,
        context=context,
    )
    apply_manual_overrides_to_entry(entry, manual_picks, manual_exclusions)
    apply_snooze_overrides([entry], active_snoozed)
    entries[candidate.skill_id] = entry

    mutate_catalog(root, lambda current: build_catalog(list(entries.values()), context=context, overrides=cfg.get("overrides"), snoozed=cfg.get("snoozed"), owned=cfg.get("owned")))
    return entry


def save_and_render(
    run_dir: Path,
    local: Path,
    report: dict,
    pool: Any,
    usage: UsageTotals,
    settings: dict,
    entries: dict[str, dict],
    old_recommended: set[str],
    *,
    context: CatalogContext | None = None,
    baseline: dict | None = None,
    dirty: bool = False,
    run_id: str = "",
    cfg: dict | None = None,
    owned_ids: set[str] | None = None,
) -> None:
    """管道步骤 4：落盘运行报告、可读 Markdown 以及目录差异报告。"""
    report["usage"] = usage.snapshot()
    report["budget_tokens"] = usage.total_tokens + report.get("unknown_usage_reserved_tokens", 0)
    report["updated_at"] = now_local().isoformat()
    owned_set = owned_ids or set()
    report["recommendations"] = [
        {key: e.get(key) for key in ("skill_id", "name", "url", "summary_zh", "main_category")}
        for sid, e in entries.items()
        if sid not in old_recommended and e.get("status") == STATUS_RECOMMENDED and not e.get("needs_review") and not e.get("manual_pick") and not is_skill_owned(sid, owned_set)
    ]
    report["new_recommended"] = len(report["recommendations"])
    if pool is not None:
        report["pool_stats"] = pool.stats()
    write_json_atomic(run_dir / "report.json", report)
    write_json_atomic(local / "latest-run.json", report)

    lines = [
        "# 本地运行报告",
        "",
        f"- 运行：{run_id}",
        f"- 状态：{STOP_LABELS.get(report.get('stop_reason'), '运行中')}",
    ]
    if pool is not None:
        pst = pool.stats()
        lines.append(
            f"- 候选池：共 {pst['total']} 条，待处理 {pst['pending']} 条"
            f"（已完成 {pst['done']}，排除 {pst['excluded']}，抓取失败 {pst['fetch_failed']}，非技能 {pst['not_skill']}，超长跳过 {pst['length_exceeded']}，已阻止 {pst['blocked']}）"
        )
    if report.get("skipped_owned"):
        lines.append(f"- 已收录跳过：{report['skipped_owned']}")
    lines.append(f"- 超长跳过候选：{report.get('skipped_length_exceeded', 0)} 条（超过单条输出 Token 上限）")
    lines.extend([
        f"- 本次新增推荐：{report['new_recommended']} / {settings['target_recommended']}",
        f"- 评估次数：{report['evaluations']}；复用已有评估：{report['cached']}",
        f"- 请求次数（含重试）：{usage.requests}；失败请求：{report['failed_requests']}",
        f"- 已知输入 Token：{usage.prompt_tokens:,}",
        f"- 已知输出 Token：{usage.completion_tokens:,}",
        f"- 已知总 Token：{usage.total_tokens:,} / {settings['max_total_tokens']:,}",
        f"- 未知用量预留预算：{report.get('unknown_usage_reserved_tokens', 0):,} Token（估算，不是实际用量）",
        f"- 预算占用合计：{report['budget_tokens']:,} Token",
        f"- 推理 Token：{usage.reasoning_tokens:,}（已包含在输出内）",
        f"- 用量未知请求：{usage.unknown_usage_requests}",
        f"- 分项不完整请求：{usage.incomplete_breakdown_requests}",
        "",
        "未知用量按输入字节数＋最大输出＋消息余量预留预算；这不是精确计费。Token 上限在每次请求结束后检查。金额以服务商账单为准。",
        "",
        "## 本次新增推荐",
        "",
    ])
    for item in report["recommendations"]:
        name = str(item["name"] or item["skill_id"]).replace("[", "（").replace("]", "）").replace("\n", " ")
        summary = str(item["summary_zh"] or "").replace("\n", " ")
        lines.append(f"- [{name}]({item['url']})：{summary}")
    write_text_atomic(run_dir / "report.md", "\n".join(lines) + "\n")

    if dirty and context is not None and baseline is not None:
        changes = build_report(
            build_catalog(list(entries.values()), context=context, overrides=(cfg or {}).get("overrides"), snoozed=(cfg or {}).get("snoozed"), owned=(cfg or {}).get("owned")),
            previous_catalog=baseline,
            run_meta={"usage": usage.snapshot(), "run_id": run_id},
        )
        write_report(changes, json_path=run_dir / "changes.json", markdown_path=run_dir / "changes.md")


@catalog_task
def run_local(
    root: Path,
    settings: dict,
    *,
    cfg=None,
    discover_fn=discover,
    fetch_fn=fetch_text,
    evaluate_fn=evaluate,
    log=print,
    sleep=time.sleep,
) -> dict:
    """每个候选本轮最多处理一次；每次模型响应后立即保存用量、记录与页面数据。"""
    _valid_settings(settings)
    root = Path(root).resolve()
    cfg = deepcopy(cfg if cfg is not None else load_all_config(root / "config"))
    problems = precheck(cfg)
    if not resolve_api_key(cfg["model"]):
        problems.append("缺少模型 API Key，请设置 config/model.local.json 或对应环境变量")
    if problems:
        raise ValueError("；".join(problems))
    # 在本地外层逐次重试并落账；底层禁用嵌套重试，避免 6×6 次请求。
    cfg["model"].setdefault("request", {})["max_attempts"] = 1
    local = root / "data" / "local"
    local.mkdir(parents=True, exist_ok=True)
    lock = local / "run.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("已有本地任务或上次强制终止留下 run.lock；确认旧进程退出后才能移除该锁") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        return _collect(root, local, settings, cfg, discover_fn, fetch_fn, evaluate_fn, log, sleep)
    finally:
        lock.unlink(missing_ok=True)


@dataclass
class LocalCollection:
    """Facts and injected dependencies for one local collection; never shared globally."""
    root: Any
    local: Any
    settings: Any
    cfg: Any
    discover_fn: Any
    fetch_fn: Any
    evaluate_fn: Any
    log: Any
    sleep: Any
    run_id: Any
    run_dir: Any
    usage: Any
    report: Any
    ledger: Any
    context: Any
    entries: Any
    old_recommended: Any
    baseline: Any
    active_snoozed: Any
    manual_exclusions: Any
    manual_picks: Any
    pool_path: Any
    pool: Any
    dirty: Any
    consecutive_failures: Any
    active_eid: Any
    active_call: Any
    unknown_reserve: Any
    max_attempts: Any
    max_retries: Any
    pending_items: Any
    owned_ids: Any
    skipped_owned_ids: Any
    format_failures: int = 0
    max_format_failures: int = 10
    stop_causes: set = field(default_factory=set)

    def save(self):
        save_and_render(self.run_dir, self.local, self.report, self.pool, self.usage,
            self.settings, self.entries, self.old_recommended, context=self.context,
            baseline=self.baseline, dirty=self.dirty, run_id=self.run_id,
            cfg=self.cfg, owned_ids=self.owned_ids)

    def publish(self, candidate, pres, outcome=None, upstream_status="ok"):
        apply_result(candidate, pres, outcome, upstream_status=upstream_status,
            entries=self.entries, context=self.context, manual_picks=self.manual_picks,
            manual_exclusions=self.manual_exclusions, active_snoozed=self.active_snoozed,
            root=self.root, cfg=self.cfg)
        self.dirty = True

def _evaluate_with_retries(state, candidate, text, eid, record):
    resume_error = validate_pending_evaluation(candidate, text, state.cfg['rules'],
                                               state.cfg['taxonomy'], record.get('pending_evaluation'))
    if resume_error:
        state.ledger.fail(eid, resume_error['reason_code'], resume_error['error'])
        checkpoint = state.ledger.get(eid)
        checkpoint.update(error_kind=resume_error['error_kind'], stage='resume', retryable=False)
        state.ledger.save_record(eid, checkpoint)
        return resume_error
    result = None
    attempt_limit = int(record.get('max_attempts') or state.max_attempts)
    initial_attempts = int(record.get('attempts') or 0)
    for index in range(initial_attempts, attempt_limit):
        if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
            state.stop_causes.add(STOP_TOKEN_LIMIT)
            state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
            break
        if index and (not record.get('pending_evaluation') or index > initial_attempts):
            delay = min(2 ** (index - 1), 8)
            state.log(f'重连 {index}/{state.max_retries}：{candidate.name}，{delay} 秒后重试。')
            state.sleep(delay)
        attempt = state.ledger.begin_attempt(eid)
        state.active_eid = eid
        state.active_call = {'skill_id': candidate.skill_id, 'attempt': attempt, 'max_attempts': attempt_limit, 'status': 'in_progress', 'usage': None}
        state.report['calls'].append(state.active_call)
        state.save()
        observed = []

        def on_request(event, stage, request_call):
            if event == 'before':
                if stage == 'review':
                    if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
                        state.stop_causes.add(STOP_TOKEN_LIMIT)
                        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
                        return False
                    if observed:
                        state.active_call = {'skill_id': candidate.skill_id, 'attempt': attempt,
                            'max_attempts': attempt_limit, 'status': 'in_progress', 'usage': None}
                        state.report['calls'].append(state.active_call)
                state.active_call['stage'] = stage
                checkpoint = state.ledger.get(eid)
                checkpoint.setdefault('requests', []).append(dict(state.active_call))
                state.ledger.save_record(eid, checkpoint)
                state.save()
                return True
            unknown_before = state.usage.unknown_usage_requests
            state.active_call['usage'] = state.usage.add(request_call)
            reserved = (state.usage.unknown_usage_requests - unknown_before) * state.unknown_reserve
            state.report['unknown_usage_reserved_tokens'] += reserved
            state.active_call['unknown_usage_reserved_tokens'] = reserved
            state.active_call['diagnostics'] = {'error_type': getattr(request_call, 'error_type', None),
                'http_status': getattr(request_call, 'http_status', None), 'latency_ms': getattr(request_call, 'latency_ms', None)}
            state.active_call['status'] = 'completed' if request_call.ok else 'failed'
            if getattr(request_call, 'reason_code', None) == REASON_LENGTH_EXCEEDED:
                state.active_call.update(status=STATUS_LENGTH_EXCEEDED,
                                         reason_code=REASON_LENGTH_EXCEEDED, is_sample_error=True)
            checkpoint = state.ledger.get(eid)
            checkpoint['requests'][-1] = dict(state.active_call)
            state.ledger.save_record(eid, checkpoint)
            observed.append(request_call)
            if state.active_call['usage']['total_tokens'] is None:
                state.stop_causes.add(STOP_USAGE_UNKNOWN)
                state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
            state.save()

        result = state.evaluate_fn(candidate, text, model_cfg=state.cfg['model'], rules=state.cfg['rules'], taxonomy=state.cfg['taxonomy'], sleep=state.sleep, on_request=on_request, pending_evaluation=record.get('pending_evaluation'))
        call = result.get('call')
        if not observed and call is not None:
            # 兼容单次评估适配器；生产评估逐请求即时落账。
            unknown_before = state.usage.unknown_usage_requests
            state.active_call['usage'] = state.usage.add(call)
            reserved_tokens = (state.usage.unknown_usage_requests - unknown_before) * state.unknown_reserve
            state.report['unknown_usage_reserved_tokens'] += reserved_tokens
            state.active_call['unknown_usage_reserved_tokens'] = reserved_tokens
            state.active_call['diagnostics'] = {'error_type': getattr(call, 'error_type', None), 'http_status': getattr(call, 'http_status', None), 'latency_ms': getattr(call, 'latency_ms', None)}
        
        decision = classify_result(result)
        if state.active_call:
            state.active_call['stage'] = result.get('stage') or state.active_call.get('stage')
            state.active_call['status'] = (STATUS_LENGTH_EXCEEDED
                if decision.is_length_exceeded
                else 'completed' if result['ok'] else 'failed')
            if decision.is_length_exceeded:
                state.active_call['reason_code'] = REASON_LENGTH_EXCEEDED
                state.active_call['is_sample_error'] = True
            elif decision.is_format_error:
                state.active_call['reason_code'] = decision.reason_code
                state.active_call['error_kind'] = decision.error_kind
                state.active_call['is_sample_error'] = True
            elif not result['ok']:
                state.active_call['reason_code'] = decision.reason_code
                state.active_call['error_kind'] = decision.error_kind
        state.save()
        if result.get('pending_evaluation'):
            checkpoint = state.ledger.get(eid)
            checkpoint.update(status='reserved', pending_evaluation=result['pending_evaluation'],
                              max_attempts=state.max_attempts + 1)
            state.ledger.save_record(eid, checkpoint)
            if state.active_call:
                state.active_call['status'] = 'completed'
            state.active_eid = None
            break
        if result['ok']:
            break
        code = result.get('reason_code') or decision.reason_code or 'MODEL_ERROR'
        diagnostic = state.active_call.get('diagnostics', {}) if state.active_call else {}
        details = [code]
        if result.get('error_kind'):
            details.append(result['error_kind'])
        if diagnostic.get('error_type'):
            details.append(diagnostic['error_type'])
        if diagnostic.get('http_status') is not None:
            details.append(f"HTTP {diagnostic['http_status']}")
        if diagnostic.get('latency_ms') is not None:
            details.append(f"耗时 {diagnostic['latency_ms'] / 1000:.1f} 秒")
        message = '；'.join(details)
        state.ledger.fail(eid, code, message)
        failure_record = state.ledger.get(eid)
        failure_record['retryable'] = decision.retryable
        if result.get('error_kind'):
            failure_record['error_kind'] = result.get('error_kind')
        if result.get('stage'):
            failure_record['stage'] = result.get('stage')
        state.ledger.save_record(eid, failure_record)
        state.active_eid = None
        if state.active_call:
            state.active_call['reason_code'] = code
        if decision.is_length_exceeded:
            if state.active_call:
                state.active_call['status'] = STATUS_LENGTH_EXCEEDED
                state.active_call['is_sample_error'] = True
            state.save()
            break
        if decision.is_format_error:
            # 格式异常为样本异常，不触发服务重连
            state.save()
            break
        if decision.stop_cause in (STOP_ACCESS_DENIED, STOP_RESUME_STATE_INVALID, STOP_REQUEST_CONFIG_ERROR):
            state.stop_causes.add(decision.stop_cause)
            state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
            state.save()
            break
        state.report['failed_requests'] += 1
        state.save()
        state.log(f'请求失败：{candidate.name}；{message}。')
        if not decision.retryable or state.report['stop_reason']:
            break
    return result


def _update_blocked(pool, seq: int, block_info: dict) -> None:
    """把候选池条目推进为 blocked 状态并记录阻止元数据。"""
    item = next((it for it in pool.items if it.seq == seq), None)
    if item is not None:
        item.block_info = block_info
    update_candidate_status(pool, seq, STATUS_BLOCKED)


def process_candidate(state, item):
    candidate = item.candidate
    seq = item.seq
    if is_skill_owned(candidate.skill_id, state.owned_ids):
        state.skipped_owned_ids.add(candidate.skill_id)
        state.report["skipped_owned"] = len(state.skipped_owned_ids)
        return True
    if candidate.skill_id in state.active_snoozed:
        return True
    if candidate.skill_id in state.manual_exclusions:
        return True
    if state.report.get('stop_reason'):
        return False
    if state.report['new_recommended'] >= state.settings['target_recommended']:
        state.stop_causes.add(STOP_TARGET_REACHED)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False
    if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
        state.stop_causes.add(STOP_TOKEN_LIMIT)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False
    if state.settings.get('max_evaluations') and state.report['evaluations'] >= state.settings['max_evaluations']:
        state.stop_causes.add(STOP_EVALUATION_LIMIT)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False
    state.report['checked'] += 1
    if candidate.path.split('/')[-1] != 'SKILL.md':
        state.report['not_skill_files'] += 1
        update_candidate_status(state.pool, seq, STATUS_NOT_SKILL)
        save_pool(state.pool_path, state.pool)
        return True
    pres = prescreen(candidate, state.cfg['prescreen'], None)
    if pres.excluded:
        state.report['prescreen_excluded'] += 1
        state.publish(candidate, pres)
        update_candidate_status(state.pool, seq, POOL_STATUS_EXCLUDED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True
    state.log(f"检查 #{seq}（本轮进度 {state.report['checked']}/{len(state.pending_items)}，全池 {len(state.pool)}）：{candidate.skill_id}")
    if '/blob/' not in candidate.url:
        candidate.url = f'https://github.com/{candidate.owner}/{candidate.repo}/blob/HEAD/{candidate.path}'
    url = candidate.url.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1).replace('/blob/', '/', 1)
    fetched = state.fetch_fn(url, sleep=state.sleep, max_bytes=int(state.cfg['model'].get('limits', {}).get('max_input_bytes') or 262144))
    if not fetched.ok or not fetched.text or fetched.truncated or (not validate_document(candidate.path, fetched.text)[0]):
        state.report['fetch_failed'] += 1
        update_candidate_status(state.pool, seq, STATUS_FETCH_FAILED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True
    text = fetched.text
    candidate.content_fingerprint = content_fingerprint(text)
    pres = prescreen(candidate, state.cfg['prescreen'], text)
    if pres.excluded:
        state.report['prescreen_excluded'] += 1
        state.publish(candidate, pres)
        update_candidate_status(state.pool, seq, POOL_STATUS_EXCLUDED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True
    eid = evaluation_id(candidate, state.cfg['model'], state.cfg['rules'])
    local_record = state.ledger.get(eid)
    record = local_record or _read(state.root / 'data' / 'state' / 'evaluations' / evaluation_filename(eid), {})
    if candidate.skill_id in state.manual_picks:
        outcome = record.get('outcome') or ({'decision': (state.entries.get(candidate.skill_id) or {}).get('status')} if candidate.skill_id in state.entries else None)
        state.publish(candidate, pres, outcome=outcome)
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True
    if record.get('status') == 'completed':
        state.report['cached'] += 1
        cached_outcome = dict(record['outcome']) if isinstance(record['outcome'], dict) else {}
        cached_outcome['cached'] = True
        state.publish(candidate, pres, cached_outcome)
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True
    if (record.get('error') or {}).get('reason_code') == REASON_LENGTH_EXCEEDED:
        # 账本已落盘而候选池写入前中断时，补齐终态，不再付费重试。
        update_candidate_status(state.pool, seq, STATUS_LENGTH_EXCEEDED)
        save_pool(state.pool_path, state.pool)
        state.report['skipped_length_exceeded'] += 1
        state.report['reconciled_blocked'] += 1
        state.save()
        return True
    resumable_failure = bool(local_record and record.get('status') == 'failed' and ((record.get('error') or {}).get('reason_code') == 'NETWORK_ERROR' or record.get('retryable')) and (int(record.get('attempts') or 0) < state.max_attempts))
    if record.get('status') in ('failed', 'in_progress', 'needs_recovery') and (not resumable_failure):
        reason = 'NON_RETRYABLE_FAILURE' if record.get('status') == 'failed' else 'UNKNOWN_IN_PROGRESS'
        block_info = {
            'evaluation_id': eid,
            'reason': reason,
            'reason_code': (record.get('error') or {}).get('reason_code'),
            'error_kind': (record.get('error') or {}).get('error_kind') or record.get('error_kind'),
            'stage': record.get('stage'),
            'http_status': (record.get('error') or {}).get('http_status'),
            'blocked_at': now_local().isoformat(),
            'source': 'local_ledger',
            'model': state.cfg['model'].get('model'),
            'model_config_version': state.cfg['model'].get('model_config_version'),
        }
        _update_blocked(state.pool, seq, block_info)
        state.report['blocked_records'] += 1
        state.report['reconciled_blocked'] += 1
        save_pool(state.pool_path, state.pool)
        state.log(f"[阻止] #{seq} {candidate.skill_id}：存在不可重试评估记录（{reason}），已持久化为 blocked，本次未调用模型。")
        state.save()
        return True
    state.ledger.reserve([{'evaluation_id': eid, 'skill_id': candidate.skill_id, 'content_fingerprint': candidate.content_fingerprint, 'rules_version': state.cfg['rules']['rules_version'], 'model_config_version': state.cfg['model'].get('model_config_version')}])
    record = state.ledger.get(eid)
    record['max_attempts'] = state.max_attempts + int(bool(record.get('pending_evaluation')))
    state.ledger.save_record(eid, record)
    state.report['evaluations'] += 1
    state.log(f"评估 #{state.report['evaluations']}：{candidate.name}（累计 {state.usage.total_tokens:,} Token）")
    state.unknown_reserve = _unknown_usage_reserve(candidate, text, state.cfg)
    result = None
    result = _evaluate_with_retries(state, candidate, text, eid, record)
    if result is None:
        return False
    decision = classify_result(result)
    if result['ok']:
        evaluation = result['evaluation']
        outcome = {**decide(evaluation, state.cfg['rules']), 'evaluation': evaluation, 'materials': primary_material_bundle(candidate, text, now_local().isoformat()).manifest(), 'main_category': evaluation.get('main_category'), 'usage': state.active_call['usage'] if state.active_call else None}
        outcome['candidate'] = asdict(candidate)
        outcome['request_usage'] = (state.ledger.get(eid) or {}).get('requests', [])
        outcome['prescreen'] = asdict(pres)
        outcome['evaluated_at'] = now_local().isoformat()
        state.ledger.complete(eid, outcome)
        if state.active_call:
            state.active_call['decision'] = outcome['decision']
        state.publish(candidate, pres, outcome)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)
    elif decision.is_length_exceeded:
        update_candidate_status(state.pool, seq, STATUS_LENGTH_EXCEEDED)
        save_pool(state.pool_path, state.pool)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        state.report['skipped_length_exceeded'] += 1
        limit = state.cfg['model'].get('limits', {}).get('max_output_tokens', 4000)
        state.log(f"[自动跳过] #{seq} {candidate.name}：输出达到 {limit} Token 上限，已保存 length_exceeded。")
    elif decision.is_format_error:
        block_info = {
            'evaluation_id': eid,
            'reason': decision.block_reason or 'OUTPUT_FORMAT_INVALID',
            'reason_code': decision.reason_code,
            'error_kind': decision.error_kind,
            'stage': decision.stage,
            'http_status': decision.http_status or 200,
            'blocked_at': now_local().isoformat(),
            'source': 'local_ledger',
            'model': state.cfg['model'].get('model'),
            'model_config_version': state.cfg['model'].get('model_config_version'),
        }
        _update_blocked(state.pool, seq, block_info)
        save_pool(state.pool_path, state.pool)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        state.report['skipped_output_format'] += 1
        state.report['blocked_records'] += 1
        state.report['blocked_new'] += 1
        stage_desc = "复核" if decision.stage == "review" else "初评"
        state.log(f"[格式异常] #{seq} {candidate.name}：{stage_desc}输出格式不合法（{decision.error_kind}），已保存 blocked（距上次有效结果累计 {state.format_failures}/{state.max_format_failures}）。")
        if state.format_failures >= state.max_format_failures:
            state.stop_causes.add(STOP_FORMAT_FAILURES)
            state.log("[停止] 输出格式异常达到阈值，请检查模型与评估输出契约；后续候选未调用。")
    elif decision.category == 'access_denied':
        block_info = {
            'evaluation_id': eid,
            'reason': decision.block_reason or 'ACCESS_DENIED',
            'reason_code': decision.reason_code,
            'error_kind': decision.error_kind,
            'stage': decision.stage,
            'http_status': decision.http_status,
            'blocked_at': now_local().isoformat(),
            'source': 'local_ledger',
            'model': state.cfg['model'].get('model'),
            'model_config_version': state.cfg['model'].get('model_config_version'),
        }
        _update_blocked(state.pool, seq, block_info)
        save_pool(state.pool_path, state.pool)
        state.report['blocked_records'] += 1
        state.report['blocked_new'] += 1
        state.stop_causes.add(STOP_ACCESS_DENIED)
        state.log(f"[停止] 访问认证或权限失败 (HTTP {decision.http_status})，已将本条标记 blocked 并终止运行。")
    elif decision.category == 'resume_state_invalid':
        block_info = {
            'evaluation_id': eid,
            'reason': decision.block_reason or 'RESUME_STATE_INVALID',
            'reason_code': decision.reason_code,
            'error_kind': decision.error_kind,
            'stage': decision.stage,
            'blocked_at': now_local().isoformat(),
            'source': 'local_ledger',
            'model': state.cfg['model'].get('model'),
            'model_config_version': state.cfg['model'].get('model_config_version'),
        }
        _update_blocked(state.pool, seq, block_info)
        save_pool(state.pool_path, state.pool)
        state.report['blocked_records'] += 1
        state.report['blocked_new'] += 1
        state.stop_causes.add(STOP_RESUME_STATE_INVALID)
        state.log(f"[停止] 恢复状态冲突：{result.get('error')}，已停止运行。")
    elif decision.category == 'request_config_error':
        block_info = {
            'evaluation_id': eid,
            'reason': decision.block_reason or 'REQUEST_CONFIG_ERROR',
            'reason_code': decision.reason_code,
            'error_kind': decision.error_kind,
            'stage': decision.stage,
            'http_status': decision.http_status,
            'blocked_at': now_local().isoformat(),
            'source': 'local_ledger',
            'model': state.cfg['model'].get('model'),
            'model_config_version': state.cfg['model'].get('model_config_version'),
        }
        _update_blocked(state.pool, seq, block_info)
        save_pool(state.pool_path, state.pool)
        state.report['blocked_records'] += 1
        state.report['blocked_new'] += 1
        state.stop_causes.add(STOP_REQUEST_CONFIG_ERROR)
        state.log(f"[停止] 请求配置错误 (HTTP {decision.http_status})，已停止运行。")
    elif not result.get('pending_evaluation'):
        state.report['failed_evaluations'] += 1
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        if _retryable(result) and state.max_retries:
            state.stop_causes.add(STOP_RETRY_EXHAUSTED)
        elif not _retryable(result):
            block_info = {
                'evaluation_id': eid,
                'reason': 'NON_RETRYABLE_FAILURE',
                'reason_code': result.get('reason_code'),
                'error_kind': result.get('error_kind'),
                'stage': result.get('stage'),
                'http_status': getattr(result.get('call'), 'http_status', None),
                'blocked_at': now_local().isoformat(),
                'source': 'local_ledger',
                'model': state.cfg['model'].get('model'),
                'model_config_version': state.cfg['model'].get('model_config_version'),
            }
            _update_blocked(state.pool, seq, block_info)
            save_pool(state.pool_path, state.pool)
            state.report['blocked_records'] += 1
            state.report['blocked_new'] += 1
            state.log(f"[阻止] #{seq} {candidate.skill_id}：评估失败且不可重试（{result.get('reason_code')}），已持久化为 blocked。")
        if state.consecutive_failures >= state.settings['max_consecutive_failures']:
            state.stop_causes.add(STOP_MODEL_FAILURES)
    if state.active_call and (state.active_call.get('usage') or {}).get('total_tokens') is None:
        state.stop_causes.add(STOP_USAGE_UNKNOWN)
        state.log("[停止] 当前响应用量未知；本条状态已保存，后续付费调用已停止。")
    state.active_eid = state.active_call = None
    state.report['format_failures_without_valid_result'] = state.format_failures
    state.report['blocked_total'] = state.pool.stats().get('blocked', 0) if state.pool else 0
    state.report['stop_causes'] = sorted(list(state.stop_causes))
    primary_stop = resolve_primary_stop_reason(state.stop_causes)
    if primary_stop:
        state.report['stop_reason'] = primary_stop
    state.save()
    state.log(f"新增推荐 {state.report['new_recommended']}/{state.settings['target_recommended']}；输入 {state.usage.prompt_tokens:,}，输出 {state.usage.completion_tokens:,}，合计 {state.usage.total_tokens:,} Token。")
    if state.report['stop_reason']:
        return False
    return True

def _collect(root, local, settings, cfg, discover_fn, fetch_fn, evaluate_fn, log, sleep):
    run_id = now_local().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:6]
    run_dir = local / 'runs' / run_id
    usage = UsageTotals()
    max_retries = settings.get('max_retries', 5)
    max_attempts = max_retries + 1
    max_format_failures = settings.get('max_format_failures_without_valid_result', 10)
    manual_picks = get_manual_picks(cfg.get('overrides') or {})
    manual_exclusions = get_manual_exclusions(cfg.get('overrides') or {})
    baseline = _read(root / 'data' / 'catalog.json', {'entries': []})
    entries = index_by_id(baseline.get('entries') or [])
    for e in entries.values():
        apply_manual_overrides_to_entry(e, manual_picks, manual_exclusions)
    old_recommended = {k for k, v in entries.items() if v.get('status') == STATUS_RECOMMENDED and (not v.get('manual_pick'))}
    owned_cfg = cfg.get('owned') or {}
    owned_ids = {it['skill_id'] for it in owned_cfg.get('items', [])}
    skipped_owned_ids = set()
    report = {
        'run_id': run_id,
        'started_at': now_local().isoformat(),
        'status': 'running',
        'model': cfg['model']['model'],
        'settings': settings,
        'discovered': 0,
        'checked': 0,
        'evaluations': 0,
        'cached': 0,
        'fetch_failed': 0,
        'prescreen_excluded': 0,
        'not_skill_files': 0,
        'blocked_records': 0,
        'blocked_new': 0,
        'blocked_total': 0,
        'reconciled_blocked': 0,
        'skipped_output_format': 0,
        'format_failures_without_valid_result': 0,
        'failed_evaluations': 0,
        'new_recommended': 0,
        'failed_requests': 0,
        'unknown_usage_reserved_tokens': 0,
        'skipped_owned': 0,
        'skipped_length_exceeded': 0,
        'recommendations': [],
        'calls': [],
        'stop_causes': [],
        'stop_reason': None,
        'report_path': str(run_dir / 'report.json'),
    }
    ledger = BudgetLedger.load(local / 'state', cap=1, max_attempts=max_attempts)
    ledger.rollover()
    ledger.mark_in_progress_as_needs_recovery()
    context = CatalogContext(rules_version=cfg['rules']['rules_version'], domain_names=cfg['prescreen'].domain_names, source_types=cfg['source_types'])
    dirty = False
    active_snoozed = get_active_snoozed(cfg.get('snoozed') or {})
    pool_path = local / 'pool.json'
    pool = None
    consecutive_failures = 0
    format_failures = 0
    stop_causes = set()
    active_eid = None
    active_call = None
    unknown_reserve = 0
    state = LocalCollection(
        root=root, local=local, settings=settings, cfg=cfg, discover_fn=discover_fn,
        fetch_fn=fetch_fn, evaluate_fn=evaluate_fn, log=log, sleep=sleep, run_id=run_id,
        run_dir=run_dir, usage=usage, report=report, ledger=ledger, context=context,
        entries=entries, old_recommended=old_recommended, baseline=baseline,
        active_snoozed=active_snoozed, manual_exclusions=manual_exclusions,
        manual_picks=manual_picks, pool_path=pool_path, pool=pool, dirty=dirty,
        consecutive_failures=consecutive_failures, format_failures=format_failures,
        max_format_failures=max_format_failures, stop_causes=stop_causes,
        active_eid=active_eid, active_call=active_call, unknown_reserve=unknown_reserve,
        max_attempts=max_attempts, max_retries=max_retries, pending_items=[],
        owned_ids=owned_ids, skipped_owned_ids=skipped_owned_ids,
    )
    try:
        state.save()
        state.log(f"目标：新增 {state.settings['target_recommended']} 个推荐技能；上限 {state.settings['max_total_tokens']:,} Token。")
        state.pool = prepare_pool(state.root, state.local, state.cfg, state.settings, state.old_recommended, discover_fn=state.discover_fn, sleep=state.sleep, log=state.log, report=state.report)
        state.report['discovered'] = len(state.pool)
        state.report['blocked_total'] = state.pool.stats().get('blocked', 0)
        state.ledger.cap = state.ledger.reserved_count + max(1, len(state.pool))
        state.ledger.save()
        state.save()
        state.pending_items = get_pending_candidates(state.pool)
        for item in state.pending_items:
            if not process_candidate(state, item):
                break
        if state.report['new_recommended'] >= state.settings['target_recommended']:
            state.stop_causes.add(STOP_TARGET_REACHED)
        elif state.report['budget_tokens'] >= state.settings['max_total_tokens']:
            state.stop_causes.add(STOP_TOKEN_LIMIT)
        elif state.settings.get('max_evaluations') and state.report['evaluations'] >= state.settings['max_evaluations']:
            state.stop_causes.add(STOP_EVALUATION_LIMIT)
        else:
            state.stop_causes.add(STOP_CANDIDATES_EXHAUSTED)
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
    except KeyboardInterrupt:
        state.stop_causes.add(STOP_INTERRUPTED)
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
    except Exception as exc:
        state.stop_causes.add('error')
        state.report['stop_causes'] = sorted(list(state.stop_causes))
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes) or 'error'
        state.report['error_type'] = type(exc).__name__
    finally:
        if state.pool is not None:
            save_pool(state.pool_path, state.pool)
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
            changes = build_report(build_catalog(list(state.entries.values()), context=state.context, overrides=state.cfg.get('overrides'), snoozed=state.cfg.get('snoozed'), owned=state.cfg.get('owned')), previous_catalog=state.baseline, run_meta={'usage': state.usage.snapshot(), 'run_id': state.run_id})
            write_report(changes, json_path=state.run_dir / 'changes.json', markdown_path=state.run_dir / 'changes.md')
    return state.report


def main(argv=None, *, root: Path | None = None) -> int:
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    parser = argparse.ArgumentParser(description="本地收集 50 个推荐 Skill，并显示 Token 消耗")
    parser.add_argument("--recover-catalog", action="store_true", help="离线恢复已完成评估与页面，不调用模型")
    parser.add_argument("--check", action="store_true", help="仅本地预检：不联网、不调用模型、不写运行数据")
    parser.add_argument("--target", type=int, help="本次新增推荐目标")
    parser.add_argument("--max-tokens", type=int, help="输入加输出的本次 Token 上限")
    parser.add_argument("--max-evaluations", type=int, help="可选：本次最多评估多少条")
    parser.add_argument("--max-retries", type=int, help="首次失败后的最多重连次数，默认 5")
    parser.add_argument("--max-format-failures", type=int, help="距上次有效评估允许的最大连续格式异常数，默认 10")
    parser.add_argument("--limit-queries", type=int)
    parser.add_argument("--expand-limit", type=int)
    parser.add_argument("--sync-config", action="store_true", help="纯离线重建：无需模型凭据与网络，将 config/*.json 同步到 data 与 public/data")
    parser.add_argument("--enrich-catalog", action="store_true", help="离线结构化增强：从现有数据中提取形态、示例请求与亮点，不修改原中文简述")
    parser.add_argument("--refresh-pool", action="store_true", help="重新搜索并重建候选池，保留超长跳过标记")
    parser.add_argument("--pool-watermark", type=int, help="候选池待处理数量低于此水位线时自动增量补水，默认 20")
    args = parser.parse_args(argv)
    log = lambda message: print(message, flush=True)

    if args.recover_catalog:
        from .maintenance import recover_completed_results
        try:
            log(json.dumps(recover_completed_results(root), ensure_ascii=False))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            log(f"恢复失败：{exc}")
            return 1

    if args.enrich_catalog:
        try:
            from .maintenance import enrich_catalog_offline
            stats = enrich_catalog_offline(root)
            log("离线数据增强完成！")
            log(f"统计：处理 {stats['total']} 条，具有有效中文简述 {stats['with_summary']} 条")
            log(f"增强结果：示例请求 {stats['with_example_requests']} 条 · 核心亮点 {stats['with_key_features']} 条 · 明确形态 {stats['with_skill_type']} 条")
            log("原始 summary_zh 完整保留，未做任何修改。")
            log(f"已更新主索引：{stats['catalog_path']}")
            log(f"已生成页面数据：{stats['page_path']}")
            log("部署到 GitHub Pages 请执行：git add data/ public/data/ && git commit -m 'chore: enrich catalog' && git push")
            return 0
        except Exception as exc:
            log(f"增强失败（{type(exc).__name__}）：{exc}")
            return 1

    if args.sync_config:
        try:
            from .maintenance import sync_config_offline
            manifest = sync_config_offline(root)
            counts = manifest["counts"]
            log("离线配置同步完成！")
            log(f"统计：推荐 {counts.get('recommended', 0)} · 候选 {counts.get('candidate', 0)} · 收藏 {counts.get('manual', 0)} · 排除 {counts.get('excluded', 0)}（当前活跃冷冻 {manifest.get('active_snoozed', 0)} 条）")
            log(f"已更新主索引：{manifest['catalog_path']}")
            log(f"已生成页面数据：{manifest['page_path']}")
            log("部署到 GitHub Pages 请执行：git add config/ data/ public/data/ && git commit -m 'chore: sync config' && git push")
            return 0
        except Exception as exc:
            log(f"同步失败（{type(exc).__name__}）：{exc}")
            return 1

    try:
        run_file = root / "config" / "runners" / "local-run.json"
        if not run_file.exists():
            run_file = root / "config" / "local-run.json"
        settings = _read(run_file)
        settings.setdefault("max_format_failures_without_valid_result", 10)
        for argument, key in (
            ("target", "target_recommended"),
            ("max_tokens", "max_total_tokens"),
            ("max_evaluations", "max_evaluations"),
            ("limit_queries", "limit_queries"),
            ("expand_limit", "expand_limit"),
            ("max_retries", "max_retries"),
            ("max_format_failures", "max_format_failures_without_valid_result"),
            ("pool_watermark", "pool_watermark"),
        ):
            if getattr(args, argument) is not None:
                settings[key] = getattr(args, argument)
        if args.refresh_pool:
            settings["refresh_pool"] = True
        _valid_settings(settings)
        cfg = load_all_config(root / "config")
        problems = precheck(cfg)
        if not resolve_api_key(cfg["model"]):
            problems.append("缺少模型 API Key")
        if problems:
            log("预检失败：" + "；".join(problems))
            return 1
        log(f"预检通过；模型 {cfg['model']['model']}；API Key 已配置（不显示密钥）。")
        log(f"目标 {settings['target_recommended']} 个新增推荐，上限 {settings['max_total_tokens']:,} Token。")
        log(f"网络及临时 HTTP 错误最多重连 {settings.get('max_retries', 5)} 次，尝试次数会保存。")
        if not os.environ.get("GITHUB_TOKEN"):
            log("未设置 GITHUB_TOKEN；GitHub 限流可能导致本轮候选不足，可在 PyCharm 的环境变量中设置。")
        if args.check:
            return 0
        result = run_local(root, settings, cfg=cfg, log=log)
        usage = result["usage"]
        stop_reason = result.get("stop_reason")
        log(STOP_LABELS.get(stop_reason, stop_reason or "运行结束"))
        log(f"本次新增推荐：{result['new_recommended']}/{settings['target_recommended']}；评估 {result['evaluations']} 次。")
        if result.get("pool_stats"):
            pst = result["pool_stats"]
            log(f"候选池状态：总计 {pst['total']} 条，待处理 {pst['pending']} 条，已完成 {pst['done']} 条，排除 {pst['excluded']} 条。")
        if result.get("skipped_length_exceeded"):
            log(f"超长跳过：{result['skipped_length_exceeded']} 条（输出达 Token 上限，已标 length_exceeded）。")
        if result.get("skipped_output_format"):
            log(f"格式异常隔离：{result['skipped_output_format']} 条（输出非合法结构，已标 blocked）。")
        log(f"请求 {usage['requests']} 次（含重试），失败请求 {result['failed_requests']} 次。")
        log(f"已知输入 {usage['prompt_tokens']:,} / 输出 {usage['completion_tokens']:,} / 合计 {usage['total_tokens']:,} Token。")
        log(f"其中推理 {usage['reasoning_tokens']:,}（已含在输出中）；用量未知请求 {usage['unknown_usage_requests']}。")
        if result["unknown_usage_reserved_tokens"]:
            log(f"未知用量预留预算 {result['unknown_usage_reserved_tokens']:,} Token（估算）；预算占用合计 {result['budget_tokens']:,}。")
        if usage["incomplete_breakdown_requests"]:
            log("部分响应未返回完整输入/输出明细，分项数值仅包含已返回的部分。")
        log(f"完整报告与本次推荐链接：{result['report_path']}")
        log(f"可读报告：{Path(result['report_path']).with_suffix('.md')}")
        log("查看目录：运行 scripts/preview.ps1，或 python -m http.server 8000 --directory public")
        return 0 if result["stop_reason"] == "target_reached" else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log(f"本地启动失败（{type(exc).__name__}），请检查配置、依赖与运行锁。")
        return 1


__all__ = [
    "STOP_LABELS",
    "_valid_settings",
    "_retryable",
    "_unknown_usage_reserve",
    "prepare_pool",
    "apply_result",
    "save_and_render",
    "run_local",
    "main",
]
