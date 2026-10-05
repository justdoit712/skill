"""Neutral request, plan, call result, and diagnostic contracts for LLM pipelines.

Pure domain types and invariants (no network, filesystem, or business logic dependencies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


def freeze_json(value: Any) -> Any:
    """Copy and deeply freeze JSON data, rejecting unsupported or nonfinite values."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('JSON 对象的键必须为字符串')
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError('只支持有限数值及有效 JSON 数据')


def thaw_json(value: Any) -> Any:
    """Create mutable JSON containers only at serialization boundaries."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value

# Preflight Reason Codes (Section 9.1)
PREFLIGHT_SPEC_UNKNOWN = "PREFLIGHT_SPEC_UNKNOWN"
PREFLIGHT_INPUT_TOO_LARGE = "PREFLIGHT_INPUT_TOO_LARGE"
PREFLIGHT_OUTPUT_LIMIT_EXCEEDED = "PREFLIGHT_OUTPUT_LIMIT_EXCEEDED"
PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL = "PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL"
PREFLIGHT_CAPABILITY_UNSUPPORTED = "PREFLIGHT_CAPABILITY_UNSUPPORTED"
PREFLIGHT_TOKEN_COUNT_UNCERTAIN = "PREFLIGHT_TOKEN_COUNT_UNCERTAIN"

PREFLIGHT_REASON_CODES = frozenset({
    PREFLIGHT_SPEC_UNKNOWN,
    PREFLIGHT_INPUT_TOO_LARGE,
    PREFLIGHT_OUTPUT_LIMIT_EXCEEDED,
    PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL,
    PREFLIGHT_CAPABILITY_UNSUPPORTED,
    PREFLIGHT_TOKEN_COUNT_UNCERTAIN,
})

# Standard Provider/Infra Reason Codes
REASON_MODEL_ERROR = "MODEL_ERROR"
REASON_RESPONSE_EMPTY = "RESPONSE_EMPTY"
REASON_NETWORK_ERROR = "NETWORK_ERROR"
REASON_LENGTH_EXCEEDED = "LENGTH_EXCEEDED"
REASON_RESPONSE_FORMAT_UNSUPPORTED = "RESPONSE_FORMAT_UNSUPPORTED"
REASON_MODEL_REQUEST_INCOMPATIBLE = "MODEL_REQUEST_INCOMPATIBLE"


@dataclass(frozen=True)
class TokenAssessment:
    """Token counting result and quality indicator."""
    value: int | None
    quality: str  # 'exact' | 'estimated' | 'unknown'
    method: str
    revision: str

    def __post_init__(self) -> None:
        if self.quality not in ("exact", "estimated", "unknown"):
            raise ValueError(f"token assessment quality 无效：{self.quality}")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
                raise ValueError("token assessment value 必须为非负整数")


@dataclass(frozen=True)
class PlanAdjustment:
    """Record of parameter clipping/adjustment by preflight adaptation."""
    field: str
    original: Any
    effective: Any
    reason: str


@dataclass(frozen=True)
class LocalRejection:
    """Preflight local rejection fact for a specific model candidate."""
    reason_code: str
    message: str
    model_identity: str
    normalized_endpoint: str

    def __post_init__(self) -> None:
        if self.reason_code not in PREFLIGHT_REASON_CODES:
            raise ValueError(f"未知的预检拒绝原因码：{self.reason_code}")


@dataclass(frozen=True)
class RequestIntent:
    """Business layer neutral request declaration."""
    messages: tuple[Mapping[str, str], ...]
    requested_output_tokens: int
    min_output_tokens: int = 1
    response_format: Any = None
    temperature: float = 0.0

    def __init__(
        self,
        messages: Any,
        requested_output_tokens: int,
        min_output_tokens: int = 1,
        response_format: Any = None,
        temperature: float = 0.0,
    ) -> None:
        if not messages or not isinstance(messages, (list, tuple)):
            raise ValueError("messages 必须为非空列表或元组")
        validated_messages: list[dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, Mapping) or set(msg) != {'role', 'content'}:
                raise ValueError("messages 中的项必须为包含 role 与 content 的字典")
            if not isinstance(msg['role'], str) or msg['role'] not in ('system', 'user', 'assistant', 'developer'):
                raise ValueError('不支持的消息 role')
            if not isinstance(msg['content'], str):
                raise ValueError('当前预检仅支持字符串 content')
            validated_messages.append(dict(msg))
        object.__setattr__(self, "messages", freeze_json(validated_messages))

        if (
            isinstance(requested_output_tokens, bool)
            or not isinstance(requested_output_tokens, int)
            or requested_output_tokens <= 0
        ):
            raise ValueError("requested_output_tokens 必须为正整数")
        object.__setattr__(self, "requested_output_tokens", int(requested_output_tokens))

        if (
            isinstance(min_output_tokens, bool)
            or not isinstance(min_output_tokens, int)
            or min_output_tokens <= 0
        ):
            raise ValueError("min_output_tokens 必须为正整数")
        if min_output_tokens > requested_output_tokens:
            raise ValueError(
                f"min_output_tokens ({min_output_tokens}) 不得大于 requested_output_tokens ({requested_output_tokens})"
            )
        object.__setattr__(self, "min_output_tokens", int(min_output_tokens))

        if response_format is not None:
            errs = validate_response_format(response_format)
            if errs:
                raise ValueError("非法 response_format：" + "; ".join(errs))
        object.__setattr__(self, "response_format", freeze_json(response_format))
        if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError('temperature 必须为 0 到 2 之间的有限数值')
        object.__setattr__(self, "temperature", float(temperature))


def validate_response_format(fmt: Any) -> list[str]:
    """校验 response_format 格式对象的合法性。"""
    if fmt is None:
        return []
    if isinstance(fmt, str):
        if fmt == "json_schema":
            return ["json_schema 必须使用包含 name 与 schema 的完整配置对象"]
        if fmt not in ("json_object", "text"):
            return [f"未知字符串 response_format 类型：{fmt}"]
        return []
    if not isinstance(fmt, Mapping):
        return ["response_format 必须是字符串或对象"]
    if set(fmt) - {'type', 'json_schema'}:
        return ['response_format 包含不支持字段']
    fmt_type = fmt.get("type")
    if not fmt_type:
        return ["response_format 对象缺少 type 字段"]
    if fmt_type == "json_schema":
        js = fmt.get("json_schema")
        if not isinstance(js, Mapping):
            return ["json_schema 类型必须包含 json_schema 配置对象"]
        if (set(js) - {'name', 'schema', 'strict', 'description'}
                or ('strict' in js and type(js['strict']) is not bool)
                or ('description' in js and not isinstance(js['description'], str))):
            return ['json_schema 包含不支持字段或字段类型错误']
        if not isinstance(js.get("name"), str) or not js["name"].strip():
            return ["json_schema 必须提供非空的 name"]
        if "schema" not in js or not isinstance(js["schema"], Mapping):
            return ["json_schema 必须提供有效的 schema 对象"]
    elif fmt_type not in ("json_object", "text"):
        return [f"不支持的 response_format type: {fmt_type}"]
    elif set(fmt) != {'type'}:
        return ['response_format 包含不支持字段']
    return []


@dataclass(frozen=True)
class RequestPlan:
    """Immutable final request execution plan consumed by budget, transport, and ledger."""
    model_identity: str
    normalized_endpoint: str
    encoded_body: bytes
    requested_output_tokens: int
    effective_output_tokens: int
    input_token_assessment: Mapping[str, Any]
    reservation_tokens: int
    reservation_method: str
    preflight_mode: str  # 'off' | 'validate' | 'adapt'
    preflight_outcome: str  # 'bypassed' | 'validated' | 'adapted'
    spec_revision: str | None = None
    spec_digest: str | None = None
    adapter_revision: str = "openai-compatible-v1"
    policy_revision: str = "preflight-policy-v1"
    adjustments: tuple[Mapping[str, Any], ...] = ()
    compatibility_fingerprint: str = ""

    def __init__(
        self,
        *,
        model_identity: str,
        normalized_endpoint: str,
        encoded_body: bytes,
        requested_output_tokens: int,
        effective_output_tokens: int,
        input_token_assessment: dict[str, Any] | TokenAssessment | Mapping[str, Any],
        reservation_tokens: int,
        reservation_method: str,
        preflight_mode: str,
        preflight_outcome: str,
        spec_revision: str | None = None,
        spec_digest: str | None = None,
        adapter_revision: str = "openai-compatible-v1",
        policy_revision: str = "preflight-policy-v1",
        adjustments: tuple[Any, ...] | list[Any] = (),
        compatibility_fingerprint: str = "",
    ) -> None:
        object.__setattr__(self, "model_identity", str(model_identity))
        object.__setattr__(self, "normalized_endpoint", str(normalized_endpoint))
        if not isinstance(encoded_body, bytes):
            raise ValueError("encoded_body 必须为 bytes")
        object.__setattr__(self, "encoded_body", encoded_body)

        if isinstance(requested_output_tokens, bool) or not isinstance(requested_output_tokens, int) or requested_output_tokens <= 0:
            raise ValueError("requested_output_tokens 必须为正整数")
        object.__setattr__(self, "requested_output_tokens", requested_output_tokens)

        if isinstance(effective_output_tokens, bool) or not isinstance(effective_output_tokens, int) or effective_output_tokens <= 0:
            raise ValueError("effective_output_tokens 必须为正整数")
        object.__setattr__(self, "effective_output_tokens", effective_output_tokens)

        if isinstance(input_token_assessment, TokenAssessment):
            assessment_dict = {
                "value": input_token_assessment.value,
                "quality": input_token_assessment.quality,
                "method": input_token_assessment.method,
                "revision": input_token_assessment.revision,
            }
        else:
            assessment_dict = dict(input_token_assessment)
        object.__setattr__(self, "input_token_assessment", freeze_json(assessment_dict))

        if isinstance(reservation_tokens, bool) or not isinstance(reservation_tokens, int) or reservation_tokens <= 0:
            raise ValueError("reservation_tokens 必须为正整数")
        object.__setattr__(self, "reservation_tokens", reservation_tokens)
        object.__setattr__(self, "reservation_method", str(reservation_method))

        if preflight_mode not in ("off", "validate", "adapt"):
            raise ValueError(f"preflight_mode 必须为 off/validate/adapt：{preflight_mode}")
        object.__setattr__(self, "preflight_mode", preflight_mode)

        if preflight_outcome not in ("bypassed", "validated", "adapted"):
            raise ValueError(f"preflight_outcome 必须为 bypassed/validated/adapted：{preflight_outcome}")
        object.__setattr__(self, "preflight_outcome", preflight_outcome)

        object.__setattr__(self, "spec_revision", spec_revision)
        object.__setattr__(self, "spec_digest", spec_digest)
        object.__setattr__(self, "adapter_revision", str(adapter_revision))
        object.__setattr__(self, "policy_revision", str(policy_revision))

        frozen_adjustments = tuple(
            freeze_json(adj) if isinstance(adj, Mapping) else freeze_json({
                "field": adj.field,
                "original": adj.original,
                "effective": adj.effective,
                "reason": adj.reason,
            })
            for adj in adjustments
        )
        object.__setattr__(self, "adjustments", frozen_adjustments)
        object.__setattr__(self, "compatibility_fingerprint", str(compatibility_fingerprint))


@dataclass(frozen=True)
class PreparationResult:
    """The result of prepare(): exactly one of plan or rejection."""
    plan: RequestPlan | None = None
    rejection: LocalRejection | None = None

    def __post_init__(self) -> None:
        if (self.plan is None and self.rejection is None) or (self.plan is not None and self.rejection is not None):
            raise ValueError("PreparationResult 必须且只能包含 plan 或 rejection 之一")

    @property
    def ok(self) -> bool:
        return self.plan is not None


@dataclass
class ModelCallResult:
    """Result of an individual model invocation attempt."""
    ok: bool = False
    content: str | None = None
    finish_reason: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 0
    reason_code: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    # Persistable safe diagnostic fields; does NOT include credentials or prompt body.
    error_type: str | None = None
    http_status: int | None = None
    is_sample_error: bool = False
    provider_error_code: str | None = None
    incompatible_parameter: str | None = None
    billing_state: str | None = None
    requested_model: str | None = None
    returned_model: str | None = None
    model_config_fingerprint: str | None = None
    compatibility_fingerprint: str | None = None

    @property
    def reasoning_tokens(self) -> int:
        details = (self.usage or {}).get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def total_tokens(self) -> int:
        return int((self.usage or {}).get("total_tokens") or 0)


def build_compatibility_fingerprint(
    *,
    model: str,
    endpoint: str,
    effective_output_tokens: int,
    response_format: Any = None,
    temperature: float = 0.0,
    roles: tuple[str, ...] = (),
    run_mode: str = "chat",
    preflight_mode: str = "off",
    spec_digest: str | None = None,
    spec_revision: str | None = None,
    adapter_revision: str = "openai-compatible-v1",
    policy_revision: str = "preflight-policy-v1",
) -> str:
    """Build canonical compatibility fingerprint (no secrets, no prompt text)."""
    fmt_summary: Any = None
    if isinstance(response_format, str):
        fmt_summary = {"type": response_format}
    elif isinstance(response_format, Mapping):
        fmt_type = response_format.get("type")
        if fmt_type == "json_schema":
            js = response_format.get("json_schema") or {}
            schema_data = js.get("schema") or {}
            schema_str = json.dumps(thaw_json(schema_data), sort_keys=True, separators=(",", ":"), allow_nan=False)
            schema_digest = hashlib.sha256(schema_str.encode("utf-8")).hexdigest()[:16]
            fmt_summary = {
                "type": "json_schema",
                "name": str(js.get("name") or ""),
                "schema_digest": schema_digest,
                "strict": bool(js.get("strict", False)),
            }
        else:
            fmt_summary = {"type": fmt_type}

    from src.shared.model_config import normalize_endpoint

    safe = {
        "model": str(model).strip(),
        "endpoint": normalize_endpoint(str(endpoint)),
        "effective_output_tokens": int(effective_output_tokens),
        "response_format": fmt_summary,
        "temperature": float(temperature),
        "roles": list(roles),
        "run_mode": str(run_mode),
        "preflight_mode": str(preflight_mode),
        "spec_digest": str(spec_digest or ""),
        "spec_revision": str(spec_revision or ""),
        "adapter_revision": str(adapter_revision),
        "policy_revision": str(policy_revision),
    }
    raw = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
