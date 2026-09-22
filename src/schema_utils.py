"""技能评估与描述结构化字段的规范化与校验工具。

已迁移至 src.shared.schema，本模块保持完全向后兼容重导出。
"""

from __future__ import annotations

from src.shared.schema import (
    VALID_SKILL_TYPES,
    normalize_skill_type,
    normalize_string_list,
)

__all__ = [
    "VALID_SKILL_TYPES",
    "normalize_skill_type",
    "normalize_string_list",
]
