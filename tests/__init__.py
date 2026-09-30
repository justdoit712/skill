r"""测试包。用标准库 unittest，零新增外部依赖。

运行全量测试：.\.venv\Scripts\python.exe tools/run_tests.py
运行日常冒烟：.\.venv\Scripts\python.exe tools/run_tests.py --smoke
"""

def smoke(item):
    """装饰器：标记测试类或测试方法为日常冒烟核心测试。"""
    setattr(item, "__smoke__", True)
    return item


def is_smoke_test(test):
    """判断一个 TestCase 实例或测试方法是否标记为冒烟测试。"""
    method_name = getattr(test, "_testMethodName", "")
    method = getattr(test, method_name, None)
    if method and getattr(method, "__smoke__", False):
        return True
    return getattr(test.__class__, "__smoke__", False)
