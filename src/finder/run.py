"""定向查找全生命周期调度与状态控制。

职责：
1. 查找运行状态追踪（FinderRunState）；
2. 严密的 3 计数器（尝试次数、已评估数、Token 用量）记账与安全熔断；
3. 统一收尾（finalize_run）与多退出码规范映射（0/1/2/130）；
4. 零目录依赖与零目录副作用红线保障；
5. 过滤已收录项，全收录正常完成（all_candidates_owned）。
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
from src.shared.owned import is_skill_owned
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
    CLARIFICATION_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_CLARIFICATION_TURNS,
    PLAN_MAX_OUTPUT_TOKENS,
    build_clarification_question_prompt,
    build_interactive_plan_prompt,
    build_plan_prompt,
    parse_clarification_question,
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
STATUS_ALL_CANDIDATES_OWNED = "all_candidates_owned"
STATUS_MODEL_FAILURES = "model_failures"
STATUS_USAGE_UNKNOWN = "usage_unknown"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"
STATUS_INVALID_CONFIG = "invalid_config"


MAX_CONSECUTIVE_FAILURES = 20


class FinderRunState:
    """Per-run facts; no catalog state or global execution context."""

    def __init__(self, topic, params, run_dir=None):
        self.run_dir = run_dir
        self.usage = UsageTotals()
        self.report = {"schema_version": "1.0.0", "topic": topic, "parameters": params,
            "status": "running", "stop_reason": None, "plan": None,
            "evaluation_attempts": 0, "evaluated_count": 0, "evaluations": [],
            "shortlist": [], "alternatives": [], "calls": [], "errors": [],
            "coverage_incomplete": False,
            "search": {"queries_executed": [], "repos_discovered": 0,
                       "candidates_found": 0, "expansions": [], "skipped": [],
                       "skipped_owned": 0, "skipped_owned_ids": []}}

    def save(self):
        self.report["usage"] = self.usage.snapshot()
        self.report["updated_at"] = now_local().isoformat()
        # During execution only the authoritative JSON is updated.
        write_json_atomic(self.run_dir / "report.json", self.report)

    def call(self, transport, cfg, system, user, *, api_key, sleep, candidate_id=None):
        call = {"stage": "evaluation" if candidate_id else "planning", "skill_id": candidate_id,
                "state": "started", "usage": None}
        self.report["calls"].append(call)
        if candidate_id:
            self.report["evaluation_attempts"] += 1
        try:
            self.save()  # a failed write prevents the paid request
        except BaseException:
            call["state"] = "not_sent"
            if candidate_id:
                self.report["evaluation_attempts"] -= 1
            raise
        try:
            result = transport(cfg, system, user, api_key=api_key, sleep=sleep)
        except BaseException:
            call["state"] = "unknown"
            self.usage.record_unknown_request()
            raise
        call["usage"] = self.usage.add(result)
        call["state"] = "not_sent" if call["usage"]["attempts"] == 0 else "unknown" if call["usage"]["total_tokens"] is None else "received"
        self.save()
        return result, call["state"] == "unknown"


def finalize_run(report, stop_reason, *, status=None, evaluated_items=None, plan=None,
                 limit=DEFAULT_LIMIT, run_dir=None, root_dir=None, usage=None,
                 evaluation_attempts=None, evaluated_count=None, log=print):
    if status is None:
        status = (STATUS_INTERRUPTED if stop_reason == STATUS_INTERRUPTED else
                  STATUS_STOPPED if stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES) else
                  STATUS_COMPLETED if stop_reason in (STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED, STATUS_ALL_CANDIDATES_OWNED, STATUS_COMPLETED) else STATUS_ERROR)
    report.update(schema_version="1.0.0", status=status, stop_reason=stop_reason,
                  updated_at=now_local().isoformat())
    items = evaluated_items if evaluated_items is not None else report.get("evaluations", [])
    report["evaluations"] = items
    shortlist, alternatives = rank_find_results(items, plan=plan or report.get("plan") or {}, limit=limit)
    report.update(shortlist=shortlist, alternatives=alternatives, shortlist_count=len(shortlist),
                  alternatives_count=len(alternatives), evaluated_count=len(items))
    if evaluation_attempts is not None:
        report["evaluation_attempts"] = evaluation_attempts
    if usage is not None:
        report["usage"] = usage.snapshot()
    if stop_reason == STATUS_ALL_CANDIDATES_OWNED:
        log("本次发现的候选已全部收录。")
    if run_dir is not None:
        try:
            write_local_report(report, run_dir)
        except OSError as exc:
            report.update(status=STATUS_ERROR, stop_reason="artifact_failed")
            report.setdefault("errors", []).append({"stage": "local_report", "code": "write_failed", "message": str(exc)})
            write_json_atomic(run_dir / "report.json", report)
            log("报告派生文件写入失败；事实 JSON 已保留，可离线重建。")
    if root_dir is not None:
        try:
            update_public_snapshot(report, Path(root_dir) / "public" / "data")
        except (OSError, RuntimeError) as exc:
            report.update(status=STATUS_ERROR, stop_reason="artifact_failed")
            report.setdefault("errors", []).append({"stage": "public_report", "code": "write_failed", "message": str(exc)})
            if run_dir is not None:
                write_local_report(report, run_dir)
            log("公共报告未更新；本地事实已保留，可离线重建。")
    return report


def _find_candidates(state, search, expand, sleep, log, owned_ids=None):
    report, plan = state.report, state.report["plan"]
    groups = []
    for query in plan["queries"]:
        ok, repos, error = search(query, sleep=sleep)
        report["search"]["queries_executed"].append({"query": query, "ok": ok, "repos_returned": len(repos), "error": error})
        report["coverage_incomplete"] |= not ok or len(repos) >= 20
        groups.append(repos if ok else [])
    state.save()
    if not any(q["ok"] for q in report["search"]["queries_executed"]):
        return [], "search_failed"
    repos = _round_robin_merge_repos(groups, max_repos=MAX_REPOS_TO_EXPAND)
    report["search"]["repos_discovered"] = len(repos)
    report["search"]["omitted_repositories"] = max(0, len({(r["owner"], r["repo"]) for g in groups for r in g}) - len(repos))
    if not repos:
        return [], STATUS_CANDIDATES_EXHAUSTED
    keywords = set(re.findall(r"[\w]+", report["topic"].lower()))
    candidates, expansions = expand(repos, keywords=keywords, sleep=sleep, log=log)
    report["search"].update(expansions=expansions, candidates_found=len(candidates))
    report["coverage_incomplete"] |= any(not e.get("ok", False) or e.get("truncated") or e.get("omitted_files", 0) for e in expansions)
    if expansions and all(not e.get("ok", False) for e in expansions):
        return [], "expansion_failed"

    # 过滤已收录项（必须在调度截断前完成，避免已收录项占满候选上限）
    remaining_candidates = []
    skipped_owned_cands = []
    for c in candidates:
        if is_skill_owned(c.skill_id, owned_ids):
            skipped_owned_cands.append(c)
        else:
            remaining_candidates.append(c)

    report["search"]["skipped_owned"] = len(skipped_owned_cands)
    report["search"]["skipped_owned_ids"] = [c.skill_id for c in skipped_owned_cands]
    for c in skipped_owned_cands:
        report["search"]["skipped"].append({
            "skill_id": c.skill_id,
            "code": "owned",
            "message": "已收录跳过",
        })

    if candidates and not remaining_candidates:
        state.save()
        return [], STATUS_ALL_CANDIDATES_OWNED

    scheduled = schedule_candidates_fairly(remaining_candidates)
    report["search"]["omitted_candidates"] = len(remaining_candidates) - len(scheduled)
    report["coverage_incomplete"] |= bool(report["search"]["omitted_candidates"] or report["search"]["omitted_repositories"])
    state.save()
    return scheduled, None if scheduled else STATUS_CANDIDATES_EXHAUSTED


def _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep):
    from src.shared.materials import MaterialBundle
    report = state.report
    system, user = build_evaluation_prompt(candidate, materials, report["plan"], report["topic"])
    result, unknown = state.call(transport, cfg, system, user, api_key=api_key, sleep=sleep, candidate_id=candidate.skill_id)
    successful = False
    try:
        if not result.ok or not result.content:
            raise ValueError(result.error or "model_failed")
        parsed = parse_skill_evaluation(result.content, report["plan"]["criteria"])
        verified = verify_and_adjust_evaluation(parsed, materials, report["plan"]["criteria"])
        record = {"candidate": {"skill_id": candidate.skill_id, "name": candidate.name,
                  "repo_url": candidate.repo_url, "url": candidate.url, "author": candidate.owner,
                  "path": candidate.path, "content_fingerprint": candidate.content_fingerprint},
                  "evaluation": verified,
                  "materials": materials.manifest() if isinstance(materials, MaterialBundle) else {"identity_version": "primary-only-legacy"}}
        report["evaluations"].append(record)
        report["evaluated_count"] = len(report["evaluations"])
        successful = True
    except (ValueError, TypeError, KeyError) as exc:
        report["errors"].append({"stage": "evaluation", "skill_id": candidate.skill_id,
                                 "code": "invalid_result", "message": str(exc)})
    state.save()
    return successful, unknown


def _evaluate_candidates(state, candidates, cfg, api_key, transport, fetch, sleep):
    report, failures, readable = state.report, 0, 0
    for candidate in candidates:
        if report["evaluation_attempts"] >= report["parameters"]["max_evaluations"]:
            return STATUS_EVALUATION_LIMIT
        if state.usage.total_tokens >= report["parameters"]["max_tokens"]:
            return STATUS_TOKEN_LIMIT
        if failures >= MAX_CONSECUTIVE_FAILURES:
            return STATUS_MODEL_FAILURES
        ok, materials, error = fetch(candidate, sleep=sleep)
        if not ok or not materials:
            report["coverage_incomplete"] = True
            report["search"]["skipped"].append({"skill_id": candidate.skill_id, "code": "material_failed", "message": error})
            state.save()
            continue
        readable += 1
        if getattr(materials, "fetch_errors", None):
            report["coverage_incomplete"] = True
        successful, unknown = _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep)
        failures = 0 if successful else failures + 1
        if unknown:
            return STATUS_USAGE_UNKNOWN
    if not readable:
        return "material_failed"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return STATUS_MODEL_FAILURES
    return STATUS_TARGET_REACHED if len(rank_find_results(report["evaluations"], report["plan"], report["parameters"]["limit"])[0]) >= report["parameters"]["limit"] else STATUS_CANDIDATES_EXHAUSTED


def _run_planning_phase(
    state: FinderRunState,
    topic: str,
    cfg: dict,
    api_key: str,
    transport: Any,
    sleep: Any,
    *,
    max_turns: int = DEFAULT_MAX_CLARIFICATION_TURNS,
    input_fn: Any = input,
    log: Any = print,
) -> tuple[dict[str, Any] | None, str | None]:
    """执行阶段一需求理解与规划：
    执行最多 max_turns 轮人机交互澄清轮询；
    用户可随时回车（空输入）提前结束沟通进入搜索。
    """
    history: list[dict[str, str]] = []

    if max_turns > 0:
        log("\n" + "=" * 60)
        log("【阶段一：需求理解与意图澄清轮询】")
        log(f"用户初始需求：{topic}")
        log(f"将进行最多 {max_turns} 轮关键意图澄清（直接回车跳过，按当前理解开始搜索）。")
        log("=" * 60)

        clarify_cfg = deepcopy(cfg)
        clarify_cfg.setdefault("limits", {})["max_output_tokens"] = CLARIFICATION_MAX_OUTPUT_TOKENS

        for turn_idx in range(1, max_turns + 1):
            if state.usage.total_tokens >= state.report["parameters"]["max_tokens"]:
                return None, STATUS_TOKEN_LIMIT

            system, user = build_clarification_question_prompt(
                topic, history, turn=turn_idx, max_turns=max_turns
            )
            result, unknown = state.call(transport, clarify_cfg, system, user, api_key=api_key, sleep=sleep)
            if unknown:
                return None, STATUS_USAGE_UNKNOWN
            if not result.ok or not result.content:
                log(f"[提示] 第 {turn_idx} 轮澄清生成未果，直接收敛为最终规划。")
                break

            try:
                clarification = parse_clarification_question(result.content)
            except Exception:
                log(f"[提示] 第 {turn_idx} 轮澄清格式解析异常，直接收敛为最终规划。")
                break

            focus = clarification.get("focus", "需求澄清")
            question = clarification.get("question", "")
            options = clarification.get("options", [])

            log(f"\n[轮询澄清 {turn_idx}/{max_turns}] 聚焦：{focus}")
            log(f"提问：{question}")
            if options:
                log("建议选项：")
                for o_idx, opt in enumerate(options, 1):
                    log(f"  {o_idx}) {opt}")
            log("（直接回车跳过后续沟通，按当前理解直接开始搜索）")

            try:
                user_reply = input_fn("您的答复 / 补充 > ").strip()
            except (KeyboardInterrupt, EOFError):
                log("\n用户结束沟通，基于已收集信息生成规划。")
                break

            if not user_reply or user_reply.lower() in ("skip", "q", "exit", "直接搜索", "开始搜索"):
                log("[提示] 用户确认直接进入搜索阶段。")
                break

            if user_reply.isdigit() and options:
                choice_idx = int(user_reply) - 1
                if 0 <= choice_idx < len(options):
                    user_reply = options[choice_idx]
                    log(f"已选择：{user_reply}")

            history.append({
                "turn": str(turn_idx),
                "focus": focus,
                "question": question,
                "answer": user_reply,
            })

    # 最终收敛为 QueryPlan
    if state.usage.total_tokens >= state.report["parameters"]["max_tokens"]:
        return None, STATUS_TOKEN_LIMIT

    plan_cfg = deepcopy(cfg)
    plan_cfg.setdefault("limits", {})["max_output_tokens"] = PLAN_MAX_OUTPUT_TOKENS

    if history:
        system, user = build_interactive_plan_prompt(topic, history)
    else:
        system, user = build_plan_prompt(topic)

    result, unknown = state.call(transport, plan_cfg, system, user, api_key=api_key, sleep=sleep)
    if unknown:
        return None, STATUS_USAGE_UNKNOWN
    if not result.ok or not result.content:
        state.report["errors"].append({"stage": "planning", "code": "plan_failed", "message": result.error})
        return None, "plan_failed"

    try:
        plan = parse_query_plan(result.content)
        if history:
            plan["clarification_history"] = history
            plan["clarification_turns"] = len(history)

            log("\n" + "=" * 60)
            log("【已精准收敛的搜索规划】")
            log(f"核心意图: {plan.get('intent')}")
            log(f"搜索短语: {', '.join(plan.get('queries', []))}")
            reqs = [c['description'] for c in plan.get('criteria', []) if c.get('kind') == 'required']
            sigs = [c['description'] for c in plan.get('criteria', []) if c.get('kind') == 'quality_signal']
            if reqs:
                log(f"必须满足 (required): {'; '.join(reqs)}")
            if sigs:
                log(f"加分特征 (quality_signal): {'; '.join(sigs)}")
            log("=" * 60 + "\n")
        return plan, None
    except Exception as exc:
        state.report["errors"].append({"stage": "planning", "code": "plan_failed", "message": str(exc)})
        return None, "plan_failed"


def execute_find_skill(topic, *, limit=None, max_evaluations=None, max_tokens=None,
                       root_dir=".", model_cfg=None, log=print, sleep=time.sleep,
                       call_model_fn=None, fetch_candidate_materials_fn=None,
                       expand_and_collect_candidates_fn=None, search_github_repos_fn=None,
                       owned_ids=None, max_clarification_turns=None,
                       input_fn=input):
    from src.infra.llm import validate_model_config
    from src.infra.owned import load_owned_ids
    root = Path(root_dir).resolve()
    if owned_ids is None:
        owned_ids = load_owned_ids(root / "config")
    run_cfg = load_finder_run_config(root / "config")
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("必须提供有效非空的查找需求 topic")
    params = {k: _parse_int_val(explicit if explicit is not None else run_cfg.get(k), default, k)
              for k, explicit, default in (("limit", limit, DEFAULT_LIMIT), ("max_evaluations", max_evaluations, DEFAULT_MAX_EVALUATIONS), ("max_tokens", max_tokens, DEFAULT_MAX_TOKENS))}
    if params["limit"] < 1 or params["max_evaluations"] < params["limit"] or params["max_tokens"] < 1000:
        raise ValueError("要求 limit >= 1、max_evaluations >= limit、max_tokens >= 1000")

    if max_clarification_turns is None:
        raw_turns = run_cfg.get("max_clarification_turns")
        max_clarification_turns = _parse_int_val(
            raw_turns,
            DEFAULT_MAX_CLARIFICATION_TURNS,
            "max_clarification_turns",
        )
        # 若未mock输入且不在交互终端，安全不阻塞
        if input_fn is input and not sys.stdin.isatty():
            max_clarification_turns = 0

    cfg = deepcopy(model_cfg if model_cfg is not None else load_finder_model_config(root / "config"))
    problems = validate_model_config(cfg)
    if problems:
        raise ValueError("；".join(problems))
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("缺少模型 API Key")
    cfg.setdefault("request", {})["max_attempts"] = 1
    started = now_local()
    run_id = started.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    directory = root / "data" / "local" / "find-skills" / run_id
    state = FinderRunState(topic.strip(), params, directory)
    state.report.update(run_id=run_id, started_at=started.isoformat(), model=cfg.get("model"),
                        report_paths={"json": str(directory / "report.json"), "md": str(directory / "report.md")})
    state.report["parameters"].update(plan_max_output_tokens=PLAN_MAX_OUTPUT_TOKENS, evaluation_max_output_tokens=EVAL_MAX_OUTPUT_TOKENS,
                                        max_consecutive_failures=MAX_CONSECUTIVE_FAILURES)
    transport = call_model_fn or call_model
    reason = "plan_failed"
    try:
        plan, plan_error = _run_planning_phase(
            state,
            topic.strip(),
            cfg,
            api_key,
            transport,
            sleep,
            max_turns=max_clarification_turns,
            input_fn=input_fn,
            log=log,
        )
        if plan_error:
            reason = plan_error
        elif plan:
            state.report["plan"] = plan
            candidates, reason = _find_candidates(state, search_github_repos_fn or search_github_repos_for_query,
                expand_and_collect_candidates_fn or expand_and_collect_candidates, sleep, log, owned_ids=owned_ids)
            if reason is None:
                cfg.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS
                reason = _evaluate_candidates(state, candidates, cfg, api_key, transport,
                    fetch_candidate_materials_fn or fetch_candidate_materials, sleep)
    except KeyboardInterrupt:
        reason = STATUS_INTERRUPTED
    except Exception as exc:
        stage = "planning" if state.report.get("plan") is None else "execution"
        code = "plan_failed" if state.report.get("plan") is None else "execution_error"
        state.report["errors"].append({"stage": stage, "code": code, "message": str(exc)})
        reason = code
    return finalize_run(state.report, reason, limit=params["limit"], run_dir=directory,
                        root_dir=root, usage=state.usage, log=log)


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    """CLI 入口点，解析参数并返回规范退出码。"""
    root_path = Path(root or Path(__file__).resolve().parents[2]).resolve()
    raw_args = list(argv if argv is not None else sys.argv[1:])
    if "--rebuild-report" in raw_args:
        recovery_parser = argparse.ArgumentParser(description="离线重建查找报告")
        recovery_parser.add_argument("--rebuild-report", type=Path, required=True)
        recovery_parser.add_argument("--publish-snapshot", action="store_true")
        try:
            recovery_args = recovery_parser.parse_args(raw_args)
            from .report import rebuild_find_report
            rebuild_find_report(recovery_args.rebuild_report,
                root_path / "public" / "data" if recovery_args.publish_snapshot else None)
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"恢复失败：{exc}", file=sys.stderr)
            return 1

    try:
        help_requested = any(arg in ("-h", "--help") for arg in (argv if argv is not None else sys.argv[1:]))
        run_cfg = {} if help_requested else load_finder_run_config(root_path / "config")
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
    parser.add_argument("--rebuild-report", metavar="RUN_DIR", help="离线重建已有运行报告（独立模式）")
    parser.add_argument("--publish-snapshot", action="store_true", help="配合 --rebuild-report 更新本地公共快照")
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
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        help="阶段一人机澄清轮数（默认 3 轮）",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.publish_snapshot:
        print("参数错误：--publish-snapshot 必须配合 --rebuild-report 使用", file=sys.stderr)
        return 2

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
            max_clarification_turns=args.turns,
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


if __name__ == "__main__":
    sys.exit(main())
