"""Unified in-process LLM request preparation gateway.

Composes policy, spec matching, token counting, protocol encoding, and parameter adaptation into immutable RequestPlan.
Does NOT perform HTTP transmission, credential management, token budget reservation, retry, or model pool scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable

from src.shared.llm_contracts import (
    LocalRejection,
    PreparationResult,
    RequestIntent,
    RequestPlan,
    build_compatibility_fingerprint,
    thaw_json,
)
from src.shared.model_config import normalize_endpoint, parse_preflight_config
from src.infra.llm_adapters import OpenAIProtocolAdapter
from src.infra.model_specs import SpecSnapshot, load_specs
from src.infra.preflight import (
    PreflightDecision,
    PreflightPolicy,
    evaluate_preflight,
)


@dataclass
class PreflightRuntime:
    """Injected runtime dependencies for pure request preparation."""
    policy: PreflightPolicy
    adapter: Any = field(default_factory=OpenAIProtocolAdapter)
    counter: Any = None
    spec_snapshot: SpecSnapshot | None = None
    reservation_method: str = "utf8-bound-v1"
    estimate_reservation_fn: Callable[[RequestIntent, int], int] | None = None


def build_runtime(
    model_cfg: dict,
    config_dir: Path | str | None = None,
    spec_snapshot: SpecSnapshot | None = None,
) -> PreflightRuntime:
    """Build preflight runtime from configuration; bypasses spec loading when mode is off."""
    if not isinstance(model_cfg, dict):
        raise ValueError('模型配置必须为对象')
    policy = PreflightPolicy(**parse_preflight_config(model_cfg.get('preflight', {})))

    snapshot = spec_snapshot
    if snapshot is None and policy.mode != "off":
        if config_dir is None:
            config_dir = Path(__file__).resolve().parents[2] / "config"
        cd = Path(config_dir).resolve()
        candidates = [
            cd / "models" / "model_specs.json",
            cd / "model_specs.json",
        ]
        for path in candidates:
            if path.is_file():
                snapshot = load_specs(path)
                break
        if snapshot is None:
            raise ValueError(f"启用预检模式 ({policy.mode}) 但未找到规格文件 model_specs.json：{cd}")

    return PreflightRuntime(
        policy=policy,
        spec_snapshot=snapshot,
    )


def default_estimate_reservation(intent: RequestIntent, effective_output_tokens: int) -> int:
    """Conservative budget reservation based on UTF-8 bytes and effective output tokens."""
    schema_bytes = (
        len(json.dumps(thaw_json(intent.response_format), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if intent.response_format
        else 0
    )
    input_bytes = sum(len(m["content"].encode("utf-8")) for m in intent.messages) + schema_bytes + 256
    return input_bytes + effective_output_tokens


def prepare(
    intent: RequestIntent,
    effective_config: dict,
    runtime: PreflightRuntime | None = None,
) -> PreparationResult:
    """Pure preparation pipeline: intent -> preflight evaluation -> protocol encoding -> immutable plan.

    Mode='off' skips spec lookup/counting but shares encoding and estimation.
    Enabled modes require an explicitly built runtime; this function never loads files.
    In case of configuration errors (missing endpoint or model), fails fast with ValueError instead of guessing defaults.
    """
    if not isinstance(intent, RequestIntent):
        raise TypeError("intent 必须为 RequestIntent 实例")
    if not isinstance(effective_config, dict):
        raise TypeError("effective_config 必须为字典")

    raw_endpoint = effective_config.get("endpoint")
    if not raw_endpoint or not isinstance(raw_endpoint, str) or not raw_endpoint.strip():
        raise ValueError("effective_config 缺少有效的 endpoint")
    endpoint = normalize_endpoint(raw_endpoint)

    model = effective_config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("effective_config 缺少有效的 model")
    model = model.strip()

    pf = parse_preflight_config(effective_config.get('preflight', {}))
    if runtime is None:
        if pf['mode'] != 'off':
            raise ValueError('启用预检必须先显式调用 build_runtime 并复用 runtime')
        runtime = PreflightRuntime(policy=PreflightPolicy(**pf))

    provider = str(effective_config.get("provider") or "").strip() or "dashscope"

    spec = None
    snapshot = runtime.spec_snapshot if runtime.policy.mode != 'off' else None
    if snapshot is not None:
        spec = snapshot.find(provider, endpoint, model, "chat")

    decision = evaluate_preflight(
        intent=intent,
        spec=spec,
        counter=runtime.counter,
        adapter=runtime.adapter,
        policy=runtime.policy,
        model_identity=model,
        endpoint=endpoint,
    )

    if isinstance(decision, LocalRejection):
        return PreparationResult(rejection=decision)

    assert isinstance(decision, PreflightDecision)

    effective_output = decision.effective_output_tokens
    encoded_body = runtime.adapter.encode_request(intent, model, effective_output)

    if runtime.estimate_reservation_fn is not None:
        reservation_tokens = runtime.estimate_reservation_fn(intent, effective_output)
    else:
        reservation_tokens = default_estimate_reservation(intent, effective_output)

    spec_digest = snapshot.digest if snapshot is not None else None
    spec_revision = snapshot.revision if snapshot is not None else None
    adapter_rev = getattr(runtime.adapter, "revision", "openai-compatible-v1")
    policy_rev = "preflight-policy-v1"

    compat_fingerprint = build_compatibility_fingerprint(
        model=model,
        endpoint=endpoint,
        effective_output_tokens=effective_output,
        response_format=intent.response_format,
        temperature=intent.temperature,
        roles=tuple(message['role'] for message in intent.messages),
        run_mode="chat",
        preflight_mode=runtime.policy.mode,
        spec_digest=spec_digest,
        spec_revision=spec_revision,
        adapter_revision=adapter_rev,
        policy_revision=policy_rev,
    )

    plan = RequestPlan(
        model_identity=model,
        normalized_endpoint=endpoint,
        encoded_body=encoded_body,
        requested_output_tokens=intent.requested_output_tokens,
        effective_output_tokens=effective_output,
        input_token_assessment=decision.token_assessment,
        reservation_tokens=reservation_tokens,
        reservation_method=runtime.reservation_method,
        preflight_mode=runtime.policy.mode,
        preflight_outcome=decision.outcome,
        spec_revision=spec_revision,
        spec_digest=spec_digest,
        adapter_revision=adapter_rev,
        policy_revision=policy_rev,
        adjustments=decision.adjustments,
        compatibility_fingerprint=compat_fingerprint,
    )

    return PreparationResult(plan=plan)
