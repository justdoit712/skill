"""文档与静态站点约束测试。

保留核心契约：
1. 文档本地相对链接有效性。
2. 静态页面数据入口有效性。
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests import smoke

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
DOC_FILES = ["README.md", "LICENSE-CONTENT.md"]
DOC_FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+?)\)")


class PublicSiteTest(unittest.TestCase):
    """公开静态资源与页面契约测试。"""

    @smoke
    def test_all_local_links_resolve(self) -> None:
        """检查所有项目 Markdown 文档中的本地链接是否均有效可解析。"""
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

    def test_loads_index_generated_page_data(self) -> None:
        """检查前端页面正确请求数据入口 data/catalog.json。"""
        raw_html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        js_files = sorted((PUBLIC / "js").glob("*.js"))
        js_content = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        html = raw_html + "\n" + js_content
        self.assertIn('fetch("data/catalog.json"', html)


if __name__ == "__main__":
    unittest.main()
