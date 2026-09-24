"""临时冷冻（snooze）名单加载、校验、到期判定与标记注入。

依据 docs/运行说明.md §10 与实施方案。
核心规则：
1. 冷冻判定统一采用左闭右开：snoozed_at <= today < expires_at。
2. 默认冷冻期为 150 天，到期当天（expires_at）即刻解冻恢复。
3. 全链路统一使用 Asia/Shanghai (UTC+8) 时区计算日期。
4. 互斥校验只针对活跃冷冻项；过期记录不阻碍收藏或拉黑。
5. 解冻或撤销时，从条目中彻底清理残留的 snooze 字段。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

DEFAULT_SNOOZE_PATH = "config/governance/snoozed.json"
DEFAULT_SNOOZE_DAYS = 150
SKILL_ID_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+(:[^\s]+)?$")
SHANGHAI_TZ = timezone(timedelta(hours=8))


def now_shanghai_date() -> str:
    """获取当前 Asia/Shanghai 时区的 YYYY-MM-DD 日期字符串。"""
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")


def compute_expires_at(snoozed_at_str: str, days: int = DEFAULT_SNOOZE_DAYS) -> str:
    """计算冷冻到期日：snoozed_at + days 天。"""
    dt = datetime.strptime(snoozed_at_str.strip(), "%Y-%m-%d")
    exp = dt + timedelta(days=days)
    return exp.strftime("%Y-%m-%d")


def is_active_snooze(
    item: dict[str, Any],
    today_str: str | None = None,
    today: str | None = None,
) -> bool:
    """判断单个冷冻记录是否在活跃冷冻期内（snoozed_at <= today < expires_at）。"""
    snoozed_at = item.get("snoozed_at")
    expires_at = item.get("expires_at")
    if not snoozed_at or not expires_at:
        return False
    current_today = today or today_str or now_shanghai_date()
    # 字符串在 YYYY-MM-DD 格式下可直接比对字典序
    return str(snoozed_at).strip() <= current_today < str(expires_at).strip()


def load_snooze(path: str | Path = DEFAULT_SNOOZE_PATH) -> dict[str, Any]:
    """加载 snoozed.json，文件不存在时返回安全默认结构。"""
    file_path = Path(path)
    if file_path.name == "snoozed.json":
        alt = file_path.parent / "governance" / "snoozed.json" if "governance" not in file_path.parts else file_path.parent.parent / "snoozed.json"
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
            "snooze_version": "1.0.0",
            "default_snooze_days": DEFAULT_SNOOZE_DAYS,
            "snoozed": [],
        }
    return json.loads(file_path.read_text(encoding="utf-8"))


def validate_snooze(
    data: dict[str, Any],
    known_skill_ids: set[str] | None = None,
    active_pick_ids: set[str] | None = None,
    active_exclusion_ids: set[str] | None = None,
    overrides: dict[str, Any] | None = None,
    today_str: str | None = None,
    today: str | None = None,
) -> list[str]:
    """校验 snoozed 数据结构与名单合法性。

    注意：互斥校验仅针对【当前处于活跃冷冻期】的条目。
    已自然过期的历史记录不阻碍后续的手工收藏或拉黑。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["snoozed 根结构必须是 JSON 对象"]

    version = data.get("snooze_version")
    if not version:
        data["snooze_version"] = "1.0.0"

    items = data.get("snoozed")
    if items is None:
        errors.append("缺少 snoozed 数组")
        return errors
    if not isinstance(items, list):
        errors.append("snoozed 必须是数组")
        return errors

    if overrides:
        from .overrides import get_manual_exclusions, get_manual_picks
        if active_pick_ids is None:
            active_pick_ids = set(get_manual_picks(overrides).keys())
        if active_exclusion_ids is None:
            active_exclusion_ids = set(get_manual_exclusions(overrides).keys())

    current_today = today or today_str or now_shanghai_date()
    seen_ids: set[str] = set()

    for idx, item in enumerate(items):
        prefix = f"snoozed[{idx}]"
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

        if skill_id in seen_ids:
            errors.append(f"snoozed 中重复的 skill_id：{skill_id}")
        seen_ids.add(skill_id)

        if known_skill_ids is not None and skill_id not in known_skill_ids:
            errors.append(f"skill_id 未在当前索引或候选中找到：{skill_id}")

        reason = item.get("reason")
        if reason is not None and not str(reason).strip():
            errors.append(f"{prefix}（{skill_id}）的 reason 不能为空")

        snoozed_at = item.get("snoozed_at")
        snoozed_dt: datetime | None = None
        if not snoozed_at or not isinstance(snoozed_at, str):
            errors.append(f"{prefix}（{skill_id}）缺少合法的 snoozed_at 日期字符串")
        else:
            try:
                snoozed_dt = datetime.strptime(snoozed_at.strip(), "%Y-%m-%d")
            except ValueError:
                errors.append(f"{prefix}（{skill_id}）的 snoozed_at 格式不合法（应为 YYYY-MM-DD）：{snoozed_at}")

        expires_at = item.get("expires_at")
        expires_dt: datetime | None = None
        if not expires_at or not isinstance(expires_at, str):
            if snoozed_dt:
                days = item.get("days", DEFAULT_SNOOZE_DAYS)
                item["days"] = days
                item["expires_at"] = compute_expires_at(snoozed_at.strip(), days)
                expires_at = item["expires_at"]
                expires_dt = datetime.strptime(expires_at, "%Y-%m-%d")
            else:
                errors.append(f"{prefix}（{skill_id}）缺少合法的 expires_at 日期字符串")
        else:
            try:
                expires_dt = datetime.strptime(expires_at.strip(), "%Y-%m-%d")
            except ValueError:
                errors.append(f"{prefix}（{skill_id}）的 expires_at 格式不合法（应为 YYYY-MM-DD）：{expires_at}")

        if snoozed_dt and expires_dt and expires_dt <= snoozed_dt:
            errors.append(f"{prefix}（{skill_id}）的 expires_at 必须晚于 snoozed_at")

        # 仅对【处于活跃冷冻期】的条目做互斥校验
        if is_active_snooze(item, current_today):
            if active_pick_ids and skill_id in active_pick_ids:
                errors.append(f"活跃冷冻条目与 manual_picks 冲突：{skill_id}（收藏优先，请先从 snoozed 中移除该条目）")
            if active_exclusion_ids and skill_id in active_exclusion_ids:
                errors.append(f"活跃冷冻条目与 manual_exclusions 冲突：{skill_id}（黑名单优先，请先从 snoozed 中移除该条目）")

    return errors


def get_active_snoozed(
    data: dict[str, Any],
    today_str: str | None = None,
    today: str | None = None,
) -> dict[str, dict[str, Any]]:
    """提取当前处于活跃冷冻期的条目映射表（skill_id -> snooze_item）。"""
    if not isinstance(data, dict):
        return {}
    items = data.get("snoozed") or []
    current_today = today or today_str or now_shanghai_date()
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            sid = item.get("skill_id")
            if sid and is_active_snooze(item, current_today):
                result[sid] = item
    return result


def apply_snooze_overrides(
    entries: list[dict[str, Any]],
    snoozed_data_or_dict: dict[str, Any] | None,
    today_str: str | None = None,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """批量为条目注入或清理冷冻标记。

    若条目处于活跃冷冻期，注入 snooze 字段；
    若条目已过期或不在冷冻名单中，彻底清理条目上的 snooze 字段。
    """
    current_today = today or today_str or now_shanghai_date()
    if snoozed_data_or_dict is None:
        active_snoozed: dict[str, dict[str, Any]] = {}
    elif "snoozed" in snoozed_data_or_dict:
        active_snoozed = get_active_snoozed(snoozed_data_or_dict, current_today)
    else:
        active_snoozed = snoozed_data_or_dict

    for entry in entries:
        sid = entry.get("skill_id")
        if sid and sid in active_snoozed:
            item = active_snoozed[sid]
            entry["snooze"] = {
                "snoozed_at": item.get("snoozed_at"),
                "expires_at": item.get("expires_at"),
                "reason": item.get("reason", "当前用不到"),
            }
        else:
            entry.pop("snooze", None)
    return entries
