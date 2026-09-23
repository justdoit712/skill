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
