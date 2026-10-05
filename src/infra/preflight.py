"""Preflight check and adaptation algorithm, token counting, and pure decision logic.

Implements 'off', 'validate', and 'adapt' modes without side effects, I/O, or network calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from src.shared.llm_contracts import (
    LocalRejection,
    PlanAdjustment,
    RequestIntent,
    TokenAssessment,
    PREFLIGHT_CAPABILITY_UNSUPPORTED,
    PREFLIGHT_INPUT_TOO_LARGE,
    PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL,
    PREFLIGHT_OUTPUT_LIMIT_EXCEEDED,
    PREFLIGHT_SPEC_UNKNOWN,
    PREFLIGHT_TOKEN_COUNT_UNCERTAIN,
)
from src.infra.llm_adapters import RequestCountView
from src.infra.model_specs import ModelSpec


@dataclass(frozen=True)
class PreflightPolicy:
    """Preflight mode and error policy configuration."""
    mode: str = "off"  # 'off' | 'validate' | 'adapt'
    unknown_spec: str = "passthrough"  # 'passthrough' | 'reject'
    uncertain_tokens: str = "conservative"  # 'conservative' | 'reject'
    safety_margin_tokens: int = 1024

    def __post_init__(self) -> None:
        if self.mode not in ("off", "validate", "adapt"):
            raise ValueError(f"preflight.mode 必须为 off、validate 或 adapt：{self.mode}")
        if self.unknown_spec not in ("passthrough", "reject"):
            raise ValueError(f"preflight.unknown_spec 必须为 passthrough 或 reject：{self.unknown_spec}")
        if self.uncertain_tokens not in ("conservative", "reject"):
            raise ValueError(f"preflight.uncertain_tokens 必须为 conservative 或 reject：{self.uncertain_tokens}")
        if (
            isinstance(self.safety_margin_tokens, bool)
            or not isinstance(self.safety_margin_tokens, int)
            or self.safety_margin_tokens < 0
        ):
            raise ValueError(f"preflight.safety_margin_tokens 必须为非负整数：{self.safety_margin_tokens}")


class ConservativeTokenCounter:
    """Default local conservative token estimator based on protocol view."""
    revision = "conservative-estimator-v1"

    def count_tokens(self, count_view: RequestCountView) -> TokenAssessment:
        # Conservative estimate: byte length of messages + overhead + schema length
        text_bytes = sum(len(text.encode("utf-8")) for text in count_view.messages_text)
        schema_bytes = len(count_view.schema_text.encode("utf-8")) if count_view.schema_text else 0
        total = text_bytes + schema_bytes + count_view.estimated_overhead_tokens
        return TokenAssessment(
            value=total,
            quality="estimated",
            method="conservative-byte-upper-bound-v1",
            revision=self.revision,
        )


@dataclass(frozen=True)
class PreflightDecision:
    """Intermediate decision produced by preflight pipeline."""
    outcome: str  # 'bypassed' | 'validated' | 'adapted'
    effective_output_tokens: int
    adjustments: tuple[PlanAdjustment, ...]
    token_assessment: TokenAssessment


def evaluate_preflight(
    *,
    intent: RequestIntent,
    spec: ModelSpec | None,
    counter: Any,
    adapter: Any,
    policy: PreflightPolicy,
    model_identity: str,
    endpoint: str,
) -> PreflightDecision | LocalRejection:
    """Evaluate request intent against model spec under the chosen preflight policy."""
    # 1. Mode off bypasses completely without inspecting spec or calling counter
    if policy.mode == "off":
        return PreflightDecision(
            outcome="bypassed",
            effective_output_tokens=intent.requested_output_tokens,
            adjustments=(),
            token_assessment=TokenAssessment(
                value=None,
                quality="unknown",
                method="none",
                revision="none",
            ),
        )

    # 2. Check if spec exists
    if spec is None:
        if policy.unknown_spec == "passthrough":
            return PreflightDecision(
                outcome="bypassed",
                effective_output_tokens=intent.requested_output_tokens,
                adjustments=(),
                token_assessment=TokenAssessment(
                    value=None,
                    quality="unknown",
                    method="none",
                    revision="none",
                ),
            )
        return LocalRejection(
            reason_code=PREFLIGHT_SPEC_UNKNOWN,
            message=f"未找到匹配的模型规格：{model_identity}",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    # 3. Check spec completeness
    if (
        not spec.context_window
        or not spec.max_output_tokens
        or not spec.context_accounting
    ):
        if policy.unknown_spec == "passthrough":
            return PreflightDecision(
                outcome="bypassed",
                effective_output_tokens=intent.requested_output_tokens,
                adjustments=(),
                token_assessment=TokenAssessment(
                    value=None,
                    quality="unknown",
                    method="none",
                    revision="none",
                ),
            )
        return LocalRejection(
            reason_code=PREFLIGHT_SPEC_UNKNOWN,
            message=f"模型规格不完整（缺失必要上下文或输出限制）：{model_identity}",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    # 4. Check format capabilities
    if intent.response_format is not None:
        fmt = intent.response_format
        if isinstance(fmt, str):
            kind = fmt
        elif isinstance(fmt, dict):
            kind = fmt.get("type", "json_object")
        else:
            kind = "unknown"

        support = spec.response_formats.get(kind)
        if support is False:
            return LocalRejection(
                reason_code=PREFLIGHT_CAPABILITY_UNSUPPORTED,
                message=f"模型规格明确不支持 response_format: {kind}",
                model_identity=model_identity,
                normalized_endpoint=endpoint,
            )
        if support is None:
            # Unknown capability treated as incomplete spec
            if policy.unknown_spec == "passthrough":
                return PreflightDecision(
                    outcome="bypassed",
                    effective_output_tokens=intent.requested_output_tokens,
                    adjustments=(),
                    token_assessment=TokenAssessment(
                        value=None,
                        quality="unknown",
                        method="none",
                        revision="none",
                    ),
                )
            return LocalRejection(
                reason_code=PREFLIGHT_SPEC_UNKNOWN,
                message=f"模型规格缺少 response_format ({kind}) 支持声明：{model_identity}",
                model_identity=model_identity,
                normalized_endpoint=endpoint,
            )

    # 5. Token counting
    count_view = adapter.build_count_view(intent)
    try:
        assessment = counter.count_tokens(count_view)
    except Exception as exc:
        return LocalRejection(
            reason_code=PREFLIGHT_TOKEN_COUNT_UNCERTAIN,
            message=f"输入 Token 计数器执行异常：{exc}",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    if (
        assessment is None
        or not isinstance(assessment, TokenAssessment)
        or assessment.value is None
        or assessment.quality == "unknown"
    ):
        return LocalRejection(
            reason_code=PREFLIGHT_TOKEN_COUNT_UNCERTAIN,
            message=f"无法获取可靠的输入上下文计数：{model_identity}",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    if assessment.quality == "estimated" and policy.uncertain_tokens == "reject":
        return LocalRejection(
            reason_code=PREFLIGHT_TOKEN_COUNT_UNCERTAIN,
            message=f"策略拒绝估算的输入 Token 计数：{model_identity}",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    assessed_input = assessment.value

    # 6. Check independent max_input_tokens if defined
    if spec.max_input_tokens is not None and assessed_input > spec.max_input_tokens:
        return LocalRejection(
            reason_code=PREFLIGHT_INPUT_TOO_LARGE,
            message=f"输入 Token 评估值 ({assessed_input}) 超过 max_input_tokens 限制 ({spec.max_input_tokens})",
            model_identity=model_identity,
            normalized_endpoint=endpoint,
        )

    # 7. Context accounting
    if spec.context_accounting == "shared":
        remaining_context = spec.context_window - assessed_input - policy.safety_margin_tokens
        if remaining_context <= 0:
            return LocalRejection(
                reason_code=PREFLIGHT_INPUT_TOO_LARGE,
                message=f"输入 Token ({assessed_input}) 与安全余量 ({policy.safety_margin_tokens}) 超出上下文窗口 ({spec.context_window})",
                model_identity=model_identity,
                normalized_endpoint=endpoint,
            )

        allowed_output = min(spec.max_output_tokens, remaining_context)
        if allowed_output <= 0:
            return LocalRejection(
                reason_code=PREFLIGHT_INPUT_TOO_LARGE,
                message=f"可用输出空间为 0 或负数 ({allowed_output})",
                model_identity=model_identity,
                normalized_endpoint=endpoint,
            )

        if policy.mode == "validate":
            if intent.requested_output_tokens > allowed_output:
                return LocalRejection(
                    reason_code=PREFLIGHT_OUTPUT_LIMIT_EXCEEDED,
                    message=f"期望输出 ({intent.requested_output_tokens}) 超过允许上限 ({allowed_output})",
                    model_identity=model_identity,
                    normalized_endpoint=endpoint,
                )
            return PreflightDecision(
                outcome="validated",
                effective_output_tokens=intent.requested_output_tokens,
                adjustments=(),
                token_assessment=assessment,
            )

        elif policy.mode == "adapt":
            effective = min(intent.requested_output_tokens, allowed_output)
            if effective < intent.min_output_tokens:
                return LocalRejection(
                    reason_code=PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL,
                    message=f"裁剪后有效输出预算 ({effective}) 低于业务最低预算 ({intent.min_output_tokens})",
                    model_identity=model_identity,
                    normalized_endpoint=endpoint,
                )
            if effective < intent.requested_output_tokens:
                adjustments = (
                    PlanAdjustment(
                        field="max_tokens",
                        original=intent.requested_output_tokens,
                        effective=effective,
                        reason="context_and_spec_limit",
                    ),
                )
                outcome = "adapted"
            else:
                adjustments = ()
                outcome = "validated"

            return PreflightDecision(
                outcome=outcome,
                effective_output_tokens=effective,
                adjustments=adjustments,
                token_assessment=assessment,
            )
        else:
            raise ValueError(f"不支持的预检模式：{policy.mode}")

    # Unsupported context accounting
    if policy.unknown_spec == "passthrough":
        return PreflightDecision(
            outcome="bypassed",
            effective_output_tokens=intent.requested_output_tokens,
            adjustments=(),
            token_assessment=assessment,
        )

    return LocalRejection(
        reason_code=PREFLIGHT_SPEC_UNKNOWN,
        message=f"不支持的 context_accounting 规则：{spec.context_accounting}",
        model_identity=model_identity,
        normalized_endpoint=endpoint,
    )
