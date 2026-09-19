#!/usr/bin/env python3
"""技能验证 CLI"""

import argparse
import os
import sys
import json

# 将 tools 目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_validator import SkillValidator, parse_skill_file, read_properties, to_prompt


def cmd_validate(args):
    """验证单个或批量技能"""
    validator = SkillValidator()

    if args.path:
        target = args.path
    else:
        target = os.path.join(os.path.dirname(__file__), "..", "..", "skills")
        target = os.path.normpath(target)

    # 如果是目录，批量验证
    if os.path.isdir(target):
        results = validator.validate_all(target)

        passed = sum(1 for r in results if r["valid"])
        warned = sum(1 for r in results if r["warnings"] and r["valid"])
        failed = sum(1 for r in results if not r["valid"])

        print(f"\n{'═' * 60}")
        print(f"  技能验证报告")
        print(f"{'═' * 60}")
        print(f"  总计: {len(results)} | 通过: {passed} | 警告: {warned} | 失败: {failed}")
        print()

        for r in results:
            status = "✅" if r["valid"] else "❌"
            name = r["name"]
            print(f"  {status} {name}")

            for err in r["errors"]:
                print(f"     ❌ {err}")
            for warn in r["warnings"][:3]:
                print(f"     ⚠️  {warn}")
            if len(r["warnings"]) > 3:
                print(f"     ... 还有 {len(r['warnings']) - 3} 个警告")

        print(f"\n{'═' * 60}")
        if failed == 0:
            print("  🎉 全部通过!")
        else:
            print(f"  ⚠️  有 {failed} 个技能需要修复")

        return 0 if failed == 0 else 1

    # 单文件验证
    elif os.path.isfile(target):
        # 自动查找嵌套结构
        if os.path.basename(target) != "SKILL.md":
            nested = os.path.join(target, os.path.basename(target), "SKILL.md")
            if os.path.isfile(nested):
                target = nested
            else:
                nested2 = os.path.join(target, "SKILL.md")
                if os.path.isfile(nested2):
                    target = nested2

        errors, warnings = validator.validate(target)

        print(f"\n文件: {target}")
        if not errors:
            print("✅ 验证通过!")
        else:
            print(f"❌ 验证失败 ({len(errors)} 个错误):")
            for e in errors:
                print(f"  ❌ {e}")

        if warnings:
            print(f"\n⚠️  警告 ({len(warnings)} 个):")
            for w in warnings:
                print(f"  ⚠️  {w}")

        return 0 if not errors else 1

    else:
        print(f"路径不存在: {target}")
        return 1


def cmd_read_properties(args):
    """读取技能属性"""
    target = args.path

    # 自动查找嵌套结构
    if os.path.isdir(target):
        nested = os.path.join(target, os.path.basename(target), "SKILL.md")
        if os.path.isfile(nested):
            target = nested
        else:
            nested2 = os.path.join(target, "SKILL.md")
            if os.path.isfile(nested2):
                target = nested2
            else:
                print(f"未找到 SKILL.md: {target}")
                return 1

    props = read_properties(target)

    if args.json:
        print(json.dumps(props, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'═' * 40}")
        print(f"  技能属性: {props.get('name', 'unknown')}")
        print(f"{'═' * 40}")
        for k, v in props.items():
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v) if v else "(empty)"
            print(f"  {k}: {v}")


def cmd_to_prompt(args):
    """转换为 XML prompt"""
    target = args.path

    # 自动查找嵌套结构
    if os.path.isdir(target):
        nested = os.path.join(target, os.path.basename(target), "SKILL.md")
        if os.path.isfile(nested):
            target = nested
        else:
            nested2 = os.path.join(target, "SKILL.md")
            if os.path.isfile(nested2):
                target = nested2
            else:
                print(f"未找到 SKILL.md: {target}")
                return 1

    xml = to_prompt(target)
    print(xml)


def main():
    parser = argparse.ArgumentParser(
        description="技能验证工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python skill_validator.py validate                    # 验证所有技能
  python skill_validator.py validate skills/my-skill    # 验证单个技能
  python skill_validator.py read-properties skills/my-skill  # 读取属性
  python skill_validator.py to-prompt skills/my-skill   # 生成 XML prompt
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # validate
    val_parser = subparsers.add_parser("validate", help="验证技能")
    val_parser.add_argument("path", nargs="?", default="", help="技能目录或文件路径")

    # read-properties
    rp_parser = subparsers.add_parser("read-properties", help="读取技能属性")
    rp_parser.add_argument("path", help="技能目录路径")
    rp_parser.add_argument("--json", action="store_true", help="输出 JSON 格式")

    # to-prompt
    tp_parser = subparsers.add_parser("to-prompt", help="生成 XML prompt")
    tp_parser.add_argument("path", help="技能目录路径")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "validate":
        sys.exit(cmd_validate(args))
    elif args.command == "read-properties":
        cmd_read_properties(args)
    elif args.command == "to-prompt":
        cmd_to_prompt(args)


if __name__ == "__main__":
    main()
