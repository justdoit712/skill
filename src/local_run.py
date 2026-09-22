"""本地按推荐数量收集；复用采集、筛选、决策与索引，不改 Actions 周额度。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .budget import BudgetLedger, _write_json_atomic, evaluation_filename, now_local
from .decide import decide
from .dedupe import content_fingerprint, dedupe
from .discover import discover
from .evaluate import RETRYABLE_STATUS, build_prompt, evaluate, evaluation_id, resolve_api_key
from .fetch import fetch_text
from .index import CatalogContext, build_catalog, build_entry, index_by_id, write_catalog
from .overrides import apply_manual_overrides_to_entry, get_manual_exclusions, get_manual_picks
from .pipeline import admission_decision, load_all_config, precheck, review_state
from .snooze import apply_snooze_overrides, get_active_snoozed, load_snooze
from .pool import (
    STATUS_DONE,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_PENDING,
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
from .usage import UsageTotals


STOP_LABELS = {
    "target_reached": "已达到本次新增推荐目标",
    "token_limit": "已达到本次 Token 上限",
    "evaluation_limit": "已达到本次评估次数上限",
    "candidates_exhausted": "本轮发现的候选已处理完，未达到推荐目标",
    "usage_unknown": "接口用量缺失或请求结果不明，停止后续付费调用",
    "model_failures": "模型连续失败，停止后续付费调用",
    "retry_exhausted": "模型重连次数已用尽，停止后续付费调用",
    "interrupted": "用户中断；已收到的用量和结果已保存",
    "error": "运行异常；已收到的用量和结果已保存",
}


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
    return (len(system.encode("utf-8")) + len(material.encode("utf-8")) + 1024
            + int(cfg["model"].get("limits", {}).get("max_output_tokens", 4000)))


def run_local(
    root: Path, settings: dict, *, cfg=None, discover_fn=discover,
    fetch_fn=fetch_text, evaluate_fn=evaluate, log=print, sleep=time.sleep,
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
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("已有本地任务或上次强制终止留下 run.lock；确认旧进程退出后才能移除该锁") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        return _collect(root, local, settings, cfg, discover_fn, fetch_fn, evaluate_fn, log, sleep)
    finally:
        lock.unlink(missing_ok=True)


def _collect(root, local, settings, cfg, discover_fn, fetch_fn, evaluate_fn, log, sleep):
    run_id = now_local().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    run_dir = local / "runs" / run_id
    usage = UsageTotals()
    max_retries = settings.get("max_retries", 5)
    max_attempts = max_retries + 1
    manual_picks = get_manual_picks(cfg.get("overrides") or {})
    manual_exclusions = get_manual_exclusions(cfg.get("overrides") or {})
    baseline = _read(root / "data" / "catalog.json", {"entries": []})
    entries = index_by_id(baseline.get("entries") or [])
    for e in entries.values():
        apply_manual_overrides_to_entry(e, manual_picks, manual_exclusions)
    old_recommended = {k for k, v in entries.items() if v.get("status") == "recommended" and not v.get("manual_pick")}
    report = {
        "run_id": run_id, "started_at": now_local().isoformat(), "status": "running",
        "model": cfg["model"]["model"], "settings": settings,
        "discovered": 0, "checked": 0, "evaluations": 0, "cached": 0,
        "fetch_failed": 0, "prescreen_excluded": 0, "not_skill_files": 0,
        "blocked_records": 0, "failed_evaluations": 0, "new_recommended": 0,
        "failed_requests": 0, "unknown_usage_reserved_tokens": 0,
        "recommendations": [], "calls": [], "stop_reason": None,
        "report_path": str(run_dir / "report.json"),
    }
    ledger = BudgetLedger.load(local / "state", cap=1, max_attempts=max_attempts)
    ledger.mark_in_progress_as_needs_recovery()
    context = CatalogContext(rules_version=cfg["rules"]["rules_version"],
                             domain_names=cfg["prescreen"].domain_names,
                             source_types=cfg["source_types"])
    dirty = False

    def save():
        report["usage"] = usage.snapshot()
        report["budget_tokens"] = usage.total_tokens + report["unknown_usage_reserved_tokens"]
        report["updated_at"] = now_local().isoformat()
        report["recommendations"] = [
            {key: e.get(key) for key in ("skill_id", "name", "url", "summary_zh", "main_category")}
            for sid, e in entries.items()
            if sid not in old_recommended and e.get("status") == "recommended" and not e.get("needs_review") and not e.get("manual_pick")
        ]
        report["new_recommended"] = len(report["recommendations"])
        if pool is not None:
            report["pool_stats"] = pool.stats()
        _write_json_atomic(run_dir / "report.json", report)
        _write_json_atomic(local / "latest-run.json", report)
        lines = ["# 本地运行报告", "", f"- 运行：{run_id}",
                 f"- 状态：{STOP_LABELS.get(report['stop_reason'], '运行中')}"]
        if pool is not None:
            pst = pool.stats()
            lines.append(f"- 候选池：共 {pst['total']} 条，待处理 {pst['pending']} 条（已完成 {pst['done']}，排除 {pst['excluded']}，抓取失败 {pst['fetch_failed']}，非技能 {pst['not_skill']}）")
        lines.extend([
            f"- 本次新增推荐：{report['new_recommended']} / {settings['target_recommended']}",
            f"- 评估次数：{report['evaluations']}；复用已有评估：{report['cached']}",
            f"- 请求次数（含重试）：{usage.requests}；失败请求：{report['failed_requests']}",
            f"- 已知输入 Token：{usage.prompt_tokens:,}",
            f"- 已知输出 Token：{usage.completion_tokens:,}",
            f"- 已知总 Token：{usage.total_tokens:,} / {settings['max_total_tokens']:,}",
            f"- 未知用量预留预算：{report['unknown_usage_reserved_tokens']:,} Token（估算，不是实际用量）",
            f"- 预算占用合计：{report['budget_tokens']:,} Token",
            f"- 推理 Token：{usage.reasoning_tokens:,}（已包含在输出内）",
            f"- 用量未知请求：{usage.unknown_usage_requests}",
            f"- 分项不完整请求：{usage.incomplete_breakdown_requests}",
            "", "未知用量按输入字节数＋最大输出＋消息余量预留预算；这不是精确计费。Token 上限在每次请求结束后检查。金额以服务商账单为准。",
            "", "## 本次新增推荐", "",
        ])
        for item in report["recommendations"]:
            # 上游名称是数据，避免作为 Markdown 链接/标题语法执行。
            name = str(item["name"] or item["skill_id"]).replace("[", "（").replace("]", "）").replace("\n", " ")
            summary = str(item["summary_zh"] or "").replace("\n", " ")
            lines.append(f"- [{name}]({item['url']})：{summary}")
        (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def publish(candidate, pres, outcome=None, upstream_status="ok"):
        nonlocal dirty
        outcome = outcome or {}
        previous = entries.get(candidate.skill_id)
        changed = bool(previous and previous.get("content_fingerprint")
                       and previous["content_fingerprint"] != candidate.content_fingerprint)
        state = review_state(previous, changed, outcome.get("decision"))
        context.generated_at = now_local().isoformat()
        entry = build_entry(
            candidate, prescreen_result=pres,
            decision=admission_decision(outcome if outcome else None, pres, previous, state),
            evaluation=outcome.get("evaluation"), context=context,
            first_seen=(previous or {}).get("first_seen"), upstream_status=upstream_status, **state,
        )
        # 待复核仍展示原摘要和依据，不能用空评估覆盖。
        if previous and not outcome and not pres.excluded:
            for key in ("summary_zh", "main_category", "tags", "platform_declared",
                        "dependencies_declared", "evaluation_rules_version", "limitations", "license"):
                entry[key] = previous.get(key)
        apply_manual_overrides_to_entry(entry, manual_picks, manual_exclusions)
        apply_snooze_overrides([entry], active_snoozed)
        entries[candidate.skill_id] = entry
        dirty = True
        write_catalog(build_catalog(list(entries.values()), context=context, overrides=cfg.get("overrides"), snoozed=cfg.get("snoozed")),
                      data_path=root / "data" / "catalog.json",
                      public_path=root / "public" / "data" / "catalog.json")

    consecutive_failures = 0
    active_eid = None
    active_call = None
    unknown_reserve = 0
    active_snoozed = get_active_snoozed(cfg.get("snoozed") or {})
    pool_path = local / "pool.json"
    force_refresh = settings.get("refresh_pool", False)
    watermark = settings.get("pool_watermark", 20)
    max_age_days = settings.get("pool_max_age_days", 7)
    pool = None

    def count_actionable(p) -> int:
        return sum(
            1 for it in p.items
            if it.status == STATUS_PENDING
            and it.candidate.skill_id not in active_snoozed
            and it.candidate.skill_id not in manual_exclusions
        )

    try:
        save()
        log(f"目标：新增 {settings['target_recommended']} 个推荐技能；上限 {settings['max_total_tokens']:,} Token。")
        if not force_refresh:
            pool = load_pool(pool_path)
            if pool is not None and is_pool_expired(pool, max_age_days=max_age_days):
                log(f"本地候选池已超过 {max_age_days} 天有效期，重新运行发现并重建候选池...")
                pool = None

        if pool is None:
            if force_refresh:
                log("已指定 --refresh-pool，强制清空旧池并重新运行网络搜索...")
            else:
                log("未检测到有效本地候选池，正在首次搜索并展开真实 SKILL.md...")
            log("搜索阶段不调用模型。")
            candidates, outcomes = discover_fn(
                cfg["searches"], sources=cfg["sources"], expand=True,
                max_queries=settings.get("limit_queries"), expand_limit=settings.get("expand_limit"),
                sleep=sleep, progress=log,
            )
            report["discovery_failures"] = sum(not item.ok for item in outcomes)
            pool = create_pool_from_candidates(candidates, old_recommended, cfg["source_types"])
            save_pool(pool_path, pool)
            log(f"候选池已构建并保存至 {pool_path}，共 {len(pool)} 条候选（全部待处理）。")
        else:
            actionable_count = count_actionable(pool)
            pending_count = pool.pending_count
            log(f"加载已有候选池：共 {len(pool)} 条，待处理 {pending_count} 条（可处理 {actionable_count} 条）。")
            if actionable_count < watermark:
                log(f"可处理候选数量 ({actionable_count}) 低于水位线 ({watermark})，正在增量搜索补水...")
                candidates, outcomes = discover_fn(
                    cfg["searches"], sources=cfg["sources"], expand=True,
                    max_queries=settings.get("limit_queries"), expand_limit=settings.get("expand_limit"),
                    sleep=sleep, progress=log,
                )
                report["discovery_failures"] = sum(not item.ok for item in outcomes)
                added = append_new_candidates(pool, candidates, old_recommended, cfg["source_types"])
                save_pool(pool_path, pool)
                actionable_count = count_actionable(pool)
                log(f"增量补水完成，新增 {added} 条候选入池，当前池总量 {len(pool)} 条，可处理候选 {actionable_count} 条。")
            else:
                log(f"可处理候选充足（{actionable_count} >= 水位线 {watermark}），跳过网络搜索，秒级启动。")
                report["discovery_failures"] = 0

        report["discovered"] = len(pool)
        # 本地账本保存调用记录；每次运行的停止额度由 report 与 usage 管理。
        # 不读写 data/state/budget.json，不改变 Actions 的 50/周。
        ledger.cap = ledger.reserved_count + max(1, len(pool))
        ledger.save()
        save()
        pending_items = get_pending_candidates(pool)
        for item in pending_items:
            candidate = item.candidate
            seq = item.seq
            if candidate.skill_id in active_snoozed:
                # 审查问题 2：跳过处于活跃冷冻期的候选，保持 pending 状态，不调模型、不写账本，解冻后恢复
                continue
            if candidate.skill_id in manual_exclusions:
                continue
            if report["new_recommended"] >= settings["target_recommended"]:
                report["stop_reason"] = "target_reached"
                break
            if report["budget_tokens"] >= settings["max_total_tokens"]:
                report["stop_reason"] = "token_limit"
                break
            if settings.get("max_evaluations") and report["evaluations"] >= settings["max_evaluations"]:
                report["stop_reason"] = "evaluation_limit"
                break
            report["checked"] += 1
            # 展开失败的仓库首页不是一个 Skill，不能拿 README 或 HTML 冒充评估材料。
            if not candidate.path.endswith("SKILL.md"):
                report["not_skill_files"] += 1
                update_candidate_status(pool, seq, STATUS_NOT_SKILL)
                save_pool(pool_path, pool)
                continue
            pres = prescreen(candidate, cfg["prescreen"], None)
            if pres.excluded:
                report["prescreen_excluded"] += 1
                publish(candidate, pres)
                update_candidate_status(pool, seq, STATUS_EXCLUDED)
                save_pool(pool_path, pool)
                save()
                continue
            log(f"检查 #{seq}（本轮进度 {report['checked']}/{len(pending_items)}，全池 {len(pool)}）：{candidate.skill_id}")
            if "/blob/" not in candidate.url:
                candidate.url = f"https://github.com/{candidate.owner}/{candidate.repo}/blob/HEAD/{candidate.path}"
            url = candidate.url.replace("https://github.com/", "https://raw.githubusercontent.com/", 1).replace("/blob/", "/", 1)
            fetched = fetch_fn(url, sleep=sleep,
                               max_bytes=int(cfg["model"].get("limits", {}).get("max_input_bytes") or 262144))
            if not fetched.ok or not fetched.text or fetched.truncated:
                report["fetch_failed"] += 1
                update_candidate_status(pool, seq, STATUS_FETCH_FAILED)
                save_pool(pool_path, pool)
                # 获取失败没有证据支持新结论；保留已有目录条目。
                save()
                continue
            text = fetched.text
            candidate.content_fingerprint = content_fingerprint(text)
            pres = prescreen(candidate, cfg["prescreen"], text)
            if pres.excluded:
                report["prescreen_excluded"] += 1
                publish(candidate, pres)
                update_candidate_status(pool, seq, STATUS_EXCLUDED)
                save_pool(pool_path, pool)
                save()
                continue
            eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
            local_record = ledger.get(eid)
            record = local_record or _read(root / "data" / "state" / "evaluations" / evaluation_filename(eid), {})
            if candidate.skill_id in manual_picks:
                # §4.4: 人工收藏条目：更新真实指纹与变更时间，但不调用模型重新评估（0 模型调用）
                outcome = record.get("outcome") or ({"decision": (entries.get(candidate.skill_id) or {}).get("status")} if candidate.skill_id in entries else None)
                publish(candidate, pres, outcome=outcome)
                update_candidate_status(pool, seq, STATUS_DONE)
                save_pool(pool_path, pool)
                save()
                continue
            if record.get("status") == "completed":
                report["cached"] += 1
                publish(candidate, pres, record["outcome"])
                update_candidate_status(pool, seq, STATUS_DONE)
                save_pool(pool_path, pool)
                save()
                continue
            resumable_failure = bool(
                local_record and record.get("status") == "failed"
                and ((record.get("error") or {}).get("reason_code") == "NETWORK_ERROR" or record.get("retryable"))
                and int(record.get("attempts") or 0) < max_attempts
            )
            if record.get("status") in ("failed", "in_progress", "needs_recovery") and not resumable_failure:
                report["blocked_records"] += 1
                continue
            ledger.reserve([{"evaluation_id": eid, "skill_id": candidate.skill_id,
                             "content_fingerprint": candidate.content_fingerprint,
                             "rules_version": cfg["rules"]["rules_version"],
                             "model_config_version": cfg["model"].get("model_config_version")}])
            record = ledger.get(eid)
            record["max_attempts"] = max_attempts
            ledger._save_record(eid, record)
            report["evaluations"] += 1
            log(f"评估 #{report['evaluations']}：{candidate.name}（累计 {usage.total_tokens:,} Token）")
            unknown_reserve = _unknown_usage_reserve(candidate, text, cfg)
            result = None
            for index in range(int(record.get("attempts") or 0), max_attempts):
                if report["budget_tokens"] >= settings["max_total_tokens"]:
                    report["stop_reason"] = "token_limit"
                    break
                if index:
                    delay = min(2 ** (index - 1), 8)
                    log(f"重连 {index}/{max_retries}：{candidate.name}，{delay} 秒后重试。")
                    sleep(delay)
                attempt = ledger.begin_attempt(eid)
                active_eid = eid
                active_call = {"skill_id": candidate.skill_id, "attempt": attempt,
                               "max_attempts": max_attempts, "status": "in_progress", "usage": None}
                report["calls"].append(active_call)
                save()
                result = evaluate_fn(candidate, text, model_cfg=cfg["model"], rules=cfg["rules"],
                                     taxonomy=cfg["taxonomy"], sleep=sleep)
                call = result.get("call")
                unknown_before = usage.unknown_usage_requests
                active_call["usage"] = usage.add(call)
                reserved_tokens = (usage.unknown_usage_requests - unknown_before) * unknown_reserve
                report["unknown_usage_reserved_tokens"] += reserved_tokens
                active_call["unknown_usage_reserved_tokens"] = reserved_tokens
                active_call["diagnostics"] = {
                    "error_type": getattr(call, "error_type", None),
                    "http_status": getattr(call, "http_status", None),
                    "latency_ms": getattr(call, "latency_ms", None),
                }
                active_call["status"] = "completed" if result["ok"] else "failed"
                # 每次尝试立即持久化，包括未知用量的预留预算。
                save()
                if result["ok"]:
                    break
                code = result.get("reason_code") or "MODEL_ERROR"
                diagnostic = active_call["diagnostics"]
                details = [code]
                if diagnostic["error_type"]:
                    details.append(diagnostic["error_type"])
                if diagnostic["http_status"] is not None:
                    details.append(f"HTTP {diagnostic['http_status']}")
                if diagnostic["latency_ms"] is not None:
                    details.append(f"耗时 {diagnostic['latency_ms'] / 1000:.1f} 秒")
                message = "；".join(details)
                ledger.fail(eid, code, message)
                failure_record = ledger.get(eid)
                failure_record["retryable"] = _retryable(result)
                ledger._save_record(eid, failure_record)
                active_eid = None
                active_call["reason_code"] = code
                report["failed_requests"] += 1
                save()
                log(f"请求失败：{candidate.name}；{message}。")
                if not _retryable(result):
                    break
            if result is None:
                break
            if result["ok"]:
                evaluation = result["evaluation"]
                outcome = {**decide(evaluation, cfg["rules"]), "evaluation": evaluation,
                           "main_category": evaluation.get("main_category"), "usage": active_call["usage"]}
                ledger.complete(eid, outcome)
                active_eid = None
                active_call["decision"] = outcome["decision"]
                publish(candidate, pres, outcome)
                consecutive_failures = 0
                update_candidate_status(pool, seq, STATUS_DONE)
                save_pool(pool_path, pool)
            else:
                report["failed_evaluations"] += 1
                consecutive_failures += 1
                if _retryable(result) and max_retries:
                    report["stop_reason"] = report["stop_reason"] or "retry_exhausted"
            # 失败重试缺少用量允许用预留预算继续；最终响应本身缺用量则停止。
            if active_call["usage"]["total_tokens"] is None:
                report["stop_reason"] = report["stop_reason"] or "usage_unknown"
            active_eid = active_call = None
            save()
            log(f"新增推荐 {report['new_recommended']}/{settings['target_recommended']}；"
                f"输入 {usage.prompt_tokens:,}，输出 {usage.completion_tokens:,}，合计 {usage.total_tokens:,} Token。")
            if report["stop_reason"]:
                break
            if consecutive_failures >= settings["max_consecutive_failures"]:
                report["stop_reason"] = "model_failures"
                break
        if report["new_recommended"] >= settings["target_recommended"]:
            report["stop_reason"] = report["stop_reason"] or "target_reached"
        elif report["budget_tokens"] >= settings["max_total_tokens"]:
            report["stop_reason"] = report["stop_reason"] or "token_limit"
        elif settings.get("max_evaluations") and report["evaluations"] >= settings["max_evaluations"]:
            report["stop_reason"] = report["stop_reason"] or "evaluation_limit"
        report["stop_reason"] = report["stop_reason"] or "candidates_exhausted"
    except KeyboardInterrupt:
        report["stop_reason"] = "interrupted"
    except Exception as exc:
        report["stop_reason"] = "error"
        report["error_type"] = type(exc).__name__
    finally:
        if pool is not None:
            save_pool(pool_path, pool)
        if active_eid:
            ledger.mark_needs_recovery(active_eid, "运行中断或异常，禁止自动重复付费请求")
            if active_call["usage"] is None:
                usage.add(None)
                report["unknown_usage_reserved_tokens"] += unknown_reserve
                active_call["status"] = "unknown"
        report["status"] = "completed" if report["stop_reason"] == "target_reached" else "stopped"
        save()
        if dirty:
            changes = build_report(build_catalog(list(entries.values()), context=context),
                                   previous_catalog=baseline, run_meta={"usage": usage.snapshot(), "run_id": run_id})
            write_report(changes, json_path=run_dir / "changes.json", markdown_path=run_dir / "changes.md")
    return report


def main(argv=None, *, root: Path | None = None) -> int:
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    parser = argparse.ArgumentParser(description="本地收集 50 个推荐 Skill，并显示 Token 消耗")
    parser.add_argument("--check", action="store_true", help="仅本地预检：不联网、不调用模型、不写运行数据")
    parser.add_argument("--target", type=int, help="本次新增推荐目标")
    parser.add_argument("--max-tokens", type=int, help="输入加输出的本次 Token 上限")
    parser.add_argument("--max-evaluations", type=int, help="可选：本次最多评估多少条")
    parser.add_argument("--max-retries", type=int, help="首次失败后的最多重连次数，默认 5")
    parser.add_argument("--limit-queries", type=int)
    parser.add_argument("--expand-limit", type=int)
    parser.add_argument("--sync-config", action="store_true", help="纯离线重建：无需模型凭据与网络，将 config/*.json 同步到 data 与 public/data")
    parser.add_argument("--refresh-pool", action="store_true", help="强制丢弃现有候选池并重新运行网络搜索发现")
    parser.add_argument("--pool-watermark", type=int, help="候选池待处理数量低于此水位线时自动增量补水，默认 20")
    args = parser.parse_args(argv)
    log = lambda message: print(message, flush=True)

    if args.sync_config:
        try:
            from .index import sync_config_to_catalog
            manifest = sync_config_to_catalog(root)
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
        settings = _read(root / "config" / "local-run.json")
        for argument, key in (("target", "target_recommended"), ("max_tokens", "max_total_tokens"),
                              ("max_evaluations", "max_evaluations"), ("limit_queries", "limit_queries"),
                              ("expand_limit", "expand_limit"), ("max_retries", "max_retries"),
                              ("pool_watermark", "pool_watermark")):
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
        log(STOP_LABELS[result["stop_reason"]])
        log(f"本次新增推荐：{result['new_recommended']}/{settings['target_recommended']}；评估 {result['evaluations']} 次。")
        if result.get("pool_stats"):
            pst = result["pool_stats"]
            log(f"候选池状态：总计 {pst['total']} 条，待处理 {pst['pending']} 条，已完成 {pst['done']} 条，排除 {pst['excluded']} 条。")
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
