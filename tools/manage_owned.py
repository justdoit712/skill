"""已收录技能配置管理 CLI 工具（tools/manage_owned.py）。

遵循《已收录 Skill 管理：详细实施方案》§7.1：
- 检查变更包：python tools/manage_owned.py --check-changes <patch_path>
- 合并变更并离线更新：python tools/manage_owned.py --apply-changes <patch_path>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.maintenance import sync_config_offline
from src.catalog.store import catalog_session
from src.infra.owned import (
    apply_and_save_owned_patch,
    get_owned_config_path,
    load_owned_config,
)
from src.shared.owned import (
    apply_owned_patch,
    validate_owned_config,
    validate_owned_patch,
)


def check_changes(
    patch_path: str | Path,
    config_dir: str | Path = "config",
    log_fn=print,
) -> int:
    """检查变更包格式及与当前配置的兼容性（不写任何文件）。"""
    p = Path(patch_path)
    if not p.exists():
        log_fn(f"错误：变更包文件不存在：{p}")
        return 1

    try:
        raw_patch = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log_fn(f"错误：变更包 JSON 解析失败：{exc}")
        return 1

    try:
        validated_patch = validate_owned_patch(raw_patch)
    except ValueError as exc:
        log_fn(f"错误：变更包校验失败：{exc}")
        return 1

    cfg_dir = Path(config_dir)
    try:
        current_cfg = load_owned_config(cfg_dir)
    except Exception as exc:
        log_fn(f"错误：读取当前配置失败：{exc}")
        return 1

    try:
        trial_result = apply_owned_patch(current_cfg, validated_patch)
        changes_count = len(validated_patch["changes"])
        log_fn(f"[OK] 变更包格式合法，包含 {changes_count} 项变更。")
        log_fn(f"当前已收录：{len(current_cfg.get('items', []))} 项；应用后预计：{len(trial_result.get('items', []))} 项。")
        return 0
    except ValueError as exc:
        log_fn(f"[ERROR] 变更包冲突或前置条件不满足：{exc}")
        return 1


def apply_changes(
    patch_path: str | Path,
    root_dir: str | Path = ".",
    config_dir: str | Path = "config",
    log_fn=print,
) -> int:
    """加锁应用变更包并离线更新目录索引与页面产物。"""
    p = Path(patch_path)
    if not p.exists():
        log_fn(f"错误：变更包文件不存在：{p}")
        return 1

    try:
        raw_patch = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log_fn(f"错误：变更包 JSON 解析失败：{exc}")
        return 1

    try:
        validated_patch = validate_owned_patch(raw_patch)
    except ValueError as exc:
        log_fn(f"错误：变更包校验失败：{exc}")
        return 1

    root = Path(root_dir)
    cfg_dir = Path(config_dir)
    data_dir = root / "data"

    # 使用目录级项目锁保护读取、写入与重建全过程
    try:
        with catalog_session(data_dir, timeout=10):
            # 1. 应用变更包并写入 config
            try:
                updated_cfg = apply_and_save_owned_patch(validated_patch, config_dir=cfg_dir)
                log_fn(f"[OK] 成功将变更包合入 {get_owned_config_path(cfg_dir)}，当前共 {len(updated_cfg.get('items', []))} 项已收录。")
            except ValueError as exc:
                log_fn(f"[ERROR] 变更包合入失败（前置条件冲突或格式错误）：{exc}")
                return 1

            # 2. 离线重建主索引与页面
            try:
                manifest = sync_config_offline(root_dir=root, owned_path=get_owned_config_path(cfg_dir))
                log_fn("[OK] 目录索引与页面数据离线同步完成（0 Token 消耗）。")
                counts = manifest.get("counts", {})
                log_fn(f"当前状态：推荐 {counts.get('recommended', 0)} · 候选 {counts.get('candidate', 0)} · 收藏 {counts.get('manual', 0)} · 已收录 {manifest.get('owned_count', 0)}")
                log_fn(f"主索引：{manifest.get('catalog_path')}")
                log_fn(f"页面数据：{manifest.get('page_path')}")
                return 0
            except Exception as sync_exc:
                log_fn(f"[WARN] 配置已写入 {get_owned_config_path(cfg_dir)}，但目录与页面离线生成失败（{sync_exc}）。")
                log_fn("请稍后运行 python tools/run_local.py --sync-config 手动重建产物。")
                return 1

    except Exception as lock_exc:
        log_fn(f"❌ 获取项目运行锁失败或进程冲突：{lock_exc}")
        return 1


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        description="已收录技能配置管理 CLI 工具（manage_owned.py）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check-changes",
        "--check",
        "--verify",
        dest="check_path",
        metavar="PATCH_FILE",
        help="检查变更包格式及与当前配置的兼容性（不写文件）",
    )
    group.add_argument(
        "--apply-changes",
        "--apply-patch",
        dest="apply_path",
        metavar="PATCH_FILE",
        help="合并变更包至 config/owned-skills.json 并离线重建索引与页面",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="列出当前所有已收录条目",
    )

    parser.add_argument(
        "--config-dir",
        default="config",
        help="配置文件目录（默认：config）",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="项目根目录（默认：.）",
    )

    args = parser.parse_args(argv)
    effective_root = Path(root) if root else Path(args.root)

    if args.list:
        cfg = load_owned_config(args.config_dir)
        items = cfg.get("items", [])
        print(f"当前已收录技能（共 {len(items)} 项）：")
        for it in items:
            print(f"- {it['skill_id']}: {it.get('name', '')} (收录于 {it.get('added_at', '')})")
        return 0

    if args.check_path:
        return check_changes(args.check_path, config_dir=args.config_dir)

    if args.apply_path:
        return apply_changes(
            args.apply_path,
            root_dir=effective_root,
            config_dir=args.config_dir,
        )

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
