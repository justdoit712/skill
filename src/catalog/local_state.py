"""本地收集状态与持久化渲染模块。

管理 LocalCollection 运行状态、检查点落盘、报告渲染与条目状态更新。
零业务网络调用，零候选调度，与编排层解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional

from src.infra.files import write_json_atomic, write_text_atomic
from src.shared.materials import primary_material_bundle
from src.shared.metrics import build_run_metrics
from src.shared.owned import is_skill_owned
from src.shared.runtime import now_local
from src.shared.usage import UsageTotals
from src.shared.versions import (
    NORMALIZATION_VERSION,
    STATIC_HEURISTIC_VERSION,
    build_config_fingerprint,
)
from .budget import BudgetLedger
from .entry_state import (
    EntryUpdateEvent,
    STATUS_RECOMMENDED,
    update_entry,
)
from .evaluation import build_prompt, evaluation_id
from .failure_policy import (
    STOP_LABELS,
    STOP_USAGE_UNKNOWN,
    resolve_primary_stop_reason,
)
from .filter_rules import successful_evaluation_skill_ids
from .index import CatalogContext, build_catalog, index_by_id
from .overrides import (
    apply_manual_overrides_to_entry,
    get_manual_exclusions,
    get_manual_picks,
)
from .report import build_report, write_report
from .snooze import apply_snooze_overrides, get_active_snoozed

DEFAULT_BATCH_REPO_LIMIT = 1000


def _read(path: Path, default=None):
    """安全读取 JSON 文件，文件不存在时返回默认值。"""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _valid_settings(settings: dict) -> None:
    """校验本地运行配置项的合法性。"""
    settings.setdefault("batch_repo_limit", DEFAULT_BATCH_REPO_LIMIT)
    batch_limit = settings.get("batch_repo_limit")
    if isinstance(batch_limit, bool) or not isinstance(batch_limit, int) or batch_limit < 1:
        raise ValueError("batch_repo_limit 必须是正整数")
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


def _unknown_usage_reserve(candidate, text, cfg, *, filter_rules=None) -> int:
    """未返回 usage 的请求按输入 UTF-8 字节数＋最大输出＋消息余量预留预算。

    这是偏保守的估算，不声称是接口的精确分词或账单；与已知 Token 分列。
    """
    system, material = build_prompt(
        candidate, text, cfg["rules"], cfg["taxonomy"], filter_rules=filter_rules,
    )
    return (
        len(system.encode("utf-8"))
        + len(material.encode("utf-8"))
        + 1024
        + int(cfg["model"].get("limits", {}).get("max_output_tokens", 4000))
    )


def _record_static_observation(report: dict[str, Any], observation: dict[str, Any] | None) -> None:
    """记录静态规则分级观察模式指标（纯统计，不改变排队与调用）。"""
    if not observation:
        return
    sh = report.setdefault('static_heuristics', {
        'version': STATIC_HEURISTIC_VERSION,
        'observed_count': 0,
        'tier_counts': {},
        'signal_counts': {},
        'suggested_actions': {},
    })
    sh['observed_count'] += 1
    tier = observation.get('tier', 'unknown')
    sh['tier_counts'][tier] = sh['tier_counts'].get(tier, 0) + 1
    action = observation.get('suggested_action', 'unknown')
    sh['suggested_actions'][action] = sh['suggested_actions'].get(action, 0) + 1
    for sig in observation.get('signals', []):
        sh['signal_counts'][sig] = sh['signal_counts'].get(sig, 0) + 1


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
    """统一条目状态转移并原子持久化到目录。"""
    from .store import mutate_catalog
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

    mutate_catalog(
        root,
        lambda current: build_catalog(
            list(entries.values()),
            context=context,
            favorites=cfg.get("favorites"),
            overrides=cfg.get("overrides"),
            snoozed=cfg.get("snoozed"),
            owned=cfg.get("owned"),
        ),
    )
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
    active_snoozed: set[str] | None = None,
    manual_exclusions: set[str] | dict | None = None,
    discovery_state: Any = None,
) -> None:
    """落盘运行报告、可读 Markdown 以及目录差异报告。"""
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
    report["metrics"] = build_run_metrics(report, kind="catalog")
    if pool is not None:
        report["pool_stats"] = pool.stats()
        actionable_count, skip_breakdown = pool.count_actionable(
            active_snoozed or set(),
            manual_exclusions or set(),
            owned_set,
        )
        report["pending_breakdown"] = {
            "total_pending": pool.pending_count,
            "actionable": actionable_count,
            "snoozed": skip_breakdown.get("snoozed", 0),
            "manual_excluded": skip_breakdown.get("manual_excluded", 0),
            "owned": skip_breakdown.get("owned", 0),
        }
    if discovery_state is not None:
        report["discovery_summary"] = {
            "schema_version": discovery_state.schema_version,
            "total_repositories_known": len(discovery_state.repository_index),
            "total_repositories_processed": sum(1 for r in discovery_state.repository_index.values() if r.get("processed")),
            "unassigned_repositories_count": len(discovery_state.unassigned_repositories),
            "query_cursors_total": len(discovery_state.query_cursors),
            "query_cursors_exhausted": sum(1 for c in discovery_state.query_cursors.values() if c.get("exhausted")),
            "expansion_rounds": len(discovery_state.expansion_history),
            "failed_repositories": [k for k, r in discovery_state.repository_index.items()
                                    if not r.get("expanded") and r.get("expand_error")],
        }
        report["batches"] = {
            "active_batch": discovery_state.active_batch,
            "completed_batches_count": len(discovery_state.completed_batches),
            "completed_batches": [
                {
                    "batch_id": b.get("batch_id"),
                    "batch_seq": b.get("batch_seq"),
                    "repositories_count": len(b.get("repositories", [])),
                    "skills_count": len(b.get("skill_ids", [])),
                    "stage": b.get("stage"),
                }
                for b in discovery_state.completed_batches
            ],
        }
        report["expansion_history"] = discovery_state.expansion_history
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
        pbd = report.get("pending_breakdown", {})
        if pbd:
            lines.append(
                f"- 候选池：共 {pst['total']} 条，待处理 {pst['pending']} 条"
                f"（有效可处理 {pbd.get('actionable', 0)} 条，冷冻 {pbd.get('snoozed', 0)} 条，"
                f"排除 {pbd.get('manual_excluded', 0)} 条，已收录 {pbd.get('owned', 0)} 条；"
                f"已完成 {pst['done']}，排除 {pst['excluded']}，抓取失败 {pst['fetch_failed']}，非技能 {pst['not_skill']}，超长跳过 {pst['length_exceeded']}，已阻止 {pst['blocked']}）"
            )
        else:
            lines.append(
                f"- 候选池：共 {pst['total']} 条，待处理 {pst['pending']} 条"
                f"（已完成 {pst['done']}，排除 {pst['excluded']}，抓取失败 {pst['fetch_failed']}，非技能 {pst['not_skill']}，超长跳过 {pst['length_exceeded']}，已阻止 {pst['blocked']}）"
            )
    if discovery_state is not None:
        ds = report.get("discovery_summary", {})
        lines.append(
            f"- 仓库批次：已知仓库 {ds.get('total_repositories_known', 0)} 个，已处理 {ds.get('total_repositories_processed', 0)} 个，待分配队列 {ds.get('unassigned_repositories_count', 0)} 个；已完成批次 {len(discovery_state.completed_batches)} 个"
        )
        if discovery_state.active_batch:
            ab = discovery_state.active_batch
            lines.append(f"- 活动批次：{ab.get('batch_id')}（阶段：{ab.get('stage')}，仓库数：{len(ab.get('repositories', []))}）")
        if discovery_state.expansion_history:
            lines.append(f"- 检索扩词：已执行 {len(discovery_state.expansion_history)} 轮分类内关键词扩展")
    if report.get("skipped_owned"):
        lines.append(f"- 已收录跳过：{report['skipped_owned']}")
    if report.get('models_used'):
        lines.append('- 实际请求模型：' + '、'.join(report['models_used']))
    lines.append(f"- 超长跳过候选：{report.get('skipped_length_exceeded', 0)} 条（超过单条输出 Token 上限）")
    lines.extend([
        f"- 本次新增推荐：{report['new_recommended']} / {settings['target_recommended']}",
        f"- 评估次数：{report['evaluations']}；复用已有评估：{report['cached']}",
    ])
    if report.get("cache_observation"):
        c_obs = report["cache_observation"]
        status_text = "已启用" if c_obs.get("enabled") else "观察模式"
        lines.append(
            f"- 规范化缓存：潜在命中 {c_obs.get('potential_hits', 0)} 条，实际复用 {c_obs.get('actual_reused', 0)} 条（{status_text}）"
        )
    lines.extend([
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
            build_catalog(
                list(entries.values()),
                context=context,
                overrides=(cfg or {}).get("overrides"),
                snoozed=(cfg or {}).get("snoozed"),
                owned=(cfg or {}).get("owned"),
            ),
            previous_catalog=baseline,
            run_meta={"usage": usage.snapshot(), "run_id": run_id},
        )
        write_report(changes, json_path=run_dir / "changes.json", markdown_path=run_dir / "changes.md")


@dataclass
class LocalCollection:
    """单个本地收集运行的实例状态与注入依赖。"""
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
    _skill_eval_records_index: Any = None
    evaluated_skill_ids: set[str] = field(default_factory=set)
    search_fn: Any = None
    expand_fn: Any = None
    discovery_state: Any = None
    batch_materials: dict = field(default_factory=dict)
    model_pool: Any = None

    def save(self) -> None:
        """落盘运行报告与当前状态。"""
        save_and_render(
            self.run_dir, self.local, self.report, self.pool, self.usage,
            self.settings, self.entries, self.old_recommended, context=self.context,
            baseline=self.baseline, dirty=self.dirty, run_id=self.run_id,
            cfg=self.cfg, owned_ids=self.owned_ids, active_snoozed=self.active_snoozed,
            manual_exclusions=self.manual_exclusions, discovery_state=self.discovery_state,
        )

    def publish(self, candidate, pres, outcome=None, upstream_status="ok") -> dict:
        """发布条目状态并标记目录为脏。"""
        entry = apply_result(
            candidate, pres, outcome, upstream_status=upstream_status,
            entries=self.entries, context=self.context, manual_picks=self.manual_picks,
            manual_exclusions=self.manual_exclusions, active_snoozed=self.active_snoozed,
            root=self.root, cfg=self.cfg,
        )
        self.dirty = True
        return entry


def repair_stale_running_reports(local: Path, log: Callable = lambda _: None) -> None:
    """进程启动时核对旧运行状态并修复遗留的 running 报告。"""
    latest_path = local / "latest-run.json"
    if not latest_path.exists():
        return
    try:
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("status") == "running":
            data["status"] = "interrupted"
            if not data.get("stop_reason"):
                data["stop_reason"] = "interrupted"
            data["updated_at"] = now_local().isoformat()
            write_json_atomic(latest_path, data)
            rep_path = Path(data.get("report_path") or "")
            if rep_path.exists():
                try:
                    rep_data = json.loads(rep_path.read_text(encoding="utf-8"))
                    if isinstance(rep_data, dict) and rep_data.get("status") == "running":
                        rep_data["status"] = "interrupted"
                        if not rep_data.get("stop_reason"):
                            rep_data["stop_reason"] = "interrupted"
                        rep_data["updated_at"] = now_local().isoformat()
                        write_json_atomic(rep_path, rep_data)
                except Exception:
                    pass
            log("检测到遗留的运行中报告，已修复为中断状态：" + str(data.get("run_id")))
    except Exception:
        pass


def init_local_state(
    root: Path,
    local: Path,
    settings: dict,
    cfg: dict,
    run_id: str,
    *,
    discover_fn: Callable,
    fetch_fn: Callable,
    evaluate_fn: Callable,
    log: Callable,
    sleep: Callable,
    search_fn: Any = None,
    expand_fn: Any = None,
) -> LocalCollection:
    """初始化 LocalCollection 运行上下文对象及相关账本/报告容器。"""
    repair_stale_running_reports(local, log)
    run_dir = local / 'runs' / run_id
    usage = UsageTotals()
    max_retries = settings.get('max_retries', 5)
    max_attempts = max_retries + 1
    max_format_failures = settings.get('max_format_failures_without_valid_result', 10)
    manual_picks = get_manual_picks(cfg.get('favorites') or cfg.get('overrides') or {})
    manual_exclusions = get_manual_exclusions(cfg.get('overrides') or {})
    baseline = _read(root / 'data' / 'catalog.json', {'entries': []})
    entries = index_by_id(baseline.get('entries') or [])
    for e in entries.values():
        apply_manual_overrides_to_entry(e, manual_picks, manual_exclusions)
    old_recommended = {
        k for k, v in entries.items()
        if v.get('status') == STATUS_RECOMMENDED and (not v.get('manual_pick'))
    }
    owned_cfg = cfg.get('owned') or {}
    owned_ids = {it['skill_id'] for it in owned_cfg.get('items', [])}
    skipped_owned_ids = set()
    report = {
        'run_id': run_id,
        'started_at': now_local().isoformat(),
        'status': 'running',
        'model': cfg['model'].get('model'),
        'models_used': [],
        'config_fingerprint': build_config_fingerprint(cfg['model']),
        'settings': settings,
        'discovered': 0,
        'checked': 0,
        'evaluations': 0,
        'cached': 0,
        'fetch_failed': 0,
        'prescreen_excluded': 0,
        'static_skipped': 0,
        'static_heuristics': {
            'version': STATIC_HEURISTIC_VERSION,
            'observed_count': 0,
            'skipped_count': 0,
            'tier_counts': {},
            'signal_counts': {},
            'suggested_actions': {},
        },
        'cache_observation': {
            'version': NORMALIZATION_VERSION,
            'enabled': bool(settings.get('enable_normalized_cache', False) or (cfg.get('rules', {}).get('cache', {}).get('enable_normalized_reuse', False))),
            'observed_count': 0,
            'potential_hits': 0,
            'actual_reused': 0,
            'rejection_reasons': {},
        },
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
    context = CatalogContext(
        rules_version=cfg['rules']['rules_version'],
        domain_names=cfg['prescreen'].domain_names,
        source_types=cfg['source_types'],
    )
    dirty = False
    active_snoozed = get_active_snoozed(cfg.get('snoozed') or {})
    pool_path = local / 'pool.json'
    pool = None
    consecutive_failures = 0
    format_failures = 0
    stop_causes = set()
    if 'models' in cfg['model']:
        recovered = ledger.recover_settled_pool_pauses()
        if recovered:
            report['recovered_settled_pauses'] = recovered
            log(f'恢复 {len(recovered)} 条请求已结算的候选；保留原请求、用量和尝试历史。')
        blockers = [eid for eid in ledger.reserved if (ledger.get(eid) or {}).get('status') == 'needs_recovery']
        if blockers:
            stop_causes.add(STOP_USAGE_UNKNOWN)
            report['recovery_blockers'] = blockers
            sample = blockers[0].split('|')[0].split(':')[-1]
            log(f"[安全拦截] 检测到 {len(blockers)} 条上次异常中断时处于发送中的请求（如 {sample}）。")
            log("为防重复扣费已暂停；若无需核对，可执行 python tools/run_local.py --reset-recovery 一键重置。")

    state = LocalCollection(
        root=root, local=local, settings=settings, cfg=cfg, discover_fn=discover_fn,
        fetch_fn=fetch_fn, evaluate_fn=evaluate_fn, log=log, sleep=sleep, run_id=run_id,
        run_dir=run_dir, usage=usage, report=report, ledger=ledger, context=context,
        entries=entries, old_recommended=old_recommended, baseline=baseline,
        active_snoozed=active_snoozed, manual_exclusions=manual_exclusions,
        manual_picks=manual_picks, pool_path=pool_path, pool=pool, dirty=dirty,
        consecutive_failures=consecutive_failures, format_failures=format_failures,
        max_format_failures=max_format_failures, stop_causes=stop_causes,
        active_eid=None, active_call=None, unknown_reserve=0,
        max_attempts=max_attempts, max_retries=max_retries, pending_items=[],
        owned_ids=owned_ids, skipped_owned_ids=skipped_owned_ids,
        search_fn=search_fn, expand_fn=expand_fn,
    )
    if cfg.get("filter_rules") and cfg["filter_rules"].has_evaluation_rules:
        state.evaluated_skill_ids = successful_evaluation_skill_ids(
            ledger.evaluations_dir, root / "data" / "state" / "evaluations",
        )
    return state
