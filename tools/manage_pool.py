"""候选池管理与离线运维工具（§7.1, §7.2）。

支持离线对账（reconcile）与显式恢复（resume）：
- python tools/manage_pool.py reconcile --dry-run
- python tools/manage_pool.py reconcile --apply
- python tools/manage_pool.py resume --evaluation-id "精确ID" --reason "原因" --dry-run
- python tools/manage_pool.py resume --evaluation-id "精确ID" --reason "原因" --apply
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.maintenance import reconcile_pool, resume_candidate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="候选池离线对账与显式恢复工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="子命令")

    # reconcile 子命令
    rec_parser = subparsers.add_parser("reconcile", help="离线比对本地候选池与账本记录")
    rec_group = rec_parser.add_mutually_exclusive_group()
    rec_group.add_argument("--dry-run", action="store_true", default=True, help="仅预览对账变更（默认）")
    rec_group.add_argument("--apply", action="store_true", help="应用对账结果并保存备份")

    # resume 子命令
    res_parser = subparsers.add_parser("resume", help="显式恢复被阻止的候选并授予额外尝试额度")
    res_parser.add_argument("--evaluation-id", required=True, help="要恢复的完整评估 ID")
    res_parser.add_argument("--reason", required=True, help="本次恢复的操作原因与说明")
    res_parser.add_argument("--extra-attempts", type=int, default=1, help="额外授予的尝试额度，默认 1")
    res_group = res_parser.add_mutually_exclusive_group()
    res_group.add_argument("--dry-run", action="store_true", default=True, help="仅预览恢复操作（默认）")
    res_group.add_argument("--apply", action="store_true", help="应用恢复并在账本中追加记录")

    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 1

    try:
        if args.subcommand == "reconcile":
            apply_mode = bool(args.apply)
            result = reconcile_pool(ROOT, apply=apply_mode)
            mode_desc = "应用模式 (--apply)" if apply_mode else "预览模式 (--dry-run)"
            print(f"[{mode_desc}] 候选池对账完成")
            print(f"可对账记录：{result['reconciled']} 条；待核实记录：{len(result['unverified'])} 条")
            if result.get("backup_path"):
                print(f"备份文件已生成：{result['backup_path']}")
            if result["changes"]:
                print("\n变更清单：")
                for ch in result["changes"]:
                    target = ch["target_status"]
                    print(f"  - #{ch['seq']} {ch['skill_id']} -> {target}（原因：{ch['reason']}）")
            if result["unverified"]:
                print("\n待核实清单（缺少指纹或身份不完全，留在池中待下次抓取）：")
                for un in result["unverified"]:
                    print(f"  - #{un['seq']} {un['skill_id']}（原因：{un['reason']}）")
            if not apply_mode and result["changes"]:
                print("\n提示：当前为预览模式，如需写入请运行：python tools/manage_pool.py reconcile --apply")
            return 0

        elif args.subcommand == "resume":
            apply_mode = bool(args.apply)
            result = resume_candidate(
                ROOT,
                evaluation_id=args.evaluation_id,
                reason=args.reason,
                extra_attempts=args.extra_attempts,
                apply=apply_mode,
            )
            mode_desc = "应用模式 (--apply)" if apply_mode else "预览模式 (--dry-run)"
            print(f"[{mode_desc}] 候选恢复操作：")
            print(f"  评估 ID: {result['evaluation_id']}")
            print(f"  技能 ID: {result['skill_id']} (序号 #{result['seq']})")
            print(f"  恢复原因: {result['reason']}")
            print(f"  尝试上限: {result.get('current_max_attempts', result.get('new_max_attempts') - result['extra_attempts'])} -> {result['new_max_attempts']} (+{result['extra_attempts']})")
            print(f"  目标状态: pending（允许在下一次运行中重试）")
            if result.get("backup_path"):
                print(f"  候选池备份: {result['backup_path']}")
            if not apply_mode:
                print("\n提示：当前为预览模式，如需确认恢复请执行：")
                print(f'  python tools/manage_pool.py resume --evaluation-id "{args.evaluation_id}" --reason "{args.reason}" --apply')
            return 0

    except Exception as exc:
        print(f"操作失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
