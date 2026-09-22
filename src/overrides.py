"""人工收藏区（overrides）配置加载、校验与标记注入。

依据 docs/产品规范.md §7.4 与 docs/运行说明.md §9。
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

DEFAULT_OVERRIDES_PATH = "config/overrides.json"
SKILL_ID_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+(:[^\s]+)?$")


def load_overrides(path: str | Path = DEFAULT_OVERRIDES_PATH) -> dict:
    """加载 overrides.json，不存在时返回默认结构。"""
    file_path = Path(path)
    if not file_path.exists():
        return {
            "overrides_version": "1.0.0",
            "manual_picks": [],
            "manual_exclusions": [],
        }
    return json.loads(file_path.read_text(encoding="utf-8"))


def validate_overrides(data: dict, known_skill_ids: set[str] | None = None) -> list[str]:
    """校验 overrides 数据结构与名单合法性（§3.2）。
    
    返回问题列表；为空表示校验通过。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["overrides 根结构必须是 JSON 对象"]

    version = data.get("overrides_version")
    if not version:
        data["overrides_version"] = "1.0.0"

    picks = data.get("manual_picks")
    if picks is None:
        errors.append("缺少 manual_picks 数组")
        return errors
    if not isinstance(picks, list):
        errors.append("manual_picks 必须是数组")
        return errors

    active_pick_ids: set[str] = set()
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
        else:
            active_pick_ids.add(skill_id)

    # 校验 manual_exclusions（人工排除黑名单）
    exclusions = data.get("manual_exclusions")
    active_exclusion_ids: set[str] = set()
    if exclusions is not None:
        if not isinstance(exclusions, list):
            errors.append("manual_exclusions 必须是数组")
        else:
            seen_ex_ids: set[str] = set()
            for idx, item in enumerate(exclusions):
                prefix = f"manual_exclusions[{idx}]"
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

                if skill_id in seen_ex_ids:
                    errors.append(f"manual_exclusions 中重复的 skill_id：{skill_id}")
                seen_ex_ids.add(skill_id)

                reason = item.get("reason")
                if not reason or not isinstance(reason, str) or not reason.strip():
                    errors.append(f"{prefix}（{skill_id}）的 reason 不能为空")

                added_at = item.get("added_at")
                if not added_at or not isinstance(added_at, str):
                    errors.append(f"{prefix}（{skill_id}）缺少合法的 added_at 日期字符串")
                else:
                    try:
                        datetime.strptime(added_at.strip(), "%Y-%m-%d")
                    except ValueError:
                        errors.append(f"{prefix}（{skill_id}）的 added_at 日期格式不合法（应为 YYYY-MM-DD）：{added_at}")

                retired_at = item.get("retired_at")
                if retired_at is not None:
                    if not isinstance(retired_at, str):
                        errors.append(f"{prefix}（{skill_id}）的 retired_at 必须是日期字符串")
                    else:
                        try:
                            datetime.strptime(retired_at.strip(), "%Y-%m-%d")
                        except ValueError:
                            errors.append(f"{prefix}（{skill_id}）的 retired_at 日期格式不合法（应为 YYYY-MM-DD）：{retired_at}")
                else:
                    active_exclusion_ids.add(skill_id)

    # 互斥性校验：同一个 skill_id 不能同时在 active picks 与 active exclusions 中
    conflict_ids = active_pick_ids.intersection(active_exclusion_ids)
    for cid in sorted(conflict_ids):
        errors.append(f"skill_id 同时存在于收藏区与排除区：{cid}")

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


def get_manual_exclusions(data: dict) -> dict[str, dict]:
    """提取活跃的人工排除黑名单映射表（排除带有 retired_at 的软删除条目）。"""
    if not isinstance(data, dict):
        return {}
    exclusions = data.get("manual_exclusions") or []
    result: dict[str, dict] = {}
    for item in exclusions:
        if isinstance(item, dict):
            sid = item.get("skill_id")
            if sid and not item.get("retired_at"):
                result[sid] = item
    return result


def apply_manual_overrides_to_entry(
    entry: dict,
    manual_picks: dict[str, dict],
    manual_exclusions: dict[str, dict] | None = None,
) -> dict:
    """根据人工收藏名单与排除黑名单更新单个条目的状态与标记（§4.1、§4.2）。"""
    sid = entry.get("skill_id")
    if manual_exclusions and sid in manual_exclusions:
        entry["status"] = "excluded"
        entry["manual_pick"] = False
        entry["manual_note"] = None
        entry["needs_review"] = False
        reasons = entry.setdefault("reason_codes", [])
        if "MANUAL_EXCLUDED" not in reasons:
            reasons.append("MANUAL_EXCLUDED")
        return entry

    pick = manual_picks.get(sid)
    if pick:
        entry["manual_pick"] = True
        entry["manual_note"] = {
            "added_at": pick.get("added_at"),
            "reason": pick.get("reason"),
            "from": pick.get("from"),
            "auto_status": entry.get("status"),
        }
        # §4.4: needs_review 对人工收藏条目不生效（改为页面中性提示）
        entry["needs_review"] = False
    else:
        entry["manual_pick"] = False
        entry["manual_note"] = None
    return entry


def apply_manual_overrides(
    entries: list[dict],
    data_or_picks: dict,
    exclusions: dict | None = None,
) -> list[dict]:
    """批量为条目注入人工干预标记（包含收藏与排除）。"""
    if "manual_picks" in data_or_picks or "manual_exclusions" in data_or_picks:
        manual_picks = get_manual_picks(data_or_picks)
        manual_exclusions = get_manual_exclusions(data_or_picks)
    else:
        manual_picks = data_or_picks
        manual_exclusions = exclusions or {}

    for entry in entries:
        apply_manual_overrides_to_entry(entry, manual_picks, manual_exclusions)
    return entries
