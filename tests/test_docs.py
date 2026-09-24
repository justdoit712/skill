"""文档链接可达性：仓库内 Markdown 的本地链接必须真实存在。

此前是手工跑的检查；固定成测试后，删除或改名文档时若忘记更新引用会直接变红。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只检查仓库自身的文档；docs/ 下讨论历史删除的正文不属于链接
DOC_FILES = ["README.md", "CONTRIBUTING.md", "LICENSE-CONTENT.md"]
DOC_FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in sorted((ROOT / "docs").glob("*.md"))]

LINK = re.compile(r"\]\(([^)\s]+?)\)")


class DocLinkTest(unittest.TestCase):
    def test_all_local_links_resolve(self) -> None:
        broken: list[str] = []
        checked = 0
        for rel in DOC_FILES:
            path = ROOT / rel
            if not path.exists():
                broken.append(f"{rel}（文档本身不存在）")
                continue
            base = path.parent
            for target in LINK.findall(path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                checked += 1
                clean_target = re.sub(r":\d+$", "", target.split("#")[0])
                if not (base / clean_target).exists():
                    broken.append(f"{rel} -> {target}")
        self.assertGreater(checked, 0, "没有检查到任何本地链接")
        self.assertEqual(broken, [], "存在失效的本地链接：\n" + "\n".join(broken))

    def test_docs_dir_has_no_unexpected_files(self) -> None:
        """docs/ 根目录只保留产品规范、架构约定与运行说明。"""
        names = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
        self.assertEqual(
            names,
            [
                "产品规范.md",
                "架构约定.md",
                "运行说明.md",
            ],
            "docs/ 的文件集合发生变化；若是刻意调整，请同步更新本断言",
        )


if __name__ == "__main__":
    unittest.main()
