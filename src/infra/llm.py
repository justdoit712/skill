"""LLM 模型调用基础设施通道。

提供 OpenAI 兼容的统一传输层，保留既有调用签名、凭据解析与超时重试语义。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from urllib.parse import urlparse


def validate_model_config(config: dict) -> list[str]:
    if not isinstance(config, dict):
        return ["模型配置必须为对象"]
    if 'models' in config:
        from src.shared.model_config import parse_model_configs
        try:
            configs = parse_model_configs(config)
        except (ValueError, TypeError) as exc:
            return [str(exc)]
        return [error for item in configs for error in validate_model_config(item)]
    endpoint, model = config.get("endpoint"), config.get("model")
    if not isinstance(endpoint, str) or not isinstance(model, str) or not endpoint.strip() or not model.strip():
        return ["模型配置缺 endpoint 或 model"]
    host = (urlparse(endpoint).hostname or "").lower()
    if any(host == suffix or host.endswith('.' + suffix) for suffix in ("example.com", "example.org", "example.net")) or model.startswith("example-"):
        return ["示例模型连接不可用于请求，请配置真实 endpoint 和 model"]
    if urlparse(endpoint).scheme not in ("http", "https") or not host:
        return ["模型 endpoint 必须是有效的 HTTP(S) 地址"]
    return []

DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_ATTEMPTS = 2
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
REASON_MODEL_ERROR = "MODEL_ERROR"
REASON_RESPONSE_EMPTY = "RESPONSE_EMPTY"
REASON_NETWORK_ERROR = "NETWORK_ERROR"
REASON_LENGTH_EXCEEDED = "LENGTH_EXCEEDED"
REASON_RESPONSE_FORMAT_UNSUPPORTED = "RESPONSE_FORMAT_UNSUPPORTED"
REASON_MODEL_REQUEST_INCOMPATIBLE = "MODEL_REQUEST_INCOMPATIBLE"
INCOMPATIBLE_PARAMETERS = frozenset({
    'enable_thinking', 'max_tokens', 'max_completion_tokens',
    'response_format', 'json_schema', 'temperature', 'system_role',
})


def _rejection_token_counts(value):
    if not isinstance(value, dict):
        return None
    values = []
    for key, item in value.items():
        if key.endswith('_details'):
            nested = _rejection_token_counts(item)
            if nested is None:
                return None
            values.extend(nested)
        elif key.endswith('_tokens'):
            if type(item) is not int or item < 0:
                return None
            values.append(item)
    return values


def apply_parameter_rejection(result, data, config):
    """Recognize bounded DashScope validation errors, without persisting prose."""
    host = (urlparse(config.get('endpoint', '')).hostname or '').lower()
    if (result.http_status != 400 or config.get('provider') != 'dashscope'
            or not (host == 'aliyuncs.com' or host.endswith('.aliyuncs.com'))
            or not isinstance(data, dict) or not isinstance(data.get('error'), dict)):
        return
    error = data['error']
    codes = {'invalid_parameter_error', 'invalid_request_error', 'InvalidParameter',
             'InvalidParameterValue', 'InvalidParameter.InvalidParameterValue', 'unsupported_parameter'}
    identifiers = [error[k] for k in ('code', 'type') if k in error]
    if not identifiers or any(not isinstance(code, str) or code not in codes for code in identifiers):
        return
    message = error.get('message')
    message = message[:2048].lower() if isinstance(message, str) else ''
    parameter = error.get('param')
    if not isinstance(parameter, str) or parameter not in INCOMPATIBLE_PARAMETERS:
        parameter = None
        if any(word in message for word in ('must ', 'should ', 'not support', 'unsupported', 'invalid', 'range', 'only support')):
            parameter = next((name for name in sorted(INCOMPATIBLE_PARAMETERS)
                              if name != 'system_role' and re.search(r'\b' + name + r'\b', message)), None)
            if parameter is None and 'system' in message and 'role' in message and 'support' in message:
                parameter = 'system_role'
    if parameter is None:
        return
    # A validation rejection with content or nonzero/malformed usage cannot be
    # treated as free. Preserve its usage and leave the ordinary stop policy.
    usage = data.get('usage')
    counts = [] if usage is None else _rejection_token_counts(usage)
    result.provider_error_code = error.get('code') or identifiers[0]
    result.incompatible_parameter = parameter
    if counts is None or any(counts) or data.get('choices') or data.get('output'):
        result.reason_code = REASON_MODEL_ERROR
        result.error = f'模型参数拒绝响应存在用量或内容冲突（{parameter}）'
        return
    result.reason_code = REASON_MODEL_REQUEST_INCOMPATIBLE
    result.billing_state = 'rejected_before_inference'
    result.error = f'模型请求参数不兼容（{parameter}，{result.provider_error_code}）'


def apply_provider_error(result, data, config):
    """Classify only complete structured DashScope errors; never inspect prose."""
    if not isinstance(data, dict):
        return
    usage = data.get('usage')
    if isinstance(usage, dict):
        result.usage = usage
    error = data.get('error')
    if not isinstance(error, dict):
        return
    identifiers = [error[k] for k in ('code', 'type') if k in error]
    if not identifiers or any(not isinstance(v, str) or not v for v in identifiers):
        return
    if len(set(identifiers)) != 1:
        return
    code = identifiers[0]
    # Only a bounded safe code is diagnostic metadata, never an arbitrary body.
    if len(code) <= 100 and all(ch.isalnum() or ch in '._-' for ch in code):
        result.provider_error_code = code
    host = (urlparse(config.get('endpoint', '')).hostname or '').lower()
    if config.get('provider') != 'dashscope' or not (host == 'aliyuncs.com' or host.endswith('.aliyuncs.com')):
        return
    if code == 'Arrearage':
        result.reason_code = 'ACCOUNT_ERROR'
        result.error = '模型服务账户欠费，请检查账户状态'
        return
    if code != 'AllocationQuota.FreeTierOnly' or result.http_status not in (400, 403, 429):
        return
    counts = [] if usage is None else _rejection_token_counts(usage)
    if counts is None or any(counts) or data.get('choices'):
        result.reason_code = 'QUOTA_RESPONSE_CONFLICT'
        result.error = '额度拒绝响应存在用量或内容冲突'
        return
    result.reason_code = 'QUOTA_EXHAUSTED'
    result.billing_state = 'rejected_before_inference'
    result.error = '模型免费额度耗尽'


def validate_response_format(fmt: Any) -> list[str]:
    """校验 response_format 格式对象的合法性。"""
    if fmt is None:
        return []
    if isinstance(fmt, str):
        if fmt not in ("json_object", "text", "json_schema"):
            return [f"未知字符串 response_format 类型：{fmt}"]
        return []
    if not isinstance(fmt, dict):
        return ["response_format 必须是字符串或对象"]
    fmt_type = fmt.get("type")
    if not fmt_type:
        return ["response_format 对象缺少 type 字段"]
    if fmt_type == "json_schema":
        js = fmt.get("json_schema")
        if not isinstance(js, dict):
            return ["json_schema 类型必须包含 json_schema 配置对象"]
        if not js.get("name") or not isinstance(js.get("name"), str):
            return ["json_schema 必须提供非空的 name"]
        if "schema" not in js or not isinstance(js["schema"], dict):
            return ["json_schema 必须提供有效的 schema 对象"]
    elif fmt_type not in ("json_object", "text"):
        return [f"不支持的 response_format type: {fmt_type}"]
    return []


@dataclass
class ModelCallResult:
    """一次模型调用的结果。ok 与 content 是否可用是两件事。"""

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
    # 可安全持久化的诊断字段；不包含服务端正文、请求 URL 或认证头。
    error_type: str | None = None
    http_status: int | None = None
    is_sample_error: bool = False
    provider_error_code: str | None = None
    incompatible_parameter: str | None = None
    billing_state: str | None = None
    requested_model: str | None = None
    returned_model: str | None = None
    model_config_fingerprint: str | None = None

    @property
    def reasoning_tokens(self) -> int:
        details = (self.usage or {}).get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def total_tokens(self) -> int:
        return int((self.usage or {}).get("total_tokens") or 0)


from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_secrets(config_dir: str | Path | None = None) -> dict[str, str]:
    """读取本地独立密钥文件 config/secrets.local.json。"""
    candidates: list[Path] = []
    if config_dir:
        cd = Path(config_dir)
        candidates.extend([cd / "secrets.local.json", cd / "config" / "secrets.local.json"])
    candidates.append(ROOT / "config" / "secrets.local.json")

    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v).strip() for k, v in data.items() if v}
            except Exception:
                pass
    return {}


def resolve_api_key(model_cfg: dict, config_dir: str | Path | None = None) -> str | None:
    """解析凭据：先环境变量，次 secrets.local.json[key_ref]，后本地私密配置文件字面量。

    环境变量优先，这样部署环境（GitHub Secrets 注入）总能覆盖本地遗留值，
    不会被陈旧配置遮蔽。文件里的字面量与 key_ref 只服务本机运行。
    """
    auth = model_cfg.get("auth") or {}
    env_name = auth.get("api_key_env") or "LLM_API_KEY"
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value

    # 1. 尝试从独立密钥文件按 key_ref 解析
    key_ref = auth.get("key_ref")
    if key_ref:
        ref_env = os.environ.get(f"SKILL_SECRET_{str(key_ref).upper()}") or os.environ.get(f"{str(key_ref).upper()}_API_KEY")
        if ref_env and ref_env.strip():
            return ref_env.strip()
        secrets = load_secrets(config_dir)
        secret_val = secrets.get(str(key_ref))
        if secret_val:
            return secret_val

    # 2. 兼容历史配置中的直接内联 api_key 字面量
    literal = (auth.get("api_key") or "").strip()
    return literal or None


def api_key_source(model_cfg: dict, config_dir: str | Path | None = None) -> str:
    """凭据来源描述，用于报错与日志。**不输出凭据本身。**"""
    auth = model_cfg.get("auth") or {}
    env_name = auth.get("api_key_env") or "LLM_API_KEY"
    if (os.environ.get(env_name) or "").strip():
        return f"环境变量 {env_name}"

    key_ref = auth.get("key_ref")
    if key_ref:
        ref_env_name = f"SKILL_SECRET_{str(key_ref).upper()}"
        if (os.environ.get(ref_env_name) or "").strip():
            return f"环境变量 {ref_env_name} (对应 key_ref={key_ref})"
        secrets = load_secrets(config_dir)
        if str(key_ref) in secrets:
            return f"密钥文件 secrets.local.json[{key_ref}]"
        return f"未设置（secrets.local.json 缺少 '{key_ref}' 且环境变量均为空）"

    if (auth.get("api_key") or "").strip():
        return "配置文件中的 api_key"
    return f"未设置（环境变量 {env_name} 与配置 api_key 均为空）"


def call_model(
    model_cfg: dict,
    system: str,
    user: str,
    *,
    api_key: str | None = None,
    response_format: Any = None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> ModelCallResult:
    """通用 OpenAI 兼容传输通道：

    1. 保持现有调用签名不变（入参 model_cfg, system, user 及关键字参数）
    2. 保留从 model_cfg["request"] 中读取 timeout_seconds 与 max_attempts 的既有逻辑
    3. 保留目录现有的遇 408/429/5xx 自动重试语义（默认最多 2 次）
    4. 允许通过 session 和 sleep 注入假网络与时钟进行单元测试
    """
    if 'models' in model_cfg:
        raise ValueError('模型池必须通过有逐请求记账的调度器调用')
    key = api_key or resolve_api_key(model_cfg)
    if not key:
        return ModelCallResult(
            ok=False,
            reason_code=REASON_MODEL_ERROR,
            error="缺少凭据：" + api_key_source(model_cfg),
            notes=["无凭据时不调用模型，也不伪造中文简介与评估（§5.2）"],
        )

    request_cfg = model_cfg.get("request") or {}
    limits = model_cfg.get("limits") or {}
    timeout = float(request_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    max_attempts = int(request_cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    max_output_tokens = int(limits.get("max_output_tokens", 4000))

    payload = {
        "model": model_cfg.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(request_cfg.get("temperature", 0)),
        "max_tokens": max_output_tokens,
    }
    fmt = response_format if response_format is not None else request_cfg.get("response_format")
    if fmt:
        val_errs = validate_response_format(fmt)
        if val_errs:
            return ModelCallResult(
                ok=False,
                reason_code=REASON_MODEL_ERROR,
                error="response_format 校验失败：" + "; ".join(val_errs),
            )
        if isinstance(fmt, str):
            payload["response_format"] = {"type": fmt}
        elif isinstance(fmt, dict):
            payload["response_format"] = fmt

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    owns_session = session is None
    sess = session if session is not None else requests.Session()
    result = ModelCallResult(model=model_cfg.get("model"), requested_model=model_cfg.get('model'))
    started = time.monotonic()

    try:
        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            result.error_type = None
            result.http_status = None
            result.error = None
            try:
                response = sess.post(
                    model_cfg.get("endpoint"),
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                result.error_type = type(exc).__name__
                result.error = type(exc).__name__
                if attempt < max_attempts:
                    sleep(min(2.0 ** (attempt - 1), 8.0))
                    continue
                result.latency_ms = int((time.monotonic() - started) * 1000)
                result.reason_code = REASON_NETWORK_ERROR
                return result

            try:
                status = response.status_code
                result.http_status = status
                if status >= 400:
                    body = response.text[:300]
                    result.error = f"HTTP {status}"
                    try:
                        error_data = response.json()
                    except (ValueError, TypeError):
                        error_data = None
                    apply_provider_error(result, error_data, model_cfg)
                    if result.reason_code in ('QUOTA_EXHAUSTED', 'QUOTA_RESPONSE_CONFLICT', 'ACCOUNT_ERROR'):
                        result.latency_ms = int((time.monotonic() - started) * 1000)
                        return result
                    apply_parameter_rejection(result, error_data, model_cfg)
                    if result.reason_code == REASON_MODEL_REQUEST_INCOMPATIBLE:
                        result.latency_ms = int((time.monotonic() - started) * 1000)
                        return result
                    if status == 400 and any(keyword in body.lower() for keyword in ("response_format", "json_schema")):
                        result.latency_ms = int((time.monotonic() - started) * 1000)
                        result.reason_code = REASON_RESPONSE_FORMAT_UNSUPPORTED
                        return result
                    if status in RETRYABLE_STATUS and attempt < max_attempts:
                        response.close()
                        sleep(min(2.0 ** (attempt - 1), 8.0))
                        continue
                    result.latency_ms = int((time.monotonic() - started) * 1000)
                    result.reason_code = REASON_MODEL_ERROR
                    return result
                data = response.json()
            finally:
                response.close()

            result.latency_ms = int((time.monotonic() - started) * 1000)
            result.usage = data.get("usage") or {}
            result.returned_model = data.get('model') if isinstance(data.get('model'), str) else None
            choice = (data.get("choices") or [{}])[0]
            result.finish_reason = choice.get("finish_reason")
            message = choice.get("message") or {}
            result.content = message.get("content")

            if result.finish_reason == "length":
                # 推理 token 与正文共用 max_tokens，额度不足时 content 可能为空且不报错
                result.ok = False
                result.reason_code = REASON_LENGTH_EXCEEDED
                result.is_sample_error = True
                result.error = (
                    f"输出被截断（finish_reason=length，max_tokens={max_output_tokens}，"
                    f"reasoning_tokens={result.reasoning_tokens}）"
                )
                result.notes.append("推理模型的推理 token 计入 max_tokens，截断结果不得采用")
                return result

            if not result.content or (isinstance(result.content, str) and not result.content.strip()):
                result.ok = False
                result.reason_code = REASON_RESPONSE_EMPTY
                result.error = f"响应无内容（finish_reason={result.finish_reason}）"
                return result

            result.ok = True
            return result
    finally:
        if owns_session:
            sess.close()

    result.latency_ms = int((time.monotonic() - started) * 1000)
    result.reason_code = REASON_MODEL_ERROR
    result.error = "重试次数耗尽"
    return result
