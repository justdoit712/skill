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
import concurrent.futures
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
import time
import inspect
import re
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.llm import call_model, resolve_api_key
from src.infra.model_pool import ModelPool, PoolStopped, PoolReselect
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
STATUS_QUOTA_EXHAUSTED = "models_exhausted"


MAX_CONSECUTIVE_FAILURES = 20


def estimate_request_token_bound(cfg, system, user, response_format=None):
    """Conservative local bound, not measured usage or a billing claim.

    Reserve one token per UTF-8 byte of the messages and response schema,
    256 tokens for message framing, plus the complete output allowance.
    This intentionally overestimates ordinary byte-based LLM tokenizers.
    """
    schema = json.dumps(response_format, ensure_ascii=False) if response_format else ""
    input_bound = sum(len(text.encode("utf-8")) for text in (system, user, schema)) + 256
    output_bound = int((cfg.get("limits") or {}).get("max_output_tokens", 4000))
    if output_bound <= 0:
        raise ValueError("max_output_tokens 必须大于 0")
    return input_bound + output_bound


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
    - 'models_exhausted': 结构化明确额度拒绝；普通 403 不属于耗尽
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
    if getattr(result, "reason_code", None) in ("QUOTA_EXHAUSTED", "ALL_MODELS_EXHAUSTED"):
        return STATUS_QUOTA_EXHAUSTED, msg
    if http_status in (401, 403) or "unauthorized" in err_lower or ("api key" in err_lower and ("invalid" in err_lower or "missing" in err_lower)):
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
        self.model_pool = None
        self.pool_stop = None
        self.run_dir = run_dir
        self.usage = UsageTotals()
        self._lock = threading.RLock()
        self._active_call_ids: set[int] = set()
        self._budget_changed = threading.Condition(self._lock)
        params = {"limit": DEFAULT_LIMIT, "max_evaluations": DEFAULT_MAX_EVALUATIONS,
                  "max_tokens": DEFAULT_MAX_TOKENS, "max_rounds": DEFAULT_MAX_ROUNDS,
                  "concurrency": 1, **params}
        root_dir = run_dir.parents[3] if run_dir and len(run_dir.parents) >= 4 else None
        self.report = {"schema_version": FINDER_REPORT_SCHEMA_VERSION, "topic": topic, "parameters": params,
            "status": "running", "stop_reason": None, "plan": None,
            "terminology_observation": None,
            "evaluation_attempts": 0, "evaluated_count": 0, "evaluations": [],
            "shortlist": [], "alternatives": [], "calls": [], "errors": [],
            "coverage_incomplete": False,
            "pending_evaluations": {},
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
        with self._lock:
            self.report["usage"] = self.usage.snapshot()
            self.report["metrics"] = build_run_metrics(self.report, kind="finder")
            self.report["updated_at"] = now_local().isoformat()
            audit = audit_usage_reconciliation(self.report.get("calls", []), self.report.get("usage"))
            if audit is not None:
                self.report["usage_audit"] = audit
            # During execution only the authoritative JSON is updated.
            if self.run_dir is not None:
                write_json_atomic(self.run_dir / "report.json", self.report)

    def stop_reason(self):
        with self._lock:
            abandoned_started = any(
                c.get("state") == "started" and id(c) not in self._active_call_ids
                for c in self.report["calls"]
            )
            if self.usage.unknown_usage_requests or abandoned_started or any(c.get("state") == "unknown" for c in self.report["calls"]):
                return STATUS_USAGE_UNKNOWN
            if self.pool_stop:
                return self.pool_stop
            if _target_reached(self):
                return STATUS_TARGET_REACHED
            if self.usage.total_tokens >= self.report["parameters"]["max_tokens"]:
                return STATUS_TOKEN_LIMIT
            if self.report["evaluation_attempts"] >= self.report["parameters"]["max_evaluations"]:
                return STATUS_EVALUATION_LIMIT
            if self.report["search"].get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES:
                return STATUS_MODEL_FAILURES
            return None

    @property
    def reserved_tokens(self):
        # Reservation facts survive interruption alongside each request.
        return sum(c.get("reserved_tokens", 0) for c in self.report["calls"]
                   if c.get("reservation_state") in ("active", "unknown"))

    def call(self, transport, cfg, system, user, *, api_key, sleep, candidate_id=None, stage=None):
        stage = stage or ("evaluation" if candidate_id else "planning")
        if self.model_pool is None:
            return self._call_once(transport, cfg, system, user, api_key=api_key, sleep=sleep,
                                   candidate_id=candidate_id, stage=stage)
        limit = {"clarification": CLARIFICATION_MAX_OUTPUT_TOKENS,
                 "planning": PLAN_MAX_OUTPUT_TOKENS, "reflection": PLAN_MAX_OUTPUT_TOKENS,
                 "evaluation": EVAL_MAX_OUTPUT_TOKENS}.get(stage)
        def invoke(effective, fmt, context):
            result, unknown = self._call_once(transport, effective, system, user,
                api_key=api_key, sleep=sleep, candidate_id=candidate_id, stage=stage, context=context)
            if result.reason_code == 'ACCOUNT_ERROR' or (result.http_status in (401, 403) and result.reason_code not in ('QUOTA_EXHAUSTED', 'QUOTA_RESPONSE_CONFLICT')):
                if unknown:
                    self.report.setdefault('stop_causes', []).append(STATUS_USAGE_UNKNOWN)
                raise RunStopped('access_denied')
            if result.http_status in (400, 404, 422) and result.reason_code not in ('QUOTA_EXHAUSTED', 'QUOTA_RESPONSE_CONFLICT'):
                raise RunStopped('request_config_error')
            if unknown and result.reason_code != 'QUOTA_RESPONSE_CONFLICT':
                raise RunStopped(STATUS_USAGE_UNKNOWN)
            return result
        try:
            result, attempts = self.model_pool.run(system, user, stage, invoke,
                                                   stage_limit=limit, sleep=sleep)
            return result, bool(self.usage.unknown_usage_requests)
        except RunStopped as exc:
            with self._budget_changed:
                self.pool_stop = exc.reason
                self.report.setdefault('stop_causes', []).append(exc.reason)
                self._budget_changed.notify_all()
            raise
        except OSError as exc:
            self.model_pool.failure = "storage_error"
            self.pool_stop = "storage_error"
            raise RunStopped("storage_error") from exc
        except PoolStopped as exc:
            with self._budget_changed:
                self.pool_stop = exc.reason
                self.report.setdefault('stop_causes', []).append(exc.reason)
                self.report['errors'].append({'stage': stage, 'code': exc.reason, 'message': str(exc)})
                self._budget_changed.notify_all()
            raise RunStopped(self.stop_reason() or exc.reason) from exc

    def _call_once(self, transport, cfg, system, user, *, api_key, sleep, candidate_id=None, stage=None, context=None):
        stage = stage or ("evaluation" if candidate_id else "planning")
        fmt = resolve_response_format(cfg, stage)
        reserve = estimate_request_token_bound(cfg, system, user, fmt)
        with self._budget_changed:
            while True:
                reason = self.stop_reason()
                if reason:
                    raise RunStopped(reason)
                if self.usage.total_tokens + self.reserved_tokens + reserve <= self.report["parameters"]["max_tokens"]:
                    break
                # An active request may return unused budget. Wait for its
                # settlement rather than treating temporary occupancy as final.
                if not self._active_call_ids:
                    raise RunStopped(STATUS_TOKEN_LIMIT)
                self._budget_changed.wait()
            if self.model_pool and not self.model_pool.model_available(cfg['model']):
                raise PoolReselect()
            request_id = (context or {}).get('request_id') or uuid4().hex
            call = {"request_id": request_id, "stage": stage, "skill_id": candidate_id,
                    "state": "started", "usage": None, "reserved_tokens": reserve,
                    "reservation_state": "active", "reservation_method": "utf8-bound-v1"}
            call.update(context or {})
            self.report["calls"].append(call)
            if candidate_id:
                self.report["evaluation_attempts"] += 1
                checkpoint = self.report.get("pending_evaluations", {}).get(candidate_id)
                if checkpoint is not None:
                    checkpoint["request_id"] = request_id
            call_id = id(call)
            self._active_call_ids.add(call_id)
            try:
                self.save()  # Persist request, reservation and checkpoint before sending.
            except BaseException:
                self._active_call_ids.discard(call_id)
                call.update(state="not_sent", reservation_state="released")
                if candidate_id:
                    self.report["evaluation_attempts"] -= 1
                self._budget_changed.notify_all()
                raise

        try:
            if _supports_kwarg(transport, "response_format"):
                result = transport(cfg, system, user, api_key=api_key, response_format=fmt, sleep=sleep)
            else:
                result = transport(cfg, system, user, api_key=api_key, sleep=sleep)
        except BaseException:
            with self._budget_changed:
                self._active_call_ids.discard(call_id)
                call.update(state="unknown", reservation_state="unknown")
                self.usage.record_unknown_request()
                try:
                    self.save()
                finally:
                    self._budget_changed.notify_all()
            raise

        with self._budget_changed:
            self._active_call_ids.discard(call_id)
            result.requested_model = cfg.get('model')
            if context:
                result.model_config_fingerprint = context['model_config_fingerprint']
            call['requested_model'] = cfg.get('model')
            call['returned_model'] = getattr(result, 'returned_model', None)
            call['provider_error_code'] = getattr(result, 'provider_error_code', None)
            call['billing_state'] = getattr(result, 'billing_state', None)
            call['raw_usage'] = getattr(result, 'usage', None)
            used = self.report.setdefault('models_used', [])
            if result.attempts and cfg.get('model') not in used:
                used.append(cfg.get('model'))
            rejected = getattr(result, 'billing_state', None) == 'rejected_before_inference'
            if rejected and candidate_id:
                self.report['evaluation_attempts'] -= 1
            call["usage"] = self.usage.add(result)
            call["response"] = {key: getattr(result, key, None) for key in
                                ("ok", "content", "error", "reason_code", "finish_reason", "http_status", "requested_model", "returned_model", "model_config_fingerprint")}
            if not getattr(result, "ok", False):
                call["state"] = "not_sent" if call["usage"]["attempts"] == 0 else "error"
            else:
                call["state"] = "unknown" if call["usage"]["total_tokens"] is None else "received"
            call["reservation_state"] = (
                "unknown" if call["usage"]["attempts"] > 0 and call["usage"]["total_tokens"] is None and not rejected else "settled"
            )
            try:
                self.save()
            finally:
                self._budget_changed.notify_all()
            return result, bool(self.usage.unknown_usage_requests)


def finalize_run(report, stop_reason, *, status=None, evaluated_items=None, plan=None,
                 limit=DEFAULT_LIMIT, run_dir=None, root_dir=None, usage=None,
                 evaluation_attempts=None, evaluated_count=None, log=print):
    if status is None:
        status = (STATUS_INTERRUPTED if stop_reason == STATUS_INTERRUPTED else
                  STATUS_STOPPED if stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES, STATUS_ROUND_LIMIT, STATUS_QUOTA_EXHAUSTED, 'input_limit_mismatch') else
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
    return _record_evaluation(state, candidate, materials, manifest, result), unknown


def _record_evaluation(state, candidate, materials, manifest, result):
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
            # 本地实时投影：每完成 1 个候选的评估，即刻在内存重算并向 report 投影当前的最新短名单与备选名单
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


def _recover_pending_evaluations(state):
    """Replay saved responses locally before marking attempted skills processed."""
    from src.shared.models import Candidate

    report = state.report
    checkpoints = report.setdefault("pending_evaluations", {})
    legacy = report.get("pending_evaluation")
    if legacy:
        checkpoints.setdefault(legacy["candidate"]["skill_id"], legacy)
    for skill_id, checkpoint in list(checkpoints.items()):
        request_id = checkpoint.get("request_id")
        calls = [c for c in report["calls"] if c.get("skill_id") == skill_id
                 and c.get("stage") == "evaluation" and c.get("state") != "not_sent"
                 and (not request_id or c.get("request_id") == request_id)]
        if len(calls) > 1:
            raise ValueError(f"恢复请求身份不唯一：{skill_id}")
        if not calls or not calls[0].get("response"):
            # Unsent material may be retried; an unknown request stays blocked.
            continue
        if calls[0].get("billing_state") == "rejected_before_inference":
            continue
        if not any(e["candidate"]["skill_id"] == skill_id for e in report["evaluations"]):
            _record_evaluation(state, Candidate(**checkpoint["candidate"]), checkpoint["materials"],
                               checkpoint["manifest"], SimpleNamespace(**calls[0]["response"]))
        else:
            checkpoints.pop(skill_id, None)
            if (report.get("pending_evaluation") or {}).get("candidate", {}).get("skill_id") == skill_id:
                report.pop("pending_evaluation", None)
            state.save()


def _evaluate_candidates_concurrent(
    state,
    ordered_candidates,
    cfg,
    api_key,
    transport,
    fetch,
    sleep,
    log=print,
    max_workers: int = 2,
):
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

            # 2. 拉取材料（锁外执行）
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

            # 3. 评估候选（在 state.call 与 _record_evaluation 内部加锁互斥）
            if stop_event.is_set():
                return None
            successful, unknown = _evaluate_candidate(state, candidate, materials, cfg, api_key, transport, sleep)

            with state._lock:
                if unknown:
                    last_call = next((c for c in reversed(state.report["calls"]) if c.get("skill_id") == candidate.skill_id), {})
                    last_resp = last_call.get("response") or {}
                    last_err = (last_resp.get("error") or "").lower()
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
                    continue  # Drain and inspect every already submitted request.

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
            last_err = (last_resp.get("error") or "").lower()
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
                       concurrency=None,
                       root_dir=".", model_cfg=None, log=print, sleep=time.sleep,
                       call_model_fn=None, fetch_candidate_materials_fn=None,
                       expand_and_collect_candidates_fn=None, search_github_repos_fn=None,
                       owned_ids=None, max_clarification_turns=None,
                       input_fn=input, resume_dir=None, retry_failed_searches=False,
                       enable_terminology_completion=None,
                       enable_active_reflection=None):
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
                'at': now_local().isoformat()})
            state.report['stop_causes'] = []
        state.report.setdefault("calls", [])
        state.report.setdefault("errors", [])
        state.report.setdefault("coverage_incomplete", False)
        state.report.setdefault("evaluation_attempts", max(len(state.report.get("evaluations", [])), sum(bool(c.get("skill_id")) for c in state.report["calls"])))
        previous_fp = state.report.get("environment", {}).get("config_fingerprint")
        if previous_fp != build_config_fingerprint(cfg):
            state.report.setdefault("config_changes", []).append({"previous": previous_fp, "current": build_config_fingerprint(cfg), "at": now_local().isoformat()})
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
        state.report.update(run_id=run_id, started_at=started.isoformat(), model=cfg.get("model"),
                            report_paths={"json": str(directory / "report.json"), "md": str(directory / "report.md")})
        state.report["parameters"].update(plan_max_output_tokens=PLAN_MAX_OUTPUT_TOKENS, evaluation_max_output_tokens=EVAL_MAX_OUTPUT_TOKENS,
                                            max_consecutive_failures=MAX_CONSECUTIVE_FAILURES)

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
                log(f"若需继续评估更多候选，请通过命令行参数增加上限，例如：")
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
                reason = run_rounds(state, cfg, api_key, transport,
                    search_github_repos_fn or search_github_repos_for_query,
                    expand_and_collect_candidates_fn or expand_and_collect_candidates,
                    fetch_candidate_materials_fn or fetch_candidate_materials,
                    _evaluate_candidates, owned_ids, sleep, log,
                    **run_rounds_kwargs)
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
