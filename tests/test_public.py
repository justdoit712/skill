"""文档与静态站点约束测试。

合并自原有分散的 test_public.py 与 test_docs.py。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from tests import smoke

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
SITE_ROOT = "https://justdoit712.github.io/skill/"
PAGES = ["index.html", "styles.css"]
DOC_FILES = ["README.md", "LICENSE-CONTENT.md"]
DOC_FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+?)\)")


@smoke
class DocLinkTest(unittest.TestCase):
    """文档链接与文件存在性检查。"""
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
        names = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
        self.assertEqual(
            names,
            ["产品规范.md", "架构约定.md", "运行说明.md"],
            "docs/ 的文件集合发生变化；若是刻意调整，请同步更新本断言",
        )


@smoke
class NoUpstreamTracesTest(unittest.TestCase):
    """站点身份与内容范围。"""
    FORBIDDEN = {
        "旧域名": "miyucaicai",
        "上游署名": "anbeime",
        "站群标识": "TOPGO",
        "站群用词": "站群",
        "旧计数 182": "182",
        "旧计数 245": "245",
        "旧计数 2326": "2326",
        "旧英文页": "skills-en",
        "本地技能页": "local-skills",
        "聊天演示页": "chat-demo",
        "项目案例页": "projects.html",
    }

    def test_no_forbidden_traces(self) -> None:
        for name in PAGES:
            blob = (PUBLIC / name).read_text(encoding="utf-8")
            for label, needle in self.FORBIDDEN.items():
                with self.subTest(page=name, label=label):
                    self.assertNotIn(needle, blob, f"{name} 出现{label}")

    def test_no_orphan_promo_selectors_in_css(self) -> None:
        css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
        for selector in (".eco-grid", ".eco-link", ".contact-badge", ".qr-box"):
            self.assertNotIn(selector, css, f"styles.css 残留孤立样式 {selector}")

    def test_skills_page_is_merged_and_removed(self) -> None:
        self.assertFalse((PUBLIC / "skills.html").exists(), "skills.html 应已合并删除")


class IndexPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        js_files = sorted((PUBLIC / "js").glob("*.js"))
        js_content = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        cls.raw_html = raw_html
        cls.html = raw_html + "\n" + js_content

    def test_loads_index_generated_page_data(self) -> None:
        self.assertIn('fetch("data/catalog.json"', self.html)

    def test_has_search_and_filters(self) -> None:
        for element_id in ('id="q"', 'id="category"', 'id="source"', 'id="tab-candidate"'):
            self.assertIn(element_id, self.html)

    def test_no_custom_domain_cname(self) -> None:
        self.assertFalse((PUBLIC / "CNAME").exists())

    def test_relative_links_do_not_escape_public_dir(self) -> None:
        offenders = [
            m.group(1)
            for m in re.finditer(r'(?:href|src)="([^"]+)"', self.html)
            if m.group(1).startswith("../")
        ]
        self.assertEqual(offenders, [], f"存在跳出 public/ 的链接：{offenders}")


if __name__ == "__main__":
    unittest.main()
