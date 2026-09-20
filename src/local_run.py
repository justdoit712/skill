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
from .evaluate import evaluate, evaluation_id, resolve_api_key
from .fetch import fetch_text
from .index import CatalogContext, build_catalog, build_entry, index_by_id, write_catalog
from .pipeline import admission_decision, load_all_config, precheck, review_state
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
    "interrupted": "用户中断；已收到的用量和结果已保存",
    "error": "运行异常；已收到的用量和结果已保存",
}


def _read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _valid_settings(settings: dict) -> None:
    for name in ("target_recommended", "max_total_tokens", "max_consecutive_failures"):
        value = settings.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    for name in ("max_evaluations", "limit_queries", "expand_limit"):
        value = settings.get(name)
        minimum = 0 if name == "limit_queries" else 1
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < minimum):
            raise ValueError(f"{name} 必须 >= {minimum}，或设为 null")


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
    # 不自动重试付费请求，避免超时后的未知费用或重复调用。
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
    baseline = _read(root / "data" / "catalog.json", {"entries": []})
    entries = index_by_id(baseline.get("entries") or [])
    old_recommended = {k for k, v in entries.items() if v.get("status") == "recommended"}
    report = {
        "run_id": run_id, "started_at": now_local().isoformat(), "status": "running",
        "model": cfg["model"]["model"], "settings": settings,
        "discovered": 0, "checked": 0, "evaluations": 0, "cached": 0,
        "fetch_failed": 0, "prescreen_excluded": 0, "not_skill_files": 0,
        "blocked_records": 0, "failed_evaluations": 0, "new_recommended": 0,
        "recommendations": [], "calls": [], "stop_reason": None,
        "report_path": str(run_dir / "report.json"),
    }
    ledger = BudgetLedger.load(local / "state", cap=1, max_attempts=1)
    ledger.mark_in_progress_as_needs_recovery()
    context = CatalogContext(rules_version=cfg["rules"]["rules_version"],
                             domain_names=cfg["prescreen"].domain_names,
                             source_types=cfg["source_types"])
    dirty = False

    def save():
        report["usage"] = usage.snapshot()
        report["updated_at"] = now_local().isoformat()
        report["recommendations"] = [
            {key: e.get(key) for key in ("skill_id", "name", "url", "summary_zh", "main_category")}
            for sid, e in entries.items()
            if sid not in old_recommended and e.get("status") == "recommended" and not e.get("needs_review")
        ]
        report["new_recommended"] = len(report["recommendations"])
        _write_json_atomic(run_dir / "report.json", report)
        _write_json_atomic(local / "latest-run.json", report)
        lines = ["# 本地运行报告", "", f"- 运行：{run_id}",
                 f"- 状态：{STOP_LABELS.get(report['stop_reason'], '运行中')}",
                 f"- 本次新增推荐：{report['new_recommended']} / {settings['target_recommended']}",
                 f"- 评估次数：{report['evaluations']}；复用已有评估：{report['cached']}",
                 f"- 已知输入 Token：{usage.prompt_tokens:,}",
                 f"- 已知输出 Token：{usage.completion_tokens:,}",
                 f"- 已知总 Token：{usage.total_tokens:,} / {settings['max_total_tokens']:,}",
                 f"- 推理 Token：{usage.reasoning_tokens:,}（已包含在输出内）",
                 f"- 用量未知请求：{usage.unknown_usage_requests}",
                 f"- 分项不完整请求：{usage.incomplete_breakdown_requests}",
                 "", "接口未返回的用量无法准确统计；Token 上限在每次请求结束后检查。金额以服务商账单为准。",
                 "", "## 本次新增推荐", ""]
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
        entries[candidate.skill_id] = entry
        dirty = True
        write_catalog(build_catalog(list(entries.values()), context=context),
                      data_path=root / "data" / "catalog.json",
                      public_path=root / "public" / "data" / "catalog.json")

    consecutive_failures = 0
    active_eid = None
    active_call = None
    try:
        save()
        log(f"目标：新增 {settings['target_recommended']} 个推荐技能；上限 {settings['max_total_tokens']:,} Token。")
        log("正在搜索并展开真实 SKILL.md；搜索阶段不调用模型。")
        candidates, outcomes = discover_fn(
            cfg["searches"], sources=cfg["sources"], expand=True,
            max_queries=settings.get("limit_queries"), expand_limit=settings.get("expand_limit"),
            sleep=sleep, progress=log,
        )
        pool = dedupe(candidates)
        # 优先寻找新增推荐，其次处理已有推荐；同组内官方来源优先。
        pool.sort(key=lambda c: (c.skill_id in old_recommended,
                                not any(cfg["source_types"].get(s) == "official" for s in c.source_ids)))
        report["discovered"] = len(pool)
        report["discovery_failures"] = sum(not item.ok for item in outcomes)
        # 本地账本保存调用记录；每次运行的停止额度由 report 与 usage 管理。
        # 不读写 data/state/budget.json，不改变 Actions 的 50/周。
        ledger.cap = ledger.reserved_count + max(1, len(pool))
        ledger.save()
        save()
        for candidate in pool:
            if report["new_recommended"] >= settings["target_recommended"]:
                report["stop_reason"] = "target_reached"
                break
            if usage.unknown_usage_requests:
                report["stop_reason"] = "usage_unknown"
                break
            if usage.total_tokens >= settings["max_total_tokens"]:
                report["stop_reason"] = "token_limit"
                break
            if settings.get("max_evaluations") and report["evaluations"] >= settings["max_evaluations"]:
                report["stop_reason"] = "evaluation_limit"
                break
            report["checked"] += 1
            # 展开失败的仓库首页不是一个 Skill，不能拿 README 或 HTML 冒充评估材料。
            if not candidate.path.endswith("SKILL.md"):
                report["not_skill_files"] += 1
                continue
            pres = prescreen(candidate, cfg["prescreen"], None)
            if pres.excluded:
                report["prescreen_excluded"] += 1
                publish(candidate, pres)
                save()
                continue
            log(f"检查 {report['checked']}/{len(pool)}：{candidate.skill_id}")
            if "/blob/" not in candidate.url:
                candidate.url = f"https://github.com/{candidate.owner}/{candidate.repo}/blob/HEAD/{candidate.path}"
            url = candidate.url.replace("https://github.com/", "https://raw.githubusercontent.com/", 1).replace("/blob/", "/", 1)
            fetched = fetch_fn(url, sleep=sleep,
                               max_bytes=int(cfg["model"].get("limits", {}).get("max_input_bytes") or 262144))
            if not fetched.ok or not fetched.text or fetched.truncated:
                report["fetch_failed"] += 1
                # 获取失败没有证据支持新结论；保留已有目录条目。
                save()
                continue
            text = fetched.text
            candidate.content_fingerprint = content_fingerprint(text)
            pres = prescreen(candidate, cfg["prescreen"], text)
            if pres.excluded:
                report["prescreen_excluded"] += 1
                publish(candidate, pres)
                save()
                continue
            eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
            record = ledger.get(eid) or _read(root / "data" / "state" / "evaluations" / evaluation_filename(eid), {})
            if record.get("status") == "completed":
                report["cached"] += 1
                publish(candidate, pres, record["outcome"])
                save()
                continue
            if record.get("status") in ("failed", "in_progress", "needs_recovery"):
                report["blocked_records"] += 1
                continue
            ledger.reserve([{"evaluation_id": eid, "skill_id": candidate.skill_id,
                             "content_fingerprint": candidate.content_fingerprint,
                             "rules_version": cfg["rules"]["rules_version"],
                             "model_config_version": cfg["model"].get("model_config_version")}])
            ledger.begin_attempt(eid)
            active_eid = eid
            active_call = {"skill_id": candidate.skill_id, "status": "in_progress", "usage": None}
            report["calls"].append(active_call)
            report["evaluations"] += 1
            save()
            log(f"评估 #{report['evaluations']}：{candidate.name}（累计 {usage.total_tokens:,} Token）")
            result = evaluate_fn(candidate, text, model_cfg=cfg["model"], rules=cfg["rules"],
                                 taxonomy=cfg["taxonomy"], sleep=sleep)
            active_call["usage"] = usage.add(result.get("call"))
            active_call["status"] = "completed" if result["ok"] else "failed"
            # 先保存 usage，即使后续分类或索引写入失败，已知消耗仍可查。
            save()
            if result["ok"]:
                evaluation = result["evaluation"]
                outcome = {**decide(evaluation, cfg["rules"]), "evaluation": evaluation,
                           "main_category": evaluation.get("main_category"), "usage": active_call["usage"]}
                ledger.complete(eid, outcome)
                active_eid = None
                active_call["decision"] = outcome["decision"]
                publish(candidate, pres, outcome)
                consecutive_failures = 0
            else:
                # 不把可能含敏感回显的原始 HTTP 错误写进报告。
                code = result.get("reason_code") or "MODEL_ERROR"
                ledger.fail(eid, code, "本地评估失败，详情按原因码排查")
                active_eid = None
                active_call["reason_code"] = code
                report["failed_evaluations"] += 1
                consecutive_failures += 1
            active_eid = active_call = None
            save()
            log(f"新增推荐 {report['new_recommended']}/{settings['target_recommended']}；"
                f"输入 {usage.prompt_tokens:,}，输出 {usage.completion_tokens:,}，合计 {usage.total_tokens:,} Token。")
            if usage.unknown_usage_requests:
                report["stop_reason"] = "usage_unknown"
                break
            if consecutive_failures >= settings["max_consecutive_failures"]:
                report["stop_reason"] = "model_failures"
                break
        if report["new_recommended"] >= settings["target_recommended"]:
            report["stop_reason"] = report["stop_reason"] or "target_reached"
        elif usage.total_tokens >= settings["max_total_tokens"]:
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
        if active_eid:
            ledger.mark_needs_recovery(active_eid, "运行中断或异常，禁止自动重复付费请求")
            if active_call["usage"] is None:
                usage.add(None)
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
    parser.add_argument("--limit-queries", type=int)
    parser.add_argument("--expand-limit", type=int)
    args = parser.parse_args(argv)
    log = lambda message: print(message, flush=True)
    try:
        settings = _read(root / "config" / "local-run.json")
        for argument, key in (("target", "target_recommended"), ("max_tokens", "max_total_tokens"),
                              ("max_evaluations", "max_evaluations"), ("limit_queries", "limit_queries"),
                              ("expand_limit", "expand_limit")):
            if getattr(args, argument) is not None:
                settings[key] = getattr(args, argument)
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
        if not os.environ.get("GITHUB_TOKEN"):
            log("未设置 GITHUB_TOKEN；GitHub 限流可能导致本轮候选不足，可在 PyCharm 的环境变量中设置。")
        if args.check:
            return 0
        result = run_local(root, settings, cfg=cfg, log=log)
        usage = result["usage"]
        log(STOP_LABELS[result["stop_reason"]])
        log(f"本次新增推荐：{result['new_recommended']}/{settings['target_recommended']}；评估 {result['evaluations']} 次。")
        log(f"已知输入 {usage['prompt_tokens']:,} / 输出 {usage['completion_tokens']:,} / 合计 {usage['total_tokens']:,} Token。")
        log(f"其中推理 {usage['reasoning_tokens']:,}（已含在输出中）；用量未知请求 {usage['unknown_usage_requests']}。")
        if usage["incomplete_breakdown_requests"]:
            log("部分响应未返回完整输入/输出明细，分项数值仅包含已返回的部分。")
        log(f"完整报告与本次推荐链接：{result['report_path']}")
        log(f"可读报告：{Path(result['report_path']).with_suffix('.md')}")
        log("查看目录：运行 scripts/preview.ps1，或 python -m http.server 8000 --directory public")
        return 0 if result["stop_reason"] == "target_reached" else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log(f"本地启动失败（{type(exc).__name__}），请检查配置、依赖与运行锁。")
        return 1
