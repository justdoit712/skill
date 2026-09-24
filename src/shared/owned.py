"""已收录 Skill 共享纯规则与数据契约（src/shared/owned.py）。

依据《已收录 Skill 管理：详细实施方案》：
- 纯数据校验、ID 规范化与严格比对；
- 变更包（Patch）校验与冲突合并；
- 白名单公共投影，严禁泄露私人字段；
- 零业务依赖：绝对不导入 catalog、finder 或 infra，不执行文件与网络 I/O。
"""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any
from urllib.parse import urlparse

OWNED_SCHEMA_VERSION = "1.0.0"
ALLOWED_ITEM_FIELDS = frozenset({"skill_id", "name", "source_url", "added_at"})
FORBIDDEN_PRIVATE_FIELDS = frozenset({"managed_url", "note", "private_details"})


class OwnedPatchConflictError(ValueError):
    """变更包前置条件与仓库当前配置冲突。"""

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


def normalize_owned_id(raw_id: str) -> str:
    """规范化已收录 Skill ID。

    规则（方案 §3.3）：
    1. 格式为 owner/repo 或 owner/repo:path；
    2. owner 和 repo 归一化为小写；
    3. path 保留原始大小写；
    4. 严禁绝对路径、盘符、上级目录（..）及空路径段；
    5. 仓库所有者与仓库名只允许英文字母、数字、短横线、下划线和点。
    """
    if not isinstance(raw_id, str):
        raise ValueError(f"skill_id 必须为字符串，收到：{type(raw_id).__name__}")
    cleaned = raw_id.strip()
    if not cleaned:
        raise ValueError("skill_id 不能为空")

    parts = cleaned.split(":")
    if len(parts) > 2:
        raise ValueError(f"skill_id 格式非法（最多包含一个冒号）：{raw_id}")

    repo_part = parts[0].strip()
    repo_segments = repo_part.split("/")
    if len(repo_segments) != 2 or not repo_segments[0] or not repo_segments[1]:
        raise ValueError(f"skill_id 仓库部分必须为 owner/repo 格式：{raw_id}")

    owner = repo_segments[0].strip().lower()
    repo = repo_segments[1].strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]+", owner) or not re.fullmatch(r"[a-z0-9_.-]+", repo):
        raise ValueError(f"skill_id 仓库所有者或仓库名包含非法字符：{raw_id}")

    if len(parts) == 1:
        return f"{owner}/{repo}"

    path_part = parts[1].strip()
    if not path_part:
        return f"{owner}/{repo}"

    # 路径安全性与有效性检查
    if path_part.startswith(("/", "\\")) or "\\" in path_part:
        raise ValueError(f"skill_id 路径不能为绝对路径或包含反斜杠：{raw_id}")
    if re.match(r"^[a-zA-Z]:", path_part):
        raise ValueError(f"skill_id 路径不能包含驱动器盘符：{raw_id}")

    segments = path_part.split("/")
    for seg in segments:
        if not seg or seg == ".":
            raise ValueError(f"skill_id 路径包含空段或当前目录段：{raw_id}")
        if seg == "..":
            raise ValueError(f"skill_id 路径不能包含上级目录（..）：{raw_id}")

    clean_path = "/".join(segments)
    return f"{owner}/{repo}:{clean_path}"


def _validate_date_str(val: str, field_name: str) -> str:
    """校验 YYYY-MM-DD 格式有效日期。"""
    if not isinstance(val, str) or not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", val):
        raise ValueError(f"{field_name} 必须为 YYYY-MM-DD 格式日期字符串")
    try:
        date.fromisoformat(val)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是合法日历日期：{val}") from exc
    return val


def _validate_source_url(val: Any) -> str | None:
    """校验可选公开来源链接。"""
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError("source_url 必须为字符串或 null")
    cleaned = val.strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except Exception as exc:
        raise ValueError(f"source_url 解析失败：{cleaned}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"source_url 必须是有效的 HTTP(S) 地址：{cleaned}")
    if parsed.username or parsed.password:
        raise ValueError("source_url 严禁包含凭据信息（用户名/密码）")
    return cleaned


def validate_owned_item(raw_item: Any) -> dict[str, Any]:
    """校验单条已收录条目并产出纯净白名单字典。

    严密防范：
    1. 必须包含合法的 skill_id、name、added_at；
    2. 严格拒绝 managed_url、note 等任何私人详情字段；
    3. source_url 必须为安全 HTTP(S) 链接；
    4. 剔除未知多余字段。
    """
    if not isinstance(raw_item, dict):
        raise ValueError(f"已收录条目必须为对象，收到：{type(raw_item).__name__}")

    # 隐私红线：坚决拦截私人字段
    leaked_private = set(raw_item.keys()).intersection(FORBIDDEN_PRIVATE_FIELDS)
    if leaked_private:
        raise ValueError(f"已收录配置严禁包含私人字段（{', '.join(sorted(leaked_private))}）")

    if "skill_id" not in raw_item:
        raise ValueError("已收录条目缺少必填字段 skill_id")
    normalized_id = normalize_owned_id(raw_item["skill_id"])

    name = raw_item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"已收录条目 [{normalized_id}] 的 name 必须为非空字符串")

    added_at = _validate_date_str(raw_item.get("added_at"), "added_at")
    source_url = _validate_source_url(raw_item.get("source_url"))

    item: dict[str, Any] = {
        "skill_id": normalized_id,
        "name": name.strip(),
        "added_at": added_at,
    }
    if source_url:
        item["source_url"] = source_url
    return item


def validate_owned_config(raw_config: Any) -> dict[str, Any]:
    """校验已收录名单全量配置（config/owned-skills.json）。

    规则：
    1. schema_version 必须为 1.0.0；
    2. items 为合法数组，不可包含重复 skill_id；
    3. 白名单输出。
    """
    if not isinstance(raw_config, dict):
        raise ValueError(f"已收录配置顶层必须为对象，收到：{type(raw_config).__name__}")

    version = raw_config.get("schema_version")
    if version != OWNED_SCHEMA_VERSION:
        raise ValueError(f"不支持的 schema_version：{version}（当前仅支持 {OWNED_SCHEMA_VERSION}）")

    items_raw = raw_config.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("已收录配置缺少 items 数组或类型错误")

    seen_ids: set[str] = set()
    cleaned_items: list[dict[str, Any]] = []

    for idx, raw_item in enumerate(items_raw):
        try:
            item = validate_owned_item(raw_item)
        except ValueError as exc:
            raise ValueError(f"第 {idx + 1} 个已收录条目校验失败：{exc}") from exc

        sid = item["skill_id"]
        if sid in seen_ids:
            raise ValueError(f"已收录名单包含重复的 skill_id：{sid}")
        seen_ids.add(sid)
        cleaned_items.append(item)

    result: dict[str, Any] = {
        "schema_version": OWNED_SCHEMA_VERSION,
        "items": cleaned_items,
    }
    if isinstance(raw_config.get("source"), str):
        result["source"] = raw_config["source"]
    if isinstance(raw_config.get("note"), str):
        result["note"] = raw_config["note"]
    return result


def is_skill_owned(skill_id: str, owned_ids: set[str] | dict[str, Any] | list[str]) -> bool:
    """判断指定技能是否在已收录名单中（纯函数，忽略大小写规范化差异）。"""
    if not skill_id or not owned_ids:
        return False
    try:
        normalized = normalize_owned_id(skill_id)
    except ValueError:
        return False

    if isinstance(owned_ids, set):
        return normalized in owned_ids
    if isinstance(owned_ids, dict):
        return normalized in owned_ids
    return normalized in set(owned_ids)


def validate_owned_patch(patch_data: Any) -> dict[str, Any]:
    """校验带前置条件的已收录变更包（Patch）。

    结构：
    {
      "schema_version": "1.0.0",
      "changes": [
        {
          "skill_id": "owner/repo:path",
          "before": null | { ... },
          "after": null | { ... }
        }
      ]
    }
    """
    if not isinstance(patch_data, dict):
        raise ValueError("变更包顶层必须为对象")
    if patch_data.get("schema_version") != OWNED_SCHEMA_VERSION:
        raise ValueError(f"变更包版本非法：{patch_data.get('schema_version')}")

    changes_raw = patch_data.get("changes")
    if not isinstance(changes_raw, list) or not changes_raw:
        raise ValueError("变更包缺少 changes 数组或数组为空")

    cleaned_changes: list[dict[str, Any]] = []
    seen_change_ids: set[str] = set()

    for idx, ch in enumerate(changes_raw):
        if not isinstance(ch, dict):
            raise ValueError(f"第 {idx + 1} 个变更必须为对象")

        raw_id = ch.get("skill_id")
        if not raw_id:
            raise ValueError(f"第 {idx + 1} 个变更缺少 skill_id")
        sid = normalize_owned_id(raw_id)
        if sid in seen_change_ids:
            raise ValueError(f"变更包中同一条目存在多次修改：{sid}")
        seen_change_ids.add(sid)

        before = ch.get("before")
        after = ch.get("after")

        if before is None and after is None:
            raise ValueError(f"变更 [{sid}] 的 before 与 after 不能同时为 null")

        clean_before = validate_owned_item(before) if before is not None else None
        clean_after = validate_owned_item(after) if after is not None else None

        if clean_before and clean_before["skill_id"] != sid:
            raise ValueError(f"变更 [{sid}] 的 before.skill_id 不匹配")
        if clean_after and clean_after["skill_id"] != sid:
            raise ValueError(f"变更 [{sid}] 的 after.skill_id 不匹配")

        cleaned_changes.append({
            "skill_id": sid,
            "before": clean_before,
            "after": clean_after,
        })

    return {
        "schema_version": OWNED_SCHEMA_VERSION,
        "changes": cleaned_changes,
    }


def _items_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """比较两个已收录条目的白名单核心字段是否完全一致。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return (
        a.get("skill_id") == b.get("skill_id")
        and a.get("name") == b.get("name")
        and a.get("source_url") == b.get("source_url")
        and a.get("added_at") == b.get("added_at")
    )


def apply_owned_patch(current_config: dict[str, Any] | None, patch_data: dict[str, Any]) -> dict[str, Any]:
    """将变更包应用到当前配置，支持前置条件比对、幂等与冲突拦截。

    原则（方案 §4.4）：
    1. before=null 表示预期不存在；
    2. after=null 表示预期删除；
    3. 若当前记录与 before 相等：应用 after；
    4. 若当前记录已等于 after：视为幂等成功，不报错；
    5. 若当前记录与 before 不一致且不等于 after：判定冲突，整批不写入，抛出 OwnedPatchConflictError。
    """
    base_config = current_config if current_config else {"schema_version": OWNED_SCHEMA_VERSION, "items": []}
    validated_config = validate_owned_config(base_config)
    validated_patch = validate_owned_patch(patch_data)

    items_map: dict[str, dict[str, Any]] = {
        item["skill_id"]: dict(item) for item in validated_config["items"]
    }

    conflicts: list[dict[str, Any]] = []

    # 第一轮：全量检查前置条件与冲突，确保要么整批成功，要么整批不写入
    for change in validated_patch["changes"]:
        sid = change["skill_id"]
        before = change["before"]
        after = change["after"]
        cur = items_map.get(sid)

        # 检查是否已幂等达到 after 状态
        if _items_equal(cur, after):
            continue

        if before is None:
            # 预期不存在，但当前已存在且与 after 不一致
            if cur is not None:
                conflicts.append({
                    "skill_id": sid,
                    "reason": "预期条目不存在，但仓库中已存在该条目",
                    "current": cur,
                    "expected_before": None,
                })
        else:
            # 预期存在特定值
            if cur is None:
                conflicts.append({
                    "skill_id": sid,
                    "reason": "预期条目存在，但仓库中已被删除或不存在",
                    "current": None,
                    "expected_before": before,
                })
            elif not _items_equal(cur, before):
                conflicts.append({
                    "skill_id": sid,
                    "reason": "仓库中条目当前内容与变更包预期前置条件不一致",
                    "current": cur,
                    "expected_before": before,
                })

    if conflicts:
        conflict_ids = ", ".join(c["skill_id"] for c in conflicts)
        raise OwnedPatchConflictError(f"变更包与当前配置存在冲突，整批不写入：{conflict_ids}", conflicts=conflicts)

    # 第二轮：前置条件全部满足，正式应用修改
    for change in validated_patch["changes"]:
        sid = change["skill_id"]
        after = change["after"]
        if after is None:
            items_map.pop(sid, None)
        else:
            items_map[sid] = dict(after)

    # 保持以 skill_id 升序排列，确保确定性
    sorted_items = [items_map[k] for k in sorted(items_map.keys())]

    result: dict[str, Any] = {
        "schema_version": OWNED_SCHEMA_VERSION,
        "items": sorted_items,
    }
    if "source" in validated_config:
        result["source"] = validated_config["source"]
    if "note" in validated_config:
        result["note"] = validated_config["note"]
    return result


def build_public_owned_projection(owned_config: dict[str, Any]) -> dict[str, Any]:
    """构建安全公开的已收录投影（嵌入 public/data/catalog.json）。

    绝对白名单，杜绝私人字段泄漏。
    """
    validated = validate_owned_config(owned_config)
    public_items = []
    for it in validated["items"]:
        item_proj: dict[str, Any] = {
            "skill_id": it["skill_id"],
            "name": it["name"],
            "added_at": it["added_at"],
        }
        if it.get("source_url"):
            item_proj["source_url"] = it["source_url"]
        public_items.append(item_proj)

    return {
        "schema_version": OWNED_SCHEMA_VERSION,
        "items": public_items,
    }


__all__ = [
    "OWNED_SCHEMA_VERSION",
    "OwnedPatchConflictError",
    "normalize_owned_id",
    "validate_owned_item",
    "validate_owned_config",
    "is_skill_owned",
    "validate_owned_patch",
    "apply_owned_patch",
    "build_public_owned_projection",
]
