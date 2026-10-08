r"""测试运行器：支持分层运行（日常冒烟 vs 全量回归）。

用法：
    # 运行日常核心冒烟测试（约 10 项，1 秒内完成）：
    .\.venv\Scripts\python.exe tools/run_tests.py --smoke

    # 运行全量单元测试（约 32 项）：
    .\.venv\Scripts\python.exe tools/run_tests.py

    # 详细模式与首次失败即停：
    .\.venv\Scripts\python.exe tools/run_tests.py -v -f
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import is_smoke_test


def filter_suite(suite: unittest.TestSuite, predicate) -> unittest.TestSuite:
    """递归过滤 TestSuite 中的测试用例。"""
    filtered = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            sub = filter_suite(item, predicate)
            if sub.countTestCases() > 0:
                filtered.addTest(sub)
        elif predicate(item):
            filtered.addTest(item)
    return filtered


def main() -> int:
    parser = argparse.ArgumentParser(description="本地单元测试分层运行器")
    parser.add_argument("-s", "--smoke", action="store_true", help="仅运行日常核心冒烟测试（约 10 项）")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示每个测试方法的详细结果")
    parser.add_argument("-f", "--failfast", action="store_true", help="遇到第一个失败立即停止")
    parser.add_argument("-p", "--pattern", default="test_*.py", help="测试文件匹配模式 (默认: test_*.py)")
    parser.add_argument("tests", nargs="*", help="指定要运行的特定测试模块或类")
    args = parser.parse_args()

    loader = unittest.TestLoader()

    if args.tests:
        suite = loader.loadTestsFromNames(args.tests)
    else:
        suite = loader.discover(start_dir=str(ROOT / "tests"), pattern=args.pattern, top_level_dir=str(ROOT))

    if args.smoke:
        suite = filter_suite(suite, is_smoke_test)

    total_count = suite.countTestCases()
    mode_name = "日常核心冒烟测试 (Smoke Tests)" if args.smoke else "全量单元测试 (Full Regression)"
    print(f"============================================================")
    print(f"模式: {mode_name} | 用例: {total_count} 项")
    print(f"============================================================")

    if total_count == 0:
        print("未匹配到任何测试用例。")
        return 0

    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
        failfast=args.failfast,
    )
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
