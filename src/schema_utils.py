"""技能评估与描述结构化字段的规范化与校验工具。

包含：
1. skill_type 英文枚举清洗（仅允许 {"tool_script", "guideline", "template", "reference"}，其余归为 None）。
2. 字符串数组清洗（normalize_string_list）：过滤空串、去除首尾空格、有序去重、限制单项长度与条数。
"""

from __future__ import annotations

from typing import Any

VALID_SKILL_TYPES = {"tool_script", "guideline", "template", "reference"}


def normalize_skill_type(val: Any) -> str | None:
    """归一化形态分类。仅在输入为合法英文枚举时返回，否则一律返回 None（不猜测）。"""
    if not val or not isinstance(val, str):
        return None
    cleaned = val.strip().lower()
    return cleaned if cleaned in VALID_SKILL_TYPES else None


def normalize_string_list(
    raw: Any,
    *,
    max_items: int = 3,
    max_length: int = 120,
) -> list[str]:
    """清洗字符串列表字段（如 example_requests, key_features）。

    规则：
    1. 允许单个字符串输入（自动转为单元素列表）；
    2. 仅保留字符串类型条目，非字符串元素直接忽略；
    3. 去除首尾空白，跳过纯空白字符串；
    4. 超过 max_length 的字符串截断；
    5. 保持原有顺序并去重；
    6. 最多返回 max_items 条。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []

    result: list[str] = []
    seen: set[str] = set()

    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= max_items:
            break

    return result
