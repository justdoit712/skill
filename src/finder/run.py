"""定向查找全生命周期调度与状态控制。

职责：
1. 查找运行状态追踪（FinderRunState 代理自 src.finder.session）；
2. 调度单候选评估与并发控制（代理自 src.finder.candidates）；
3. 阶段一人机澄清与搜索意图规划（_run_planning_phase, parse_clarification_choice）；
4. 多轮补水与全生命周期编排（execute_find_skill）；
5. 统一收尾（finalize_run）与多退出码规范映射（main 入口，0/1/2/130）。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.llm import call_model, resolve_api_key
from src.infra.model_pool import ModelPool, PoolStopped
from src.shared.runtime import is_test_environment, now_local
from src.shared.usage import UsageTotals, audit_usage_reconciliation
from src.shared.versions import (
    FINDER_REPORT_SCHEMA_VERSION,
    LLM_OUTPUT_CONTRACT_VERSION,
    build_config_fingerprint,
    get_git_commit_hash,
)

from .candidates import (
    _evaluate_candidate,
    _evaluate_candidates,
    _evaluate_candidates_concurrent,
    _record_evaluation,
    _recover_pending_evaluations,
    classify_evaluation_error,
)
from .config import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_EVALUATIONS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TOKENS,
    _parse_int_val,
    load_finder_model_config,
    load_finder_run_config,
)
from .evaluation import rank_find_results
from .plan import (
    CLARIFICATION_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_CLARIFICATION_TURNS,
    PLAN_MAX_OUTPUT_TOKENS,
    build_clarification_question_prompt,
    build_interactive_plan_prompt,
    build_plan_prompt,
    parse_clarification_question,
    parse_plan_with_observation,
)
from .refill import reset_failed_searches, run_rounds
from .report import (
    update_public_snapshot,
    write_local_report,
)
from .search import (
    expand_and_collect_candidates,
    fetch_candidate_materials,
    search_github_repos_for_query,
)
from .session import (
    MAX_CONSECUTIVE_FAILURES,
    STATUS_ALL_CANDIDATES_OWNED,
    STATUS_CANDIDATES_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_EVALUATION_LIMIT,
    STATUS_INTERRUPTED,
    STATUS_INVALID_CONFIG,
    STATUS_MODEL_FAILURES,
    STATUS_QUOTA_EXHAUSTED,
    STATUS_ROUND_LIMIT,
    STATUS_STOPPED,
    STATUS_TARGET_REACHED,
    STATUS_TOKEN_LIMIT,
    STATUS_USAGE_UNKNOWN,
    FinderRunState,
    RunStopped,
    _resolve_resume_dir,
    _supports_kwarg,
    _target_reached,
    estimate_request_token_bound,
)

EVAL_MAX_OUTPUT_TOKENS = 10000

__all__ = [
    "CLARIFICATION_MAX_OUTPUT_TOKENS",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_CLARIFICATION_TURNS",
    "DEFAULT_MAX_EVALUATIONS",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_TOKENS",
    "EVAL_MAX_OUTPUT_TOKENS",
    "FinderRunState",
    "MAX_CONSECUTIVE_FAILURES",
    "PLAN_MAX_OUTPUT_TOKENS",
    "RunStopped",
    "STATUS_ALL_CANDIDATES_OWNED",
    "STATUS_CANDIDATES_EXHAUSTED",
    "STATUS_COMPLETED",
    "STATUS_ERROR",
    "STATUS_EVALUATION_LIMIT",
    "STATUS_INTERRUPTED",
    "STATUS_INVALID_CONFIG",
    "STATUS_MODEL_FAILURES",
    "STATUS_QUOTA_EXHAUSTED",
    "STATUS_ROUND_LIMIT",
    "STATUS_STOPPED",
    "STATUS_TARGET_REACHED",
    "STATUS_TOKEN_LIMIT",
    "STATUS_USAGE_UNKNOWN",
    "_evaluate_candidate",
    "_evaluate_candidates",
    "_evaluate_candidates_concurrent",
    "_record_evaluation",
    "_recover_pending_evaluations",
    "_resolve_resume_dir",
    "_run_planning_phase",
    "_supports_kwarg",
    "_target_reached",
    "classify_evaluation_error",
    "estimate_request_token_bound",
    "execute_find_skill",
    "finalize_run",
    "main",
    "parse_clarification_choice",
]


def parse_clarification_choice(user_reply: str, options: list[str]) -> dict[str, Any]:
    """解析用户的澄清回答。

    支持格式：
    1. 单选/多选数字（支持逗号、顿号、空格、分号等分隔符，如 "1,3"、"1 2 4"、"1、2、3"、"1，2，3；4"）
    2. 自由文本（如 "需要专门针对金融领域的分析"）
    3. 混合或超出范围输入：校验编号合法性，若无法全部识别为有效数字索引，则保留原始输入为 answer，不盲目推测。
    """
    raw = (user_reply or "").strip()
    opt_list = list(options) if options else []
    if not raw:
        return {
            "answer_raw": raw,
            "options": opt_list,
            "selected_indices": [],
            "selected_texts": [],
            "answer": "",
            "input_type": "empty",
        }

    if opt_list:
        tokens = [t.strip() for t in re.split(r"[,，、\s;；]+", raw) if t.strip()]
        if tokens and all(t.isdigit() for t in tokens):
            valid_indices = []
            selected = []
            for t in tokens:
                idx = int(t)  # 1-indexed
                if 1 <= idx <= len(opt_list) and idx not in valid_indices:
                    valid_indices.append(idx)
                    selected.append(opt_list[idx - 1])
            if selected:
                formatted = "；".join(selected) if len(selected) > 1 else selected[0]
                return {
                    "answer_raw": raw,
                    "options": opt_list,
                    "selected_indices": valid_indices,
                    "selected_texts": selected,
                    "answer": formatted,
                    "input_type": "multiple_choice" if len(selected) > 1 else "single_choice",
                }

    return {
        "answer_raw": raw,
        "options": opt_list,
        "selected_indices": [],
        "selected_texts": [],
        "answer": raw,
        "input_type": "free_text",
    }


def finalize_run(
    report: dict[str, Any],
    stop_reason: str,
    *,
    status: str | None = None,
    evaluated_items: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    limit: int = DEFAULT_LIMIT,
    run_dir: Path | None = None,
    root_dir: Path | str | None = None,
    usage: UsageTotals | None = None,
    evaluation_attempts: int | None = None,
    evaluated_count: int | None = None,
    log: Any = print,
) -> dict[str, Any]:
    """统一收尾，完成短名单与备选名单排序、报告生成、公共快照同步与控制台摘要汇报。"""
    if status is None:
        status = (
            STATUS_INTERRUPTED if stop_reason == STATUS_INTERRUPTED else
            STATUS_STOPPED if stop_reason in (
                STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN,
                STATUS_MODEL_FAILURES, STATUS_ROUND_LIMIT, STATUS_QUOTA_EXHAUSTED,
                'input_limit_mismatch', 'models_incompatible',
            ) else
            STATUS_COMPLETED if stop_reason in (
                STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED,
                STATUS_ALL_CANDIDATES_OWNED, STATUS_COMPLETED,
            ) else STATUS_ERROR
        )
    history = report.get("search", {}).get("rounds_history", [])
    if history:
        history[-1]["evaluated"] = len(report.get("evaluations", [])) - history[-1].get("evaluation_start", 0)
    report.update(
        schema_version=FINDER_REPORT_SCHEMA_VERSION,
        status=status,
        stop_reason=stop_reason,
        updated_at=now_local().isoformat(),
    )
    items = evaluated_items if evaluated_items is not None else report.get("evaluations", [])
    report["evaluations"] = items
    shortlist, alternatives = rank_find_results(items, plan=plan or report.get("plan") or {}, limit=limit)
    report.update(
        shortlist=shortlist,
        alternatives=alternatives,
        shortlist_count=len(shortlist),
        alternatives_count=len(alternatives),
        evaluated_count=len(items),
    )
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

    log("\n" + "=" * 60)
    log("【定向查找已完成】")
    log(f"状态: {status} (原因: {stop_reason})")
    log(f"实际已评估技能: {len(report.get('evaluations', []))} 个")
    if usage:
        log(f"本次累计消耗 Token: {usage.total_tokens:,}")
    shortlist = report.get("shortlist", [])
    log(f"推荐短名单 (Shortlist): {len(shortlist)} 个")
    for idx, it in enumerate(shortlist, 1):
        cand = it.get("candidate", {})
        log(f"  {idx}. [{cand.get('name')}] {cand.get('repo_url')}")
    alternatives = report.get("alternatives", [])
    log(f"相关备选 (Alternatives): {len(alternatives)} 个")
    for idx, it in enumerate(alternatives, 1):
        cand = it.get("candidate", {})
        ev = it.get("evaluation", {})
        log(f"  {idx}. [{cand.get('name')}] (匹配度: {ev.get('match')}) {cand.get('repo_url')}")
    if run_dir:
        log(f"完整报告已生成: {run_dir / 'report.md'}")
    log("=" * 60 + "\n")
    return report


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
    enable_terminology_completion: bool = False,
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
            result, unknown = state.call(transport, clarify_cfg, system, user, api_key=api_key, sleep=sleep, stage="clarification")
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

            parsed_reply = parse_clarification_choice(user_reply, options)
            if parsed_reply["selected_texts"]:
                log(f"已选择：{parsed_reply['answer']}")
            else:
                log(f"已记录补充说明：{user_reply}")

            history.append({
                "turn": str(turn_idx),
                "focus": focus,
                "question": question,
                "answer_raw": parsed_reply["answer_raw"],
                "options": parsed_reply["options"],
                "selected_indices": parsed_reply["selected_indices"],
                "selected_texts": parsed_reply["selected_texts"],
                "answer": parsed_reply["answer"],
                "input_type": parsed_reply["input_type"],
            })

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
        plan, obs = parse_plan_with_observation(
            result.content,
            topic=topic,
            enable_completion=enable_terminology_completion,
        )
        state.report["terminology_observation"] = obs
        if enable_terminology_completion and obs.get("applied"):
            log(f"【术语补全】已合并补充 {len(obs.get('added_queries', []))} 条术语短语至搜索规划: {', '.join(obs.get('added_queries', []))}")
        elif obs and obs.get("gaps"):
            log(f"【术语观察模式】检测到 {len(obs['gaps'])} 处表述缺口，建议短语: {', '.join(obs.get('suggested_queries', []))} (观察模式未追加检索)")
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


def execute_find_skill(
    topic: str = "",
    *,
    limit: int | None = None,
    max_evaluations: int | None = None,
    max_tokens: int | None = None,
    max_rounds: int | None = None,
    concurrency: int | None = None,
    root_dir: Path | str = ".",
    model_cfg: dict | None = None,
    log: Any = print,
    sleep: Any = time.sleep,
    call_model_fn: Any = None,
    fetch_candidate_materials_fn: Any = None,
    expand_and_collect_candidates_fn: Any = None,
    search_github_repos_fn: Any = None,
    owned_ids: set[str] | list[str] | None = None,
    max_clarification_turns: int | None = None,
    input_fn: Any = input,
    resume_dir: Path | str | None = None,
    retry_failed_searches: bool = False,
    enable_terminology_completion: bool | None = None,
    enable_active_reflection: bool | None = None,
) -> dict[str, Any]:
    """定向查找全流程执行主函数。"""
    from src.infra.llm import validate_model_config
    from src.infra.owned import load_owned_ids

    if retry_failed_searches and resume_dir is None:
        raise ValueError("重新尝试失败搜索必须配合 --resume 使用")
    root = Path(root_dir).resolve()
    project_root = Path(__file__).resolve().parents[2].resolve()
    if root == project_root and is_test_environment():
        raise RuntimeError(
            "测试环境中禁止直接写入工程生产 data/local 目录！"
            "请在测试用例中显式提供临时隔离目录（如 tempfile.TemporaryDirectory）。"
        )
    if owned_ids is None:
        owned_ids = load_owned_ids(root / "config")
    run_cfg = load_finder_run_config(root / "config")
    resumed = False
    prev_report = {}
    if resume_dir is not None:
        resume_dir = Path(resume_dir).resolve()
        report_file = resume_dir / "report.json"
        if not report_file.exists():
            raise FileNotFoundError(f"续跑目录中未找到 report.json：{resume_dir}")
        prev_report = json.loads(report_file.read_text(encoding="utf-8"))
        if topic and topic.strip() != prev_report.get("topic"):
            raise ValueError("续跑不能更改原始需求；请为新需求启动新的查找。")
        topic = (topic or prev_report.get("topic") or "").strip()
        directory = resume_dir
        resumed = True
    else:
        started = now_local()
        run_id = started.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
        directory = root / "data" / "local" / "find-skills" / run_id

    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("必须提供有效非空的查找需求 topic")

    prev_params = prev_report.get("parameters", {}) if resumed else {}
    params = {
        k: _parse_int_val(explicit if explicit is not None else (prev_params.get(k) if resumed else run_cfg.get(k)), default, k)
        for k, explicit, default in (
            ("limit", limit, DEFAULT_LIMIT),
            ("max_evaluations", max_evaluations, DEFAULT_MAX_EVALUATIONS),
            ("max_tokens", max_tokens, DEFAULT_MAX_TOKENS),
            ("max_rounds", max_rounds, DEFAULT_MAX_ROUNDS),
        )
    }
    if params["limit"] < 1 or params["max_evaluations"] < params["limit"] or params["max_tokens"] < 1000 or params["max_rounds"] < 1:
        raise ValueError("要求 limit >= 1、max_evaluations >= limit、max_tokens >= 1000、max_rounds >= 1")

    raw_concurrency = concurrency if concurrency is not None else (prev_params.get("concurrency") if resumed else run_cfg.get("concurrency"))
    params["concurrency"] = _parse_int_val(raw_concurrency, 1, "concurrency") if raw_concurrency is not None else 1
    params["concurrency"] = max(1, min(2, params["concurrency"]))

    if enable_terminology_completion is None:
        enable_terminology_completion = bool(
            prev_params.get("enable_terminology_completion", False)
            if resumed
            else run_cfg.get("enable_terminology_completion", False)
        )
    params["enable_terminology_completion"] = bool(enable_terminology_completion)

    if enable_active_reflection is None:
        enable_active_reflection = bool(
            prev_params.get("enable_active_reflection", False)
            if resumed
            else run_cfg.get("enable_active_reflection", False)
        )
    params["enable_active_reflection"] = bool(enable_active_reflection)

    if max_clarification_turns is None:
        raw_turns = run_cfg.get("max_clarification_turns")
        max_clarification_turns = _parse_int_val(
            raw_turns,
            DEFAULT_MAX_CLARIFICATION_TURNS,
            "max_clarification_turns",
        )
        if input_fn is input and (not sys.stdin.isatty() or is_test_environment()):
            max_clarification_turns = 0

    cfg = deepcopy(model_cfg if model_cfg is not None else load_finder_model_config(root / "config"))
    problems = validate_model_config(cfg)
    if problems:
        raise ValueError("；".join(problems))
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("缺少模型 API Key")
    if "models" not in cfg:
        cfg.setdefault("request", {})["max_attempts"] = 1

    state = FinderRunState(topic.strip(), params, directory, cfg=cfg)
    if "models" in cfg:
        state.model_pool = ModelPool(cfg, root, authoritative=model_cfg is None, log=log)
    if resumed:
        state.report = deepcopy(prev_report)
        if state.model_pool:
            state.report.setdefault('resume_history', []).append({
                'previous_stop_reason': state.report.get('stop_reason'),
                'stop_causes': state.report.get('stop_causes', []),
                'at': now_local().isoformat(),
            })
            state.report['stop_causes'] = []
        state.report.setdefault("calls", [])
        state.report.setdefault("errors", [])
        state.report.setdefault("coverage_incomplete", False)
        state.report.setdefault("evaluation_attempts", max(len(state.report.get("evaluations", [])), sum(bool(c.get("skill_id")) for c in state.report["calls"])))
        previous_fp = state.report.get("environment", {}).get("config_fingerprint")
        if previous_fp != build_config_fingerprint(cfg):
            state.report.setdefault("config_changes", []).append({
                "previous": previous_fp,
                "current": build_config_fingerprint(cfg),
                "at": now_local().isoformat(),
            })
        state.report.setdefault("environment", {}).update({
            "schema_version": FINDER_REPORT_SCHEMA_VERSION,
            "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
            "git_commit": get_git_commit_hash(root),
            "config_fingerprint": build_config_fingerprint(cfg),
        })
        defaults = FinderRunState(topic, params, cfg=cfg).report["search"]
        for key, value in defaults.items():
            state.report.setdefault("search", {}).setdefault(key, value)
        has_unconfirmed_inflight = any(
            c.get("state") in ("started", "unknown") and not c.get("response")
            for c in state.report["calls"]
        )
        for call in state.report["calls"]:
            if call.get("state") == "started":
                call["state"] = "unknown"
                if call.get("reservation_state") == "active":
                    call["reservation_state"] = "unknown"
                state.report.setdefault("recovery_warning", "存在发送后未确认的请求，停止自动重试。")
        state.report["parameters"].update(params)
        state.report["status"] = "running"
        state.report["stop_reason"] = None
        state.report["updated_at"] = now_local().isoformat()
        u = prev_report.get("usage", {})
        initial_unknown_usage = u.get("unknown_usage_requests") or 0 if has_unconfirmed_inflight else 0
        state.usage = UsageTotals(
            prompt_tokens=u.get("prompt_tokens") or 0,
            completion_tokens=u.get("completion_tokens") or 0,
            reasoning_tokens=u.get("reasoning_tokens") or 0,
            total_tokens=u.get("total_tokens") or 0,
            requests=u.get("requests") or 0,
            unknown_usage_requests=initial_unknown_usage,
            incomplete_breakdown_requests=u.get("incomplete_breakdown_requests") or 0,
        )
        if has_unconfirmed_inflight:
            for call in prev_report.get("calls", []):
                if call.get("state") == "started":
                    state.usage.record_unknown_request()
        else:
            state.report.setdefault("search", {})["consecutive_failures"] = 0
    else:
        state.report.update(
            run_id=run_id,
            started_at=started.isoformat(),
            model=cfg.get("model"),
            report_paths={"json": str(directory / "report.json"), "md": str(directory / "report.md")},
        )
        state.report["parameters"].update(
            plan_max_output_tokens=PLAN_MAX_OUTPUT_TOKENS,
            evaluation_max_output_tokens=EVAL_MAX_OUTPUT_TOKENS,
            max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
        )

    transport = call_model_fn or call_model
    reason = "plan_failed"
    try:
        if state.model_pool:
            initial_reason = state.stop_reason()
            if initial_reason:
                raise RunStopped(initial_reason)
            try:
                state.model_pool.start()
            except PoolStopped as exc:
                state.report.setdefault('stop_causes', []).append(exc.reason)
                state.report['errors'].append({'stage': 'startup', 'code': exc.reason, 'message': str(exc)})
                raise RunStopped(exc.reason) from exc
        if retry_failed_searches:
            reset_failed_searches(state)
        if resumed:
            _recover_pending_evaluations(state)
            last = state.report["calls"][-1] if state.report["calls"] else {}
            if not state.report.get("plan") and last.get("stage") == "planning" and last.get("response"):
                result = last["response"]
                if result.get("ok") and result.get("content"):
                    state.report["plan"], state.report["terminology_observation"] = parse_plan_with_observation(
                        result["content"],
                        topic=topic,
                        enable_completion=params["enable_terminology_completion"],
                    )
        initial_stop = state.stop_reason()
        if initial_stop:
            if initial_stop == STATUS_TOKEN_LIMIT:
                cur_tokens = state.usage.total_tokens
                max_tokens = state.report["parameters"]["max_tokens"]
                log(f"\n[提示] 历史任务已消耗 Token ({cur_tokens:,}) 达到设定的上限 ({max_tokens:,})，运行停止。")
                log("若需继续评估更多候选，请通过命令行参数增加上限，例如：")
                log(f"  .\\.venv\\Scripts\\python.exe tools/find_skill.py --resume --max-tokens {cur_tokens + 5000000:,}")
            raise RunStopped(initial_stop)
        plan = state.report.get("plan")
        if resumed and plan:
            log(f"已恢复历史运行：{directory.name}")
            log(f"复用已有搜索规划与 {len(state.report.get('evaluations', []))} 条已评估结果，跳过人机澄清与规划阶段...")
            plan_error = None
        else:
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
                enable_terminology_completion=params["enable_terminology_completion"],
            )
        if plan_error:
            reason = plan_error
        elif plan:
            state.report["plan"] = plan
            reason = state.stop_reason()
            if reason is None:
                cfg.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS
                run_rounds_kwargs = {}
                if _supports_kwarg(run_rounds, "enable_active_reflection"):
                    run_rounds_kwargs["enable_active_reflection"] = params["enable_active_reflection"]
                finder_run = sys.modules.get("src.finder.run")
                eval_fn = getattr(finder_run, "_evaluate_candidates", _evaluate_candidates) if finder_run else _evaluate_candidates
                reason = run_rounds(
                    state,
                    cfg,
                    api_key,
                    transport,
                    search_github_repos_fn or search_github_repos_for_query,
                    expand_and_collect_candidates_fn or expand_and_collect_candidates,
                    fetch_candidate_materials_fn or fetch_candidate_materials,
                    eval_fn,
                    owned_ids,
                    sleep,
                    log,
                    **run_rounds_kwargs,
                )
    except RunStopped as exc:
        reason = exc.reason
    except KeyboardInterrupt:
        reason = STATUS_INTERRUPTED
    except Exception as exc:
        stage = "planning" if state.report.get("plan") is None else "execution"
        code = "plan_failed" if state.report.get("plan") is None else "execution_error"
        state.report["errors"].append({"stage": stage, "code": code, "message": str(exc)})
        reason = code
    state.report["usage_audit"] = audit_usage_reconciliation(state.report["calls"], state.usage.snapshot())
    return finalize_run(
        state.report,
        reason,
        limit=params["limit"],
        run_dir=directory,
        root_dir=root,
        usage=state.usage,
        log=log,
    )


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
            rebuild_find_report(
                recovery_args.rebuild_report,
                root_path / "public" / "data" if recovery_args.publish_snapshot else None,
            )
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
    parser.add_argument(
        "--resume",
        nargs="?",
        const="LATEST",
        default=None,
        metavar="RUN_DIR",
        help="接着上一次中断的运行继续执行（可选指定运行目录或编号，默认自动选取最近一次运行）",
    )
    parser.add_argument("topic", nargs="?", help=topic_help)
    parser.add_argument(
        "--retry-failed-searches", action="store_true",
        help="配合 --resume 显式重置失败页的尝试次数，保留成功结果及限流等待时间",
    )
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
        "--max-rounds",
        type=lambda v: _parse_int_val(v, DEFAULT_MAX_ROUNDS, "max_rounds"),
        default=None,
        help="总检索轮数，包含首轮（默认 3）",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        help="阶段一人机澄清轮数（默认 3 轮）",
    )
    parser.add_argument(
        "--enable-terminology-completion",
        action="store_true",
        help="启用技术术语有界补全（在现有查询预算内合并术语别名）",
    )
    parser.add_argument(
        "--enable-active-reflection",
        action="store_true",
        help="启用基于评估滑动窗口的主动反思（连续批次无合格技能时主动拓词检索）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="评估最大并发度（支持 1 或 2，默认 1）",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.publish_snapshot:
        print("参数错误：--publish-snapshot 必须配合 --rebuild-report 使用", file=sys.stderr)
        return 2

    resume_path = None
    if getattr(args, "resume", None):
        try:
            resume_path = _resolve_resume_dir(root_path, args.resume)
        except ValueError as exc:
            print(f"续跑参数错误：{exc}", file=sys.stderr)
            return 2

    topic = (args.topic or ("" if resume_path is not None else cfg_topic)).strip()
    if resume_path is None and not topic:
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
            max_rounds=args.max_rounds,
            concurrency=args.concurrency,
            root_dir=root_path,
            max_clarification_turns=args.turns,
            resume_dir=resume_path,
            retry_failed_searches=args.retry_failed_searches,
            enable_terminology_completion=args.enable_terminology_completion if args.enable_terminology_completion else None,
            enable_active_reflection=args.enable_active_reflection if args.enable_active_reflection else None,
        )
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        if is_test_environment() and "测试环境中禁止直接写入" in str(exc):
            raise
        print(f"运行错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"启动错误：{exc}", file=sys.stderr)
        return 1

    status = report.get("status")
    stop_reason = report.get("stop_reason")

    if status == STATUS_INTERRUPTED or stop_reason == STATUS_INTERRUPTED:
        return 130
    if status == STATUS_STOPPED or stop_reason in (
        STATUS_TOKEN_LIMIT,
        STATUS_ROUND_LIMIT,
        STATUS_EVALUATION_LIMIT,
        STATUS_USAGE_UNKNOWN,
        STATUS_MODEL_FAILURES,
        STATUS_QUOTA_EXHAUSTED,
    ):
        return 2
    if status == STATUS_ERROR or stop_reason in ("search_failed", "plan_failed"):
        return 1
    if status == STATUS_COMPLETED:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
