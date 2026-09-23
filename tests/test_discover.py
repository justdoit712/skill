"""发现层：展开到具体技能、接入来源种子、以及失败时的行为。

对应 §4.2「合集仓库尽量展开到具体技能」「普通仓库、空模板、插件容器或整个合集
不能直接计为一个独立技能」。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.catalog.discovery import (
    _skill_name_from_path,
    build_queries,
    candidates_from_sources,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCES = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
SEARCHES = json.loads((ROOT / "config" / "searches.json").read_text(encoding="utf-8"))


class SkillNameTest(unittest.TestCase):
    def test_uses_parent_directory(self) -> None:
        self.assertEqual(_skill_name_from_path("skills/market-news-analyst/SKILL.md", "repo"), "market-news-analyst")

    def test_root_level_falls_back(self) -> None:
        self.assertEqual(_skill_name_from_path("SKILL.md", "repo"), "repo")

    def test_nested_path(self) -> None:
        self.assertEqual(_skill_name_from_path("a/b/c/SKILL.md", "repo"), "c")


class SourcesSeedTest(unittest.TestCase):
    """官方来源此前完全没有接入主动扫描，这里固定住这个行为。"""

    def test_seeds_include_official_repositories(self) -> None:
        seeds = candidates_from_sources(SOURCES)
        ids = {c.skill_id for c in seeds}
        for expected in ("anthropics/skills", "cloudflare/skills", "trailofbits/skills"):
            self.assertIn(expected, ids, f"官方来源 {expected} 未进入发现种子")

    def test_excluded_sources_are_skipped(self) -> None:
        seeds = candidates_from_sources(SOURCES)
        for candidate in seeds:
            self.assertNotEqual(candidate.owner, "huggingface", "被排除的来源不得作为种子")

    def test_known_skill_paths_become_path_level_candidates(self) -> None:
        """sources.json 里已定位到 SKILL.md 的线索应直接是路径级候选。"""
        seeds = candidates_from_sources(SOURCES)
        paths = {c.skill_id: c.path for c in seeds}
        key = "tradermonty/claude-trading-skills:skills/market-news-analyst/SKILL.md"
        self.assertIn(key, paths, "已定位的单技能未按路径级候选生成")

    def test_seeds_carry_discovery_provenance(self) -> None:
        seeds = candidates_from_sources(SOURCES)
        provenanced = [c for c in seeds if c.source_ids]
        self.assertTrue(provenanced, "种子候选必须带来源 id 以便追溯")
        for candidate in provenanced:
            self.assertTrue(candidate.discovery_methods, f"{candidate.skill_id} 缺发现方式")


class QueryTest(unittest.TestCase):
    def test_queries_constrain_to_skill_md(self) -> None:
        queries = build_queries(SEARCHES)
        self.assertTrue(queries)
        for query in queries:
            self.assertIn("SKILL.md", query.q)
            self.assertIn("in:readme", query.q)
            self.assertIn("-dsh", query.q)


if __name__ == "__main__":
    unittest.main()
