"""端到端演练：用固定评估输出跑通 候选 → 预筛 → 决策 → 索引 → 页面数据 → 周报。

不联网、不调用模型，全部使用 config/ 的真实配置与 tests/fixtures 的固定评估输出。
输出写入临时目录，不污染真实的 data/ 与 public/data/。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.decide import decide
from src.dedupe import candidate_from_repo, dedupe
from src.index import CatalogContext, build_catalog, build_entry, write_catalog
from src.prescreen import load_config, prescreen
from src.report import (
    COLLECTION_FAILED,
    CONTENT_CHANGED,
    NEW,
    STATUS_CHANGED,
    UPSTREAM_REMOVED,
    build_report,
    render_report_markdown,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "evaluations.json").read_text(encoding="utf-8"))
EVAL_BY_ID = {c["id"]: c["evaluation"] for c in FIXTURE["cases"]}


def candidate(owner, repo, **kwargs):
    return candidate_from_repo(owner, repo, **kwargs)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(ROOT / "config")
        cls.ctx = CatalogContext(
            rules_version=RULES["rules_version"],
            generated_at="2026-09-20T00:00:00+00:00",
            domain_names=cls.cfg.domain_names,
            source_types={"gauss314_skills": "community_list", "anthropics_skills": "official"},
        )

    def build(self):
        """跑一遍 候选 → 预筛 → 决策 → 索引。"""
        candidates = dedupe(
            [
                # 同一技能从两个来源被发现，应合并为一条并保留全部依据
                candidate("tradermonty", "claude-trading-skills", path="skills/market-news-analyst",
                          url="https://github.com/tradermonty/claude-trading-skills/blob/main/skills/market-news-analyst/SKILL.md",
                          name="market-news-analyst", description="政策、地缘事件、能源与贵金属关联",
                          source_id="gauss314_skills", discovery_method="provided_lead", search_term="地缘",
                          content_fingerprint="sha256:news1"),
                candidate("tradermonty", "claude-trading-skills", path="skills/market-news-analyst",
                          name="market-news-analyst", source_id="anthropics_skills",
                          discovery_method="github_search", search_term="macro"),
                candidate("acme", "widget", url="https://github.com/acme/widget", name="widget",
                          description="一个通用小工具", content_fingerprint="sha256:widget1"),
                candidate("someone", "x", path="_template",
                          url="https://github.com/someone/x/tree/main/_template"),
                candidate("huggingface", "skills", url="https://github.com/huggingface/skills"),
                # 未评估的候选，状态应为 pending 且字段留空，不得编造
                candidate("acme", "unrated", url="https://github.com/acme/unrated",
                          description="视频创作工具"),
            ]
        )

        evaluations = {
            "tradermonty/claude-trading-skills:skills/market-news-analyst": dict(
                EVAL_BY_ID["in_scope_normal"],
                summary_zh="汇总政策与地缘事件，并关联能源与贵金属行情。",
                main_category="finance",
                platform_declared="需联网获取新闻",
                dependencies_declared=["requests"],
            ),
            "acme/widget": EVAL_BY_ID["missing_dependency"],
        }

        entries = []
        for cand in candidates:
            pres = prescreen(cand, self.cfg)
            evaluation = evaluations.get(cand.skill_id)
            decision = decide(evaluation, RULES) if evaluation else None
            entries.append(
                build_entry(
                    cand,
                    prescreen_result=pres,
                    decision=decision,
                    evaluation=evaluation,
                    context=self.ctx,
                    first_seen=cand.discovered_at or "2026-09-20T00:00:00+00:00",
                    last_checked=self.ctx.generated_at,
                )
            )
        return build_catalog(entries, context=self.ctx)

    def test_prescreen_exclusions_land_in_catalog(self) -> None:
        catalog = self.build()
        by_id = {e["skill_id"]: e for e in catalog["entries"]}
        self.assertEqual(by_id["someone/x:_template"]["status"], "excluded")
        self.assertIn("NOT_A_SKILL", by_id["someone/x:_template"]["reason_codes"])
        self.assertEqual(by_id["huggingface/skills"]["status"], "excluded")
        self.assertIn("NOT_STANDALONE_DIRECTION", by_id["huggingface/skills"]["reason_codes"])

    def test_dedupe_leaves_one_entry_with_all_evidence(self) -> None:
        catalog = self.build()
        entry = next(e for e in catalog["entries"] if e["skill_id"].startswith("tradermonty/"))
        self.assertEqual(set(entry["discovery"]["source_ids"]), {"gauss314_skills", "anthropics_skills"})
        self.assertEqual(set(entry["discovery"]["terms"]), {"地缘", "macro"})
        self.assertEqual(entry["source_type"], "community_list")

    def test_statuses_and_counts(self) -> None:
        catalog = self.build()
        counts = catalog["counts"]
        self.assertEqual(counts.get("recommended"), 1)
        self.assertEqual(counts.get("candidate"), 1)
        self.assertEqual(counts.get("excluded"), 2)
        self.assertEqual(counts.get("pending"), 1)

    def test_unevaluated_entry_fabricates_nothing(self) -> None:
        """§6：不编造用途。未评估的条目简述为空，状态为 pending。"""
        catalog = self.build()
        entry = next(e for e in catalog["entries"] if e["skill_id"] == "acme/unrated")
        self.assertEqual(entry["status"], "pending")
        self.assertIsNone(entry["summary_zh"])
        self.assertIsNone(entry["platform_declared"])
        self.assertEqual(entry["dependencies_declared"], [])
        # §6：上游未声明就不得推测为全平台兼容
        self.assertNotEqual(entry["platform_declared"], "all")

    def test_last_checked_is_not_content_changed_at(self) -> None:
        """§6：最近检查时间不能冒充技能更新时间。"""
        catalog = self.build()
        entry = next(e for e in catalog["entries"] if e["skill_id"].startswith("tradermonty/"))
        self.assertEqual(entry["last_checked"], self.ctx.generated_at)
        self.assertIsNone(entry["content_changed_at"])

    def test_write_catalog_produces_index_and_page_data(self) -> None:
        catalog = self.build()
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "catalog.json"
            public_path = Path(tmp) / "page.json"
            manifest = write_catalog(catalog, data_path=data_path, public_path=public_path)

            self.assertTrue(data_path.exists())
            self.assertTrue(public_path.exists())
            self.assertTrue(manifest["catalog_digest"].startswith("sha256:"))

            page = json.loads(public_path.read_text(encoding="utf-8"))
            self.assertEqual(page["counts"]["recommended"], 1)
            self.assertEqual(page["counts"]["candidate"], 1)
            self.assertEqual(page["counts"]["pending"], 1)
            # 页面数据由索引派生，推荐区来自索引而非另写一份
            index_ids = {e["skill_id"] for e in catalog["entries"]}
            self.assertTrue({e["skill_id"] for e in page["recommended"]} <= index_ids)
            self.assertEqual(page["categories"], [{"id": "finance", "count": 1}])

    def test_report_classifies_changes(self) -> None:
        current = self.build()
        # 上次索引只含三条：A 当时是候选且指纹不同，acme/widget 未变，gone/repo 本次消失
        previous = {
            "entries": [
                {
                    "skill_id": "tradermonty/claude-trading-skills:skills/market-news-analyst",
                    "name": "market-news-analyst",
                    "url": "https://github.com/tradermonty/claude-trading-skills",
                    "status": "candidate",
                    "upstream_status": "ok",
                    "main_category": None,
                    "content_fingerprint": "sha256:old",
                },
                {
                    "skill_id": "acme/widget",
                    "name": "widget",
                    "url": "https://github.com/acme/widget",
                    "status": "candidate",
                    "upstream_status": "ok",
                    "main_category": None,
                    "content_fingerprint": "sha256:widget1",
                },
                {
                    "skill_id": "gone/repo",
                    "name": "repo",
                    "url": "https://github.com/gone/repo",
                    "status": "candidate",
                    "upstream_status": "ok",
                    "main_category": None,
                    "content_fingerprint": None,
                },
            ]
        }

        outcomes = [
            {"query": "macro", "ok": True},
            {"query": "sleep", "ok": False, "reason_code": "HTTP_ERROR", "error": "HTTP 500"},
        ]
        report = build_report(
            current,
            previous_catalog=previous,
            run_meta={"quota": {"cap": 50, "used": 2, "remaining": 48}},
            outcomes=outcomes,
        )

        self.assertEqual(report["counts"][NEW], 3)
        self.assertEqual(report["counts"][CONTENT_CHANGED], 1)
        self.assertEqual(report["counts"][STATUS_CHANGED], 1)
        self.assertEqual(report["counts"][UPSTREAM_REMOVED], 1)
        self.assertEqual(report["counts"][COLLECTION_FAILED], 1)

        markdown = render_report_markdown(report)
        self.assertIn("# 运行报告", markdown)
        self.assertIn("采集失败", markdown)
        self.assertIn("HTTP_ERROR", markdown)
        self.assertIn("本周额度", markdown)

    def test_write_report_outputs_both_formats(self) -> None:
        catalog = self.build()
        report = build_report(catalog, run_meta={"quota": {"cap": 50, "used": 0, "remaining": 50}})
        with tempfile.TemporaryDirectory() as tmp:
            written = write_report(
                report,
                json_path=Path(tmp) / "r.json",
                markdown_path=Path(tmp) / "r.md",
            )
            self.assertTrue(Path(written["report_json"]).exists())
            self.assertTrue(Path(written["report_markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
