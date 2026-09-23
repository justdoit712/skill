"""基于 AST 抽象语法树的物理分层架构守卫测试。

严格校验 4 条架构红线：
1. 规则 1（双向绝缘）：src/catalog/ 与 src/finder/ 绝对不得互相导入。
2. 规则 2（底层纯净）：src/infra/ 绝对不依赖 catalog 或 finder 业务包。
3. 规则 3（数据中性）：src/shared/ 零业务依赖（不导入 catalog, finder, infra）。
4. 规则 4（入口单向）：src/ 内部业务包绝对不得反向依赖命令行入口（tools 目录及 src.pipeline）。
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from importlib.util import resolve_name

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"


class ArchitectureGuardTest(unittest.TestCase):
    """基于 AST 抽象语法树的物理分层红线守卫。"""

    def _collect_imported_targets(self, py_path: Path) -> list[str]:
        """完整解析单个 Python 文件的所有导入目标，消除别名与相对路径绕过。"""
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        targets: list[str] = []
        rel_parts = py_path.relative_to(SRC_DIR).parts[:-1]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # 解析相对导入（level > 0）
                if node.level > 0:
                    if node.level <= len(rel_parts):
                        resolved_base = ".".join(rel_parts[: len(rel_parts) - node.level + 1])
                        full_mod = f"{resolved_base}.{module}".strip(".")
                    else:
                        full_mod = module
                else:
                    full_mod = module

                targets.append(full_mod)
                # 同时捕获导入的具体成员（应对 from src import finder）
                for alias in node.names:
                    targets.append(f"{full_mod}.{alias.name}".strip("."))
                    targets.append(alias.name)

        return targets

    def test_catalog_and_finder_are_strictly_isolated(self):
        """规则 1：catalog 与 finder 互不导入（双向绝对绝缘）。"""
        for p in (SRC_DIR / "catalog").glob("**/*.py"):
            imps = self._collect_imported_targets(p)
            for imp in imps:
                self.assertFalse(
                    "src.finder" in imp or imp == "finder" or imp.startswith("finder."),
                    f"架构违规：catalog 模块 {p.relative_to(ROOT)} 非法导入了 finder: {imp}",
                )

        for p in (SRC_DIR / "finder").glob("**/*.py"):
            imps = self._collect_imported_targets(p)
            for imp in imps:
                self.assertFalse(
                    "src.catalog" in imp or imp == "catalog" or imp.startswith("catalog."),
                    f"架构违规：finder 模块 {p.relative_to(ROOT)} 非法导入了 catalog: {imp}",
                )

    def test_infra_never_imports_business_packages(self):
        """规则 2：infra 绝对不依赖 catalog 或 finder。"""
        for p in (SRC_DIR / "infra").glob("**/*.py"):
            imps = self._collect_imported_targets(p)
            for imp in imps:
                self.assertFalse(
                    any(pkg in imp for pkg in ["catalog", "finder"]),
                    f"架构违规：infra 模块 {p.relative_to(ROOT)} 非法依赖了业务包: {imp}",
                )

    def test_shared_has_zero_internal_dependencies(self):
        """规则 3：shared 零业务依赖。"""
        for p in (SRC_DIR / "shared").glob("**/*.py"):
            imps = self._collect_imported_targets(p)
            for imp in imps:
                self.assertFalse(
                    any(pkg in imp for pkg in ["catalog", "finder", "infra"]),
                    f"架构违规：shared 模块 {p.relative_to(ROOT)} 包含非法依赖: {imp}",
                )

    def test_business_code_never_imports_cli_entries(self):
        """规则 4：业务包代码绝对不得反向依赖 CLI 入口。"""
        for p in SRC_DIR.glob("**/*.py"):
            if p.name == "pipeline.py":  # 顶层 CLI 自身除外
                continue
            imps = self._collect_imported_targets(p)
            for imp in imps:
                self.assertFalse(
                    imp == "tools" or imp.startswith("tools.") or imp == "src.pipeline" or imp.startswith("src.pipeline."),
                    f"架构违规：业务模块 {p.relative_to(ROOT)} 反向依赖了 CLI: {imp}",
                )

    def test_internal_import_graph_has_no_cycles(self):
        paths = {".".join(p.relative_to(ROOT).with_suffix("").parts): p
                 for p in SRC_DIR.rglob("*.py") if p.name != "__init__.py"}
        graph = {name: set() for name in paths}
        for name, path in paths.items():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = resolve_name("." * node.level + (node.module or ""), name.rsplit(".", 1)[0]) if node.level else node.module or ""
                    targets = [base, *(base + "." + a.name for a in node.names)]
                else:
                    continue
                graph[name].update(target for target in targets if target in paths)
        visited = set()
        def visit(name, stack):
            self.assertNotIn(name, stack, "循环依赖：" + " -> ".join([*stack, name]))
            if name in visited:
                return
            for target in graph[name]:
                visit(target, [*stack, name])
            visited.add(name)
        for name in graph:
            visit(name, [])

    def test_pure_rules_do_not_call_io_or_import_runners(self):
        modules = ["catalog/index.py", "catalog/entry_state.py", "catalog/decide.py",
                   "catalog/enrich.py", "finder/evidence.py", "finder/plan.py", "finder/evaluation.py"]
        forbidden_calls = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "read_json", "write_json_atomic", "fetch_text", "call_model"}
        forbidden_imports = {"requests", "src.infra", "local", "run", "sync_evaluate", "sync_reserve", "maintenance", "store"}
        for module in modules:
            tree = ast.parse((SRC_DIR / module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    self.assertNotIn(name, forbidden_calls, f"{module}:{node.lineno}")
                if isinstance(node, ast.ImportFrom):
                    target = node.module or ""
                    self.assertFalse(any(target == name or target.startswith(name + ".") for name in forbidden_imports), f"{module}: {target}")

    def test_store_does_not_depend_on_use_cases(self):
        for target in self._collect_imported_targets(SRC_DIR / "catalog" / "store.py"):
            self.assertFalse(any(target.endswith("." + name) for name in ("local", "maintenance", "sync_evaluate", "sync_reserve")), target)


if __name__ == "__main__":
    unittest.main()
