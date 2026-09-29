"""按接口实际返回的 usage 汇总事实账本。

精确记录已知 Token、未知用量请求数、不完整统计数；推理 Token 属于输出的一部分，不重复相加。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    unknown_usage_requests: int = 0
    incomplete_breakdown_requests: int = 0

    def add(self, call) -> dict:
        usage = getattr(call, "usage", None) or {}
        prompt = _number(usage.get("prompt_tokens"))
        completion = _number(usage.get("completion_tokens"))
        total = _number(usage.get("total_tokens"))
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        reasoning = _number((usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        attempts = max(0, int(getattr(call, "attempts", 1)))
        # A received response proves a request happened even for legacy adapters
        # which leave attempts at its default zero value.
        if attempts == 0 and (getattr(call, "ok", False) or usage or getattr(call, "content", None)):
            attempts = 1
        if attempts == 0:
            return {"prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None,
                    "total_tokens": None, "attempts": 0}
        self.requests += attempts
        # 失败重试的响应可能没有 usage，不假装它们免费。
        self.unknown_usage_requests += attempts - 1 + int(total is None)
        self.incomplete_breakdown_requests += int(prompt is None or completion is None)
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0
        self.reasoning_tokens += reasoning or 0
        self.total_tokens += total or 0
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "total_tokens": total,
            "attempts": attempts,
        }

    def snapshot(self) -> dict:
        return asdict(self)

    def record_unknown_request(self) -> None:
        """A transport was entered but never returned a result."""
        self.requests += 1
        self.unknown_usage_requests += 1
        self.incomplete_breakdown_requests += 1


def recompute_usage_from_calls(calls: list[dict]) -> UsageTotals:
    """从调用明细严格复算 UsageTotals。"""
    totals = UsageTotals()
    if not isinstance(calls, list):
        return totals
    for c in calls:
        if not isinstance(c, dict):
            continue
        u = c.get("usage") or {}
        resp = c.get("response") or {}
        prompt = _number(u.get("prompt_tokens"))
        completion = _number(u.get("completion_tokens"))
        total = _number(u.get("total_tokens"))
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        reasoning = _number(u.get("reasoning_tokens"))
        attempts = max(0, int(u.get("attempts", 1)))
        if attempts == 0 and (resp.get("ok") or u or resp.get("content")):
            attempts = 1
        if attempts == 0:
            continue
        totals.requests += attempts
        totals.unknown_usage_requests += attempts - 1 + int(total is None)
        totals.incomplete_breakdown_requests += int(prompt is None or completion is None)
        totals.prompt_tokens += prompt or 0
        totals.completion_tokens += completion or 0
        totals.reasoning_tokens += reasoning or 0
        totals.total_tokens += total or 0
    return totals


def audit_usage_reconciliation(calls: list[dict], recorded_usage: dict | None) -> dict[str, Any] | None:
    """核查 calls 明细复算与持久化 recorded_usage 之间的差异。"""
    if not isinstance(calls, list) or not recorded_usage:
        return None
    recomputed = recompute_usage_from_calls(calls).snapshot()
    discrepancies = {}
    for k in (
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "requests",
        "unknown_usage_requests",
        "incomplete_breakdown_requests",
    ):
        rec_val = recorded_usage.get(k)
        recomp_val = recomputed.get(k)
        if rec_val != recomp_val:
            discrepancies[k] = {"recorded": rec_val, "recomputed": recomp_val}
    if discrepancies:
        return {
            "status": "discrepancy_detected",
            "discrepancies": discrepancies,
            "recomputed": recomputed,
            "recorded": recorded_usage,
        }
    return {
        "status": "matched",
        "recomputed": recomputed,
        "recorded": recorded_usage,
    }


__all__ = [
    "UsageTotals",
    "_number",
    "recompute_usage_from_calls",
    "audit_usage_reconciliation",
]
