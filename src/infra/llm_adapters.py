"""Protocol adapters for LLM requests and count views.

Extracts wire protocol details from business logic for preflight inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.shared.llm_contracts import RequestIntent


@dataclass(frozen=True)
class RequestCountView:
    """Protocol-derived view of request components for token counting."""
    messages_text: tuple[str, ...]
    message_count: int
    schema_text: str | None
    estimated_overhead_tokens: int


class OpenAIProtocolAdapter:
    """OpenAI compatible REST chat completions protocol adapter for preflight inspection."""
    revision = "openai-compatible-v1"

    def build_count_view(self, intent: RequestIntent) -> RequestCountView:
        texts = tuple(m["content"] for m in intent.messages)
        schema_text: str | None = None
        if isinstance(intent.response_format, dict):
            fmt_type = intent.response_format.get("type")
            if fmt_type == "json_schema":
                js = intent.response_format.get("json_schema") or {}
                schema_text = json.dumps(js, sort_keys=True, ensure_ascii=False)
        return RequestCountView(
            messages_text=texts,
            message_count=len(intent.messages),
            schema_text=schema_text,
            estimated_overhead_tokens=4 * len(intent.messages) + 3,
        )

    def encode_request(
        self,
        intent: RequestIntent,
        model: str,
        effective_output_tokens: int,
    ) -> bytes:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(intent.messages),
            "temperature": intent.temperature,
            "max_tokens": effective_output_tokens,
        }
        fmt = intent.response_format
        if fmt is not None:
            if isinstance(fmt, str):
                payload["response_format"] = {"type": fmt}
            elif isinstance(fmt, dict):
                payload["response_format"] = fmt
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
