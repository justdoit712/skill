"""Finder 候选评估与调度模块。

负责单候选评估、证据校验、结果记录、断点恢复以及串行与双并发调度。
"""

from __future__ import annotations

import concurrent.futures
from contextvars import copy_context
from dataclasses import asdict
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable

from src.shared.materials import MaterialBundle
from src.shared.models import Candidate
from src.shared.runtime import now_local
from .evaluation import (
    build_evaluation_prompt,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from .relevance import (
    extract_relevance_terms,
    schedule_candidates_by_relevance_and_fairness,
)
from .report import write_local_report
from .session import (
    DEFAULT_LIMIT,
    FinderRunState,
    MAX_CONSECUTIVE_FAILURES,
    RunStopped,
    STATUS_CANDIDATES_EXHAUSTED,
    STATUS_INTERRUPTED,
    STATUS_MODEL_FAILURES,
    STATUS_QUOTA_EXHAUSTED,
    STATUS_TARGET_REACHED,
    STATUS_USAGE_UNKNOWN,
    _target_reached,
)


def classify_evaluation_error(result: Any, exc: BaseException | None = None) -> tuple[str, str]:
    """细化评估错误类型。"""
    msg = str(exc) if exc is not None else (getattr(result, "error", "") or "未知评估错误")

    http_status = getattr(result, "http_status", None)
    err_lower = msg.lower()
    if getattr(result, "reason_code", None) in ("QUOTA_EXHAUSTED", "ALL_MODELS_EXHAUSTED"):
        return STATUS_QUOTA_EXHAUSTED, msg
    if http_status in (401, 403) or "unauthorized" in err_lower or ("api key" in err_lower and ("invalid" in err_lower or "missing" in err_lower)):
        return "auth_error", msg

    if getattr(result, "finish_reason", None) == "length" or "截断" in msg or getattr(result, "reason_code", None) == "LENGTH_EXCEEDED":
        return "length_exceeded", msg

    if "timeout" in err_lower or "timed out" in err_lower or getattr(result, "error_type", "") in ("ReadTimeout", "ConnectTimeout"):
        return "network_timeout", msg
    if getattr(result, "reason_code", None) == "NETWORK_ERROR" or "connection" in err_lower or "requests.exceptions" in err_lower:
        return "network_error", msg

    if getattr(result, "ok", False) and exc is not None:
        return "format_error", msg

    if getattr(result, "reason_code", None) == "MODEL_ERROR" or (http_status and http_status >= 500):
        return "model_error", msg

    return "invalid_result", msg


def _evaluate_candidate(state: FinderRunState, candidate: Any, materials: Any, cfg: dict, api_key: str | None, transport: Any, sleep: Any) -> tuple[bool, bool]:
    """调用模型评估单个候选条目。"""
    manifest = materials.manifest() if isinstance(materials, MaterialBundle) else {"identity_version": "primary-only-legacy"}
    with state._lock:
        report = state.report
        checkpoint = {
            "candidate": asdict(candidate),
            "materials": dict(materials),
            "manifest": manifest,
            "started_at": now_local().isoformat(),
        }
        report["pending_evaluation"] = checkpoint
        report.setdefault("pending_evaluations", {})[candidate.skill_id] = checkpoint
        state.save()

    system, user = build_evaluation_prompt(candidate, materials, report["plan"], report["topic"])
    result, unknown = state.call(transport, cfg, system, user, api_key=api_key, sleep=sleep, candidate_id=candidate.skill_id)
    finder_run = sys.modules.get("src.finder.run")
    record_fn = getattr(finder_run, "_record_evaluation", _record_evaluation) if finder_run else _record_evaluation
    return record_fn(state, candidate, materials, manifest, result), unknown


def _record_evaluation(state: FinderRunState, candidate: Any, materials: Any, manifest: dict, result: Any) -> bool:
    """解析评估结果并更新短名单与备选名单。"""
    with state._lock:
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
                "model": getattr(result, "requested_model", None) or getattr(result, "model", None),
                "model_config_fingerprint": getattr(result, "model_config_fingerprint", None),
                "raw_match": parsed.get("match"),
                "verified_match": verified.get("match"),
                "downgrade_reason": downgrade_reason,
                "evaluation": verified,
                "materials": manifest,
            }
            report["evaluations"].append(record)
            report["evaluated_count"] = len(report["evaluations"])
            shortlist, alternatives = rank_find_results(
                report["evaluations"],
                report.get("plan"),
                report.get("parameters", {}).get("limit", DEFAULT_LIMIT),
            )
            report["shortlist"] = shortlist
            report["alternatives"] = alternatives
            report["shortlist_count"] = len(shortlist)
            report["alternatives_count"] = len(alternatives)
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
        if (report.get("pending_evaluation") or {}).get("candidate", {}).get("skill_id") == candidate.skill_id:
            report.pop("pending_evaluation", None)
        if "pending_evaluations" in report:
            report["pending_evaluations"].pop(candidate.skill_id, None)
        state.save()
        if state.run_dir is not None:
            try:
                write_local_report(report, state.run_dir)
            except OSError:
                pass
        return successful


def _recover_pending_evaluations(state: FinderRunState) -> None:
    """在标记候选为已处理之前，优先在本地回放未决评估结果。"""
    report = state.report
    checkpoints = report.setdefault("pending_evaluations", {})
    legacy = report.get("pending_evaluation")
    if legacy:
        checkpoints.setdefault(legacy["candidate"]["skill_id"], legacy)
    for skill_id, checkpoint in list(checkpoints.items()):
        request_id = checkpoint.get("request_id")
        calls = [
            c for c in report["calls"]
            if c.get("skill_id") == skill_id
            and c.get("stage") == "evaluation" and c.get("state") != "not_sent"
            and (not request_id or c.get("request_id") == request_id)
        ]
        if len(calls) > 1:
            raise ValueError(f"恢复请求身份不唯一：{skill_id}")
        if not calls or not calls[0].get("response"):
            continue
        if calls[0].get("billing_state") == "rejected_before_inference":
            continue
        if not any(e["candidate"]["skill_id"] == skill_id for e in report["evaluations"]):
            finder_run = sys.modules.get("src.finder.run")
            record_fn = getattr(finder_run, "_record_evaluation", _record_evaluation) if finder_run else _record_evaluation
            record_fn(
                state, Candidate(**checkpoint["candidate"]), checkpoint["materials"],
                checkpoint["manifest"], SimpleNamespace(**calls[0]["response"]),
            )
        else:
            checkpoints.pop(skill_id, None)
            if (report.get("pending_evaluation") or {}).get("candidate", {}).get("skill_id") == skill_id:
                report.pop("pending_evaluation", None)
            state.save()


def _evaluate_candidates_concurrent(
    state: FinderRunState,
    ordered_candidates: list[Any],
    cfg: dict,
    api_key: str | None,
    transport: Any,
    fetch: Callable,
    sleep: Any,
    log: Callable = print,
    max_workers: int = 2,
) -> str:
    """并发调度多候选评估，支持在途预算协调与中断安全。"""
    report = state.report
    processed = report["search"].setdefault("processed_skill_ids", [])
    already_evaluated_ids = {e["candidate"]["skill_id"] for e in report.get("evaluations", [])}
    already_evaluated_ids.update(processed)

    to_eval = [c for c in ordered_candidates if c.skill_id not in already_evaluated_ids]
    if not to_eval:
        return state.stop_reason() or (STATUS_TARGET_REACHED if _target_reached(state) else STATUS_CANDIDATES_EXHAUSTED)

    stop_event = threading.Event()
    stop_reason_holder = [None]
    readable_counter = [0]
    failures_counter = [report["search"].get("consecutive_failures", 0)]

    def _worker_task(candidate):
        if stop_event.is_set():
            return None
        reason = state.stop_reason()
        if reason:
            stop_reason_holder[0] = reason
            stop_event.set()
            return None

        try:
            attempt_num = report["evaluation_attempts"] + 1
            max_num = report["parameters"]["max_evaluations"]
            log(f"[评估 #{attempt_num}/{max_num}] {candidate.name} ({candidate.skill_id})...")

            ok, materials, error = fetch(candidate, sleep=sleep)
            if not ok or not materials:
                with state._lock:
                    report["coverage_incomplete"] = True
                    report["search"]["skipped"].append({"skill_id": candidate.skill_id, "code": "material_failed", "message": error})
                    processed.append(candidate.skill_id)
                    state.save()
                return False

            with state._lock:
                readable_counter[0] += 1
                if getattr(materials, "fetch_errors", None):
                    report["coverage_incomplete"] = True

            if stop_event.is_set():
                return None
            successful, unknown = _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep)

            with state._lock:
                if unknown:
                    last_call = next((c for c in reversed(state.report["calls"]) if c.get("skill_id") == candidate.skill_id), {})
                    last_resp = last_call.get("response") or {}
                    if last_resp.get("reason_code") in ("QUOTA_EXHAUSTED", "ALL_MODELS_EXHAUSTED"):
                        log(f"  -> 模型免费额度已耗尽 (HTTP 403: {last_resp.get('error')})，立即终止后续尝试。")
                        stop_reason_holder[0] = STATUS_QUOTA_EXHAUSTED
                    elif not last_resp.get("ok", False):
                        log(f"  -> 请求失败并缺失用量（{last_resp.get('reason_code') or '网络/服务端异常'}：{last_resp.get('error')}），触发未知用量熔断。")
                        stop_reason_holder[0] = STATUS_USAGE_UNKNOWN
                    else:
                        log("  -> 接口成功响应但缺失用量数据，触发用量未知熔断。")
                        stop_reason_holder[0] = STATUS_USAGE_UNKNOWN
                    stop_event.set()
                    return False

                if successful:
                    failures_counter[0] = 0
                    last_ev = next(e["evaluation"] for e in reversed(report["evaluations"]) if e["candidate"]["skill_id"] == candidate.skill_id)
                    m = last_ev.get("match", "none")
                    tokens = state.usage.total_tokens
                    log(f"  -> 评估完成: match={m} (全库已完成: {len(report['evaluations'])}, 累计消耗: {tokens:,} Token)")
                    if _target_reached(state):
                        log(f"[目标达成] 短名单已集齐 {report['parameters']['limit']} 个，停止后续评估。")
                        stop_reason_holder[0] = STATUS_TARGET_REACHED
                        stop_event.set()
                        return True
                else:
                    failures_counter[0] += 1
                    if failures_counter[0] >= MAX_CONSECUTIVE_FAILURES:
                        stop_reason_holder[0] = STATUS_MODEL_FAILURES
                        stop_event.set()
                    log("  -> 候选评估未通过格式校验或调用出错，已记录并继续下一个候选...")
                return successful
        except RunStopped as exc:
            stop_reason_holder[0] = exc.reason
            stop_event.set()
            return False
        except BaseException:
            stop_event.set()
            raise

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="finder-eval") as executor:
            futures = set()
            cand_iter = iter(to_eval)
            for _ in range(max_workers):
                c = next(cand_iter, None)
                if c is not None:
                    futures.add(executor.submit(copy_context().run, _worker_task, c))

            while futures:
                done, futures = concurrent.futures.wait(futures, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    exc = f.exception()
                    if exc is not None:
                        if isinstance(exc, KeyboardInterrupt):
                            raise exc
                        stop_event.set()
                        raise exc

                if stop_event.is_set():
                    continue

                for _ in range(len(done)):
                    c = next(cand_iter, None)
                    if c is not None and not stop_event.is_set():
                        futures.add(executor.submit(copy_context().run, _worker_task, c))
    except KeyboardInterrupt:
        return STATUS_INTERRUPTED

    final_reason = state.stop_reason()
    if final_reason:
        return final_reason
    if stop_reason_holder[0]:
        return stop_reason_holder[0]
    if not readable_counter[0]:
        return "material_failed"
    if failures_counter[0] >= MAX_CONSECUTIVE_FAILURES:
        return STATUS_MODEL_FAILURES
    return STATUS_TARGET_REACHED if _target_reached(state) else STATUS_CANDIDATES_EXHAUSTED


def _evaluate_candidates(
    state: FinderRunState,
    candidates: list[Any],
    cfg: dict,
    api_key: str | None,
    transport: Any,
    fetch: Callable,
    sleep: Any,
    log: Callable = print,
) -> str:
    """顺序或并发评估候选列表。"""
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

    terms = extract_relevance_terms(state.report.get("topic", ""), state.report.get("plan"))
    ordered_candidates = schedule_candidates_by_relevance_and_fairness(candidates, terms)

    concurrency = int(report.get("parameters", {}).get("concurrency", 1) or 1)
    if concurrency > 1:
        return _evaluate_candidates_concurrent(
            state, ordered_candidates, cfg, api_key, transport, fetch, sleep, log=log, max_workers=min(2, concurrency)
        )

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
            last_call = next((c for c in reversed(state.report["calls"]) if c.get("skill_id") == candidate.skill_id), {})
            last_resp = last_call.get("response") or {}
            if last_resp.get("reason_code") in ("QUOTA_EXHAUSTED", "ALL_MODELS_EXHAUSTED"):
                log(f"  -> 模型免费额度已耗尽 (HTTP 403: {last_resp.get('error')})，立即终止后续尝试。")
                return STATUS_QUOTA_EXHAUSTED
            elif not last_resp.get("ok", False):
                log(f"  -> 请求失败并缺失用量（{last_resp.get('reason_code') or '网络/服务端异常'}：{last_resp.get('error')}），触发未知用量熔断。")
            else:
                log("  -> 接口成功响应但缺失用量数据，触发用量未知熔断。")
            return STATUS_USAGE_UNKNOWN
        if successful:
            last_ev = next(e["evaluation"] for e in reversed(report["evaluations"]) if e["candidate"]["skill_id"] == candidate.skill_id)
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
