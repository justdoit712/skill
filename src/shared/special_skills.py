"""特定品种 Skill (special-skills) 纯规则与中性数据契约（src/shared/special_skills.py）。

负责特定品种专区（如 special-skills/*.json）的规范化、身份匹配与条目清洗纯规则。
零外部框架依赖，不导入 catalog、finder 或 infra，不访问文件或网络。
"""

from __future__ import annotations

from typing import Any

from src.shared.owned import normalize_owned_id


def normalize_special_skill_id(raw_id: str) -> str:
    """规范化特定品种 Skill ID。

    规则：
    1. 统一将 owner/repo:path 中的 owner 和 repo 规范化为小写；
    2. 去除整个 ID 的首尾空白符；
    3. 保留 path 的原始大小写，不合并大小写不同的仓库内文件路径；
    4. 异常格式容错处理，保障纯函数不因单条脏数据崩溃。
    """
    if not isinstance(raw_id, str):
        if raw_id is None:
            return ""
        return str(raw_id).strip()

    cleaned = raw_id.strip()
    if not cleaned:
        return ""

    if cleaned.lower().startswith("github:"):
        cleaned = cleaned[7:].strip()

    try:
        return normalize_owned_id(cleaned)
    except ValueError:
        if ":" in cleaned:
            repo_part, path_part = cleaned.split(":", 1)
            return f"{repo_part.strip().lower()}:{path_part.strip().lstrip('/')}"
        return cleaned.lower()


def is_special_skill(
    skill_id: str,
    special_ids: set[str] | list[str] | dict[str, Any] | None,
) -> bool:
    """判断某个 skill_id 是否归属于特定品种专区。

    若 special_ids 为空或 None，返回 False。
    匹配时执行严格规范化比较。
    """
    if not skill_id or not special_ids:
        return False
    normalized = normalize_special_skill_id(skill_id)
    if not normalized:
        return False
    if isinstance(special_ids, set):
        return normalized in special_ids
    if isinstance(special_ids, dict):
        return normalized in special_ids
    return normalized in set(special_ids)


def filter_out_special_skills(
    entries: list[dict[str, Any]],
    special_ids: set[str] | list[str] | dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], int]:
    """从条目列表中剔除属于特定品种专区的条目。

    返回 (保留条目列表, 剔除数量)。
    """
    if not special_ids:
        return list(entries), 0

    target_ids = special_ids if isinstance(special_ids, set) else set(special_ids)
    kept: list[dict[str, Any]] = []
    purged_count = 0
    for entry in entries:
        sid = entry.get("skill_id") or ""
        if is_special_skill(sid, target_ids):
            purged_count += 1
        else:
            kept.append(entry)
    return kept, purged_count


__all__ = [
    "normalize_special_skill_id",
    "is_special_skill",
    "filter_out_special_skills",
]
