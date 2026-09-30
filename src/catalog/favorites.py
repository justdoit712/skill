"""人工收藏区（favorites.json）配置加载、校验与提取。

依据 docs/收藏与阶段过滤实施方案.md §2.1 与 docs/产品规范.md §7.4。
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

DEFAULT_FAVORITES_PATH = "config/governance/favorites.json"
SKILL_ID_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+(:[^\s]+)?$")


def load_favorites(path: str | Path = DEFAULT_FAVORITES_PATH) -> dict:
    """加载 favorites.json，不存在时返回默认空结构。"""
    file_path = Path(path)
    if file_path.name == "favorites.json":
        alt = (
            file_path.parent / "governance" / "favorites.json"
            if "governance" not in file_path.parts
            else file_path.parent.parent / "favorites.json"
        )
        if file_path.exists() and alt.exists():
            try:
                if alt.stat().st_mtime > file_path.stat().st_mtime:
                    file_path = alt
            except OSError:
                pass
        elif alt.exists() and not file_path.exists():
            file_path = alt

    if not file_path.exists():
        return {
            "favorites_version": "1.0.0",
            "manual_picks": [],
        }
    return json.loads(file_path.read_text(encoding="utf-8"))


def validate_favorites(data: dict, known_skill_ids: set[str] | None = None) -> list[str]:
    """校验 favorites 数据结构与条目合法性。

    返回问题列表；为空表示校验通过。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["favorites 根结构必须是 JSON 对象"]

    version = data.get("favorites_version")
    if not version:
        data["favorites_version"] = "1.0.0"

    picks = data.get("manual_picks")
    if picks is None:
        errors.append("缺少 manual_picks 数组")
        return errors
    if not isinstance(picks, list):
        errors.append("manual_picks 必须是数组")
        return errors

    seen_pick_ids: set[str] = set()
    for idx, item in enumerate(picks):
        prefix = f"manual_picks[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue

        skill_id = item.get("skill_id")
        if not skill_id or not isinstance(skill_id, str) or not skill_id.strip():
            errors.append(f"{prefix}.skill_id 不能为空")
            continue

        skill_id = skill_id.strip()
        if not SKILL_ID_PATTERN.match(skill_id):
            errors.append(f"{prefix}.skill_id 格式不合法（应为 owner/repo 或 owner/repo:path）：{skill_id}")

        if skill_id in seen_pick_ids:
            errors.append(f"重复的 skill_id：{skill_id}")
        seen_pick_ids.add(skill_id)

        # 校验已知 skill_id
        if known_skill_ids is not None and skill_id not in known_skill_ids:
            errors.append(f"skill_id 未在当前索引或候选中找到（拼写错误或该技能尚未被收录）：{skill_id}")

        # reason 必须非空
        reason = item.get("reason")
        if not reason or not isinstance(reason, str) or not reason.strip():
            errors.append(f"{prefix}（{skill_id}）的 reason 不能为空")

        # added_at 必须是合法日期
        added_at = item.get("added_at")
        if not added_at or not isinstance(added_at, str):
            errors.append(f"{prefix}（{skill_id}）缺少合法的 added_at 日期字符串")
        else:
            try:
                datetime.strptime(added_at.strip(), "%Y-%m-%d")
            except ValueError:
                errors.append(f"{prefix}（{skill_id}）的 added_at 日期格式不合法（应为 YYYY-MM-DD）：{added_at}")

        # retired_at 可选，若存在则校验日期格式
        retired_at = item.get("retired_at")
        if retired_at is not None:
            if not isinstance(retired_at, str):
                errors.append(f"{prefix}（{skill_id}）的 retired_at 必须是日期字符串")
            else:
                try:
                    datetime.strptime(retired_at.strip(), "%Y-%m-%d")
                except ValueError:
                    errors.append(f"{prefix}（{skill_id}）的 retired_at 日期格式不合法（应为 YYYY-MM-DD）：{retired_at}")

    return errors


def get_manual_picks(data: dict) -> dict[str, dict]:
    """提取活跃的人工收藏映射表（排除带有 retired_at 的软删除条目）。"""
    if not isinstance(data, dict):
        return {}
    picks = data.get("manual_picks") or []
    result: dict[str, dict] = {}
    for item in picks:
        if isinstance(item, dict):
            sid = item.get("skill_id")
            if sid and not item.get("retired_at"):
                result[sid] = item
    return result
