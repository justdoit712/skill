"""端到端演练与回归测试。

不联网、不调用模型：发现与抓取用替身，评估用固定输出。
输出写入临时目录，不污染真实的 data/ 与 public/data/。

本文件同时是四条已修缺陷的回归测试：
1. 采集失败不得清空既有目录
2. 额度预留必须先落盘，之后才可付费调用；重复运行不得重复计费
3. 仓库级候选必须能展开到具体技能（见 tests/test_discover.py）
4. 评估产出的中文简述、分类与依赖必须随索引保留
5. 内容指纹必须反映内容版本，周报必须与上次索引对比
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.dedupe import candidate_from_repo, dedupe
from src.fetch import FetchResult
from src.index import CatalogContext, build_catalog, build_entry, write_catalog
from src.pipeline import phase_evaluate, phase_reserve
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

SKILL_TEXT = "---\nname: widget\ndescription: 一个通用小工具\n---\n" + "内容行\n" * 40


def passing_evaluation(summary: str = "用于处理通用小工具的中文简述。") -> dict:
    evaluation = json.loads(json.dumps(EVAL_BY_ID["in_scope_normal"]))
    evaluation.update(
        {
            "summary_zh": summary,
            "main_category": "finance",
            "tags": ["期货"],
            "platform_declared": "需联网",
            "dependencies_declared": ["requests"],
            "domain_checks": {"finance": {"value": "pass", "evidence": "来源与时间已记录"}},
        }
    )
    return evaluation


def fake_fetch(text: str | None = SKILL_TEXT, ok: bool = True, reason_code: str | None = None):
    def _fetch(url, **kwargs):
        if not ok:
            return FetchResult(url=url, ok=False, reason_code=reason_code or "HTTP_ERROR", error="HTTP 500")
        return FetchResult(
            url=url, ok=True, status=200, text=text, bytes_read=len((text or "").encode("utf-8"))
        )

    return _fetch


def fake_discover(candidates, outcomes=None):
    def _discover(*args, **kwargs):
        return list(candidates), list(outcomes or [])

    return _discover


def fake_evaluate(evaluation: dict | None = None, ok: bool = True):
    calls = []

    def _evaluate(candidate, text, **kwargs):
        calls.append(candidate.skill_id)
        if not ok:
            return {"ok": False, "evaluation": None, "call": None, "reason_code": "MODEL_ERROR", "error": "失败"}
        return {
            "ok": True,
            "evaluation": evaluation if evaluation is not None else passing_evaluation(),
            "call": None,
            "reason_code": None,
            "error": None,
        }

    _evaluate.calls = calls
    return _evaluate


def one_candidate(skill_id_repo: str = "widget", owner: str = "acme", **kwargs):
    return candidate_from_repo(
        owner, skill_id_repo, url=f"https://github.com/{owner}/{skill_id_repo}", **kwargs
    )


class PipelineHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.public = self.root / "public"
        self.state = self.data / "state"
        self.data.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_previous_catalog(self, entries: list[dict]) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "catalog.json").write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )

    def read_catalog(self) -> dict:
        return json.loads((self.data / "catalog.json").read_text(encoding="utf-8"))

    def previous_entry(self, skill_id: str) -> dict:
        return {
            "skill_id": skill_id,
            "name": skill_id,
            "url": "https://github.com/old/repo",
            "author": "old",
            "summary_zh": "旧条目",
            "main_category": None,
            "tags": [],
            "platform_declared": None,
            "dependencies_declared": [],
            "source_type": "community",
            "status": "candidate",
            "needs_review": False,
            "review_note": None,
            "limitations": None,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_checked": "2026-01-01T00:00:00+00:00",
            "content_changed_at": None,
            "content_fingerprint": "sha256:old",
            "upstream_status": "ok",
            "license": None,
            "excluded": False,
            "reason_codes": [],
            "flags": [],
            "candidate_domains": [],
            "discovery": {"source_ids": [], "methods": [], "terms": []},
            "evaluation_rules_version": None,
        }

    def reserve(self, candidates, *, outcomes=None, text=SKILL_TEXT, ok=True, **kwargs):
        return phase_reserve(
            config_dir=ROOT / "config",
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover(candidates, outcomes),
            fetch_fn=fake_fetch(text, ok=ok),
            **kwargs,
        )

    def evaluate(self, *, evaluation=None, ok=True, **kwargs):
        return phase_evaluate(
            config_dir=ROOT / "config",
            data_dir=self.data,
            public_dir=self.public,
            state_dir=self.state,
            evaluate_fn=fake_evaluate(evaluation, ok=ok),
            fetch_fn=fake_fetch(),
            **kwargs,
        )


# ---------------------------------------------------------------- 缺陷 1


class FailureMustNotWipeCatalogTest(PipelineHarness):
    def test_total_discovery_failure_aborts_and_keeps_catalog(self) -> None:
        self.write_previous_catalog([self.previous_entry("old/keepme")])
        failed = [
            type("O", (), {"query": type("Q", (), {"q": "macro", "domain_id": "finance"})(),
                           "ok": False, "total_count": 0, "candidates": [], "reason_code": "HTTP_ERROR",
                           "error": "HTTP 503"})()
        ]
        result = self.reserve([], outcomes=failed)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "discover")
        entries = self.read_catalog()["entries"]
        self.assertEqual([e["skill_id"] for e in entries], ["old/keepme"], "目录被清空")

    def test_fresh_run_with_no_previous_catalog_is_allowed(self) -> None:
        result = self.reserve([], outcomes=[])
        self.assertTrue(result["ok"], "首次运行没有既有目录时不应中止")

    def test_unseen_entries_are_retained_after_merge(self) -> None:
        self.write_previous_catalog([self.previous_entry("old/keepme")])
        self.reserve([one_candidate()])
        self.evaluate()
        ids = {e["skill_id"] for e in self.read_catalog()["entries"]}
        self.assertIn("old/keepme", ids, "本轮未出现的条目被删除")
        self.assertIn("acme/widget", ids)


# ---------------------------------------------------------------- 缺陷 2


class ReserveBeforeCallTest(PipelineHarness):
    def test_reserve_writes_state_without_calling_model(self) -> None:
        spy = fake_evaluate()
        result = self.reserve([one_candidate()])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reserved"], 1)
        self.assertTrue((self.state / "budget.json").exists(), "额度未在预留阶段落盘")
        self.assertEqual(spy.calls, [], "预留阶段不得调用模型")

    def test_second_run_does_not_double_charge(self) -> None:
        self.reserve([one_candidate()])
        first = self.evaluate()
        self.assertEqual(first["evaluated"], 1)

        # 第二次运行：同一内容，评估 ID 相同，应复用结果而不是再调用
        spy = fake_evaluate()
        again = phase_evaluate(
            config_dir=ROOT / "config", data_dir=self.data, public_dir=self.public,
            state_dir=self.state, evaluate_fn=spy, fetch_fn=fake_fetch(),
        )
        self.assertEqual(spy.calls, [], "同一评估 ID 不应重复调用模型")
        self.assertEqual(again["evaluated"], 0)
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(again["quota"]["used"], 1, "额度被重复计费")


# ---------------------------------------------------------------- 缺陷 4


class EvaluationContentKeptTest(PipelineHarness):
    def test_summary_category_and_dependencies_reach_the_index(self) -> None:
        self.reserve([one_candidate()])
        self.evaluate(evaluation=passing_evaluation("这是模型生成的中文简述。"))

        entry = next(e for e in self.read_catalog()["entries"] if e["skill_id"] == "acme/widget")
        self.assertEqual(entry["summary_zh"], "这是模型生成的中文简述。")
        self.assertEqual(entry["status"], "recommended")
        self.assertEqual(entry["main_category"], {"id": "finance", "name": "金融与投资"})
        self.assertEqual(entry["dependencies_declared"], ["requests"])
        self.assertEqual(entry["platform_declared"], "需联网")
        self.assertEqual(entry["tags"], ["期货"])

    def test_page_data_exposes_the_summary(self) -> None:
        self.reserve([one_candidate()])
        self.evaluate(evaluation=passing_evaluation("中文简述进页面。"))
        page = json.loads((self.public / "data" / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(page["recommended"][0]["summary_zh"], "中文简述进页面。")


# ---------------------------------------------------------------- 缺陷 5


class FingerprintAndHistoryTest(PipelineHarness):
    def test_fingerprint_is_computed_from_fetched_content(self) -> None:
        self.reserve([one_candidate()])
        queue = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        fingerprint = queue["queued"][0]["candidate"]["content_fingerprint"]
        self.assertTrue(fingerprint and fingerprint.startswith("sha256:"), "未计算内容指纹")
        self.assertNotIn("nofingerprint", json.dumps(queue), "评估 ID 退化为 nofingerprint")

    def test_changed_content_produces_a_new_evaluation_id(self) -> None:
        self.reserve([one_candidate()], text="第一版内容\n" * 40)
        self.evaluate()
        first = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        first_id = first["queued"][0]["candidate"]["content_fingerprint"]

        # 内容变化后再次运行：指纹必须不同
        self.reserve([one_candidate()], text="第二版内容\n" * 40)
        second = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        second_id = second["queued"][0]["candidate"]["content_fingerprint"]
        self.assertNotEqual(first_id, second_id, "内容变化未反映到指纹")

    def test_report_compares_against_previous_catalog(self) -> None:
        self.reserve([one_candidate()])
        self.evaluate()
        report = json.loads((self.data / "reports" / "2026-W38.json").read_text(encoding="utf-8")) \
            if (self.data / "reports" / "2026-W38.json").exists() else None
        # 周报文件按真实周命名，这里直接读目录下唯一文件
        reports = list((self.data / "reports").glob("*.json"))
        self.assertTrue(reports, "未生成周报")
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["counts"][NEW], 1, "首次运行应记 1 条新增")

        # 第二次运行：同样的内容不应再记新增
        self.reserve([one_candidate()])
        self.evaluate()
        report2 = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report2["counts"][NEW], 0, "重复运行仍被记作新增")


# ---------------------------------------------------------------- 条目与报告


class EntryAndReportTest(PipelineHarness):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(ROOT / "config")
        cls.ctx = CatalogContext(
            rules_version=RULES["rules_version"],
            generated_at="2026-09-20T00:00:00+00:00",
            domain_names=cls.cfg.domain_names,
            source_types={"gauss314_skills": "community_list", "anthropics_skills": "official"},
        )

    def test_prescreen_exclusion_reaches_catalog_status(self) -> None:
        template = candidate_from_repo("someone", "x", path="_template",
                                       url="https://github.com/someone/x/tree/main/_template")
        pres = prescreen(template, self.cfg)
        entry = build_entry(template, prescreen_result=pres, context=self.ctx)
        self.assertEqual(entry["status"], "excluded")
        self.assertIn("NOT_A_SKILL", entry["reason_codes"])

    def test_unevaluated_entry_fabricates_nothing(self) -> None:
        candidate = one_candidate("unrated", description="视频创作工具")
        entry = build_entry(candidate, prescreen_result=prescreen(candidate, self.cfg), context=self.ctx)
        self.assertEqual(entry["status"], "pending")
        self.assertIsNone(entry["summary_zh"])
        self.assertIsNone(entry["platform_declared"])
        self.assertEqual(entry["dependencies_declared"], [])
        self.assertIsNone(entry["main_category"], "未评估不得凭预筛线索编造分类")

    def test_write_catalog_and_report(self) -> None:
        entry = build_entry(
            one_candidate(), prescreen_result=prescreen(one_candidate(), self.cfg),
            decision={"decision": "recommended", "reason_codes": []},
            evaluation=passing_evaluation(), context=self.ctx,
        )
        catalog = build_catalog([entry], context=self.ctx)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = write_catalog(
                catalog, data_path=Path(tmp) / "c.json", public_path=Path(tmp) / "p.json"
            )
            self.assertTrue(manifest["catalog_digest"].startswith("sha256:"))
            page = json.loads(Path(tmp).joinpath("p.json").read_text(encoding="utf-8"))
            self.assertEqual(page["counts"]["recommended"], 1)

    def test_report_classifies_changes(self) -> None:
        candidate = one_candidate("widget", content_fingerprint="sha256:new")
        entry = build_entry(
            candidate, prescreen_result=prescreen(candidate, self.cfg),
            decision={"decision": "recommended", "reason_codes": []},
            evaluation=passing_evaluation(), context=self.ctx,
        )
        catalog = build_catalog([entry], context=self.ctx)
        previous = {
            "entries": [
                dict(entry, status="candidate", content_fingerprint="sha256:old"),
                self.previous_entry("old/keepme"),
            ]
        }
        report = build_report(
            catalog, previous_catalog=previous,
            run_meta={"quota": {"cap": 50, "used": 2, "remaining": 48}},
            outcomes=[{"query": "macro", "ok": False, "reason_code": "HTTP_ERROR", "error": "HTTP 500"}],
        )
        self.assertEqual(report["counts"][CONTENT_CHANGED], 1)
        self.assertEqual(report["counts"][STATUS_CHANGED], 1)
        self.assertEqual(report["counts"][UPSTREAM_REMOVED], 1)
        self.assertEqual(report["counts"][COLLECTION_FAILED], 1)

        markdown = render_report_markdown(report)
        self.assertIn("# 运行报告", markdown)
        self.assertIn("HTTP_ERROR", markdown)

    def test_write_report_outputs_both_formats(self) -> None:
        entry = build_entry(
            one_candidate(), prescreen_result=prescreen(one_candidate(), self.cfg),
            decision={"decision": "recommended", "reason_codes": []},
            evaluation=passing_evaluation(), context=self.ctx,
        )
        report = build_report(build_catalog([entry], context=self.ctx))
        with tempfile.TemporaryDirectory() as tmp:
            written = write_report(
                report, json_path=Path(tmp) / "r.json", markdown_path=Path(tmp) / "r.md"
            )
            self.assertTrue(Path(written["report_json"]).exists())
            self.assertTrue(Path(written["report_markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
