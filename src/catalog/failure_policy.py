"""无 I/O 的错误分类、动作决策、计数器与停止优先级规则（§3.3, §4, §5.2）。

本模块为纯函数与数据结构，不进行任何文件读写、网络调用或模型交互。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# 错误类型常量 (§3.2)
ERROR_KIND_OUTPUT_JSON_INVALID = "OUTPUT_JSON_INVALID"
ERROR_KIND_OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
ERROR_KIND_RESUME_STATE_INVALID = "RESUME_STATE_INVALID"
ERROR_KIND_RESPONSE_EMPTY = "RESPONSE_EMPTY"
ERROR_KIND_LEGACY_PARSE_UNKNOWN = "LEGACY_PARSE_UNKNOWN"

# 错误原因码常量
REASON_LENGTH_EXCEEDED = "LENGTH_EXCEEDED"
REASON_PARSE_ERROR = "PARSE_ERROR"
REASON_RESUME_STATE_INVALID = "RESUME_STATE_INVALID"
REASON_ACCESS_DENIED = "ACCESS_DENIED"
REASON_REQUEST_CONFIG_ERROR = "REQUEST_CONFIG_ERROR"
REASON_NETWORK_ERROR = "NETWORK_ERROR"
REASON_MODEL_ERROR = "MODEL_ERROR"

# 候选动作常量 (§3.3)
ACTION_DONE = "done"
ACTION_LENGTH_EXCEEDED = "length_exceeded"
ACTION_BLOCKED = "blocked"
ACTION_RETRY = "retry"

# 停止原因常量 (§4.3)
STOP_INTERRUPTED = "interrupted"
STOP_STORAGE_ERROR = "storage_error"
STOP_USAGE_UNKNOWN = "usage_unknown"
STOP_RESUME_STATE_INVALID = "resume_state_invalid"
STOP_ACCESS_DENIED = "access_denied"
STOP_REQUEST_CONFIG_ERROR = "request_config_error"
STOP_TOKEN_LIMIT = "token_limit"
STOP_RETRY_EXHAUSTED = "retry_exhausted"
STOP_FORMAT_FAILURES = "format_failures"
STOP_MODEL_FAILURES = "model_failures"
STOP_TARGET_REACHED = "target_reached"
STOP_EVALUATION_LIMIT = "evaluation_limit"
STOP_CANDIDATES_EXHAUSTED = "candidates_exhausted"

# 停止原因优先级 (§4.3): 确定主停止原因
STOP_PRIORITY: list[str] = [
    STOP_INTERRUPTED,
    STOP_STORAGE_ERROR,
    STOP_RESUME_STATE_INVALID,
    STOP_ACCESS_DENIED,
    STOP_REQUEST_CONFIG_ERROR,
    STOP_TOKEN_LIMIT,
    STOP_RETRY_EXHAUSTED,
    STOP_USAGE_UNKNOWN,
    STOP_FORMAT_FAILURES,
    STOP_MODEL_FAILURES,
    STOP_TARGET_REACHED,
    STOP_EVALUATION_LIMIT,
    STOP_CANDIDATES_EXHAUSTED,
]

STOP_LABELS: dict[str, str] = {
    STOP_TARGET_REACHED: "已达成目标推荐数",
    STOP_TOKEN_LIMIT: "已达 Token 上限",
    STOP_EVALUATION_LIMIT: "已达评估次数上限",
    STOP_CANDIDATES_EXHAUSTED: "候选池已耗尽",
    STOP_MODEL_FAILURES: "模型连续响应失败过多（已触发熔断）",
    STOP_FORMAT_FAILURES: "输出格式异常达到阈值，请检查模型与评估输出契约",
    STOP_ACCESS_DENIED: "模型服务认证或权限失败 (401/403)",
    STOP_REQUEST_CONFIG_ERROR: "模型请求配置错误",
    STOP_RESUME_STATE_INVALID: "恢复状态或待复核数据冲突",
    STOP_USAGE_UNKNOWN: "模型调用的用量未知（Token 为空或无法计费）",
    STOP_RETRY_EXHAUSTED: "网络重试次数已耗尽",
    STOP_INTERRUPTED: "用户主动中断 (Ctrl+C)",
    STOP_STORAGE_ERROR: "持久化写入失败",
}


@dataclass
class ClassificationDecision:
    """对单次评估结果的纯策略分类。"""

    action: str
    category: str
    reason_code: str | None = None
    error_kind: str | None = None
    stage: str | None = None
    http_status: int | None = None
    block_reason: str | None = None
    stop_cause: str | None = None
    is_format_error: bool = False
    is_length_exceeded: bool = False
    is_service_failure: bool = False
    retryable: bool = False


def classify_result(result: dict[str, Any]) -> ClassificationDecision:
    """根据响应事实与评估结果进行无 I/O 的策略分类 (§3.3)。"""
    if result.get("ok"):
        return ClassificationDecision(
            action=ACTION_DONE,
            category="success",
            reason_code=None,
            error_kind=None,
            stage=result.get("stage"),
        )

    call = result.get("call")
    finish_reason = getattr(call, "finish_reason", None)
    reason_code = result.get("reason_code") or getattr(call, "reason_code", None)
    error_kind = result.get("error_kind")
    http_status = getattr(call, "http_status", None)
    stage = result.get("stage")

    # 1. 明确 length_exceeded (reason_code=LENGTH_EXCEEDED 或 finish_reason=length 且非显式 MODEL_ERROR)
    if reason_code == REASON_LENGTH_EXCEEDED or (
        finish_reason == "length" and reason_code != REASON_MODEL_ERROR
    ):
        return ClassificationDecision(
            action=ACTION_LENGTH_EXCEEDED,
            category="length_exceeded",
            reason_code=REASON_LENGTH_EXCEEDED,
            error_kind=error_kind or REASON_LENGTH_EXCEEDED,
            stage=stage,
            http_status=http_status,
            is_length_exceeded=True,
        )

    # 2. 401 / 403 访问认证/权限异常
    if http_status in (401, 403) or reason_code == REASON_ACCESS_DENIED:
        return ClassificationDecision(
            action=ACTION_BLOCKED,
            category="access_denied",
            reason_code=REASON_ACCESS_DENIED,
            error_kind="HTTP_401_403",
            stage=stage,
            http_status=http_status,
            block_reason="ACCESS_DENIED",
            stop_cause=STOP_ACCESS_DENIED,
        )

    # 3. 待复核恢复状态冲突
    if (
        reason_code == REASON_RESUME_STATE_INVALID
        or error_kind == ERROR_KIND_RESUME_STATE_INVALID
    ):
        return ClassificationDecision(
            action=ACTION_BLOCKED,
            category="resume_state_invalid",
            reason_code=REASON_RESUME_STATE_INVALID,
            error_kind=ERROR_KIND_RESUME_STATE_INVALID,
            stage=stage,
            http_status=http_status,
            block_reason="RESUME_STATE_INVALID",
            stop_cause=STOP_RESUME_STATE_INVALID,
        )

    # 4. 模型输出格式异常 (JSON 解码错误、结构检验失败)
    if (
        error_kind in (ERROR_KIND_OUTPUT_JSON_INVALID, ERROR_KIND_OUTPUT_SCHEMA_INVALID)
        or reason_code == REASON_PARSE_ERROR
    ):
        effective_error_kind = error_kind or ERROR_KIND_LEGACY_PARSE_UNKNOWN
        return ClassificationDecision(
            action=ACTION_BLOCKED,
            category="format_failure",
            reason_code=REASON_PARSE_ERROR,
            error_kind=effective_error_kind,
            stage=stage,
            http_status=http_status,
            block_reason="OUTPUT_FORMAT_INVALID",
            is_format_error=True,
        )

    # 5. 非可重试 4xx (排除 408, 429) 或请求配置错误
    if (
        http_status is not None
        and 400 <= http_status < 500
        and http_status not in (408, 429)
    ) or reason_code == REASON_REQUEST_CONFIG_ERROR:
        return ClassificationDecision(
            action=ACTION_BLOCKED,
            category="request_config_error",
            reason_code=REASON_REQUEST_CONFIG_ERROR,
            error_kind=f"HTTP_{http_status}" if http_status else "REQUEST_CONFIG_ERROR",
            stage=stage,
            http_status=http_status,
            block_reason="REQUEST_CONFIG_ERROR",
            stop_cause=STOP_REQUEST_CONFIG_ERROR,
        )

    # 6. 可重试网络/5xx/429
    retryable = (
        reason_code == REASON_NETWORK_ERROR
        or (http_status is not None and http_status in (408, 429, 500, 502, 503, 504))
    )
    if retryable:
        return ClassificationDecision(
            action=ACTION_RETRY,
            category="network_error",
            reason_code=reason_code or REASON_NETWORK_ERROR,
            error_kind=error_kind,
            stage=stage,
            http_status=http_status,
            is_service_failure=True,
            retryable=True,
        )

    # 7. 其他模型异常 / 正常结束无正文
    return ClassificationDecision(
        action=ACTION_BLOCKED,
        category="model_failure",
        reason_code=reason_code or REASON_MODEL_ERROR,
        error_kind=error_kind or (ERROR_KIND_RESPONSE_EMPTY if finish_reason else "MODEL_FAILURE"),
        stage=stage,
        http_status=http_status,
        block_reason="NON_RETRYABLE_FAILURE",
        is_service_failure=True,
    )


def update_failure_counters(
    result: dict[str, Any],
    decision: ClassificationDecision,
    consecutive_failures: int,
    format_failures: int,
) -> tuple[int, int]:
    """根据分类决策更新连续失败计数与独立格式异常计数 (§4.1)。

    规则表：
    - 有效业务评估 (ok): 通用连续失败清零，格式异常清零
    - 明确 length 响应: 通用连续失败清零，格式异常保持原值
    - 模型格式异常: 通用连续失败清零，格式异常加一
    - 通用服务/模型异常: 通用连续失败加一，格式异常保持原值
    - 其他阻断性停止异常: 保持原值
    """
    if result.get("ok"):
        return 0, 0

    if decision.is_length_exceeded:
        return 0, format_failures

    if decision.is_format_error:
        return 0, format_failures + 1

    if decision.is_service_failure:
        return consecutive_failures + 1, format_failures

    return consecutive_failures, format_failures


def resolve_primary_stop_reason(stop_causes: Iterable[str]) -> str | None:
    """根据优先级矩阵确定唯一主停止原因 (§4.3)。

    优先级：
    中断/持久化失败 -> 用量未知 -> 恢复状态冲突 -> 访问/配置错误 -> 总预算 -> 重试耗尽 ->
    格式异常阈值 -> 通用失败阈值 -> 正常目标/次数/候选结束。
    """
    cause_set = set(stop_causes)
    if not cause_set:
        return None
    for priority_cause in STOP_PRIORITY:
        if priority_cause in cause_set:
            return priority_cause
    # 如果有未在列表中的原因，返回任意一个
    return next(iter(cause_set))
