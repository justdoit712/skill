"""public/ 页面约束：站点身份、内容范围与统计必须符合产品规范。

这些断言对应产品规范 §8 与 §7.1：发布地址是项目的 Pages 子路径，
不包含无关域名或推广入口，也不使用与实际不同步的固定计数。
"""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
SITE_ROOT = "https://justdoit712.github.io/skill/"

PAGES = ["index.html", "styles.css", "robots.txt", "sitemap.xml"]


class NoUpstreamTracesTest(unittest.TestCase):
    """产品规范 §7.1 与 §8：站点身份和页面内容范围一致。"""

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
        """产品规范 §8：使用主导航，不维护重复的技能列表入口。"""
        self.assertFalse((PUBLIC / "skills.html").exists(), "skills.html 应已合并删除")


class IndexPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        js_files = sorted((PUBLIC / "js").glob("*.js"))
        js_content = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        cls.raw_html = raw_html
        cls.html = raw_html + "\n" + js_content

    def test_canonical_points_at_user_pages_subpath(self) -> None:
        self.assertIn(f'<link rel="canonical" href="{SITE_ROOT}">', self.html)

    def test_loads_index_generated_page_data(self) -> None:
        """§7.2：页面数据由索引生成，不从别处另取一份。"""
        self.assertIn('fetch("data/catalog.json"', self.html)

    def test_has_search_and_filters(self) -> None:
        """§6：关键词搜索、用途与来源筛选、候选区切换。"""
        for element_id in ('id="q"', 'id="category"', 'id="source"', 'id="tab-candidate"'):
            self.assertIn(element_id, self.html)

    def test_no_custom_domain_cname(self) -> None:
        """产品规范 §7.1：当前没有自定义域名，不生成 CNAME。"""
        self.assertFalse((PUBLIC / "CNAME").exists())

    def test_relative_links_do_not_escape_public_dir(self) -> None:
        """Pages 只发布 public/，因此 ../ 这类链接在线上必然 404。"""
        offenders = [
            m.group(1)
            for m in re.finditer(r'(?:href|src)="([^"]+)"', self.html)
            if m.group(1).startswith("../")
        ]
        self.assertEqual(offenders, [], f"存在跳出 public/ 的链接：{offenders}")


class RobotsAndSitemapTest(unittest.TestCase):
    def test_robots_uses_new_site_and_sitemap_url(self) -> None:
        robots = (PUBLIC / "robots.txt").read_text(encoding="utf-8")
        self.assertIn(f"Sitemap: {SITE_ROOT}sitemap.xml", robots)
        self.assertIn("justdoit712.github.io", robots)
        self.assertIn("User-agent: *", robots)

    def test_sitemap_is_valid_xml_and_only_lists_new_site(self) -> None:
        tree = ET.parse(PUBLIC / "sitemap.xml")
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [e.text.strip() for e in tree.getroot().findall(".//sm:loc", ns)]
        self.assertTrue(locs, "sitemap 至少要有一个 URL")
        for loc in locs:
            self.assertTrue(loc.startswith(SITE_ROOT), f"{loc} 不在发布地址下")

    def test_sitemap_has_no_deleted_pages(self) -> None:
        blob = (PUBLIC / "sitemap.xml").read_text(encoding="utf-8")
        for gone in ("skills.html", "skills-en.html", "local-skills.html", "projects.html", "llms.txt"):
            self.assertNotIn(gone, blob, f"sitemap 仍列出已删除页面 {gone}")


if __name__ == "__main__":
    unittest.main()
