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
import inspect
import re
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.llm import call_model, resolve_api_key
from src.shared.output_contracts import resolve_response_format
from src.shared.runtime import is_test_environment, now_local
from src.shared.usage import UsageTotals, audit_usage_reconciliation
from src.shared.versions import (
    FINDER_REPORT_SCHEMA_VERSION,
    LLM_OUTPUT_CONTRACT_VERSION,
    build_config_fingerprint,
    get_git_commit_hash,
)
from src.shared.metrics import build_run_metrics

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
    parse_plan_with_observation,
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
from .refill import run_rounds, reset_failed_searches

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
STATUS_QUOTA_EXHAUSTED = "quota_exhausted"


MAX_CONSECUTIVE_FAILURES = 20


def _supports_kwarg(fn, kwarg_name: str) -> bool:
    """检查可调用对象是否支持指定的关键字参数。"""
    try:
        sig = inspect.signature(fn)
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg_name:
                return True
        return False
    except (ValueError, TypeError):
        return True


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
        # 分隔符：中文逗号，英文逗号，中文顿号，空格，中英文分号
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


def classify_evaluation_error(result, exc: BaseException | None = None) -> tuple[str, str]:
    """细化评估错误类型。

    分类包括：
    - 'quota_exhausted': 免费额度耗尽或账户欠费 (HTTP 403 / FreeTierOnly / Quota)
    - 'auth_error': API Key 无效或未授权 (HTTP 401)
    - 'network_timeout': 网络读取或连接超时 (ReadTimeout / ConnectTimeout)
    - 'network_error': 其他底层网络故障
    - 'length_exceeded': 模型输出达到 token 限制导致截断
    - 'format_error': 模型输出未能通过 JSON 解析、字段类型、数组长度限制等格式校验
    - 'model_error': 服务端 5xx 或其它模型通道异常
    - 'invalid_result': 默认通用错误
    """
    msg = str(exc) if exc is not None else (getattr(result, "error", "") or "未知评估错误")

    # 1. 额度耗尽与认证
    http_status = getattr(result, "http_status", None)
    err_lower = msg.lower()
    if http_status == 403 or "free quota exhausted" in err_lower or "allocationquota" in err_lower or ("quota" in err_lower and "exhausted" in err_lower):
        return "quota_exhausted", msg
    if http_status == 401 or "unauthorized" in err_lower or ("api key" in err_lower and ("invalid" in err_lower or "missing" in err_lower)):
        return "auth_error", msg

    # 2. 输出截断
    if getattr(result, "finish_reason", None) == "length" or "截断" in msg or getattr(result, "reason_code", None) == "LENGTH_EXCEEDED":
        return "length_exceeded", msg

    # 3. 网络超时与故障
    if "timeout" in err_lower or "timed out" in err_lower or getattr(result, "error_type", "") in ("ReadTimeout", "ConnectTimeout"):
        return "network_timeout", msg
    if getattr(result, "reason_code", None) == "NETWORK_ERROR" or "connection" in err_lower or "requests.exceptions" in err_lower:
        return "network_error", msg

    # 4. 格式与契约校验失败 (此时 result.ok 为 True 但解析/验证抛出异常)
    if getattr(result, "ok", False) and exc is not None:
        return "format_error", msg

    # 5. 模型服务端错误
    if getattr(result, "reason_code", None) == "MODEL_ERROR" or (http_status and http_status >= 500):
        return "model_error", msg

    return "invalid_result", msg


class RunStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class FinderRunState:
    """Per-run facts; no catalog state or global execution context."""

    def __init__(self, topic, params, run_dir=None, cfg=None):
        self.run_dir = run_dir
        self.usage = UsageTotals()
        params = {"limit": DEFAULT_LIMIT, "max_evaluations": DEFAULT_MAX_EVALUATIONS,
                  "max_tokens": DEFAULT_MAX_TOKENS, "max_rounds": DEFAULT_MAX_ROUNDS, **params}
        root_dir = run_dir.parents[3] if run_dir and len(run_dir.parents) >= 4 else None
        self.report = {"schema_version": FINDER_REPORT_SCHEMA_VERSION, "topic": topic, "parameters": params,
            "status": "running", "stop_reason": None, "plan": None,
            "terminology_observation": None,
            "evaluation_attempts": 0, "evaluated_count": 0, "evaluations": [],
            "shortlist": [], "alternatives": [], "calls": [], "errors": [],
            "coverage_incomplete": False,
            "search": {"queries_executed": [], "repos_discovered": 0,
                       "candidates_found": 0, "expansions": [], "skipped": [],
                       "skipped_owned": 0, "skipped_owned_ids": []},
            "environment": {
                "schema_version": FINDER_REPORT_SCHEMA_VERSION,
                "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
                "git_commit": get_git_commit_hash(root_dir),
                "config_fingerprint": build_config_fingerprint(cfg),
            }}

    def save(self):
        self.report["usage"] = self.usage.snapshot()
        self.report["metrics"] = build_run_metrics(self.report, kind="finder")
        self.report["updated_at"] = now_local().isoformat()
        audit = audit_usage_reconciliation(self.report.get("calls", []), self.report.get("usage"))
        if audit and audit.get("status") == "discrepancy_detected":
            self.report["usage_audit"] = audit
        # During execution only the authoritative JSON is updated.
        if self.run_dir is not None:
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
            fmt = resolve_response_format(cfg, stage or ("evaluation" if candidate_id else "planning"))
            if _supports_kwarg(transport, "response_format"):
                result = transport(cfg, system, user, api_key=api_key, response_format=fmt, sleep=sleep)
            else:
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
                  STATUS_STOPPED if stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES, STATUS_ROUND_LIMIT, STATUS_QUOTA_EXHAUSTED) else
                  STATUS_COMPLETED if stop_reason in (STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED, STATUS_ALL_CANDIDATES_OWNED, STATUS_COMPLETED) else STATUS_ERROR)
    history = report.get("search", {}).get("rounds_history", [])
    if history:
        history[-1]["evaluated"] = len(report.get("evaluations", [])) - history[-1].get("evaluation_start", 0)
    report.update(schema_version=FINDER_REPORT_SCHEMA_VERSION, status=status, stop_reason=stop_reason,
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
        downgrade_reason = verified.get("downgrade_reason")
        if parsed.get("match") != verified.get("match") and not downgrade_reason:
            downgrade_reason = "准则支持证据不充分或缺少必需能力"

        record = {
            "candidate": {
                "skill_id": candidate.skill_id,
                "name": candidate.name,
                "repo_url": candidate.repo_url,
                "url": candidate.url,
                "author": candidate.owner,
                "path": candidate.path,
                "content_fingerprint": candidate.content_fingerprint,
            },
            "raw_match": parsed.get("match"),
            "verified_match": verified.get("match"),
            "downgrade_reason": downgrade_reason,
            "evaluation": verified,
            "materials": manifest,
        }
        report["evaluations"].append(record)
        report["evaluated_count"] = len(report["evaluations"])
        successful = True
    except (ValueError, TypeError, KeyError) as exc:
        code, err_msg = classify_evaluation_error(result, exc)
        err_entry = {
            "stage": "evaluation",
            "skill_id": candidate.skill_id,
            "code": code,
            "message": err_msg,
        }
        if code == "format_error":
            exc_str = str(exc)
            for f in ("dependencies", "limitations", "quote", "criteria_results", "summary_zh", "usage_zh", "why_consider"):
                if f in exc_str:
                    err_entry["field"] = f
                    break
        report["errors"].append(err_entry)
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

    from .relevance import extract_relevance_terms, schedule_candidates_by_relevance_and_fairness
    terms = extract_relevance_terms(state.report.get("topic", ""), state.report.get("plan"))
    ordered_candidates = schedule_candidates_by_relevance_and_fairness(candidates, terms)

    for candidate in ordered_candidates:
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
            last_call = state.report["calls"][-1] if state.report.get("calls") else {}
            last_resp = last_call.get("response") or {}
            last_err = (last_resp.get("error") or "").lower()
            if "allocationquota" in last_err or "free quota exhausted" in last_err or last_resp.get("http_status") == 403:
                log(f"  -> 模型免费额度已耗尽 (HTTP 403: {last_resp.get('error')})，立即终止后续尝试。")
                return STATUS_QUOTA_EXHAUSTED
            elif not last_resp.get("ok", False):
                log(f"  -> 请求失败并缺失用量（{last_resp.get('reason_code') or '网络/服务端异常'}：{last_resp.get('error')}），触发未知用量熔断。")
            else:
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


def execute_find_skill(topic="", *, limit=None, max_evaluations=None, max_tokens=None, max_rounds=None,
                       root_dir=".", model_cfg=None, log=print, sleep=time.sleep,
                       call_model_fn=None, fetch_candidate_materials_fn=None,
                       expand_and_collect_candidates_fn=None, search_github_repos_fn=None,
                       owned_ids=None, max_clarification_turns=None,
                       input_fn=input, resume_dir=None, retry_failed_searches=False,
                       enable_terminology_completion=None):
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
    params = {k: _parse_int_val(explicit if explicit is not None else (prev_params.get(k) if resumed else run_cfg.get(k)), default, k)
              for k, explicit, default in (("limit", limit, DEFAULT_LIMIT), ("max_evaluations", max_evaluations, DEFAULT_MAX_EVALUATIONS), ("max_tokens", max_tokens, DEFAULT_MAX_TOKENS), ("max_rounds", max_rounds, DEFAULT_MAX_ROUNDS))}
    if params["limit"] < 1 or params["max_evaluations"] < params["limit"] or params["max_tokens"] < 1000 or params["max_rounds"] < 1:
        raise ValueError("要求 limit >= 1、max_evaluations >= limit、max_tokens >= 1000、max_rounds >= 1")

    if enable_terminology_completion is None:
        enable_terminology_completion = bool(
            prev_params.get("enable_terminology_completion", False)
            if resumed
            else run_cfg.get("enable_terminology_completion", False)
        )
    params["enable_terminology_completion"] = bool(enable_terminology_completion)

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

    state = FinderRunState(topic.strip(), params, directory, cfg=cfg)
    if resumed:
        state.report = deepcopy(prev_report)
        state.report.setdefault("calls", [])
        state.report.setdefault("errors", [])
        state.report.setdefault("coverage_incomplete", False)
        state.report.setdefault("evaluation_attempts", max(len(state.report.get("evaluations", [])), sum(bool(c.get("skill_id")) for c in state.report["calls"])))
        state.report.setdefault("environment", {}).update({
            "schema_version": FINDER_REPORT_SCHEMA_VERSION,
            "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
            "git_commit": get_git_commit_hash(root),
            "config_fingerprint": build_config_fingerprint(cfg),
        })
        defaults = FinderRunState(topic, params, cfg=cfg).report["search"]
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
        if retry_failed_searches:
            reset_failed_searches(state)
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
                    state.report["plan"], state.report["terminology_observation"] = parse_plan_with_observation(
                        result["content"],
                        topic=topic,
                        enable_completion=params["enable_terminology_completion"],
                    )
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
                enable_terminology_completion=params["enable_terminology_completion"],
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
    parser.add_argument("--retry-failed-searches", action="store_true",
                        help="配合 --resume 显式重置失败页的尝试次数，保留成功结果及限流等待时间")
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
    parser.add_argument(
        "--enable-terminology-completion",
        action="store_true",
        help="启用技术术语有界补全（在现有查询预算内合并术语别名）",
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
            retry_failed_searches=args.retry_failed_searches,
            enable_terminology_completion=args.enable_terminology_completion if args.enable_terminology_completion else None,
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
