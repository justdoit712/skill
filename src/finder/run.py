"""定向查找全生命周期调度与状态控制。

职责：
1. 查找运行状态追踪（FinderRunState）；
2. 严密的 3 计数器（尝试次数、已评估数、Token 用量）记账与安全熔断；
3. 统一收尾（finalize_run）与多退出码规范映射（0/1/2/130）；
4. 零目录依赖与零目录副作用红线保障。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.llm import call_model, resolve_api_key
from src.shared.runtime import now_local
from src.shared.usage import UsageTotals

from .config import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_EVALUATIONS,
    DEFAULT_MAX_TOKENS,
    _parse_int_val,
    load_finder_model_config,
    load_finder_run_config,
)
from .evaluation import (
    build_evaluation_prompt,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from .plan import (
    PLAN_MAX_OUTPUT_TOKENS,
    build_plan_prompt,
    parse_query_plan,
)
from .report import (
    render_find_markdown_report,
    update_public_snapshot,
    write_local_report,
)
from .search import (
    MAX_REPOS_TO_EXPAND,
    _round_robin_merge_repos,
    expand_and_collect_candidates,
    fetch_candidate_materials,
    schedule_candidates_fairly,
    search_github_repos_for_query,
)

EVAL_MAX_OUTPUT_TOKENS = 10000

STATUS_TARGET_REACHED = "target_reached"
STATUS_COMPLETED = "completed"
STATUS_TOKEN_LIMIT = "token_limit"
STATUS_EVALUATION_LIMIT = "evaluation_limit"
STATUS_CANDIDATES_EXHAUSTED = "candidates_exhausted"
STATUS_MODEL_FAILURES = "model_failures"
STATUS_USAGE_UNKNOWN = "usage_unknown"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"
STATUS_INVALID_CONFIG = "invalid_config"


class FinderRunState:
    """追踪单次定向查找运行中的全部事实状态。"""

    def __init__(self, topic: str, params: dict[str, Any], run_dir: Path | None = None):
        self.started_at = now_local()
        self.completed_at = self.started_at.isoformat()
        self.run_id = self.started_at.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
        self.topic = topic
        self.params = params
        self.run_dir = run_dir
        self.plan: dict[str, Any] = {}
        self.evaluation_attempts = 0
        self.evaluated_items: list[dict[str, Any]] = []
        self.failed_items: list[dict[str, Any]] = []
        self.usage = UsageTotals()
        self.status = "running"
        self.stop_reason = ""
        self.shortlist: list[dict[str, Any]] = []
        self.alternatives: list[dict[str, Any]] = []


def finalize_run(
    report: dict[str, Any],
    stop_reason: str,
    *,
    status: str | None = None,
    evaluated_items: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    limit: int = DEFAULT_LIMIT,
    run_dir: Path | None = None,
    root_dir: Path | None = None,
    usage: UsageTotals | None = None,
    evaluation_attempts: int | None = None,
    evaluated_count: int | None = None,
    log=print,
) -> dict[str, Any]:
    """中心化收尾函数：
    1. 无论何种停止原因，只要存在已评估候选，重新执行 rank_find_results(..., plan=plan)
    2. 生成规范的 shortlist 与 alternatives
    3. 生成 Markdown 与 JSON 事实报告并持久化
    4. 依据条件发布矩阵投影到 public/data/find-report.json
    5. 规范状态与停止原因
    """
    if status is None:
        if stop_reason in (STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED, STATUS_COMPLETED):
            status = STATUS_COMPLETED
        elif stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES):
            status = STATUS_STOPPED
        elif stop_reason == STATUS_INTERRUPTED:
            status = STATUS_INTERRUPTED
        elif stop_reason in ("plan_failed", "search_failed") or "error" in stop_reason.lower():
            status = STATUS_ERROR
        else:
            status = report.get("status") or STATUS_COMPLETED

    report["status"] = status
    report["stop_reason"] = stop_reason

    items = evaluated_items if evaluated_items is not None else report.get("evaluations", [])
    plan_dict = plan if plan is not None else (report.get("plan") or {"criteria": []})

    shortlist, alternatives = rank_find_results(items, plan=plan_dict, limit=limit)
    report["shortlist"] = shortlist
    report["alternatives"] = alternatives
    report["shortlist_count"] = len(shortlist)
    report["alternatives_count"] = len(alternatives)
    report["evaluated_count"] = evaluated_count if evaluated_count is not None else len(items)
    if evaluation_attempts is not None:
        report["evaluation_attempts"] = evaluation_attempts

    if usage is not None:
        report["usage"] = usage.snapshot()

    report["updated_at"] = now_local().isoformat()

    if run_dir is not None:
        write_local_report(report, run_dir)

    if root_dir is not None:
        public_data_dir = Path(root_dir) / "public" / "data"
        update_public_snapshot(report, public_data_dir)

    return report


def execute_find_skill(
    topic: str,
    *,
    limit: int | None = None,
    max_evaluations: int | None = None,
    max_tokens: int | None = None,
    root_dir: str | Path = ".",
    model_cfg: dict[str, Any] | None = None,
    log=print,
    sleep=time.sleep,
    call_model_fn=None,
    fetch_candidate_materials_fn=None,
    expand_and_collect_candidates_fn=None,
    search_github_repos_fn=None,
) -> dict[str, Any]:
    """执行定向查找全流程并生成报告。"""
    root = Path(root_dir).resolve()
    run_cfg = load_finder_run_config(root / "config")

    _call_model = call_model_fn or call_model
    _fetch_materials = fetch_candidate_materials_fn or fetch_candidate_materials
    _expand_candidates = expand_and_collect_candidates_fn or expand_and_collect_candidates
    _search_repos = search_github_repos_fn or search_github_repos_for_query

    # 1. 前置参数防御性校验（不建目录、不静默吞错）
    clean_topic = (topic or "").strip()
    if not clean_topic:
        raise ValueError("必须提供有效非空的查找需求 topic")

    final_limit = (
        _parse_int_val(limit, DEFAULT_LIMIT, "limit")
        if limit is not None
        else _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT, "limit")
    )
    final_max_evaluations = (
        _parse_int_val(max_evaluations, DEFAULT_MAX_EVALUATIONS, "max_evaluations")
        if max_evaluations is not None
        else _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS, "max_evaluations")
    )
    final_max_tokens = (
        _parse_int_val(max_tokens, DEFAULT_MAX_TOKENS, "max_tokens")
        if max_tokens is not None
        else _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")
    )

    if final_limit < 1:
        raise ValueError("limit 必须是正整数")
    if final_max_evaluations < 1:
        raise ValueError("max_evaluations 必须是正整数")
    if final_max_tokens < 1000:
        raise ValueError("max_tokens 必须 >= 1000")
    if final_limit > final_max_evaluations:
        raise ValueError("limit 不能大于 max_evaluations")

    cfg = model_cfg if model_cfg is not None else load_finder_model_config(root / "config")
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("缺少模型 API Key，请设置环境变量 LLM_API_KEY 或配置 model.local.json")

    cfg = deepcopy(cfg)
    cfg.setdefault("request", {})["max_attempts"] = 1

    # 2. 校验通过后创建运行输出目录
    started_at = now_local()
    run_id = started_at.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    run_dir = root / "data" / "local" / "find-skills" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    usage = UsageTotals()
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "topic": clean_topic,
        "status": "running",
        "parameters": {
            "limit": final_limit,
            "max_evaluations": final_max_evaluations,
            "max_tokens": final_max_tokens,
        },
        "model": cfg.get("model"),
        "plan": None,
        "search": {
            "queries_executed": [],
            "repos_discovered": 0,
            "candidates_found": 0,
            "expansions": [],
        },
        "evaluation_attempts": 0,
        "evaluated_count": 0,
        "shortlist_count": 0,
        "alternatives_count": 0,
        "stop_reason": None,
        "shortlist": [],
        "alternatives": [],
        "evaluations": [],
        "usage": None,
        "report_paths": {
            "json": str(run_dir / "report.json"),
            "md": str(run_dir / "report.md"),
        },
    }

    def save_current_report():
        report["usage"] = usage.snapshot()
        report["updated_at"] = now_local().isoformat()
        write_local_report(report, run_dir)
        public_data_dir = root / "public" / "data"
        if public_data_dir.exists():
            try:
                update_public_snapshot(report, public_data_dir)
            except Exception:
                pass

    save_current_report()

    evaluated_items: list[dict[str, Any]] = []
    evaluation_attempts = 0
    plan = None

    try:
        # 1. 需求规划
        log(f"正在分析需求并规划搜索策略：'{clean_topic}'...")
        plan_sys, plan_user = build_plan_prompt(clean_topic)
        cfg_plan = deepcopy(cfg)
        cfg_plan.setdefault("limits", {})["max_output_tokens"] = PLAN_MAX_OUTPUT_TOKENS
        call_plan = _call_model(cfg_plan, plan_sys, plan_user, api_key=api_key, sleep=sleep)
        usage.add(call_plan)
        save_current_report()

        # 校验规划未知用量 (F1)
        call_usage = getattr(call_plan, "usage", None)
        if not call_usage or not isinstance(call_usage, dict) or call_usage.get("total_tokens") is None:
            log("规划模型调用缺少有效 usage，触发零容忍熔断停机。")
            return finalize_run(
                report,
                stop_reason=STATUS_USAGE_UNKNOWN,
                status=STATUS_STOPPED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        if not call_plan.ok or not call_plan.content:
            log(f"查询规划模型调用失败：{call_plan.error}")
            return finalize_run(
                report,
                stop_reason="plan_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        try:
            plan = parse_query_plan(call_plan.content)
        except Exception as exc:
            log(f"查询规划解析失败：{exc}")
            return finalize_run(
                report,
                stop_reason="plan_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        report["plan"] = plan
        log(f"规划意图：{plan['intent']}")
        log(f"生成搜索短语（共 {len(plan['queries'])} 条）：{', '.join(plan['queries'])}")
        save_current_report()

        # 2. GitHub 搜索（各词独立检索后轮转交织 F4）
        query_repo_lists: list[list[dict[str, str]]] = []
        all_search_failed = True

        for q in plan["queries"]:
            log(f"检索 GitHub: '{q}'...")
            ok, r_list, err = _search_repos(q, sleep=sleep)
            if ok:
                all_search_failed = False
            report["search"]["queries_executed"].append(
                {"query": q, "ok": ok, "repos_returned": len(r_list), "error": err}
            )
            query_repo_lists.append(r_list)

        if all_search_failed and plan["queries"]:
            log("所有搜索短语的 GitHub 检索均失败。")
            return finalize_run(
                report,
                stop_reason="search_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        discovered_repos = _round_robin_merge_repos(query_repo_lists, max_repos=MAX_REPOS_TO_EXPAND)
        report["search"]["repos_discovered"] = len(discovered_repos)
        log(f"发现候选仓库：共 {len(discovered_repos)} 个不同仓库（轮转去重后）。")
        save_current_report()

        if not discovered_repos:
            log("未检索到相关仓库。")
            return finalize_run(
                report,
                stop_reason=STATUS_CANDIDATES_EXHAUSTED,
                status=STATUS_COMPLETED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        # 3. 展开仓库获取 SKILL.md
        search_keywords: set[str] = set()
        for text in [clean_topic] + plan.get("queries", []):
            for token in re.findall(r"[\w\u4e00-\u9fa5]+", text.lower()):
                if len(token) >= 2:
                    search_keywords.add(token)

        log("正在扫描各仓库中的真实 SKILL.md 文件...")
        raw_candidates, expansion_logs = _expand_candidates(
            discovered_repos,
            keywords=search_keywords,
            max_repos=MAX_REPOS_TO_EXPAND,
            sleep=sleep,
            log=log,
        )
        report["search"]["expansions"] = expansion_logs
        report["search"]["candidates_found"] = len(raw_candidates)
        log(f"精确定位技能文件：共 {len(raw_candidates)} 个。")
        save_current_report()

        if not raw_candidates:
            log("各仓库中均未定位到有效的 SKILL.md 技能文件。")
            return finalize_run(
                report,
                stop_reason=STATUS_CANDIDATES_EXHAUSTED,
                status=STATUS_COMPLETED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        # 4. 候选轮转调度与抓取评估
        scheduled_candidates = schedule_candidates_fairly(raw_candidates)
        consecutive_failures = 0

        cfg_eval = deepcopy(cfg)
        cfg_eval.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS

        for idx, cand in enumerate(scheduled_candidates, start=1):
            if evaluation_attempts >= final_max_evaluations:
                return finalize_run(
                    report,
                    stop_reason=STATUS_EVALUATION_LIMIT,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )
            if usage.total_tokens >= final_max_tokens:
                return finalize_run(
                    report,
                    stop_reason=STATUS_TOKEN_LIMIT,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )
            if consecutive_failures >= 20:
                return finalize_run(
                    report,
                    stop_reason=STATUS_MODEL_FAILURES,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )

            log(f"抓取材料 [{idx}/{len(scheduled_candidates)}]：{cand.skill_id}...")
            ok, materials, fetch_err = _fetch_materials(cand, sleep=sleep)
            if not ok or not materials:
                log(f"材料获取跳过（{fetch_err}）：{cand.skill_id}")
                continue

            # 先占名额后请求 (F1)
            evaluation_attempts += 1
            report["evaluation_attempts"] = evaluation_attempts
            save_current_report()

            cand_info = {
                "name": cand.name,
                "repo_url": cand.repo_url,
                "path": cand.path,
                "description": cand.description,
            }
            eval_sys, eval_user = build_evaluation_prompt(cand_info, materials, plan, clean_topic)

            call_res = _call_model(cfg_eval, eval_sys, eval_user, api_key=api_key, sleep=sleep)
            usage.add(call_res)

            # 校验单次评估未知用量 (F1)
            call_res_usage = getattr(call_res, "usage", None)
            if not call_res_usage or not isinstance(call_res_usage, dict) or call_res_usage.get("total_tokens") is None:
                log(f"条目 {cand.skill_id} 评估返回未知用量，触发零容忍熔断停机。")
                return finalize_run(
                    report,
                    stop_reason=STATUS_USAGE_UNKNOWN,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )

            if not call_res.ok or not call_res.content:
                consecutive_failures += 1
                log(f"评估失败（{call_res.error}）：{cand.skill_id}")
                save_current_report()
                continue

            try:
                raw_eval = parse_skill_evaluation(call_res.content, plan["criteria"])
                verified_eval = verify_and_adjust_evaluation(raw_eval, materials, plan["criteria"])
                consecutive_failures = 0

                item_record = {
                    "candidate": {
                        "skill_id": cand.skill_id,
                        "name": cand.name,
                        "repo_url": cand.repo_url,
                        "url": cand.url,
                        "author": cand.owner,
                        "path": cand.path,
                        "content_fingerprint": cand.content_fingerprint,
                    },
                    "evaluation": verified_eval,
                }
                evaluated_items.append(item_record)
                report["evaluations"].append(item_record)
                report["evaluated_count"] = len(evaluated_items)
                log(
                    f"完成评估 [{len(evaluated_items)}/{final_max_evaluations}]：{cand.name} "
                    f"-> 匹配度: {verified_eval['match']} | 说明质量: {verified_eval['documentation']} "
                    f"（累计消耗: {usage.total_tokens:,} Token）"
                )
                save_current_report()
            except Exception as exc:
                consecutive_failures += 1
                log(f"评估解析失败（{exc}）：{cand.skill_id}")
                save_current_report()
                continue

        # 5. 循环自然结束
        shortlist, alternatives = rank_find_results(evaluated_items, plan=plan, limit=final_limit)
        stop_reason = STATUS_TARGET_REACHED if len(shortlist) >= final_limit else STATUS_CANDIDATES_EXHAUSTED

        res_report = finalize_run(
            report,
            stop_reason=stop_reason,
            status=STATUS_COMPLETED,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )
        log(f"\n查找完成！优先推荐短名单：{len(res_report['shortlist'])} 项，相关备选：{len(res_report['alternatives'])} 项。")
        log(f"完整报告已生成：{res_report['report_paths']['md']}")
        return res_report

    except KeyboardInterrupt:
        log("\n用户主动中断查找；已完成的结果与 Token 用量已成功保存。")
        return finalize_run(
            report,
            stop_reason=STATUS_INTERRUPTED,
            status=STATUS_INTERRUPTED,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )
    except Exception as exc:
        log(f"\n查找异常中止：{exc}")
        return finalize_run(
            report,
            stop_reason=f"未处理异常：{type(exc).__name__}: {exc}",
            status=STATUS_ERROR,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    """CLI 入口点，解析参数并返回规范退出码。"""
    root_path = Path(root or Path(__file__).resolve().parents[2]).resolve()
    try:
        run_cfg = load_finder_run_config(root_path / "config")
        cfg_limit = _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT, "limit")
        cfg_max_eval = _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS, "max_evaluations")
        cfg_max_tokens = _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")
        cfg_topic = (run_cfg.get("topic") or "").strip()
    except ValueError as exc:
        print(f"配置参数错误：{exc}", file=sys.stderr)
        return 2

    topic_help = (
        f"想要查找的技能需求（默认取自 config/find-skill.json: '{cfg_topic}'）"
        if cfg_topic
        else "想要查找的技能需求（如：生成高质量 Prompt）"
    )

    parser = argparse.ArgumentParser(description="定向查找特定需求的 AI Agent Skill 并生成短名单对比报告")
    parser.add_argument("topic", nargs="?", help=topic_help)
    parser.add_argument(
        "--limit",
        type=lambda v: _parse_int_val(v, DEFAULT_LIMIT, "limit"),
        default=None,
        help=f"优先查看的短名单数量（默认 {cfg_limit}）",
    )
    parser.add_argument(
        "--max-evaluations",
        type=lambda v: _parse_int_val(v, DEFAULT_MAX_EVALUATIONS, "max_evaluations"),
        default=None,
        help=f"本次最多评估的技能数量（默认 {cfg_max_eval}）",
    )
    parser.add_argument(
        "--max-tokens",
        type=lambda v: _parse_int_val(v, DEFAULT_MAX_TOKENS, "max_tokens"),
        default=None,
        help=f"本次模型调用的 Token 消耗停止阈值（默认 {cfg_max_tokens:,}）",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    topic = (args.topic or cfg_topic).strip()
    if not topic:
        if sys.stdin.isatty():
            try:
                print("你想找什么 Skill？")
                topic = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n操作取消。")
                return 130
        if not topic:
            parser.error("必须提供需求 topic 参数（或在 config/find-skill.json 中配置 topic，或在交互终端中输入）")

    try:
        report = execute_find_skill(
            topic,
            limit=args.limit,
            max_evaluations=args.max_evaluations,
            max_tokens=args.max_tokens,
            root_dir=root_path,
        )
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"启动错误：{exc}", file=sys.stderr)
        return 1

    status = report.get("status")
    stop_reason = report.get("stop_reason")

    if status == STATUS_INTERRUPTED or stop_reason == STATUS_INTERRUPTED:
        return 130
    if status == STATUS_STOPPED or stop_reason in (
        STATUS_TOKEN_LIMIT,
        STATUS_EVALUATION_LIMIT,
        STATUS_USAGE_UNKNOWN,
        STATUS_MODEL_FAILURES,
    ):
        return 2
    if status == STATUS_ERROR or stop_reason in ("search_failed", "plan_failed"):
        return 1
    if status == STATUS_COMPLETED:
        return 0
    return 0
