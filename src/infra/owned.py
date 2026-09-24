"""已收录配置持久化与基础设施读写适配（src/infra/owned.py）。

职责：
- 负责 config/owned-skills.json 的安全读取与原子写入；
- 文件缺失时优雅降级为空名单（兼容未配置仓库及测试夹具）；
- 文件内容损坏或格式非法时坚决报错终止，防止错误被静默吞掉；
- 配合 file_lock 保证变更包合并与保存的排他原子性。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.infra.files import file_lock, read_json, write_json_atomic
from src.shared.owned import (
    OWNED_SCHEMA_VERSION,
    apply_owned_patch,
    normalize_owned_id,
    validate_owned_config,
)

OWNED_CONFIG_FILENAME = "owned-skills.json"


def get_owned_config_path(config_dir: str | Path = "config") -> Path:
    """获取已收录配置文件路径。"""
    p = Path(config_dir)
    if p.name.endswith(".json"):
        return p
    sub = p / "governance" / OWNED_CONFIG_FILENAME
    flat = p / OWNED_CONFIG_FILENAME
    if flat.exists() and sub.exists():
        try:
            return flat if flat.stat().st_mtime >= sub.stat().st_mtime else sub
        except OSError:
            return flat
    if flat.exists():
        return flat
    if sub.exists():
        return sub
    return sub


def load_owned_config(
    config_dir: str | Path = "config",
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """读取并严格校验已收录名单配置。

    语义（方案 §4.1）：
    1. 文件不存在时按空名单处理，返回默认值或空配置；
    2. 文件存在但 JSON 损坏、版本不匹配或条目非法时直接抛出 ValueError 终止。
    """
    target = get_owned_config_path(config_dir)
    if not target.exists():
        if default is not None:
            return validate_owned_config(default)
        return {"schema_version": OWNED_SCHEMA_VERSION, "items": []}

    try:
        raw_data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"已收录配置文件 JSON 格式损坏：{target} ({exc})") from exc
    except OSError as exc:
        raise ValueError(f"读取已收录配置文件失败：{target} ({exc})") from exc

    return validate_owned_config(raw_data)


def load_owned_ids(config_dir: str | Path = "config") -> set[str]:
    """读取已收录技能的所有稳定 ID 集合（用于快速过滤）。"""
    config = load_owned_config(config_dir)
    return {item["skill_id"] for item in config.get("items", [])}


def save_owned_config(
    config: dict[str, Any],
    config_dir: str | Path = "config",
) -> Path:
    """原子写入已收录名单配置。

    写入前对配置执行严格强类型白名单校验。
    """
    validated = validate_owned_config(config)
    target = get_owned_config_path(config_dir)
    write_json_atomic(target, validated, indent=2)
    return target


def apply_and_save_owned_patch(
    patch_data: dict[str, Any],
    config_dir: str | Path = "config",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """加锁读取当前配置、应用变更包并原子写回。"""
    cfg_dir = Path(config_dir)
    lock_file = cfg_dir / ".owned.lock"

    with file_lock(lock_file, timeout=timeout):
        current = load_owned_config(cfg_dir)
        updated = apply_owned_patch(current, patch_data)
        save_owned_config(updated, cfg_dir)
        return updated


__all__ = [
    "OWNED_CONFIG_FILENAME",
    "get_owned_config_path",
    "load_owned_config",
    "load_owned_ids",
    "save_owned_config",
    "apply_and_save_owned_patch",
]
