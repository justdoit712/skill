"""Declarative model specifications registry, validation, and immutable snapshots.

Maintains non-sensitive verified model hardware limits, context accounting, and format capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.shared.model_config import normalize_endpoint


@dataclass(frozen=True)
class ModelSpec:
    """Immutable verified model hardware specification."""
    spec_id: str
    revision: str
    provider: str
    endpoint: str
    model: str
    mode: str
    context_window: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    context_accounting: str
    output_budget_semantics: str
    response_formats: Mapping[str, bool | None]
    token_counter: str
    source: str
    verified_at: str

    def __init__(
        self,
        *,
        spec_id: str,
        revision: str,
        provider: str,
        endpoint: str,
        model: str,
        mode: str,
        context_window: int | None,
        max_input_tokens: int | None,
        max_output_tokens: int | None,
        context_accounting: str | None,
        output_budget_semantics: str | None,
        response_formats: Mapping[str, bool | None],
        token_counter: str | None,
        source: str,
        verified_at: str,
    ) -> None:
        if not spec_id or not isinstance(spec_id, str):
            raise ValueError("spec_id 必须为非空字符串")
        object.__setattr__(self, "spec_id", spec_id.strip())
        object.__setattr__(self, "revision", str(revision).strip())
        object.__setattr__(self, "provider", str(provider).strip())
        norm_endpoint = normalize_endpoint(endpoint)
        object.__setattr__(self, "endpoint", norm_endpoint)
        if not model or not isinstance(model, str) or not model.strip():
            raise ValueError("model 必须为非空字符串")
        object.__setattr__(self, "model", model.strip())
        object.__setattr__(self, "mode", str(mode).strip() if mode else "chat")

        if context_window is not None and (type(context_window) is not int or context_window <= 0):
            raise ValueError("context_window 必须为正整数")
        object.__setattr__(self, "context_window", context_window)

        if max_input_tokens is not None:
            if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int) or max_input_tokens <= 0:
                raise ValueError("max_input_tokens 必须为正整数")
        object.__setattr__(self, "max_input_tokens", max_input_tokens)

        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens <= 0):
            raise ValueError("max_output_tokens 必须为正整数")
        object.__setattr__(self, "max_output_tokens", max_output_tokens)

        for name, value in (('context_accounting', context_accounting),
                            ('output_budget_semantics', output_budget_semantics),
                            ('token_counter', token_counter)):
            if value is not None and not isinstance(value, str):
                raise ValueError(name + ' 必须为字符串或 None')
            object.__setattr__(self, name, (value or '').strip())

        if not isinstance(response_formats, (dict, Mapping)):
            raise ValueError("response_formats 必须为对象")
        frozen_formats: dict[str, bool | None] = {}
        for k, v in response_formats.items():
            if v is None:
                frozen_formats[str(k)] = None
            elif type(v) is bool:
                frozen_formats[str(k)] = v
            else:
                raise ValueError(f"response_formats[{k}] 必须为布尔值或 None（严格区分支持、不支持与未知）")
        object.__setattr__(self, "response_formats", MappingProxyType(frozen_formats))

        object.__setattr__(self, "source", str(source).strip())
        object.__setattr__(self, "verified_at", str(verified_at).strip())

    @classmethod
    def from_dict(cls, item: dict[str, Any], default_revision: str = "") -> ModelSpec:
        return cls(
            spec_id=item.get("spec_id", ""),
            revision=item.get("revision", default_revision),
            provider=item.get("provider", ""),
            endpoint=item.get("endpoint", ""),
            model=item.get("model", ""),
            mode=item.get("mode", "chat"),
            context_window=item.get("context_window"),
            max_input_tokens=item.get("max_input_tokens"),
            max_output_tokens=item.get("max_output_tokens"),
            context_accounting=item.get("context_accounting"),
            output_budget_semantics=item.get("output_budget_semantics"),
            response_formats=item.get("response_formats", {}),
            token_counter=item.get("token_counter"),
            source=item.get("source", ""),
            verified_at=item.get("verified_at", ""),
        )

    @property
    def match_key(self) -> tuple[str, str, str, str]:
        return (self.provider, self.endpoint, self.model, self.mode)


@dataclass(frozen=True)
class SpecSnapshot:
    """Immutable in-memory snapshot of model specifications loaded at startup."""
    schema_version: str
    revision: str
    digest: str
    specs: Mapping[tuple[str, str, str, str], ModelSpec]

    def __post_init__(self) -> None:
        if not isinstance(self.specs, Mapping):
            raise ValueError('specs 必须为规格映射')
        copied = dict(self.specs)
        if any(not isinstance(spec, ModelSpec) or key != spec.match_key
               for key, spec in copied.items()):
            raise ValueError('规格映射身份不匹配')
        object.__setattr__(self, 'specs', MappingProxyType(copied))

    def find(
        self,
        provider: str,
        endpoint: str,
        model: str,
        mode: str = "chat",
    ) -> ModelSpec | None:
        try:
            norm_endpoint = normalize_endpoint(endpoint)
        except Exception:
            return None
        return self.specs.get((str(provider).strip(), norm_endpoint, str(model).strip(), str(mode).strip()))


def load_specs(path: Path | str) -> SpecSnapshot:
    """Load, validate, and freeze declarative model specifications."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise ValueError(f"规格文件不存在：{file_path}")

    raw_bytes = file_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()[:16]

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"规格文件损坏或非合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("规格数据必须为 JSON 对象")

    schema_version = str(data.get("schema_version") or "")
    if not schema_version:
        raise ValueError("规格文件缺失 schema_version")
    revision = str(data.get("revision") or "")

    raw_specs = data.get("specs")
    if not isinstance(raw_specs, list):
        raise ValueError("规格文件 specs 必须为列表")

    specs_map: dict[tuple[str, str, str, str], ModelSpec] = {}
    for item in raw_specs:
        if not isinstance(item, dict):
            raise ValueError("规格项必须为对象")
        spec = ModelSpec.from_dict(item, default_revision=revision)
        key = spec.match_key
        if key in specs_map:
            raise ValueError(f"规格重复匹配冲突（同身份已存在）：{key}")
        specs_map[key] = spec

    return SpecSnapshot(
        schema_version=schema_version,
        revision=revision,
        digest=digest,
        specs=MappingProxyType(specs_map),
    )
