r"""测试运行器：支持分层运行（日常冒烟 vs 全量回归）。

用法：
    # 运行日常核心冒烟测试（约 50~80 项，3 秒内完成）：
    .\.venv\Scripts\python.exe tools/run_tests.py --smoke

    # 运行全量单元测试：
    .\.venv\Scripts\python.exe tools/run_tests.py

    # 详细模式与首次失败即停：
    .\.venv\Scripts\python.exe tools/run_tests.py -v -f
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
import sys
import time
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


def count_all_cases(suite: unittest.TestSuite) -> int:
    return suite.countTestCases()


class QuietTestResult(unittest.TextTestResult):
    """简洁的测试输出，避免刷屏并精准捕获失败。"""
    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.stream = stream

    def startTest(self, test):
        super().startTest(test)
        if self.showAll:
            self.stream.write(f"  {test.id()} ... ")
            self.stream.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        if self.showAll:
            self.stream.write("ok\n")
        elif self.dots:
            self.stream.write(".")
            self.stream.flush()

    def addError(self, test, err):
        super().addError(test, err)
        if self.showAll:
            self.stream.write("ERROR\n")
        elif self.dots:
            self.stream.write("E")
            self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        if self.showAll:
            self.stream.write("FAIL\n")
        elif self.dots:
            self.stream.write("F")
            self.stream.flush()

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        if self.showAll:
            self.stream.write(f"skipped: {reason}\n")
        elif self.dots:
            self.stream.write("s")
            self.stream.flush()


class QuietTestRunner(unittest.TextTestRunner):
    resultclass = QuietTestResult


def main() -> int:
    parser = argparse.ArgumentParser(description="本地单元测试分层运行器")
    parser.add_argument("-s", "--smoke", action="store_true", help="仅运行日常核心冒烟测试（日常高频使用）")
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

    mode_name = "日常核心冒烟测试 (Smoke Tests)" if args.smoke else "全量单元测试 (Full Regression)"
    if args.smoke:
        suite = filter_suite(suite, is_smoke_test)

    total_count = count_all_cases(suite)
    print(f"============================================================")
    print(f"模式: {mode_name}")
    print(f"用例: 共 {total_count} 项")
    print(f"============================================================")

    if total_count == 0:
        print("未匹配到任何测试用例。")
        return 0

    start_time = time.perf_counter()
    runner = QuietTestRunner(
        verbosity=2 if args.verbose else 1,
        failfast=args.failfast,
    )
    result = runner.run(suite)
    duration = time.perf_counter() - start_time

    print(f"\n============================================================")
    print(f"执行耗时: {duration:.3f} 秒")
    print(f"测试通过: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"测试失败: {len(result.failures)}")
    print(f"测试错误: {len(result.errors)}")
    print(f"测试跳过: {len(result.skipped)}")
    print(f"最终结果: {'[PASS] 全部通过' if result.wasSuccessful() else '[FAIL] 存在失败或错误'}")
    print(f"============================================================")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
