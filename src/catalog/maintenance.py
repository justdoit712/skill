"""目录离线运维与轻量任务（落实 P3 设计规范）。

不依赖网络、0-Token、零模型依赖：
- sync_config_offline: 将 config/*.json (overrides, snoozed) 同步到 data 与 public/data
- enrich_catalog_offline: 从现有数据中提取形态、示例请求与亮点，不修改原中文简述
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.index import sync_config_to_catalog
from src.enrich import enrich_catalog


def sync_config_offline(
    root_dir: str | Path = ".",
    *,
    catalog_path: str | Path | None = None,
    public_catalog_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    snoozed_path: str | Path | None = None,
) -> dict[str, Any]:
    """纯离线同步配置规则到目录与页面公开数据（0-Token、无模型依赖）。"""
    return sync_config_to_catalog(
        root_dir=root_dir,
        catalog_path=catalog_path,
        public_catalog_path=public_catalog_path,
        overrides_path=overrides_path,
        snoozed_path=snoozed_path,
    )


def enrich_catalog_offline(
    root_dir: str | Path = ".",
) -> dict[str, Any]:
    """纯离线结构化增强（0-Token、无模型依赖）。"""
    return enrich_catalog(root_dir)


__all__ = [
    "sync_config_offline",
    "enrich_catalog_offline",
]
