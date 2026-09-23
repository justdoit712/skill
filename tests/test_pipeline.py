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
import shutil
import tempfile
import unittest
from pathlib import Path

from src.catalog.dedupe import candidate_from_repo, dedupe
from src.catalog.index import CatalogContext, build_catalog, build_entry
from src.catalog.store import write_catalog
from src.catalog.prescreen import load_config, prescreen
from src.catalog.report import (
    COLLECTION_FAILED,
    CONTENT_CHANGED,
    NEW,
    STATUS_CHANGED,
    UPSTREAM_REMOVED,
    build_report,
    render_report_markdown,
    write_report,
)
from src.infra.http import FetchResult
from src.pipeline import phase_evaluate, phase_reserve

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


class FakeCall:
    """替身调用结果，只需要 total_tokens 供上限累计。"""

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = total_tokens


def fake_evaluate_with_tokens(tokens_per_call: int, evaluation: dict | None = None):
    calls: list[str] = []

    def _evaluate(candidate, text, **kwargs):
        calls.append(candidate.skill_id)
        return {
            "ok": True,
            "evaluation": evaluation if evaluation is not None else passing_evaluation(),
            "call": FakeCall(tokens_per_call),
            "reason_code": None,
            "error": None,
        }

    _evaluate.calls = calls
    return _evaluate


class PipelineHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.public = self.root / "public"
        self.state = self.data / "state"
        self.data.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def temp_config(self, **limits_overrides) -> Path:
        """把真实 config 复制到临时目录，并按需覆盖模型 limits。"""
        cfg_dir = self.root / "config"
        if not cfg_dir.exists():
            shutil.copytree(ROOT / "config", cfg_dir)
        model_path = cfg_dir / "model.local.json"
        if not model_path.exists():
            model_path.write_text(
                (cfg_dir / "model.example.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
        model = json.loads(model_path.read_text(encoding="utf-8"))
        model.update(endpoint="https://fake.invalid/v1/chat/completions", model="test-model")
        model.setdefault("limits", {}).update(limits_overrides)
        model_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg_dir

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

    def counting_fetch(self, text: str = SKILL_TEXT):
        """记录抓取过的 URL，用于断言"本轮到底处理了哪几条"。"""
        seen: list[str] = []

        def _fetch(url, **kwargs):
            seen.append(url)
            return FetchResult(url=url, ok=True, status=200, text=text,
                               bytes_read=len(text.encode("utf-8")))

        _fetch.seen = seen
        return _fetch

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
        self.assertEqual(again["queue_pending"], 0, "已完成的条目不得留在队列里重复占名额")
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
        fingerprint = queue["pending"][0]["candidate"]["content_fingerprint"]
        self.assertTrue(fingerprint and fingerprint.startswith("sha256:"), "未计算内容指纹")
        self.assertNotIn("nofingerprint", json.dumps(queue), "评估 ID 退化为 nofingerprint")

    def test_changed_content_produces_a_new_evaluation_id(self) -> None:
        self.reserve([one_candidate()], text="第一版内容\n" * 40)
        queue = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        first_id = queue["pending"][0]["candidate"]["content_fingerprint"]
        self.evaluate()

        # 内容变化后再次运行：指纹必须不同
        self.reserve([one_candidate()], text="第二版内容\n" * 40)
        second = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        second_id = second["pending"][0]["candidate"]["content_fingerprint"]
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


# ---------------------------------------------------------------- token 上限


class RunTokenCapTest(PipelineHarness):
    """单次运行的 token 消耗兜底闸：达到后停止后续模型调用，已预留名额不退还。"""

    def reserve_many(self, cfg_dir: Path, count: int) -> None:
        candidates = [one_candidate(f"repo{i}") for i in range(count)]
        result = phase_reserve(
            config_dir=cfg_dir, data_dir=self.data, state_dir=self.state,
            discover_fn=fake_discover(candidates), fetch_fn=fake_fetch(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reserved"], count)

    def evaluate_with(self, cfg_dir: Path, spy):
        return phase_evaluate(
            config_dir=cfg_dir, data_dir=self.data, public_dir=self.public,
            state_dir=self.state, evaluate_fn=spy, fetch_fn=fake_fetch(),
        )

    def test_stops_once_cap_reached(self) -> None:
        cfg_dir = self.temp_config(max_total_tokens_per_run=20000)
        self.reserve_many(cfg_dir, 3)
        spy = fake_evaluate_with_tokens(15000)

        result = self.evaluate_with(cfg_dir, spy)

        self.assertEqual(len(spy.calls), 2, "达到上限后不得继续调用模型")
        self.assertEqual(result["tokens_used"], 30000)
        self.assertEqual(result["token_cap"], 20000)
        self.assertEqual(len(result["token_stopped"]), 1)
        self.assertEqual(result["evaluated"], 2)

    def test_cap_does_not_release_reserved_slots(self) -> None:
        """§7.3：达到上限而停止的条目仍占本周额度。"""
        cfg_dir = self.temp_config(max_total_tokens_per_run=20000)
        self.reserve_many(cfg_dir, 3)
        result = self.evaluate_with(cfg_dir, fake_evaluate_with_tokens(15000))
        self.assertEqual(result["quota"]["used"], 3, "名额被错误释放")

    def test_generous_cap_never_triggers(self) -> None:
        cfg_dir = self.temp_config(max_total_tokens_per_run=100000000)
        self.reserve_many(cfg_dir, 3)
        result = self.evaluate_with(cfg_dir, fake_evaluate_with_tokens(15000))
        self.assertEqual(len(result["token_stopped"]), 0)
        self.assertEqual(result["evaluated"], 3)
        self.assertEqual(result["tokens_used"], 45000)

    def test_shipped_config_carries_the_cap(self) -> None:
        example = json.loads((ROOT / "config" / "model.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["limits"]["max_total_tokens_per_run"], 100000000)


# ---------------------------------------------------------------- 抓取上限


class FetchCapTest(PipelineHarness):
    """抓取数量必须可控：默认跟随本批名额，显式设置则为硬上限。

    抓取只针对可能被评估的候选——先用无内容预筛筛掉不合格的，再抓，避免白抓。
    """

    def discover_many(self, count: int):
        return fake_discover([one_candidate(f"repo{i}") for i in range(count)])

    def counting_fetch(self):
        seen: list[str] = []

        def _fetch(url, **kwargs):
            seen.append(url)
            return FetchResult(url=url, ok=True, status=200, text=SKILL_TEXT,
                               bytes_read=len(SKILL_TEXT.encode("utf-8")))

        _fetch.seen = seen
        return _fetch

    def test_default_follows_evaluation_slots(self) -> None:
        spy = self.counting_fetch()
        result = phase_reserve(
            config_dir=ROOT / "config", data_dir=self.data, state_dir=self.state,
            discover_fn=self.discover_many(10), fetch_fn=spy, limit_evaluations=4,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(spy.seen), 4, "默认不应抓取超过本批名额的数量")
        self.assertEqual(result["fetched"], 4)
        self.assertEqual(result["fetch_cap"], 4)

    def test_explicit_cap_limits_fetches(self) -> None:
        spy = self.counting_fetch()
        result = phase_reserve(
            config_dir=ROOT / "config", data_dir=self.data, state_dir=self.state,
            discover_fn=self.discover_many(10), fetch_fn=spy,
            limit_evaluations=50, limit_fetches=3,
        )
        self.assertEqual(len(spy.seen), 3)
        self.assertEqual(result["fetch_cap"], 3)
        self.assertEqual(result["evaluation_slots"], 3, "只有抓到内容的候选才进入评估批次")

    def test_unfetched_candidates_stay_queued(self) -> None:
        result = phase_reserve(
            config_dir=ROOT / "config", data_dir=self.data, state_dir=self.state,
            discover_fn=self.discover_many(10), fetch_fn=self.counting_fetch(),
            limit_evaluations=50, limit_fetches=2,
        )
        self.assertEqual(result["queued"], 10, "未抓取的合格候选应留在队列")
        queue = json.loads((self.state / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queue["pending"]), 10)
        self.assertEqual(result["queue_pending"], 10)

    def test_excluded_candidates_are_never_fetched(self) -> None:
        """预筛排除的先筛掉，不浪费抓取。"""
        spy = self.counting_fetch()
        candidates = [
            candidate_from_repo("someone", "x", path="_template",
                                url="https://github.com/someone/x/tree/main/_template"),
            one_candidate("good"),
        ]
        phase_reserve(
            config_dir=ROOT / "config", data_dir=self.data, state_dir=self.state,
            discover_fn=fake_discover(candidates), fetch_fn=spy, limit_evaluations=50,
        )
        self.assertEqual(len(spy.seen), 1, "被预筛排除的候选不应被抓取")
        self.assertTrue(spy.seen[0].endswith("good"))

    def test_config_default_is_used_when_cli_absent(self) -> None:
        cfg_dir = self.temp_config()
        rules_path = cfg_dir / "rules.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        rules.setdefault("run_limits", {})["max_fetches_per_run"] = 2
        rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

        spy = self.counting_fetch()
        phase_reserve(
            config_dir=cfg_dir, data_dir=self.data, state_dir=self.state,
            discover_fn=self.discover_many(10), fetch_fn=spy, limit_evaluations=50,
        )
        self.assertEqual(len(spy.seen), 2, "应取 config/rules.json 的 run_limits")

    def test_cli_overrides_config(self) -> None:
        cfg_dir = self.temp_config()
        rules_path = cfg_dir / "rules.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        rules.setdefault("run_limits", {})["max_fetches_per_run"] = 5
        rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

        spy = self.counting_fetch()
        phase_reserve(
            config_dir=cfg_dir, data_dir=self.data, state_dir=self.state,
            discover_fn=self.discover_many(10), fetch_fn=spy,
            limit_evaluations=50, limit_fetches=1,
        )
        self.assertEqual(len(spy.seen), 1, "命令行应覆盖配置")


# ---------------------------------------------------------------- 队列跨轮累积

class QueueAccumulationTest(PipelineHarness):
    """§7.2：data/queue.json 是持久队列，超额候选必须在后续运行中依次处理。

    缺陷：队列每轮只保留本轮快照时，下一轮会重新发现同样的一批候选并按同样顺序
    切前 N 个，导致排在后面的候选永远轮不到评估。
    """

    def queue(self) -> dict:
        return json.loads((self.state / "queue.json").read_text(encoding="utf-8"))

    def pending_skills(self) -> list[str]:
        return [item["candidate"]["skill_id"] for item in self.queue()["pending"]]

    def test_leftover_candidates_carry_over_with_stable_order(self) -> None:
        many = [one_candidate(f"repo{i}") for i in range(6)]
        result = self.reserve(many, limit_evaluations=50, limit_fetches=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["queue_pending"], 6, "队列必须保留全部 6 条")

        # 下一次运行仍然只发现同样 6 条：应接着处理后面的候选，而不是重复前两个
        spy = self.counting_fetch()
        again = phase_reserve(
            config_dir=ROOT / "config", data_dir=self.data, state_dir=self.state,
            discover_fn=fake_discover(many), fetch_fn=spy,
            limit_evaluations=50, limit_fetches=2,
        )
        self.assertTrue(again["ok"])
        self.assertEqual(again["fetched"], 2)
        self.assertEqual(
            spy.seen,
            ["https://github.com/acme/repo2", "https://github.com/acme/repo3"],
            "第二轮必须接着处理尚未尝试过的候选",
        )
        self.assertEqual(again["queue_pending"], 6, "候选出队前不得从队列消失")

    def test_leftover_keeps_position_when_discovery_order_changes(self) -> None:
        """发现顺序变化时，未处理条目不能被挤到队尾或丢失。"""
        many = [one_candidate(f"repo{i}") for i in range(5)]
        self.reserve(many, limit_evaluations=50, limit_fetches=1)

        shuffled = [one_candidate("new0")] + many[3:] + many[:3]
        result = self.reserve(shuffled, limit_evaluations=50, limit_fetches=0)
        self.assertEqual(result["fetched"], 0, "抓取上限为 0 时不应抓取")
        self.assertEqual(result["queue_pending"], 6, "1 条已处理 + 5 条遗留 + 1 条新增")
        self.assertIn("acme/new0", self.pending_skills())

    def test_settled_item_leaves_the_queue(self) -> None:
        """评估完成后条目必须出队，否则每周名额会被同一批条目反复占用。"""
        many = [one_candidate(f"repo{i}") for i in range(4)]
        self.reserve(many, limit_evaluations=50, limit_fetches=2)
        self.evaluate()

        self.assertEqual(self.pending_skills(), ["acme/repo2", "acme/repo3"])

        again = self.reserve(many, limit_evaluations=50, limit_fetches=2)
        self.assertEqual(again["fetched"], 2)
        self.evaluate()
        self.assertEqual(self.pending_skills(), [], "全部处理完后队列应为空")

    def test_official_sources_are_prioritised(self) -> None:
        """§5.3：名额先给官方来源，再给普通新候选。"""
        community = one_candidate("community-repo")
        official = candidate_from_repo(
            "anthropics", "skills", url="https://github.com/anthropics/skills",
            source_id="anthropics_skills", discovery_method="provided_lead",
        )
        self.reserve([community, official], limit_evaluations=50, limit_fetches=1)
        queue = self.queue()["pending"]
        self.assertEqual(
            queue[0]["candidate"]["skill_id"], "anthropics/skills",
            "官方来源候选应排在前面",
        )


# ---------------------------------------------------------------- 待复核

class ReviewOnContentChangeTest(PipelineHarness):
    """§5.2：已推荐条目内容变化后保留推荐状态并醒目标记待复核，展示原评估版本；
    复核不通过则降级；复核通过才清除标记。"""

    def entry(self, skill_id: str = "acme/widget") -> dict:
        return next(e for e in self.read_catalog()["entries"] if e["skill_id"] == skill_id)

    def test_changed_content_keeps_recommendation_and_flags_review(self) -> None:
        self.reserve([one_candidate()], text="第一版\n" * 60)
        self.evaluate(evaluation=passing_evaluation("第一版简述"))
        first = self.entry()
        self.assertEqual(first["status"], "recommended")
        self.assertFalse(first["needs_review"])

        # 第二次运行：抓到新内容（配额已用尽，本轮不评估）→ 只标记待复核
        self.reserve([one_candidate()], text="第二版完全不同的内容\n" * 60,
                     limit_evaluations=0, limit_fetches=1)
        self.evaluate()  # 未预留名额 → 不调用模型，只按变化事实标记待复核
        flagged = self.entry()

        self.assertEqual(flagged["status"], "recommended", "待复核期间必须保留推荐状态")
        self.assertTrue(flagged["needs_review"], "内容变化必须标记待复核")
        self.assertIsNotNone(flagged["content_changed_at"], "内容变更时间未记录")
        self.assertNotEqual(flagged["content_fingerprint"], first["content_fingerprint"])
        self.assertIn("待复核", flagged["review_note"])

        # §5.2/§6：必须能展示原评估对应的版本，且旧评估不得冒充对新版本的验证
        prior = flagged["pending_review"]
        self.assertIsNotNone(prior, "缺少原评估快照")
        self.assertEqual(prior["content_fingerprint"], first["content_fingerprint"])
        self.assertEqual(prior["summary_zh"], "第一版简述")
        self.assertEqual(prior["evaluated_at"], first["last_checked"])
        self.assertEqual(prior["status"], "recommended")
        self.assertIsNotNone(prior["evaluated_at"], "原评估时间缺失")

    def test_quota_exhausted_still_flags_review(self) -> None:
        """§5.2：等待额度时保持待复核标记，不自动生成通过结论。"""
        self.reserve([one_candidate()], text="第一版\n" * 60)
        self.evaluate()
        self.reserve([one_candidate()], text="第二版\n" * 60,
                     limit_evaluations=0, limit_fetches=1)
        self.evaluate()

        self.assertTrue(self.entry()["needs_review"])
        self.assertEqual(self.entry()["status"], "recommended")

        # 下一轮补齐：新评估通过 → 清除标记并更新评估依据
        self.reserve([one_candidate()], text="第二版\n" * 60)
        spy = fake_evaluate(passing_evaluation("复核后的简述"))
        phase_evaluate(
            config_dir=ROOT / "config", data_dir=self.data, public_dir=self.public,
            state_dir=self.state, evaluate_fn=spy, fetch_fn=fake_fetch("第二版\n" * 60),
        )
        self.assertEqual(spy.calls, ["acme/widget"], "待复核条目必须重新评估")
        passed = self.entry()
        self.assertEqual(passed["status"], "recommended")
        self.assertFalse(passed["needs_review"], "复核通过后应清除标记")
        self.assertIsNone(passed["pending_review"])
        self.assertEqual(passed["summary_zh"], "复核后的简述")
        self.assertIsNone(passed["review_note"])

    def test_recheck_failure_downgrades_to_candidate(self) -> None:
        """§5.2：复核不通过则降级至候选区。"""
        self.reserve([one_candidate()], text="第一版\n" * 60)
        self.evaluate()
        self.reserve([one_candidate()], text="第二版\n" * 60,
                     limit_evaluations=0, limit_fetches=1)
        self.evaluate()
        self.reserve([one_candidate()], text="第二版\n" * 60)
        self.evaluate(evaluation=EVAL_BY_ID["missing_dependency"])

        downgraded = self.entry()
        self.assertEqual(downgraded["status"], "candidate", "复核不通过应降级")
        self.assertTrue(downgraded["needs_review"])
        self.assertIn("CONTENT_CHANGED", downgraded["reason_codes"])
        self.assertIsNotNone(downgraded["pending_review"])

    def test_unchanged_content_is_not_flagged(self) -> None:
        self.reserve([one_candidate()], text="同一版内容\n" * 60)
        self.evaluate()
        self.reserve([one_candidate()], text="同一版内容\n" * 60)
        self.evaluate()

        entry = self.entry()
        self.assertFalse(entry["needs_review"], "内容未变化不得标记待复核")
        self.assertIsNone(entry["content_changed_at"])
        self.assertIsNone(entry["pending_review"])

    def test_page_data_exposes_the_review_state(self) -> None:
        """§6：网页必须能看到醒目标记与原评估版本。"""
        self.reserve([one_candidate()], text="第一版\n" * 60)
        self.evaluate(evaluation=passing_evaluation("第一版简述"))
        self.reserve([one_candidate()], text="第二版\n" * 60,
                     limit_evaluations=0, limit_fetches=1)
        self.evaluate()

        page = json.loads((self.public / "data" / "catalog.json").read_text(encoding="utf-8"))
        shown = page["recommended"][0]
        self.assertTrue(shown["needs_review"])
        self.assertEqual(shown["pending_review"]["summary_zh"], "第一版简述")
        self.assertIn("待复核", shown["review_note"])

    def test_report_records_the_review_state(self) -> None:
        self.reserve([one_candidate()], text="第一版\n" * 60)
        self.evaluate()
        self.reserve([one_candidate()], text="第二版\n" * 60,
                     limit_evaluations=0, limit_fetches=1)
        self.evaluate()

        reports = sorted((self.data / "reports").glob("*.json"))
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        self.assertEqual(report["counts"][CONTENT_CHANGED], 1, "周报必须记录内容变化")
        entry = self.entry()
        self.assertTrue(entry["needs_review"], "周报与索引对待复核状态的记录必须一致")


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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            written = write_report(
                report, json_path=Path(tmp) / "r.json", markdown_path=Path(tmp) / "r.md"
            )
            self.assertTrue(Path(written["report_json"]).exists())
            self.assertTrue(Path(written["report_markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
