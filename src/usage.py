"""按接口实际返回的 usage 汇总；推理 Token 属于输出的一部分，不重复相加。"""

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
        attempts = max(1, int(getattr(call, "attempts", 1) or 1))
        self.requests += attempts
        # 失败重试的响应可能没有 usage，不假装它们免费。
        self.unknown_usage_requests += attempts - 1 + int(total is None)
        self.incomplete_breakdown_requests += int(prompt is None or completion is None)
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0
        self.reasoning_tokens += reasoning or 0
        self.total_tokens += total or 0
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "reasoning_tokens": reasoning, "total_tokens": total,
                "attempts": attempts}

    def snapshot(self) -> dict:
        return asdict(self)
