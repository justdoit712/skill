"""稳定 ID、URL 解析与去重。"""

from __future__ import annotations

import unittest

from src.dedupe import (
    candidate_from_repo,
    content_fingerprint,
    dedupe,
    make_skill_id,
    parse_github_url,
)


class ParseGithubUrlTest(unittest.TestCase):
    def test_repo_root(self) -> None:
        self.assertEqual(
            parse_github_url("https://github.com/anthropics/skills"),
            ("anthropics", "skills", "", "repo"),
        )

    def test_tree_path(self) -> None:
        self.assertEqual(
            parse_github_url("https://github.com/anthropics/skills/tree/main/skills/pdf"),
            ("anthropics", "skills", "skills/pdf", "tree"),
        )

    def test_blob_path(self) -> None:
        self.assertEqual(
            parse_github_url(
                "https://github.com/tradermonty/claude-trading-skills"
                "/blob/main/skills/market-news-analyst/SKILL.md"
            ),
            ("tradermonty", "claude-trading-skills", "skills/market-news-analyst/SKILL.md", "blob"),
        )

    def test_raw_host(self) -> None:
        self.assertEqual(
            parse_github_url(
                "https://raw.githubusercontent.com/thousandcents/futures-research/main/SKILL.md"
            ),
            ("thousandcents", "futures-research", "SKILL.md", "raw"),
        )

    def test_non_github(self) -> None:
        self.assertEqual(parse_github_url("https://gitlab.com/x/y"), ("", "", "", "unknown"))

    def test_empty(self) -> None:
        self.assertEqual(parse_github_url(""), ("", "", "", "unknown"))

    def test_case_normalised(self) -> None:
        owner, repo, _, _ = parse_github_url("https://github.com/Anthropics/SKILLS")
        self.assertEqual((owner, repo), ("anthropics", "skills"))

    def test_dot_git_suffix_stripped(self) -> None:
        self.assertEqual(make_skill_id(*parse_github_url("https://github.com/a/b.git")[:2]), "a/b")


class SkillIdTest(unittest.TestCase):
    def test_repo_level(self) -> None:
        self.assertEqual(make_skill_id("Anthropics", "Skills"), "anthropics/skills")

    def test_path_level_strips_slashes(self) -> None:
        self.assertEqual(make_skill_id("a", "b", "/skills/c/"), "a/b:skills/c")

    def test_same_skill_same_id_regardless_of_source(self) -> None:
        first = make_skill_id(*parse_github_url("https://github.com/Anthropics/Skills")[:2])
        second = make_skill_id(*parse_github_url("https://github.com/anthropics/skills/tree/main")[:2])
        self.assertEqual(first, second)

    def test_empty_owner_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_skill_id("", "b")


class FingerprintTest(unittest.TestCase):
    def test_stable(self) -> None:
        self.assertEqual(content_fingerprint("hello"), content_fingerprint("hello"))

    def test_changes_with_content(self) -> None:
        self.assertNotEqual(content_fingerprint("hello"), content_fingerprint("hello!"))

    def test_none_passthrough(self) -> None:
        self.assertIsNone(content_fingerprint(None))


class DedupeTest(unittest.TestCase):
    def test_merges_same_skill_from_two_sources(self) -> None:
        first = candidate_from_repo(
            "acme", "tool", path="p", url="https://github.com/acme/tool",
            name="tool", source_id="src_a", discovery_method="github_search", search_term="macro",
        )
        second = candidate_from_repo(
            "ACME", "Tool", path="p", description="来自社区清单",
            source_id="src_b", discovery_method="community", search_term="futures",
        )
        merged = dedupe([first, second])
        self.assertEqual(len(merged), 1)
        kept = merged[0]
        self.assertEqual(set(kept.source_ids), {"src_a", "src_b"})
        self.assertEqual(set(kept.search_terms), {"macro", "futures"})
        self.assertEqual(set(kept.discovery_methods), {"github_search", "community"})
        self.assertEqual(kept.description, "来自社区清单")

    def test_keeps_distinct_skills_separate(self) -> None:
        a = candidate_from_repo("acme", "one")
        b = candidate_from_repo("acme", "two")
        self.assertEqual(len(dedupe([a, b])), 2)

    def test_preserves_first_seen_order(self) -> None:
        a = candidate_from_repo("acme", "one")
        b = candidate_from_repo("acme", "two")
        self.assertEqual([c.skill_id for c in dedupe([a, b])], ["acme/one", "acme/two"])


if __name__ == "__main__":
    unittest.main()
