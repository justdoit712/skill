"""定向查找全生命周期调度与状态控制。

职责：
1. 查找运行状态追踪（FinderRunState）；
2. 严密的 3 计数器（尝试次数、已评估数、Token 用量）记账与安全熔断；
3. 统一收尾（finalize_run）与多退出码规范映射（0/1/2/130）；
4. 零目录依赖与零目录副作用红线保障；
5. 已收录项过滤、多轮补水和检查点恢复。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.llm import call_model, resolve_api_key
from src.shared.runtime import is_test_environment, now_local
from src.shared.usage import UsageTotals

from .config import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_ROUNDS,
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
    update_public_snapshot,
    write_local_report,
)
from .search import (
    expand_and_collect_candidates,
    fetch_candidate_materials,
    search_github_repos_for_query,
)
from .refill import run_rounds

EVAL_MAX_OUTPUT_TOKENS = 10000

STATUS_TARGET_REACHED = "target_reached"
STATUS_COMPLETED = "completed"
STATUS_TOKEN_LIMIT = "token_limit"
STATUS_EVALUATION_LIMIT = "evaluation_limit"
STATUS_ROUND_LIMIT = "round_limit"
STATUS_CANDIDATES_EXHAUSTED = "candidates_exhausted"
STATUS_ALL_CANDIDATES_OWNED = "all_candidates_owned"
STATUS_MODEL_FAILURES = "model_failures"
STATUS_USAGE_UNKNOWN = "usage_unknown"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"
STATUS_INVALID_CONFIG = "invalid_config"


MAX_CONSECUTIVE_FAILURES = 20


class RunStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class FinderRunState:
    """Per-run facts; no catalog state or global execution context."""

    def __init__(self, topic, params, run_dir=None):
        self.run_dir = run_dir
        self.usage = UsageTotals()
        params = {"limit": DEFAULT_LIMIT, "max_evaluations": DEFAULT_MAX_EVALUATIONS,
                  "max_tokens": DEFAULT_MAX_TOKENS, "max_rounds": DEFAULT_MAX_ROUNDS, **params}
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

    def stop_reason(self):
        if self.usage.unknown_usage_requests or any(c.get("state") in ("started", "unknown") for c in self.report["calls"]):
            return STATUS_USAGE_UNKNOWN
        if _target_reached(self):
            return STATUS_TARGET_REACHED
        if self.usage.total_tokens >= self.report["parameters"]["max_tokens"]:
            return STATUS_TOKEN_LIMIT
        if self.report["evaluation_attempts"] >= self.report["parameters"]["max_evaluations"]:
            return STATUS_EVALUATION_LIMIT
        if self.report["search"].get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES:
            return STATUS_MODEL_FAILURES
        return None

    def call(self, transport, cfg, system, user, *, api_key, sleep, candidate_id=None, stage=None):
        reason = self.stop_reason()
        if reason:
            raise RunStopped(reason)
        call = {"stage": stage or ("evaluation" if candidate_id else "planning"), "skill_id": candidate_id,
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
            self.save()
            raise
        call["usage"] = self.usage.add(result)
        call["response"] = {key: getattr(result, key, None) for key in
                            ("ok", "content", "error", "reason_code", "finish_reason")}
        if not getattr(result, "ok", False):
            call["state"] = "not_sent" if call["usage"]["attempts"] == 0 else "error"
        else:
            call["state"] = "unknown" if call["usage"]["total_tokens"] is None else "received"
        self.save()
        return result, bool(self.usage.unknown_usage_requests)


def finalize_run(report, stop_reason, *, status=None, evaluated_items=None, plan=None,
                 limit=DEFAULT_LIMIT, run_dir=None, root_dir=None, usage=None,
                 evaluation_attempts=None, evaluated_count=None, log=print):
    if status is None:
        status = (STATUS_INTERRUPTED if stop_reason == STATUS_INTERRUPTED else
                  STATUS_STOPPED if stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES, STATUS_ROUND_LIMIT) else
                  STATUS_COMPLETED if stop_reason in (STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED, STATUS_ALL_CANDIDATES_OWNED, STATUS_COMPLETED) else STATUS_ERROR)
    history = report.get("search", {}).get("rounds_history", [])
    if history:
        history[-1]["evaluated"] = len(report.get("evaluations", [])) - history[-1].get("evaluation_start", 0)
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


def _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep):
    from src.shared.materials import MaterialBundle
    report = state.report
    manifest = materials.manifest() if isinstance(materials, MaterialBundle) else {"identity_version": "primary-only-legacy"}
    report["pending_evaluation"] = {"candidate": asdict(candidate), "materials": dict(materials), "manifest": manifest}
    system, user = build_evaluation_prompt(candidate, materials, report["plan"], report["topic"])
    result, unknown = state.call(transport, cfg, system, user, api_key=api_key, sleep=sleep, candidate_id=candidate.skill_id)
    return _record_evaluation(state, candidate, materials, manifest, result), unknown


def _record_evaluation(state, candidate, materials, manifest, result):
    report = state.report
    successful = False
    try:
        if not result.ok or not result.content:
            raise ValueError(result.error or "model_failed")
        if result.finish_reason == "length":
            raise ValueError("模型输出被截断")
        parsed = parse_skill_evaluation(result.content, report["plan"]["criteria"])
        verified = verify_and_adjust_evaluation(parsed, materials, report["plan"]["criteria"])
        record = {"candidate": {"skill_id": candidate.skill_id, "name": candidate.name,
                  "repo_url": candidate.repo_url, "url": candidate.url, "author": candidate.owner,
                  "path": candidate.path, "content_fingerprint": candidate.content_fingerprint},
                  "evaluation": verified,
                  "materials": manifest}
        report["evaluations"].append(record)
        report["evaluated_count"] = len(report["evaluations"])
        successful = True
    except (ValueError, TypeError, KeyError) as exc:
        report["errors"].append({"stage": "evaluation", "skill_id": candidate.skill_id,
                                 "code": "invalid_result", "message": str(exc)})
    processed = report["search"].setdefault("processed_skill_ids", [])
    if candidate.skill_id not in processed:
        processed.append(candidate.skill_id)
    report["search"]["consecutive_failures"] = 0 if successful else report["search"].get("consecutive_failures", 0) + 1
    report.pop("pending_evaluation", None)
    state.save()
    return successful


def _evaluate_candidates(state, candidates, cfg, api_key, transport, fetch, sleep, log=print):
    already_evaluated_ids = {e["candidate"]["skill_id"] for e in state.report.get("evaluations", [])}
    if already_evaluated_ids:
        log(f"[断点续跑] 检测到已有 {len(already_evaluated_ids)} 个已完成评估的候选，自动跳过并从新候选继续...")
    report, readable = state.report, 0
    failures = report["search"].get("consecutive_failures", 0)
    processed = report["search"].setdefault("processed_skill_ids", [])
    already_evaluated_ids.update(processed)
    reason = state.stop_reason()
    if reason:
        return reason
    for candidate in candidates:
        if candidate.skill_id in already_evaluated_ids:
            readable += 1
            continue
        reason = state.stop_reason()
        if reason:
            return reason
        attempt_num = report["evaluation_attempts"] + 1
        max_num = report["parameters"]["max_evaluations"]
        log(f"[评估 #{attempt_num}/{max_num}] {candidate.name} ({candidate.skill_id})...")
        ok, materials, error = fetch(candidate, sleep=sleep)
        if not ok or not materials:
            report["coverage_incomplete"] = True
            report["search"]["skipped"].append({"skill_id": candidate.skill_id, "code": "material_failed", "message": error})
            processed.append(candidate.skill_id)
            already_evaluated_ids.add(candidate.skill_id)
            state.save()
            continue
        readable += 1
        if getattr(materials, "fetch_errors", None):
            report["coverage_incomplete"] = True
        successful, unknown = _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep)
        failures = 0 if successful else failures + 1
        already_evaluated_ids.add(candidate.skill_id)
        if unknown:
            log("  -> 接口成功响应但缺失用量数据，触发用量未知熔断。")
            return STATUS_USAGE_UNKNOWN
        if successful:
            last_ev = report["evaluations"][-1]["evaluation"]
            m = last_ev.get("match", "none")
            tokens = state.usage.total_tokens
            log(f"  -> 评估完成: match={m} (全库已完成: {len(report['evaluations'])}, 累计消耗: {tokens:,} Token)")
            if _target_reached(state):
                log(f"[目标达成] 短名单已集齐 {report['parameters']['limit']} 个，停止后续评估。")
                return STATUS_TARGET_REACHED
        else:
            log("  -> 候选评估未通过格式校验或调用出错，已记录并继续下一个候选...")
    if not readable:
        return "material_failed"
    if failures >= MAX_CONSECUTIVE_FAILURES:
        return STATUS_MODEL_FAILURES
    return STATUS_TARGET_REACHED if len(rank_find_results(report["evaluations"], report["plan"], report["parameters"]["limit"])[0]) >= report["parameters"]["limit"] else STATUS_CANDIDATES_EXHAUSTED


def _target_reached(state):
    report = state.report
    return len(rank_find_results(report["evaluations"], report.get("plan"),
                                report["parameters"]["limit"])[0]) >= report["parameters"]["limit"]


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


def execute_find_skill(topic="", *, limit=None, max_evaluations=None, max_tokens=None, max_rounds=None,
                       root_dir=".", model_cfg=None, log=print, sleep=time.sleep,
                       call_model_fn=None, fetch_candidate_materials_fn=None,
                       expand_and_collect_candidates_fn=None, search_github_repos_fn=None,
                       owned_ids=None, max_clarification_turns=None,
                       input_fn=input, resume_dir=None):
    from src.infra.llm import validate_model_config
    from src.infra.owned import load_owned_ids
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
    params = {k: _parse_int_val(explicit if explicit is not None else (prev_params.get(k) if resumed else run_cfg.get(k)), default, k)
              for k, explicit, default in (("limit", limit, DEFAULT_LIMIT), ("max_evaluations", max_evaluations, DEFAULT_MAX_EVALUATIONS), ("max_tokens", max_tokens, DEFAULT_MAX_TOKENS), ("max_rounds", max_rounds, DEFAULT_MAX_ROUNDS))}
    if params["limit"] < 1 or params["max_evaluations"] < params["limit"] or params["max_tokens"] < 1000 or params["max_rounds"] < 1:
        raise ValueError("要求 limit >= 1、max_evaluations >= limit、max_tokens >= 1000、max_rounds >= 1")

    if max_clarification_turns is None:
        raw_turns = run_cfg.get("max_clarification_turns")
        max_clarification_turns = _parse_int_val(
            raw_turns,
            DEFAULT_MAX_CLARIFICATION_TURNS,
            "max_clarification_turns",
        )
        # 若未mock输入且不在交互终端，安全不阻塞
        if input_fn is input and (not sys.stdin.isatty() or is_test_environment()):
            max_clarification_turns = 0

    cfg = deepcopy(model_cfg if model_cfg is not None else load_finder_model_config(root / "config"))
    problems = validate_model_config(cfg)
    if problems:
        raise ValueError("；".join(problems))
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("缺少模型 API Key")
    cfg.setdefault("request", {})["max_attempts"] = 1

    state = FinderRunState(topic.strip(), params, directory)
    if resumed:
        state.report = deepcopy(prev_report)
        state.report.setdefault("calls", [])
        state.report.setdefault("errors", [])
        state.report.setdefault("coverage_incomplete", False)
        state.report.setdefault("evaluation_attempts", max(len(state.report.get("evaluations", [])), sum(bool(c.get("skill_id")) for c in state.report["calls"])))
        defaults = FinderRunState(topic, params).report["search"]
        for key, value in defaults.items():
            state.report.setdefault("search", {}).setdefault(key, value)
        for call in state.report["calls"]:
            if call.get("state") == "started":
                call["state"] = "unknown"
                state.report.setdefault("recovery_warning", "存在发送后未确认的请求，停止自动重试。")
        state.report["parameters"].update(params)
        state.report["status"] = "running"
        state.report["stop_reason"] = None
        state.report["updated_at"] = now_local().isoformat()
        u = prev_report.get("usage", {})
        state.usage = UsageTotals(
            prompt_tokens=u.get("prompt_tokens") or 0,
            completion_tokens=u.get("completion_tokens") or 0,
            reasoning_tokens=u.get("reasoning_tokens") or 0,
            total_tokens=u.get("total_tokens") or 0,
            requests=u.get("requests") or 0,
            unknown_usage_requests=u.get("unknown_usage_requests") or 0,
            incomplete_breakdown_requests=u.get("incomplete_breakdown_requests") or 0,
        )
        for call in prev_report.get("calls", []):
            if call.get("state") == "started":
                state.usage.record_unknown_request()
    else:
        state.report.update(run_id=run_id, started_at=started.isoformat(), model=cfg.get("model"),
                            report_paths={"json": str(directory / "report.json"), "md": str(directory / "report.md")})
        state.report["parameters"].update(plan_max_output_tokens=PLAN_MAX_OUTPUT_TOKENS, evaluation_max_output_tokens=EVAL_MAX_OUTPUT_TOKENS,
                                            max_consecutive_failures=MAX_CONSECUTIVE_FAILURES)

    transport = call_model_fn or call_model
    reason = "plan_failed"
    try:
        if resumed:
            pending = state.report.get("pending_evaluation")
            last = state.report["calls"][-1] if state.report["calls"] else {}
            if pending and last.get("response") and last.get("skill_id") == pending["candidate"]["skill_id"]:
                from src.shared.models import Candidate
                if not any(e["candidate"]["skill_id"] == pending["candidate"]["skill_id"] for e in state.report["evaluations"]):
                    _record_evaluation(state, Candidate(**pending["candidate"]), pending["materials"],
                                       pending["manifest"], SimpleNamespace(**last["response"]))
                else:
                    state.report.pop("pending_evaluation", None)
            if not state.report.get("plan") and last.get("stage") == "planning" and last.get("response"):
                result = last["response"]
                if result.get("ok") and result.get("content"):
                    state.report["plan"] = parse_query_plan(result["content"])
        if state.stop_reason():
            raise RunStopped(state.stop_reason())
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
            )
        if plan_error:
            reason = plan_error
        elif plan:
            state.report["plan"] = plan
            reason = state.stop_reason()
            if reason is None:
                cfg.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS
                reason = run_rounds(state, cfg, api_key, transport,
                    search_github_repos_fn or search_github_repos_for_query,
                    expand_and_collect_candidates_fn or expand_and_collect_candidates,
                    fetch_candidate_materials_fn or fetch_candidate_materials,
                    _evaluate_candidates, owned_ids, sleep, log)
    except RunStopped as exc:
        reason = exc.reason
    except KeyboardInterrupt:
        reason = STATUS_INTERRUPTED
    except Exception as exc:
        stage = "planning" if state.report.get("plan") is None else "execution"
        code = "plan_failed" if state.report.get("plan") is None else "execution_error"
        state.report["errors"].append({"stage": stage, "code": code, "message": str(exc)})
        reason = code
    return finalize_run(state.report, reason, limit=params["limit"], run_dir=directory,
                        root_dir=root, usage=state.usage, log=log)


def _resolve_resume_dir(root_path: Path, resume_arg: str | None) -> Path | None:
    if not resume_arg:
        return None
    find_skills_dir = root_path / "data" / "local" / "find-skills"
    if resume_arg == "LATEST":
        if not find_skills_dir.exists():
            raise ValueError("未找到任何历史运行目录，无法续跑")
        runs = [p for p in find_skills_dir.iterdir() if p.is_dir() and (p / "report.json").exists()]
        if not runs:
            raise ValueError("未找到任何包含 report.json 的历史运行目录")
        runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return runs[0]
    p = Path(resume_arg)
    if p.is_dir() and (p / "report.json").exists():
        return p
    p_sub = find_skills_dir / resume_arg
    if p_sub.is_dir() and (p_sub / "report.json").exists():
        return p_sub
    raise ValueError(f"未找到指定的运行目录：{resume_arg}")


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
    parser.add_argument("--max-rounds", type=lambda v: _parse_int_val(v, DEFAULT_MAX_ROUNDS, "max_rounds"),
                        default=None, help="总检索轮数，包含首轮（默认 3）")
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
            root_dir=root_path,
            max_clarification_turns=args.turns,
            resume_dir=resume_path,
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
    ):
        return 2
    if status == STATUS_ERROR or stop_reason in ("search_failed", "plan_failed"):
        return 1
    if status == STATUS_COMPLETED:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
