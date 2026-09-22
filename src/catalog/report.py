"""目录周报与变更报告（§7.2）。

区分新增、内容变化、分类或链接修正、推荐状态变化、上游移除与采集失败。
"""

from __future__ import annotations

from src.report import (
    COLLECTION_FAILED,
    CONTENT_CHANGED,
    NEW,
    RECATEGORIZED,
    STATUS_CHANGED,
    UPSTREAM_REMOVED,
    build_report,
    render_report_markdown,
    write_report,
)

__all__ = [
    "NEW",
    "CONTENT_CHANGED",
    "RECATEGORIZED",
    "STATUS_CHANGED",
    "UPSTREAM_REMOVED",
    "COLLECTION_FAILED",
    "build_report",
    "render_report_markdown",
    "write_report",
]
