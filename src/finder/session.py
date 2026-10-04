"""Finder 会话状态、预算预留与中断恢复模块。

负责 FinderRunState 会话跟踪、Token 预留、模型请求回调落盘与断点恢复。
不承担单候选评估调度与 CLI 参数处理。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import threading
from typing import Any
from uuid import uuid4

from src.infra.files import write_json_atomic
from src.infra.model_pool import PoolReselect, PoolStopped
from src.shared.metrics import build_run_metrics
from src.shared.output_contracts import resolve_response_format
from src.shared.runtime import now_local
from src.shared.usage import UsageTotals, audit_usage_reconciliation
from src.shared.versions import (
    FINDER_REPORT_SCHEMA_VERSION,
    LLM_OUTPUT_CONTRACT_VERSION,
    build_config_fingerprint,
    get_git_commit_hash,
)
from .evaluation import rank_find_results
from .plan import (
    CLARIFICATION_MAX_OUTPUT_TOKENS,
    PLAN_MAX_OUTPUT_TOKENS,
)

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

DEFAULT_LIMIT = 5
DEFAULT_MAX_EVALUATIONS = 80
DEFAULT_MAX_TOKENS = 20_000_000
DEFAULT_MAX_ROUNDS = 4
MAX_CONSECUTIVE_FAILURES = 20


def estimate_request_token_bound(cfg: dict, system: str, user: str, response_format: Any = None) -> int:
    """保守估算单次请求输入与输出 Token 上限。"""
    schema = json.dumps(response_format, ensure_ascii=False) if response_format else ""
    input_bound = sum(len(text.encode("utf-8")) for text in (system, user, schema)) + 256
    output_bound = int((cfg.get("limits") or {}).get("max_output_tokens", 4000))
    if output_bound <= 0:
        raise ValueError("max_output_tokens 必须大于 0")
    return input_bound + output_bound


def _supports_kwarg(fn: Any, kwarg_name: str) -> bool:
    """检查可调用对象是否支持指定的关键字参数。"""
    try:
        sig = inspect.signature(fn)
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg_name:
                return True
        return False
    except (ValueError, TypeError):
        return True


def _target_reached(state: FinderRunState) -> bool:
    """检查是否已满足短名单推荐目标。"""
    report = state.report
    return len(rank_find_results(
        report["evaluations"],
        report.get("plan"),
        report["parameters"]["limit"],
    )[0]) >= report["parameters"]["limit"]


class RunStopped(Exception):
    """Finder 运行受控停止异常。"""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class FinderRunState:
    """单个 Finder 运行的运行时状态、预算与并发账本。"""

    def __init__(self, topic: str, params: dict, run_dir: Path | None = None, cfg: dict | None = None):
        self.model_pool = None
        self.pool_stop = None
        self.run_dir = run_dir
        self.usage = UsageTotals()
        self._lock = threading.RLock()
        self._active_call_ids: set[int] = set()
        self._budget_changed = threading.Condition(self._lock)
        params = {
            "limit": DEFAULT_LIMIT, "max_evaluations": DEFAULT_MAX_EVALUATIONS,
            "max_tokens": DEFAULT_MAX_TOKENS, "max_rounds": DEFAULT_MAX_ROUNDS,
            "concurrency": 1, **params,
        }
        root_dir = run_dir.parents[3] if run_dir and len(run_dir.parents) >= 4 else None
        self.report = {
            "schema_version": FINDER_REPORT_SCHEMA_VERSION, "topic": topic, "parameters": params,
            "status": "running", "stop_reason": None, "plan": None,
            "terminology_observation": None,
            "evaluation_attempts": 0, "evaluated_count": 0, "evaluations": [],
            "shortlist": [], "alternatives": [], "calls": [], "errors": [],
            "coverage_incomplete": False,
            "pending_evaluations": {},
            "search": {
                "queries_executed": [], "repos_discovered": 0,
                "candidates_found": 0, "expansions": [], "skipped": [],
                "skipped_owned": 0, "skipped_owned_ids": [],
            },
            "environment": {
                "schema_version": FINDER_REPORT_SCHEMA_VERSION,
                "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
                "git_commit": get_git_commit_hash(root_dir),
                "config_fingerprint": build_config_fingerprint(cfg),
            },
        }

    def save(self) -> None:
        """保存权威 report.json 数据。"""
        with self._lock:
            self.report["usage"] = self.usage.snapshot()
            self.report["metrics"] = build_run_metrics(self.report, kind="finder")
            self.report["updated_at"] = now_local().isoformat()
            audit = audit_usage_reconciliation(self.report.get("calls", []), self.report.get("usage"))
            if audit is not None:
                self.report["usage_audit"] = audit
            if self.run_dir is not None:
                write_json_atomic(self.run_dir / "report.json", self.report)

    def stop_reason(self) -> str | None:
        """获取当前停止原因（如有）。"""
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
    def reserved_tokens(self) -> int:
        """当前所有活跃或未决请求所预留的 Token 总量。"""
        return sum(
            c.get("reserved_tokens", 0) for c in self.report["calls"]
            if c.get("reservation_state") in ("active", "unknown")
        )

    def call(self, transport: Any, cfg: dict, system: str, user: str, *, api_key: str | None, sleep: Any, candidate_id: str | None = None, stage: str | None = None):
        """统一模型调用网关，支持单模型与百炼队列、请求级在途预留与用量统计。"""
        stage = stage or ("evaluation" if candidate_id else "planning")
        if self.model_pool is None:
            return self._call_once(transport, cfg, system, user, api_key=api_key, sleep=sleep,
                                   candidate_id=candidate_id, stage=stage)
        limit = {
            "clarification": CLARIFICATION_MAX_OUTPUT_TOKENS,
            "planning": PLAN_MAX_OUTPUT_TOKENS,
            "reflection": PLAN_MAX_OUTPUT_TOKENS,
            "evaluation": EVAL_MAX_OUTPUT_TOKENS,
        }.get(stage)

        def invoke(effective, fmt, context):
            result, unknown = self._call_once(
                transport, effective, system, user,
                api_key=api_key, sleep=sleep, candidate_id=candidate_id, stage=stage, context=context,
            )
            if result.reason_code == 'ACCOUNT_ERROR' or (result.http_status in (401, 403) and result.reason_code not in ('QUOTA_EXHAUSTED', 'QUOTA_RESPONSE_CONFLICT')):
                if unknown:
                    self.report.setdefault('stop_causes', []).append(STATUS_USAGE_UNKNOWN)
                raise RunStopped('access_denied')
            if result.http_status in (400, 404, 422) and result.reason_code not in (
                    'QUOTA_EXHAUSTED', 'QUOTA_RESPONSE_CONFLICT', 'MODEL_REQUEST_INCOMPATIBLE'):
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

    def _call_once(self, transport: Any, cfg: dict, system: str, user: str, *, api_key: str | None, sleep: Any, candidate_id: str | None = None, stage: str | None = None, context: dict | None = None):
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
                if not self._active_call_ids:
                    raise RunStopped(STATUS_TOKEN_LIMIT)
                self._budget_changed.wait()
            if self.model_pool and not self.model_pool.model_available(cfg['model']):
                raise PoolReselect()
            request_id = (context or {}).get('request_id') or uuid4().hex
            call = {
                "request_id": request_id, "stage": stage, "skill_id": candidate_id,
                "state": "started", "usage": None, "reserved_tokens": reserve,
                "reservation_state": "active", "reservation_method": "utf8-bound-v1",
            }
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
                self.save()
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
            call["response"] = {
                key: getattr(result, key, None) for key in (
                    "ok", "content", "error", "reason_code", "finish_reason",
                    "http_status", "requested_model", "returned_model", "model_config_fingerprint",
                )
            }
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


def _resolve_resume_dir(root_path: Path, resume_arg: str | None) -> Path | None:
    """根据命令行参数解析要续跑的历史运行目录。"""
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
