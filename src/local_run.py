"""本地按推荐数量收集；复用采集、筛选、决策与索引，不改 Actions 周额度。

向后兼容转发层：核心管道拆解与实现已迁入 src.catalog.local。
"""

from __future__ import annotations

from src.catalog.local import (
    STOP_LABELS,
    _collect,
    _read,
    _retryable,
    _unknown_usage_reserve,
    _valid_settings,
    apply_result,
    main,
    prepare_pool,
    run_local,
    save_and_render,
)

__all__ = [
    "STOP_LABELS",
    "_read",
    "_valid_settings",
    "_retryable",
    "_unknown_usage_reserve",
    "prepare_pool",
    "apply_result",
    "save_and_render",
    "run_local",
    "_collect",
    "main",
]


if __name__ == "__main__":
    import sys
    sys.exit(main())
