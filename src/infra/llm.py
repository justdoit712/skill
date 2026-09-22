"""LLM 模型调用基础设施通道。

提供 OpenAI 兼容的统一传输层，保留既有调用签名、凭据解析与超时重试语义。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import requests

DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_ATTEMPTS = 2
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
REASON_MODEL_ERROR = "MODEL_ERROR"
REASON_NETWORK_ERROR = "NETWORK_ERROR"


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

    @property
    def reasoning_tokens(self) -> int:
        details = (self.usage or {}).get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def total_tokens(self) -> int:
        return int((self.usage or {}).get("total_tokens") or 0)


def resolve_api_key(model_cfg: dict) -> str | None:
    """解析凭据：先环境变量，后本地私密配置文件。

    环境变量优先，这样部署环境（GitHub Secrets 注入）总能覆盖本地遗留值，
    不会被陈旧配置遮蔽。文件里的字面量只服务本机运行。
    """
    auth = model_cfg.get("auth") or {}
    env_name = auth.get("api_key_env") or "LLM_API_KEY"
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    literal = (auth.get("api_key") or "").strip()
    return literal or None


def api_key_source(model_cfg: dict) -> str:
    """凭据来源描述，用于报错与日志。**不输出凭据本身。**"""
    auth = model_cfg.get("auth") or {}
    env_name = auth.get("api_key_env") or "LLM_API_KEY"
    if (os.environ.get(env_name) or "").strip():
        return f"环境变量 {env_name}"
    if (auth.get("api_key") or "").strip():
        return "配置文件中的 api_key"
    return f"未设置（环境变量 {env_name} 与配置 api_key 均为空）"


def call_model(
    model_cfg: dict,
    system: str,
    user: str,
    *,
    api_key: str | None = None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> ModelCallResult:
    """通用 OpenAI 兼容传输通道：

    1. 保持现有调用签名不变（入参 model_cfg, system, user 及关键字参数）
    2. 保留从 model_cfg["request"] 中读取 timeout_seconds 与 max_attempts 的既有逻辑
    3. 保留目录现有的遇 408/429/5xx 自动重试语义（默认最多 2 次）
    4. 允许通过 session 和 sleep 注入假网络与时钟进行单元测试
    """
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
    response_format = request_cfg.get("response_format")
    if response_format:
        payload["response_format"] = {"type": response_format}

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    owns_session = session is None
    sess = session if session is not None else requests.Session()
    result = ModelCallResult(model=model_cfg.get("model"))
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
                result.error = f"{type(exc).__name__}: {exc}"
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
                    result.error = f"HTTP {status}: {body}"
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
            choice = (data.get("choices") or [{}])[0]
            result.finish_reason = choice.get("finish_reason")
            message = choice.get("message") or {}
            result.content = message.get("content")

            if result.finish_reason == "length":
                # 推理 token 与正文共用 max_tokens，额度不足时 content 可能为空且不报错
                result.ok = False
                result.reason_code = REASON_MODEL_ERROR
                result.error = (
                    f"输出被截断（finish_reason=length，max_tokens={max_output_tokens}，"
                    f"reasoning_tokens={result.reasoning_tokens}）"
                )
                result.notes.append("推理模型的推理 token 计入 max_tokens，截断结果不得采用")
                return result

            if not result.content:
                result.ok = False
                result.reason_code = REASON_MODEL_ERROR
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
