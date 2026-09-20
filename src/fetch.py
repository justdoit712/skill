"""抓取层：HTTP 取文本，带超时、重试与单条读取量上限。

依据：
- §7.4 单条读取量、输出长度、调用次数和重试次数设置上限
- §7.2 步骤 1 未准备好不调用付费模型
- §6 / §7 上游移除与采集失败属不同状态，报告需区分

失败分类使用 config/rules.json 中 reason_codes.fetch 的词汇，本模块不另造一套。
抓取不占用 AI 名额（§7.3）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

# 与 config/rules.json 的 reason_codes.fetch 保持一致
REASON_UPSTREAM_GONE = "UPSTREAM_GONE"
REASON_HTTP_ERROR = "HTTP_ERROR"
REASON_NETWORK_ERROR = "NETWORK_ERROR"
REASON_TRUNCATED = "TRUNCATED"

# 默认值。单条上限按实测设定：SKILL.md 实测 24-63 KB，合集 README 到 130 KB，
# 取 256 KiB 可容下已知来源，同时防止异常大的响应拖垮批处理。
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_BYTES = 262_144
DEFAULT_CHUNK_BYTES = 65_536
DEFAULT_BACKOFF_CAP_SECONDS = 8.0

GONE_STATUS = frozenset({404, 410})
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

USER_AGENT = "skills-navigator/0.1 (+https://github.com/justdoit712/skill)"


@dataclass
class FetchResult:
    """一次抓取的结果。ok 与 truncated 是两件事，不可互相替代。"""

    url: str
    ok: bool = False
    status: int | None = None
    text: str | None = None
    bytes_read: int = 0
    truncated: bool = False
    attempts: int = 0
    reason_code: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_recommendation(self) -> bool:
        """截断的内容不足以支撑推荐结论，按 §5.2 应记为 unknown。"""
        return self.ok and not self.truncated and bool(self.text)


def _backoff_seconds(attempt: int, cap: float = DEFAULT_BACKOFF_CAP_SECONDS) -> float:
    """第 attempt 次尝试失败后的等待时间，指数退避并封顶。"""
    return min(2.0 ** (attempt - 1), cap)


def _read_capped(response: requests.Response, max_bytes: int) -> tuple[bytes, bool]:
    """读取响应体，超过 max_bytes 即停止。返回 (内容, 是否截断)。"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in response.iter_content(chunk_size=DEFAULT_CHUNK_BYTES):
        if not chunk:
            continue
        remaining = max_bytes - total
        if len(chunk) >= remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def fetch_text(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    sleep=time.sleep,
) -> FetchResult:
    """取一个 URL 的文本。

    - 只对网络错误、超时与可重试状态码重试，最多 max_attempts 次尝试
    - 404/410 判为 UPSTREAM_GONE（条目状态），其余 4xx/5xx 判为 HTTP_ERROR（采集失败）
    - 读取超过 max_bytes 即停止并标记 truncated，不静默丢弃
    """
    if max_attempts < 1:
        raise ValueError("max_attempts 必须 >= 1")
    if max_bytes < 1:
        raise ValueError("max_bytes 必须 >= 1")

    owns_session = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

    result = FetchResult(url=url)

    try:
        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt

            try:
                response = sess.get(url, timeout=timeout, stream=True, allow_redirects=True)
            except requests.exceptions.RequestException as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                if attempt < max_attempts:
                    sleep(_backoff_seconds(attempt))
                    continue
                result.reason_code = REASON_NETWORK_ERROR
                result.notes.append("网络或超时失败，属采集失败，不作质量判定")
                return result

            try:
                status = response.status_code
                result.status = status

                if status in GONE_STATUS:
                    result.reason_code = REASON_UPSTREAM_GONE
                    result.error = f"HTTP {status}"
                    result.notes.append("上游已移除或不可访问：属条目状态，不是质量判定")
                    return result

                if status >= 400:
                    result.error = f"HTTP {status}"
                    if status in RETRYABLE_STATUS and attempt < max_attempts:
                        response.close()
                        sleep(_backoff_seconds(attempt))
                        continue
                    result.reason_code = REASON_HTTP_ERROR
                    result.notes.append("HTTP 状态异常，属采集失败")
                    return result

                raw, truncated = _read_capped(response, max_bytes)
                result.bytes_read = len(raw)
                result.text = raw.decode("utf-8", errors="replace")
                result.ok = True
                result.truncated = truncated
                if truncated:
                    result.notes.append(
                        f"内容超过单条读取量上限 {max_bytes} 字节，已截断；"
                        f"截断内容不得用于推荐结论（{REASON_TRUNCATED}）"
                    )
                return result
            finally:
                response.close()
    finally:
        if owns_session:
            sess.close()

    # 循环正常结束只可能在重试耗尽时发生，兜底标记为网络失败
    result.reason_code = REASON_NETWORK_ERROR
    result.notes.append("重试次数耗尽")
    return result
