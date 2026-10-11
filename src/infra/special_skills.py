"""特定品种 Skill (special-skills) 基础设施读写适配（src/infra/special_skills.py）。

负责 special-skills/*.json 目录及相关配置的安全读取与身份聚合。
严格遵循分层约定：仅依赖标准库及 src/shared，不依赖 catalog 或 finder 业务包。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.shared.special_skills import normalize_special_skill_id


def get_special_skills_dir(root_dir: str | Path = ".") -> Path:
    """获取特定品种专区目录路径。

    优先使用显式传入的 root_dir / "special-skills"。
    """
    root = Path(root_dir).resolve()
    target = root / "special-skills"
    if target.exists() and target.is_dir():
        return target
    # 向上逐级回退查找，避免孤立工作目录丢失关联
    for parent in [root] + list(root.parents):
        cand = parent / "special-skills"
        if cand.exists() and cand.is_dir():
            return cand
    return target


def load_special_skill_ids(
    special_skills_dir: str | Path | None = None,
    root_dir: str | Path = ".",
) -> set[str]:
    """读取所有特定品种 JSON 文件中登记的稳定 skill_id 集合。

    遍历 special-skills 目录下所有的 *.json 文件，收集 entries、items 列表中的 skill_id
    （两者同时存在时均收集），规范化并聚合去重后返回 set[str]。
    """
    target_dir = Path(special_skills_dir) if special_skills_dir else get_special_skills_dir(root_dir)
    special_ids: set[str] = set()

    if not target_dir.exists() or not target_dir.is_dir():
        return special_ids

    for json_file in sorted(target_dir.glob("*.json")):
        if not json_file.is_file():
            continue
        try:
            content = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        entries_to_scan: list[Any] = []
        if isinstance(content, dict):
            # entries 与 items 均尝试收集
            if isinstance(content.get("entries"), list):
                entries_to_scan.extend(content["entries"])
            if isinstance(content.get("items"), list):
                entries_to_scan.extend(content["items"])
        elif isinstance(content, list):
            entries_to_scan.extend(content)

        for item in entries_to_scan:
            if isinstance(item, dict) and item.get("skill_id"):
                norm = normalize_special_skill_id(item["skill_id"])
                if norm:
                    special_ids.add(norm)

    return special_ids


def load_special_skills(
    special_skills_dir: str | Path | None = None,
    root_dir: str | Path = ".",
) -> dict[str, dict[str, Any]]:
    """读取所有特定品种专区条目，返回以规范化 skill_id 为键的条目字典。"""
    target_dir = Path(special_skills_dir) if special_skills_dir else get_special_skills_dir(root_dir)
    items_by_id: dict[str, dict[str, Any]] = {}

    if not target_dir.exists() or not target_dir.is_dir():
        return items_by_id

    for json_file in sorted(target_dir.glob("*.json")):
        if not json_file.is_file():
            continue
        try:
            content = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        entries_to_scan: list[Any] = []
        category_name = ""
        if isinstance(content, dict):
            category_name = content.get("category", "")
            if isinstance(content.get("entries"), list):
                entries_to_scan.extend(content["entries"])
            if isinstance(content.get("items"), list):
                entries_to_scan.extend(content["items"])
        elif isinstance(content, list):
            entries_to_scan.extend(content)

        for item in entries_to_scan:
            if isinstance(item, dict) and item.get("skill_id"):
                norm = normalize_special_skill_id(item["skill_id"])
                if not norm:
                    continue
                copied = dict(item)
                if category_name and not copied.get("special_category"):
                    copied["special_category"] = category_name
                items_by_id[norm] = copied

    return items_by_id


__all__ = [
    "get_special_skills_dir",
    "load_special_skill_ids",
    "load_special_skills",
]
