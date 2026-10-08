"""基于 AST 抽象语法树的物理分层架构守卫测试。

严格校验核心架构红线：
1. 双向绝缘：src/catalog/ 与 src/finder/ 绝对不得互相导入。
2. 底层纯净：src/infra/ 绝对不依赖 catalog 或 finder 业务包。
3. 数据中性：src/shared/ 零内部依赖（不导入 catalog, finder, infra）。
4. 入口单向：src/ 内部业务包绝对不得反向依赖命令行入口（tools 目录）。
5. 依赖无环：内部模块导入图无环。
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
import unittest

from tests import smoke

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
                if node.level > 0:
                    if node.level <= len(rel_parts):
                        resolved_base = ".".join(rel_parts[: len(rel_parts) - node.level + 1])
                        full_mod = f"{resolved_base}.{module}".strip(".")
                    else:
                        full_mod = module
                else:
                    full_mod = module

                targets.append(full_mod)
                for alias in node.names:
                    targets.append(f"{full_mod}.{alias.name}".strip("."))
                    targets.append(alias.name)

        return targets

    @smoke
    def test_layer_boundaries(self):
        """核心分层依赖红线守卫。"""
        # 1. catalog 与 finder 互不导入（双向绝对绝缘）
        for p in (SRC_DIR / "catalog").glob("**/*.py"):
            for imp in self._collect_imported_targets(p):
                self.assertFalse(
                    "src.finder" in imp or imp == "finder" or imp.startswith("finder."),
                    f"架构违规：catalog 模块 {p.relative_to(ROOT)} 非法导入了 finder: {imp}",
                )

        for p in (SRC_DIR / "finder").glob("**/*.py"):
            for imp in self._collect_imported_targets(p):
                self.assertFalse(
                    "src.catalog" in imp or imp == "catalog" or imp.startswith("catalog."),
                    f"架构违规：finder 模块 {p.relative_to(ROOT)} 非法导入了 catalog: {imp}",
                )

        # 2. infra 绝对不依赖 catalog 或 finder
        for p in (SRC_DIR / "infra").glob("**/*.py"):
            for imp in self._collect_imported_targets(p):
                self.assertFalse(
                    any(pkg in imp for pkg in ["catalog", "finder"]),
                    f"架构违规：infra 模块 {p.relative_to(ROOT)} 非法依赖了业务包: {imp}",
                )

        # 3. shared 零内部业务依赖
        for p in (SRC_DIR / "shared").glob("**/*.py"):
            for imp in self._collect_imported_targets(p):
                self.assertFalse(
                    any(pkg in imp for pkg in ["catalog", "finder", "infra"]),
                    f"架构违规：shared 模块 {p.relative_to(ROOT)} 包含非法依赖: {imp}",
                )

        # 4. 业务包代码绝对不得反向依赖 CLI 入口
        for p in SRC_DIR.glob("**/*.py"):
            for imp in self._collect_imported_targets(p):
                self.assertFalse(
                    imp == "tools" or imp.startswith("tools."),
                    f"架构违规：业务模块 {p.relative_to(ROOT)} 反向依赖了 CLI: {imp}",
                )

    def test_internal_import_graph_has_no_cycles(self):
        """导入图无循环依赖。"""
        paths = {
            ".".join(p.relative_to(ROOT).with_suffix("").parts): p
            for p in SRC_DIR.rglob("*.py")
            if p.name != "__init__.py"
        }
        graph = {name: set() for name in paths}
        for name, path in paths.items():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = (
                        resolve_name("." * node.level + (node.module or ""), name.rsplit(".", 1)[0])
                        if node.level
                        else node.module or ""
                    )
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


if __name__ == "__main__":
    unittest.main()
