"""治理与干预规则单元测试：人工干预(overrides)、冷冻(snooze)、已收录(owned)及数据隔离保护。

合并自原有 test_overrides.py、test_snooze.py、test_owned.py、test_owned_integration.py、test_manage_owned.py 与 test_data_isolation.py。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.catalog.dedupe import candidate_from_repo
from src.catalog.index import (
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PROCESSING_FAILURE,
    STATUS_RECOMMENDED,
    build_catalog,
    build_entry,
    build_page_data,
)
from src.catalog.favorites import (
    get_manual_picks,
    load_favorites,
    validate_favorites,
)
from src.catalog.filter_rules import (
    FilterRules,
    eligible_for_topic_filter,
    filter_new_evaluation,
    load_filter_rules,
    validate_filter_rules,
)
from src.catalog.overrides import (
    apply_manual_overrides,
    apply_manual_overrides_to_entry,
    get_manual_exclusions,
    load_overrides,
    validate_overrides,
)
from src.catalog.snooze import (
    DEFAULT_SNOOZE_DAYS,
    apply_snooze_overrides,
    compute_expires_at,
    get_active_snoozed,
    is_active_snooze,
    load_snooze,
    validate_snooze,
)
from src.shared.owned import (
    OWNED_SCHEMA_VERSION,
    OwnedPatchConflictError,
    apply_owned_patch,
    build_public_owned_projection,
    is_skill_owned,
    normalize_owned_id,
    validate_owned_config,
    validate_owned_item,
    validate_owned_patch,
)
from src.infra.owned import (
    apply_and_save_owned_patch,
    load_owned_config,
    load_owned_ids,
    save_owned_config,
)
from src.shared.runtime import is_test_environment
from tests import smoke
from tools.manage_owned import main as manage_owned_main

ROOT = Path(__file__).resolve().parents[1]


@smoke
class OverridesTest(unittest.TestCase):
    """人工收藏与排除名单校验及应用。"""
    def test_valid_picks_pass(self):
        data = {
            "favorites_version": "1.0.0",
            "manual_picks": [
                {
                    "skill_id": "owner/repo:path/SKILL.md",
                    "reason": "结构合我习惯",
                    "added_at": "2026-09-21",
                    "from": "candidate",
                }
            ],
        }
        errors = validate_favorites(data, known_skill_ids={"owner/repo:path/SKILL.md"})
        self.assertEqual(errors, [])

    def test_missing_or_empty_skill_id(self):
        data = {
            "manual_picks": [
                {"skill_id": "", "reason": "test", "added_at": "2026-09-21"},
            ]
        }
        errors = validate_favorites(data)
        self.assertTrue(len(errors) > 0)

    def test_overrides_rejects_legacy_fields(self):
        legacy_picks = {"overrides_version": "1.0.0", "manual_picks": [{"skill_id": "a/b:c/SKILL.md"}]}
        errors = validate_overrides(legacy_picks)
        self.assertTrue(any("manual_picks" in e for e in errors))

        legacy_kw = {"overrides_version": "1.0.0", "keyword_exclusions": ["expo"]}
        errors = validate_overrides(legacy_kw)
        self.assertTrue(any("keyword_exclusions" in e for e in errors))

    def test_valid_exclusions_pass(self):
        data = {
            "overrides_version": "1.0.0",
            "manual_exclusions": [
                {
                    "skill_id": "bad/repo:skills/demo/SKILL.md",
                    "reason": "广告",
                    "added_at": "2026-09-21",
                }
            ],
        }
        errors = validate_overrides(data)
        self.assertEqual(errors, [])

    def test_apply_manual_picks_upgrades_candidate(self):
        entry = {
            "skill_id": "author/repo:skills/demo/SKILL.md",
            "name": "demo",
            "status": "candidate",
            "reason_codes": ["NOT_STANDALONE_TOOL"],
        }
        picks = {
            "author/repo:skills/demo/SKILL.md": {
                "skill_id": "author/repo:skills/demo/SKILL.md",
                "reason": "个人偏好",
                "from": "candidate",
            }
        }
        apply_manual_overrides_to_entry(entry, picks, {})
        self.assertTrue(entry.get("manual_pick"))
        self.assertEqual(entry["manual_note"]["reason"], "个人偏好")

    def test_offline_recovery_and_enrichment_preserve_favorites(self):
        from src.catalog.budget import BudgetLedger
        from src.catalog.entry_state import EntryUpdateEvent, update_entry
        from src.catalog.index import CatalogContext
        from src.catalog.maintenance import recover_completed_results, enrich_catalog, sync_config_to_catalog
        from src.catalog.models import Candidate, PrescreenResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            config = root / "config" / "governance"
            config.mkdir(parents=True)
            context = CatalogContext(rules_version="1.0.0")
            favorite = Candidate("demo/repo:skills/favorite/SKILL.md", "demo", "repo", name="favorite")
            entry = update_entry(None, favorite, EntryUpdateEvent(
                kind="fresh_evaluation", evaluation={"tags": ["营销规划"]},
                decision={"decision": "candidate"}, evaluation_id="prior-favorite",
            ), context)
            favorites = {"favorites_version": "1.0.0", "manual_picks": [
                {"skill_id": favorite.skill_id, "reason": "keep", "added_at": "2026-10-01", "from": "candidate"},
                {"skill_id": favorite.skill_id.replace("favorite", "retired"), "reason": "old", "added_at": "2026-09-01", "retired_at": "2026-09-30"},
                {"skill_id": "demo/repo:skills/owned/SKILL.md", "reason": "keep owned", "added_at": "2026-10-01"},
            ]}
            retired = copy.deepcopy(entry)
            retired["skill_id"] = favorites["manual_picks"][1]["skill_id"]
            exclusions = {"overrides_version": "1.0.0", "manual_exclusions": []}
            snoozed = {"snooze_version": "1.0.0", "snoozed": [{"skill_id": "demo/repo:skills/old-cold/SKILL.md", "reason": "wait",
                "snoozed_at": "2026-09-29", "expires_at": "2027-02-26", "days": 150}]}
            # 从旧投影开始，确认离线迁移只改变配置归属和原人工标记。
            legacy = {**exclusions, "manual_picks": favorites["manual_picks"], "keyword_exclusions": ["expo"]}
            catalog = build_catalog([entry, retired], context=context, overrides=legacy, snoozed=snoozed)
            (data / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            for name, payload in (("favorites.json", favorites), ("overrides.json", exclusions),
                                  ("snoozed.json", snoozed),
                                  ("filter-rules.json", {"blocked_topics": [{"name": "营销推广与获客增长"}]}),
                                  ("owned-skills.json", {"schema_version": OWNED_SCHEMA_VERSION, "items": [
                                      {"skill_id": "demo/repo:skills/owned/SKILL.md", "name": "owned", "added_at": "2026-10-01"}]})):
                (config / name).write_text(json.dumps(payload), encoding="utf-8")
            sync_config_to_catalog(root)
            migrated = json.loads((data / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(migrated["favorites"], favorites)
            self.assertEqual(migrated["entries"][0]["status"], "candidate")
            self.assertEqual(migrated["entries"][0]["tags"], ["营销规划"])
            self.assertEqual(migrated["snoozed"], snoozed)
            typo = copy.deepcopy(snoozed)
            typo["snoozed"][0]["skill_id"] = "demo/repo:skills/unknown-typo/SKILL.md"
            (config / "snoozed.json").write_text(json.dumps(typo), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown-typo"):
                sync_config_to_catalog(root)
            (config / "snoozed.json").write_text(json.dumps(snoozed), encoding="utf-8")

            other = Candidate("demo/repo:skills/new/SKILL.md", "demo", "repo", name="new")
            ledger = BudgetLedger.load(data / "local" / "state", 10)
            ledger.reserve([{"evaluation_id": "new-result", "skill_id": other.skill_id}])
            ledger.complete("new-result", {"decision": "candidate", "evaluation": {"tags": ["general"]},
                "candidate": asdict(other), "prescreen": asdict(PrescreenResult(other.skill_id, "queued"))})
            # 恢复只能用已提交的人工配置，不能趁机同步尚未提交的收藏修改。
            (config / "favorites.json").write_text(json.dumps({"manual_picks": []}), encoding="utf-8")
            with patch("src.catalog.prescreen.load_config", return_value=SimpleNamespace(
                    rules={"rules_version": "1.0.0"}, domain_names={} )):
                recovered = recover_completed_results(root)
            self.assertEqual(recovered["restored"], 1)
            for operation in (lambda: None, lambda: enrich_catalog(root)):
                operation()
                saved = json.loads((data / "catalog.json").read_text(encoding="utf-8"))
                page = json.loads((root / "public" / "data" / "catalog.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["favorites"], favorites)
                self.assertEqual(page["favorites"], favorites)
                found = next(e for e in saved["entries"] if e["skill_id"] == favorite.skill_id)
                self.assertTrue(found["manual_pick"])
                self.assertEqual(found["manual_note"]["reason"], "keep")
                self.assertEqual(found["status"], "candidate")


@smoke
class FilterRulesTest(unittest.TestCase):
    """主题规则、主要用途判断及首次评估边界；不增加模型调用。"""
    def test_validate_filter_rules(self):
        valid = {
            "filter_rules_version": "2.0.0",
            "_comment": ["新增主题只需要填写 name。", "description 可以省略。"],
            "blocked_topics": [
                {"name": "手机 App", "description": "主要用于 Android、iOS 或 Expo 手机应用。"},
                {"name": "营销推广与获客增长"},
            ],
        }
        self.assertEqual(validate_filter_rules(valid), [])
        configured = FilterRules(valid)
        comments_changed = {**valid, "_comment": ["这里只修改说明。"]}
        self.assertEqual(configured.fingerprint, FilterRules(comments_changed).fingerprint)
        self.assertTrue(configured.has_evaluation_rules)
        self.assertFalse(FilterRules().has_evaluation_rules)
        for bad in (
            {"blocked_topics": "手机 App"},
            {"blocked_topics": ["手机 App"]},
            {"blocked_topics": [{"name": ""}]},
            {"blocked_topics": [{"name": " 手机 App"}]},
            {"blocked_topics": [{"name": "手机 App", "description": 123}]},
            {"blocked_topics": [{"name": "手机 App"}, {"name": "手机 App"}]},
            {"discovery": {"blocked_keywords": ["expo"]}},
            {"evaluation": {"blocked_tags": ["营销规划"]}},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(validate_filter_rules(bad))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filter-rules.json"
            self.assertFalse(load_filter_rules(path).has_evaluation_rules)
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(load_filter_rules(path).fingerprint, configured.fingerprint)

    def test_topic_prompt_uses_the_normal_assessment_request(self):
        from src.catalog.evaluation import build_prompt, evaluate
        from src.catalog.local import _unknown_usage_reserve
        from src.catalog.models import Candidate
        from src.infra.llm import ModelCallResult

        topics = FilterRules({"blocked_topics": [
            {"name": "手机 App", "description": "主要用于 Android、iOS 手机应用。"},
            {"name": "营销推广与获客增长"},
        ]})
        candidate = Candidate("test/repo:SKILL.md", "test", "repo", name="demo")
        taxonomy = {"main_categories": [{"id": "dev", "name": "开发"}]}
        rules = {"rules_version": "1.0.0"}
        system, _ = build_prompt(candidate, "通用开发工具。", rules, taxonomy, filter_rules=topics)
        for topic in topics.blocked_topics:
            self.assertIn(topic["name"], system)
            self.assertIn(topic["topic_id"], system)
        self.assertIn("主要", system)
        self.assertIn("unknown", system)
        self.assertIn("no_match", system)
        cfg = {"rules": rules, "taxonomy": taxonomy, "model": {}}
        self.assertGreater(
            _unknown_usage_reserve(candidate, "材料", cfg, filter_rules=topics),
            _unknown_usage_reserve(candidate, "材料", cfg),
        )
        for inactive in (None, FilterRules()):
            baseline, _ = build_prompt(candidate, "材料", rules, taxonomy, filter_rules=inactive)
            self.assertNotIn("topic_assessments", baseline)
        # 主题字段缺失或非法不应令正常评估失败，也不应追加请求来补问。
        for topic_output in (None, "bad", [{"topic_id": "unknown", "result": "match", "evidence": "广告"}]):
            with self.subTest(topic_output=topic_output):
                raw = {"main_category": "dev", "topic_assessments": topic_output}
                response = ModelCallResult(ok=True, content=json.dumps(raw), attempts=1)
                with patch("src.catalog.evaluation.call_model", return_value=response) as model:
                    result = evaluate(candidate, "通用开发工具。", model_cfg={}, rules=rules,
                        taxonomy=taxonomy, filter_rules=topics)
                self.assertTrue(result["ok"])
                self.assertEqual(model.call_count, 1)
                self.assertEqual(len(result["calls"]), 1)
        response = ModelCallResult(ok=True, content=json.dumps({"main_category": "dev"}))
        with patch("src.catalog.evaluation.call_model", return_value=response) as model:
            # Finder 等调用方省略新参数时保持原来的评估调用方式。
            result = evaluate(candidate, "通用开发工具。", model_cfg={}, rules=rules, taxonomy=taxonomy)
        self.assertTrue(result["ok"])
        model.assert_called_once()
        self.assertNotIn("topic_assessments", model.call_args.args[1])

    def test_invalid_topic_assessments_preserve_the_normal_decision(self):
        from src.catalog.evaluation import parse_evaluation

        topics = FilterRules({"blocked_topics": [{"name": "营销推广与获客增长"}]})
        topic_id = topics.blocked_topics[0]["topic_id"]
        match = {"topic_id": topic_id, "result": "match", "evidence": "主要用于广告投放。"}
        decision = {"decision": "recommended", "reason_codes": []}
        for raw in (
            None, "bad", {}, [],
            [{"result": "match", "evidence": "广告投放"}],
            [{**match, "topic_id": "unknown-topic"}],
            [{**match, "topic_id": []}],
            [{**match, "topic_id": {"id": topic_id}}],
            [{**match, "result": "yes"}],
            [{"topic_id": topic_id, "result": "match"}],
            [{**match, "evidence": "   "}],
            [{**match, "evidence": ["广告投放"]}],
            [match, {**match, "result": "no_match"}],
            [match, match],
            [None, "match"],
        ):
            with self.subTest(raw=raw):
                parsed = parse_evaluation(json.dumps({"topic_assessments": raw}), {}, None,
                    filter_rules=topics)
                filtered = filter_new_evaluation(decision, parsed, topics)
                self.assertEqual(filtered["decision"], "recommended")
                self.assertNotIn("TOPIC_FILTERED", filtered["reason_codes"])
        self.assertEqual(decision, {"decision": "recommended", "reason_codes": []})

    def test_topic_filter_uses_main_purpose_and_preserves_history(self):
        from src.catalog.evaluation import evaluate, parse_evaluation
        from src.catalog.models import Candidate

        topics = FilterRules({"blocked_topics": [{"name": "营销推广与获客增长"}]})
        topic_id = topics.blocked_topics[0]["topic_id"]
        decision = {"decision": "candidate", "reason_codes": ["REVIEW_NEEDED"]}
        evaluation = {"tags": ["获客策略"], "topic_assessments": [
            {"topic_id": topic_id, "result": "match", "evidence": "以会员获客和广告投放为主要用途。"}]}
        before = copy.deepcopy(evaluation)
        filtered = filter_new_evaluation(decision, evaluation, topics)
        self.assertEqual(filtered["decision"], "excluded")
        self.assertEqual(filtered["blocked_topics"], ["营销推广与获客增长"])
        self.assertTrue(filtered["topic_filtered"])
        self.assertEqual(filtered["original_decision"], decision)
        self.assertEqual(filtered["reason_codes"], ["REVIEW_NEEDED", "TOPIC_FILTERED"])
        audit = filtered["topic_filter_audit"]
        self.assertEqual(audit["rules_fingerprint"], topics.fingerprint)
        self.assertEqual(audit["topic_assessments"][0]["result"], "match")
        self.assertEqual(evaluation, before)
        # 标签只是展示元数据；偶然提及和材料不足均保留基础评估结论。
        for result in ("no_match", "unknown"):
            with self.subTest(result=result):
                incidental = {"tags": ["营销规划"], "topic_assessments": [
                    {"topic_id": topic_id, "result": result, "evidence": "仅作为通用工具的示例。"}]}
                self.assertEqual(filter_new_evaluation(decision, incidental, topics)["decision"], "candidate")
        for previous in (
            {"status": "recommended"}, {"status": "candidate"}, {"manual_pick": True},
            {"status": "excluded", "evaluated_at": "2026-09-30"},
            {"status": "processing_failure", "last_evaluation_id": "old-evaluation"},
        ):
            with self.subTest(previous=previous):
                self.assertFalse(eligible_for_topic_filter(topics, previous))
                self.assertEqual(filter_new_evaluation(decision, evaluation, topics, previous), decision)
        self.assertFalse(eligible_for_topic_filter(topics, previously_evaluated=True))
        self.assertEqual(filter_new_evaluation(decision, evaluation, topics, previously_evaluated=True), decision)
        self.assertTrue(eligible_for_topic_filter(topics, {"status": "processing_failure"}))
        # 新响应不能自己声明规则；续跑只能使用当时程序保存的审计快照。
        changed = FilterRules({"blocked_topics": [{"name": "游戏开发"}]})
        forged_audit = filter_new_evaluation(decision, {}, changed)["topic_filter_audit"]
        taxonomy = {"main_categories": [{"id": "dev", "name": "开发"}]}
        rules = {"rules_version": "1.0.0"}
        parsed = parse_evaluation(json.dumps({**evaluation, "main_category": "dev",
            "topic_filter_audit": forged_audit}), rules, "content", taxonomy, filter_rules=topics)
        self.assertEqual(parsed["topic_filter_audit"]["rules_fingerprint"], topics.fingerprint)
        candidate = Candidate("test/repo:SKILL.md", "test", "repo", content_fingerprint="content")
        for current in (changed, FilterRules()):
            with self.subTest(resume=current.blocked_topics), patch("src.catalog.evaluation.call_model") as model:
                resumed = evaluate(candidate, "材料", model_cfg={}, rules=rules, taxonomy=taxonomy,
                    pending_evaluation=parsed, filter_rules=current)
                self.assertTrue(resumed["ok"])
                model.assert_not_called()
                saved = filter_new_evaluation(decision, resumed["evaluation"], current)
                self.assertEqual(saved["blocked_topics"], ["营销推广与获客增长"])
                self.assertEqual(saved["topic_filter_audit"]["rules_fingerprint"], topics.fingerprint)
        legacy = {k: v for k, v in parsed.items() if k not in ("topic_assessments", "topic_filter_audit")}
        with patch("src.catalog.evaluation.call_model") as model:
            resumed = evaluate(candidate, "材料", model_cfg={}, rules=rules, taxonomy=taxonomy,
                pending_evaluation=legacy, filter_rules=changed)
        self.assertTrue(resumed["ok"])
        model.assert_not_called()
        self.assertEqual(filter_new_evaluation(decision, resumed["evaluation"], changed), decision)

    def test_stage_filters_only_affect_new_entries(self):
        from src.catalog.local import prepare_pool, _topic_evaluation_options
        from src.catalog.pool import create_pool_from_candidates, save_pool
        from src.catalog.models import Candidate, PrescreenResult
        from src.catalog.entry_state import EntryUpdateEvent, update_entry
        from src.catalog.index import CatalogContext
        from src.catalog.budget import BudgetLedger
        from src.catalog.evaluation import evaluation_id
        from src.catalog.sync_evaluate import _evaluate_queue, _build_evaluated_catalog
        from src.shared.identity import content_fingerprint
        from src.shared.runtime import now_local

        rules = FilterRules({"blocked_topics": [{"name": "手机 App"}, {"name": "营销推广与获客增长"}]})
        topic_id = rules.blocked_topics[1]["topic_id"]
        text = "---\nname: demo\ndescription: A useful skill\n---\n# Demo\n" + "Complete a useful task. " * 20
        context = CatalogContext(rules_version="1.0.0")
        names = ("recommended", "candidate", "favorite", "pending", "new", "history", "retry")
        candidates = [Candidate(f"demo/repo:skills/expo-{name}/SKILL.md", "demo", "repo",
            name=f"expo-{name}", path=f"skills/expo-{name}/SKILL.md", content_fingerprint=content_fingerprint(text)) for name in names]
        candidates[4].description = "A general tool with an optional App Store publishing example."
        previous = [update_entry(None, c, EntryUpdateEvent(kind="fresh_evaluation",
            evaluation={"tags": ["general"]}, decision={"decision": status},
            evaluation_id="prior-" + name), context) for c, status, name in zip(
                candidates[:3], ("recommended", "candidate", "candidate"), names[:3])]
        previous[2]["manual_pick"] = True
        previous.append({"skill_id": candidates[-1].skill_id, "status": "processing_failure"})
        cfg = {"searches": {}, "sources": {}, "source_types": {}, "filter_rules": rules,
            "model": {"model": "mock-model", "model_config_version": "1.0.0"},
            "rules": {"rules_version": "1.0.0"}, "taxonomy": {}, "prescreen": SimpleNamespace(domain_names={}),
            "favorites": {"manual_picks": [{"skill_id": candidates[2].skill_id}]},
            "overrides": {"manual_exclusions": []}}
        local_state = SimpleNamespace(cfg=cfg, entries={e["skill_id"]: e for e in previous},
            evaluated_skill_ids={candidates[5].skill_id})
        for candidate, name in zip(candidates, names):
            with self.subTest(local=name):
                expected = {"filter_rules": rules} if name in ("pending", "new", "retry") else {}
                self.assertEqual(_topic_evaluation_options(local_state, candidate), expected)

        for mode in ("refresh", "expired", "refill"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                local = root / "data" / "local"
                old_pool = create_pool_from_candidates([candidates[3]])
                if mode == "expired":
                    old_pool.built_at = "2000-01-01T00:00:00+08:00"
                save_pool(local / "pool.json", old_pool)
                (root / "data" / "catalog.json").write_text(json.dumps({"entries": previous[:3]}), encoding="utf-8")
                report = {}
                discover = Mock(return_value=(candidates[:5], []))
                pool = prepare_pool(root, local, cfg, {"refresh_pool": mode == "refresh", "pool_watermark": 99},
                    {candidates[0].skill_id}, discover_fn=discover, log=lambda *a: None, report=report)
                # 名称中的 Expo / App Store 不再提前拦截，新条目进入正常评估。
                self.assertEqual({i.candidate.skill_id for i in pool.items}, {c.skill_id for c in candidates[:5]})
                self.assertFalse(report.get("discovery_filtered"))

        with tempfile.TemporaryDirectory() as tmp:
            ledger = BudgetLedger.load(Path(tmp) / "state", 20)
            ledger.reserve([{"evaluation_id": "old-history", "skill_id": candidates[5].skill_id}])
            ledger.complete("old-history", {"decision": "recommended", "evaluation": {"tags": ["general"]}})
            queue = {"pending": []}
            for c in candidates:
                eid = evaluation_id(c, cfg["model"], cfg["rules"])
                ledger.reserve([{"evaluation_id": eid, "skill_id": c.skill_id}])
                queue["pending"].append({"candidate": asdict(c), "prescreen": asdict(PrescreenResult(c.skill_id, "queued")),
                    "content_fingerprint": c.content_fingerprint, "fetch": {"ok": True}})
            def assess(candidate, _text, **kwargs):
                result = {"tags": ["获客策略"]}
                if kwargs.get("filter_rules"):
                    result["topic_assessments"] = [
                        {"topic_id": rules.blocked_topics[0]["topic_id"], "result": "no_match", "evidence": "主要做营销获客。"},
                        {"topic_id": topic_id, "result": "match", "evidence": "主要用于会员获客和广告投放。"},
                    ]
                return {"ok": True, "evaluation": result}
            evaluation = Mock(side_effect=assess)
            fetch = Mock(side_effect=AssertionError("unexpected network"))
            _evaluate_queue(queue, cfg, ledger, {c.skill_id: text for c in candidates}, now_local(), 0,
                fetch, evaluation, None, lambda *a: None, previous_entries=previous)
            outcomes = {c.name: (ledger.get(evaluation_id(c, cfg["model"], cfg["rules"])) or {}).get("outcome", {}) for c in candidates}
            for name in ("recommended", "candidate", "history"):
                self.assertEqual(outcomes[f"expo-{name}"]["decision"], "recommended")
                self.assertNotIn("TOPIC_FILTERED", outcomes[f"expo-{name}"]["reason_codes"])
            for name in ("pending", "new", "retry"):
                outcome = outcomes[f"expo-{name}"]
                self.assertEqual(outcome["decision"], "excluded")
                self.assertEqual(outcome["blocked_topics"], ["营销推广与获客增长"])
                self.assertEqual(outcome["original_decision"]["decision"], "recommended")
                self.assertEqual(outcome["topic_filter_audit"]["rules_fingerprint"], rules.fingerprint)
            calls_by_name = {call.args[0].name: call for call in evaluation.call_args_list}
            for name in ("recommended", "candidate", "history"):
                self.assertIsNone(calls_by_name[f"expo-{name}"].kwargs.get("filter_rules"))
            for name in ("pending", "new", "retry"):
                self.assertIs(calls_by_name[f"expo-{name}"].kwargs.get("filter_rules"), rules)
            evaluation_calls = evaluation.call_count
            rebuilt = _build_evaluated_catalog(previous, queue, cfg, ledger, context)
            self.assertEqual(next(e for e in rebuilt["entries"] if e["skill_id"] == candidates[0].skill_id)["status"], "recommended")
            favorite = next(e for e in rebuilt["entries"] if e["skill_id"] == candidates[2].skill_id)
            self.assertEqual(favorite["status"], "candidate")
            self.assertTrue(favorite["manual_pick"])
            # 后续缓存复用不因当前规则改变而重新检查；账本结论保持原样。
            cfg["filter_rules"] = FilterRules()
            self.assertEqual(_topic_evaluation_options(local_state, candidates[4]), {})
            _evaluate_queue(queue, cfg, ledger, {}, now_local(), 0, fetch, evaluation, None, lambda *a: None, previous_entries=previous)
            self.assertEqual(evaluation.call_count, evaluation_calls)
            self.assertEqual(ledger.get(evaluation_id(candidates[4], cfg["model"], cfg["rules"]))["outcome"]["decision"], "excluded")
            fetch.assert_not_called()


@smoke
class SnoozeTest(unittest.TestCase):
    """150天暂不关注（Snooze）边界与过期计算。"""
    def test_compute_expires_at_150_days(self):
        # 2026-09-22 + 150天 = 2027-02-19
        expires = compute_expires_at("2026-09-22", 150)
        self.assertEqual(expires, "2027-02-19")

    def test_is_active_snooze_interval(self):
        item = {"snoozed_at": "2026-09-01", "expires_at": "2027-01-29"}
        # 左闭右开：开始当天生效，到期当天恢复
        self.assertTrue(is_active_snooze(item, "2026-09-01"))
        self.assertTrue(is_active_snooze(item, "2026-12-01"))
        self.assertFalse(is_active_snooze(item, "2027-01-29"))
        self.assertFalse(is_active_snooze(item, "2027-02-01"))
        self.assertFalse(is_active_snooze(item, "2026-08-31"))

    def test_validate_snooze_rules(self):
        valid = {
            "snooze_version": "1.0.0",
            "snoozed": [
                {
                    "skill_id": "author/tool:SKILL.md",
                    "snoozed_at": "2026-09-22",
                    "expires_at": "2027-02-19",
                    "days": 150,
                    "reason": "暂不使用",
                }
            ],
        }
        self.assertEqual(validate_snooze(valid), [])


@smoke
class OwnedTest(unittest.TestCase):
    """已收录规范化、校验、补丁合并与冲突处理。"""
    def test_normalize_owned_id(self):
        self.assertEqual(
            normalize_owned_id("Anthropics/Skills:Skills/PDF/SKILL.md"),
            "anthropics/skills:Skills/PDF/SKILL.md",
        )
        self.assertEqual(
            normalize_owned_id("vercel-labs/agent-skills:skills/react/SKILL.md"),
            "vercel-labs/agent-skills:skills/react/SKILL.md",
        )

    def test_validate_owned_item_rejects_private_fields(self):
        item = {
            "skill_id": "owner/repo:skills/demo/SKILL.md",
            "name": "demo",
            "added_at": "2026-09-24",
            "managed_url": "https://private.corp.internal/skills/demo",
        }
        with self.assertRaises(ValueError) as ctx:
            validate_owned_item(item)
        self.assertIn("managed_url", str(ctx.exception))

    def test_apply_owned_patch(self):
        base = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "owner/repo:skills/a/SKILL.md", "name": "a", "added_at": "2026-09-01"}
            ]
        }
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/b/SKILL.md",
                    "before": None,
                    "after": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "b", "added_at": "2026-09-24"},
                }
            ],
        }
        res = apply_owned_patch(base, patch)
        self.assertEqual(len(res["items"]), 2)
        self.assertTrue(any(s["skill_id"] == "owner/repo:skills/b/SKILL.md" for s in res["items"]))

    def test_patch_conflict_on_stale_base(self):
        base = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-2", "added_at": "2026-09-02"}
            ]
        }
        patch = {
            "schema_version": "1.0.0",
            "changes": [
                {
                    "skill_id": "owner/repo:skills/b/SKILL.md",
                    "before": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-1", "added_at": "2026-09-01"},
                    "after": {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-3", "added_at": "2026-09-03"},
                }
            ],
        }
        with self.assertRaises(OwnedPatchConflictError):
            apply_owned_patch(base, patch)


@smoke
class DataIsolationTest(unittest.TestCase):
    """测试环境与生产数据的物理隔离保护。"""
    def test_environment_detector_identifies_unittest_runner(self) -> None:
        self.assertTrue(is_test_environment())


if __name__ == "__main__":
    unittest.main()
