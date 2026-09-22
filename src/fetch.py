"""抓取层：HTTP 取文本，带超时、重试与单条读取量上限。

已迁移至 src.infra.http，本模块保持完全向后兼容重导出。
"""

from __future__ import annotations

from src.infra.http import (
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    GONE_STATUS,
    REASON_HTTP_ERROR,
    REASON_NETWORK_ERROR,
    REASON_TRUNCATED,
    REASON_UPSTREAM_GONE,
    RETRYABLE_STATUS,
    USER_AGENT,
    FetchResult,
    fetch_text,
)

__all__ = [
    "REASON_UPSTREAM_GONE",
    "REASON_HTTP_ERROR",
    "REASON_NETWORK_ERROR",
    "REASON_TRUNCATED",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_BACKOFF_CAP_SECONDS",
    "GONE_STATUS",
    "RETRYABLE_STATUS",
    "USER_AGENT",
    "FetchResult",
    "fetch_text",
]
