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
from src.infra.llm import ModelCallResult
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




class ExistingCatalogFilterTest(unittest.TestCase):
    """存量目录黑名单一次性过滤维护工具端到端契约与治理安全测试。"""

    @staticmethod
    def _mk_entry(skill_id, name="", status="recommended", summary="", manual_pick=False, **kw):
        base = {
            "skill_id": skill_id,
            "name": name or skill_id.split(":")[0],
            "status": status,
            "url": f"https://github.com/{skill_id}",
            "author": skill_id.split("/")[0],
            "main_category": {"id": "other", "name": "其他"},
            "tags": [],
            "platform_declared": None,
            "dependencies_declared": [],
            "source_type": "github_search",
            "needs_review": False,
            "review_note": None,
            "key_features": [],
            "example_requests": [],
            "limitations": [],
            "summary_zh": summary,
            "reason_codes": [],
            "first_seen": "2026-10-01T00:00:00+08:00",
            "last_checked": "2026-10-01T00:00:00+08:00",
            "content_changed_at": "2026-10-01T00:00:00+08:00",
            "upstream_status": "active",
            "license": None,
            "manual_pick": manual_pick,
        }
        base.update(kw)
        return base

    @staticmethod
    def _mock_topic_response(topic_ecommerce_id, topic_gym_id, match_ecom=False, quote="", evidence="", usage=None):
        assessments = [
            {
                "topic_id": topic_ecommerce_id,
                "result": "match" if match_ecom else "no_match",
                "evidence_source": "summary_zh" if match_ecom else "",
                "evidence_quote": quote if match_ecom else "",
                "evidence": evidence or ("核心用途是电商商品文案" if match_ecom else "无关"),
            },
            {
                "topic_id": topic_gym_id,
                "result": "no_match",
                "evidence_source": "",
                "evidence_quote": "",
                "evidence": "无关",
            },
        ]
        content = json.dumps({"topic_assessments": assessments})
        return ModelCallResult(ok=True, content=content, model="test-model", usage=usage)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # 目录结构
        (self.root / "data").mkdir(parents=True)
        (self.root / "public" / "data").mkdir(parents=True)
        (self.root / "config" / "governance").mkdir(parents=True)
        (self.root / "config" / "models").mkdir(parents=True)

        # 1. 构造 filter-rules.json
        self.rules_data = {
            "filter_rules_version": "1.0.0",
            "blocked_topics": [
                {"name": "电商文案", "description": "用于电商商品详情、促销广告或带货文案编写。"},
                {"name": "健身房运营", "description": "用于健身房会员管理与排课。"}
            ]
        }
        (self.root / "config" / "governance" / "filter-rules.json").write_text(
            json.dumps(self.rules_data, ensure_ascii=False), encoding="utf-8"
        )

        # 2. 构造 favorites.json
        self.fav_data = {
            "favorites_version": "1.0.0",
            "manual_picks": [
                {"skill_id": "fav/skill:SKILL.md", "reason": "收藏保护", "added_at": "2026-10-01"}
            ]
        }
        (self.root / "config" / "governance" / "favorites.json").write_text(
            json.dumps(self.fav_data, ensure_ascii=False), encoding="utf-8"
        )

        # 3. 构造 overrides.json (含一条活跃排除和一条退休排除)
        self.ov_data = {
            "overrides_version": "1.0.0",
            "manual_exclusions": [
                {"skill_id": "already/excluded:SKILL.md", "reason": "原有手工排除", "added_at": "2026-09-01"},
                {"skill_id": "retired/skill:SKILL.md", "reason": "已退休排除", "added_at": "2026-08-01", "retired_at": "2026-09-15"}
            ]
        }
        (self.root / "config" / "governance" / "overrides.json").write_text(
            json.dumps(self.ov_data, ensure_ascii=False), encoding="utf-8"
        )

        # 4. 构造 snoozed.json (含一条活跃冷冻)
        self.snooze_data = {
            "snooze_version": "1.0.0",
            "default_snooze_days": 150,
            "snoozed": [
                {"skill_id": "snoozed/skill:SKILL.md", "snoozed_at": "2026-10-01", "expires_at": "2027-03-01", "reason": "临时冷冻"}
            ]
        }
        (self.root / "config" / "governance" / "snoozed.json").write_text(
            json.dumps(self.snooze_data, ensure_ascii=False), encoding="utf-8"
        )

        # 5. 构造 owned-skills.json
        self.owned_data = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "official/owned:SKILL.md", "name": "官方资产", "added_at": "2026-09-01"}
            ]
        }
        (self.root / "config" / "governance" / "owned-skills.json").write_text(
            json.dumps(self.owned_data, ensure_ascii=False), encoding="utf-8"
        )

        # 6. 构造 model.json
        self.model_data = {
            "model_config_version": "1.0.0",
            "provider_id": "test",
            "endpoint": "https://api.test.com/v1/chat/completions",
            "model": "test-model",
            "auth": {"api_key": "test-key"}
        }
        (self.root / "config" / "models" / "model.json").write_text(
            json.dumps(self.model_data, ensure_ascii=False), encoding="utf-8"
        )

        # 7. 构造 catalog.json
        self.entries = [
            self._mk_entry("target/ecommerce:SKILL.md", "电商文案爆款大师", "recommended", "核心功能是撰写电商商品详情与促销广告文案。", key_features=["一键生成商品详情页", "自动提炼卖点"], example_requests=["帮我写一段保温杯详情"], tags=["电商", "文案"]),
            self._mk_entry("target/coder:SKILL.md", "Python 编程助手", "candidate", "编写高内聚低耦合的代码，附带单元测试。", key_features=["代码重构"], example_requests=["重构这个类"], tags=["开发"]),
            self._mk_entry("snoozed/skill:SKILL.md", "健身房排课排期表", "recommended", "专为健身房运营设计的会员与私教课程排班系统。", key_features=["私教排课"], tags=["健身"]),
            self._mk_entry("retired/skill:SKILL.md", "旧版促销文案脚本", "candidate", "用于生成各类电商秒杀与满减文案。", key_features=["秒杀文案"]),
            self._mk_entry("fav/skill:SKILL.md", "受保护的收藏技能", "recommended", "虽然包含电商，但被收藏保护。", manual_pick=True),
            self._mk_entry("already/excluded:SKILL.md", "已被手工排除技能", "excluded", "已在排除名单中。"),
            self._mk_entry("official/owned:SKILL.md", "已收录官方技能", "recommended", "官方资产。"),
            self._mk_entry("failed/eval:SKILL.md", "评估失败技能", "processing_failure", "历史处理失败。"),
        ]
        self.catalog_doc = {
            "catalog_version": "1.0.0",
            "entries": self.entries,
            "counts": {"recommended": 3, "candidate": 2, "excluded": 1, "processing_failure": 1}
        }
        (self.root / "data" / "catalog.json").write_text(
            json.dumps(self.catalog_doc, ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "public" / "data" / "catalog.json").write_text(
            json.dumps(self.catalog_doc, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prepare_run_range_selection_and_snapshots(self):
        """测试快照准备：精确筛选推荐/候选，保护收藏与已收录，纳入冷冻并标记样本。"""
        from tools.filter_existing_catalog import prepare_run

        res = prepare_run(self.root, "test-batch-01")
        self.assertEqual(res["run_id"], "test-batch-01")
        # 4 个目标进入范围: target/ecommerce, target/coder, snoozed/skill, retired/skill
        self.assertEqual(res["total_targets"], 4)
        self.assertEqual(res["recommended_count"], 2)
        self.assertEqual(res["candidate_count"], 2)
        self.assertEqual(res["snoozed_count"], 1)

        run_file = Path(res["run_dir"]) / "run.json"
        targets_file = Path(res["run_dir"]) / "targets.json"
        self.assertTrue(run_file.exists())
        self.assertTrue(targets_file.exists())

        targets_doc = json.loads(targets_file.read_text(encoding="utf-8"))
        target_ids = {t["skill_id"] for t in targets_doc["targets"]}
        self.assertIn("target/ecommerce:SKILL.md", target_ids)
        self.assertIn("target/coder:SKILL.md", target_ids)
        self.assertIn("snoozed/skill:SKILL.md", target_ids)
        self.assertIn("retired/skill:SKILL.md", target_ids)

        # 保护项不得进入 targets:
        self.assertNotIn("fav/skill:SKILL.md", target_ids)
        self.assertNotIn("already/excluded:SKILL.md", target_ids)
        self.assertNotIn("official/owned:SKILL.md", target_ids)
        self.assertNotIn("failed/eval:SKILL.md", target_ids)

    def test_validate_topic_response_and_citation_verification(self):
        """测试证据校验：合法引文命中，伪造引文降级为 unknown。"""
        from tools.filter_existing_catalog import validate_topic_response
        from src.catalog.filter_rules import FilterRules

        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        target = {
            "skill_id": "target/ecommerce:SKILL.md",
            "materials": {
                "name": "电商文案爆款大师",
                "summary_zh": "核心功能是撰写电商商品详情与促销广告文案。",
                "key_features": ["一键生成商品详情页", "自动提炼卖点"]
            }
        }

        # 1. 真实字面引文 -> match
        valid_json = json.dumps({
            "topic_assessments": [
                {
                    "topic_id": topic_ecommerce["topic_id"],
                    "result": "match",
                    "evidence_source": "summary_zh",
                    "evidence_quote": "撰写电商商品详情与促销广告文案",
                    "evidence": "主要用途为电商文案撰写"
                },
                {
                    "topic_id": topic_gym["topic_id"],
                    "result": "no_match",
                    "evidence_source": "",
                    "evidence_quote": "",
                    "evidence": "不属于健身"
                }
            ]
        })
        skill_res, assessments, err = validate_topic_response(rules, target, valid_json)
        self.assertIsNone(err)
        self.assertEqual(skill_res, "match")
        self.assertEqual(assessments[0]["result"], "match")

        # 2. 编造引文（原文不存在） -> 降级为 unknown
        fake_quote_json = json.dumps({
            "topic_assessments": [
                {
                    "topic_id": topic_ecommerce["topic_id"],
                    "result": "match",
                    "evidence_source": "summary_zh",
                    "evidence_quote": "完全不存在的编造文字",
                    "evidence": "强行说匹配"
                },
                {
                    "topic_id": topic_gym["topic_id"],
                    "result": "no_match",
                    "evidence_source": "",
                    "evidence_quote": "",
                    "evidence": "不属于健身"
                }
            ]
        })
        skill_res, assessments, err = validate_topic_response(rules, target, fake_quote_json)
        self.assertIsNone(err)
        # 降级后，因无 match 且含 unknown，条目结果为 unknown
        self.assertEqual(skill_res, "unknown")
        self.assertEqual(assessments[0]["result"], "unknown")
        self.assertIn("引文校验未通过", assessments[0]["evidence"])

    def test_run_assessments_simulation_and_offline_recovery(self):
        """测试任务调度、单次记账、预算预留以及未决任务的零模型调用离线恢复。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.catalog.filter_rules import FilterRules

        prepare_run(self.root, "test-batch-02")
        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        def fake_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            quote = "撰写电商商品详情与促销广告文案" if is_ecom else ""
            return self._mock_topic_response(
                topic_ecommerce["topic_id"], topic_gym["topic_id"],
                match_ecom=is_ecom, quote=quote,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            )

        # 运行评估
        progress = run_assessments(
            self.root,
            "test-batch-02",
            max_total_tokens=100000,
            max_total_requests=10,
            model_caller=fake_caller
        )
        self.assertEqual(progress["completed"], 4)
        self.assertEqual(progress["matched"], 1)
        self.assertEqual(progress["no_match"], 3)
        self.assertEqual(progress["requests"], 4)
        self.assertEqual(progress["tokens"], 600)

        # 验证离线断点恢复：人为将一个条目置为 needs_recovery，重跑不调用模型
        results_dir = self.root / "data" / "local" / "topic-filter" / "test-batch-02" / "results"
        sample_file = next(results_dir.glob("*.json"))
        sample_doc = json.loads(sample_file.read_text(encoding="utf-8"))
        sample_doc["status"] = "needs_recovery"
        sample_file.write_text(json.dumps(sample_doc), encoding="utf-8")

        mock_caller_no_call = Mock(side_effect=AssertionError("不应产生新的网络请求！"))
        run_assessments(
            self.root,
            "test-batch-02",
            model_caller=mock_caller_no_call
        )
        # 恢复后再次变成 completed
        recovered_doc = json.loads(sample_file.read_text(encoding="utf-8"))
        self.assertEqual(recovered_doc["status"], "completed")

    def test_report_and_apply_flow_with_governance_sync(self):
        """测试审核报告生成、选择导出、干预写入、冷冻移除与离线目录投影同步。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply, execute_apply
        from src.catalog.filter_rules import FilterRules

        prepare_run(self.root, "test-batch-03")
        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        def fake_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            is_retired = "retired/skill:SKILL.md" in usr_prompt
            match = is_ecom or is_retired
            quote = "电商" if is_retired else ("撰写电商商品详情与促销广告文案" if is_ecom else "")
            return self._mock_topic_response(
                topic_ecommerce["topic_id"], topic_gym["topic_id"],
                match_ecom=match, quote=quote,
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, "test-batch-03", model_caller=fake_caller)

        # 1. 生成报告
        report_html_path = render_report(self.root, "test-batch-03")
        self.assertTrue(Path(report_html_path).exists())
        review_file = self.root / "data" / "local" / "topic-filter" / "test-batch-03" / "review.json"
        self.assertTrue(review_file.exists())
        review_doc = json.loads(review_file.read_text(encoding="utf-8"))
        # 默认选中了命中的两项: target/ecommerce 和 retired/skill
        self.assertEqual(set(review_doc["selected_skill_ids"]), {"target/ecommerce:SKILL.md", "retired/skill:SKILL.md"})

        # 2. 计划预览 (plan_apply)
        plan = plan_apply(self.root, "test-batch-03", review_file)
        self.assertEqual(plan["conflicts_count"], 0)
        self.assertEqual(plan["to_add_count"], 1)  # target/ecommerce
        self.assertEqual(plan["to_reactivate_count"], 1)  # retired/skill
        self.assertEqual(plan["already_active_count"], 0)

        # 3. 正式应用 (execute_apply)
        manifest = execute_apply(self.root, "test-batch-03")
        self.assertEqual(manifest["phase"], "completed")

        # 4. 验证 overrides.json
        ov_after = json.loads((self.root / "config" / "governance" / "overrides.json").read_text(encoding="utf-8"))
        ov_exclusions = ov_after["manual_exclusions"]
        self.assertEqual(len(ov_exclusions), 3)  # 原有 1 活跃 + 1 退休转活跃 + 1 新增 = 3

        # target/ecommerce 已新增
        ecom_ex = next(x for x in ov_exclusions if x["skill_id"] == "target/ecommerce:SKILL.md")
        self.assertIn("存量主题过滤", ecom_ex["reason"])

        # retired/skill 已就地重新激活 (retired_at 字段被移除，且未产生重复条目)
        retired_ex = next(x for x in ov_exclusions if x["skill_id"] == "retired/skill:SKILL.md")
        self.assertNotIn("retired_at", retired_ex)
        self.assertIn("存量主题过滤", retired_ex["reason"])

        # 5. 验证 catalog.json 同步结果
        cat_after = json.loads((self.root / "data" / "catalog.json").read_text(encoding="utf-8"))
        entries_after = {e["skill_id"]: e for e in cat_after["entries"]}
        # target/ecommerce 原为 recommended，应用后被置为 excluded
        self.assertEqual(entries_after["target/ecommerce:SKILL.md"]["status"], "excluded")
        # retired/skill 原为 candidate，应用后被置为 excluded
        self.assertEqual(entries_after["retired/skill:SKILL.md"]["status"], "excluded")

    def test_plan_apply_conflicts_and_idempotency(self):
        """测试应用前材料指纹变化冲突检测与重复应用的幂等性。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply, execute_apply
        from src.catalog.filter_rules import FilterRules

        prepare_run(self.root, "test-batch-04")
        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        def fake_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            quote = "撰写电商商品详情与促销广告文案" if is_ecom else ""
            return self._mock_topic_response(
                topic_ecommerce["topic_id"], topic_gym["topic_id"],
                match_ecom=is_ecom, quote=quote,
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, "test-batch-04", model_caller=fake_caller)
        render_report(self.root, "test-batch-04")
        review_file = self.root / "data" / "local" / "topic-filter" / "test-batch-04" / "review.json"

        # 模拟在准备与应用之间，外部修改了目标技能的 materials 内容
        cat_file = self.root / "data" / "catalog.json"
        cat_data = json.loads(cat_file.read_text(encoding="utf-8"))
        for e in cat_data["entries"]:
            if e["skill_id"] == "target/ecommerce:SKILL.md":
                e["summary_zh"] = "已被外部篡改为全新的无关摘要文本"
        cat_file.write_text(json.dumps(cat_data, ensure_ascii=False), encoding="utf-8")

        # 计划预览应捕获 content_changed 冲突
        plan = plan_apply(self.root, "test-batch-04", review_file)
        self.assertGreater(plan["conflicts_count"], 0)
        self.assertEqual(plan["conflicts"][0]["reason"], "content_changed")

        # 恢复原状后可正常应用
        for e in cat_data["entries"]:
            if e["skill_id"] == "target/ecommerce:SKILL.md":
                e["summary_zh"] = "核心功能是撰写电商商品详情与促销广告文案。"
        cat_file.write_text(json.dumps(cat_data, ensure_ascii=False), encoding="utf-8")

        plan_ok = plan_apply(self.root, "test-batch-04", review_file)
        self.assertEqual(plan_ok["conflicts_count"], 0)

        # 首次执行应用
        manifest1 = execute_apply(self.root, "test-batch-04")
        self.assertEqual(manifest1["phase"], "completed")

        # 再次执行应用应幂等返回
        manifest2 = execute_apply(self.root, "test-batch-04")
        self.assertEqual(manifest2["phase"], "completed")

    def test_unknown_usage_stops_dispatch(self):
        """测试响应缺少用量明细时保守预留并安全停止派发，防范未记账超额调用。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.catalog.filter_rules import FilterRules

        prepare_run(self.root, "test-batch-05")
        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        call_count = 0

        def caller_missing_usage(cfg, sys_prompt, usr_prompt, request_id):
            nonlocal call_count
            call_count += 1
            return self._mock_topic_response(
                topic_ecommerce["topic_id"], topic_gym["topic_id"],
                match_ecom=False, usage=None
            )

        prog = run_assessments(self.root, "test-batch-05", model_caller=caller_missing_usage)
        # 仅执行 1 次即因未知用量停止
        self.assertEqual(call_count, 1)
        self.assertEqual(prog["stop_reason"], "usage_unknown")
        self.assertGreaterEqual(prog["tokens"], 3000)

    def test_defect1_interrupted_run_prior_status_offline_recovery_and_no_resend(self):
        """[Defect 1] 测试中断续跑：优先恢复已存盘响应，未决请求停止自动重发。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.catalog.filter_rules import FilterRules
        from src.infra.files import write_json_atomic

        batch_id = "test-defect-01"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)
        topic_ecommerce = rules.blocked_topics[0]
        topic_gym = rules.blocked_topics[1]

        batch_dir = self.root / "data" / "local" / "topic-filter" / batch_id
        results_dir = batch_dir / "results"
        requests_dir = batch_dir / "requests"

        targets_doc = json.loads((batch_dir / "targets.json").read_text(encoding="utf-8"))
        # 将前两个标记为样本，其余标记为非样本
        for idx, t in enumerate(targets_doc["targets"]):
            t["is_sample"] = (idx < 2)
        (batch_dir / "targets.json").write_text(json.dumps(targets_doc, ensure_ascii=False), encoding="utf-8")

        t1, t2 = targets_doc["targets"][0], targets_doc["targets"][1]

        # 场景 A: t1 处于 started 状态，且本地 requests 已有保存的有效响应
        from tools.filter_existing_catalog import _task_hash
        thash1 = _task_hash(batch_id, t1["skill_id"], t1["materials_fingerprint"], rules.fingerprint)
        req_id1 = "req-done-001"
        write_json_atomic(requests_dir / f"{req_id1}.json", {
            "request_id": req_id1,
            "task_hash": thash1,
            "skill_id": t1["skill_id"],
            "response_content": json.dumps({
                "topic_assessments": [
                    {"topic_id": topic_ecommerce["topic_id"], "result": "no_match", "evidence": "无关"},
                    {"topic_id": topic_gym["topic_id"], "result": "no_match", "evidence": "无关"}
                ]
            }),
            "tokens_charged": 100,
        })
        write_json_atomic(results_dir / f"{thash1}.json", {
            "task_hash": thash1,
            "skill_id": t1["skill_id"],
            "status": "started",
            "request_id": req_id1,
        })

        # 场景 B: t2 处于 started 状态，但缺少请求/响应记录
        thash2 = _task_hash(batch_id, t2["skill_id"], t2["materials_fingerprint"], rules.fingerprint)
        write_json_atomic(results_dir / f"{thash2}.json", {
            "task_hash": thash2,
            "skill_id": t2["skill_id"],
            "status": "started",
            "request_id": "req-missing-002",
        })

        # 重启样本续跑：断言不会发起任何新的模型调用
        mock_caller = Mock(side_effect=AssertionError("不应产生任何新请求！"))
        run_assessments(self.root, batch_id, sample_only=True, model_caller=mock_caller)

        # t1 应已离线恢复为 completed
        r1 = json.loads((results_dir / f"{thash1}.json").read_text(encoding="utf-8"))
        self.assertEqual(r1["status"], "completed")
        self.assertEqual(r1["result"], "no_match")

        # t2 应被置为 needs_recovery，未决请求未被自动重发
        r2 = json.loads((results_dir / f"{thash2}.json").read_text(encoding="utf-8"))
        self.assertEqual(r2["status"], "needs_recovery")
        mock_caller.assert_not_called()

    def test_defect2_model_pool_accounting_and_invocation_budget(self):
        """[Defect 2] 测试模型队列：在每次 _invoke 内检查预算并落盘，防止漏记与超额调用。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.infra.llm import ModelCallResult

        batch_id = "test-defect-02"
        # 构造含两个模型的队列配置
        queue_model_cfg = {
            "model_config_version": "1.0.0",
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "auth": {"api_key": "test-key"},
            "models": [
                {"model": "m1"},
                {"model": "m2"}
            ]
        }
        (self.root / "config" / "models" / "model.json").write_text(
            json.dumps(queue_model_cfg, ensure_ascii=False), encoding="utf-8"
        )
        prepare_run(self.root, batch_id)

        invocations = []

        def mock_call_model(cfg, sys_prompt, usr_prompt, **kwargs):
            invocations.append(cfg["model"])
            # m1 模拟额度耗尽，促使 ModelPool 尝试轮换
            return ModelCallResult(
                ok=False,
                model=cfg["model"],
                http_status=429,
                reason_code="QUOTA_EXHAUSTED",
                usage={"total_tokens": 50},
                error="额度耗尽"
            )

        with patch("tools.filter_existing_catalog.call_model", side_effect=mock_call_model):
            prog = run_assessments(self.root, batch_id, max_total_requests=1)

        # 因为 max_total_requests=1，在 _invoke 门禁处拦截了对 m2 的调用
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0], "m1")
        self.assertEqual(prog["requests"], 1)
        self.assertEqual(prog["tokens"], 50)

        # 检查 requests 目录是否如实记录了 m1 的请求事实
        req_files = list((self.root / "data" / "local" / "topic-filter" / batch_id / "requests").glob("*.json"))
        self.assertEqual(len(req_files), 1)
        saved_req = json.loads(req_files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved_req["model"], "m1")
        self.assertEqual(saved_req["tokens_charged"], 50)

    def test_defect3_unknown_usage_reservation_persists_across_restart(self):
        """[Defect 3] 测试未知用量预留在重启后持久化，不降为 0 且防止违规派发。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-03"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        # 轮次 1: 缺少 usage，收取 3000 Token 预留并停止
        def caller_missing_usage(cfg, sys_prompt, usr_prompt, request_id):
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=False, usage=None
            )

        prog1 = run_assessments(self.root, batch_id, model_caller=caller_missing_usage)
        self.assertEqual(prog1["stop_reason"], "usage_unknown")
        self.assertEqual(prog1["tokens"], 3000)

        # 轮次 2: 重启续跑，设定上限恰为 3000 Token；重启后累计数应保持 3000，因预算不足停止，不调用下一条
        mock_caller_never = Mock(side_effect=AssertionError("预算已满，绝不应继续调用！"))
        prog2 = run_assessments(self.root, batch_id, max_total_tokens=3000, model_caller=mock_caller_never)
        self.assertEqual(prog2["tokens"], 3000)
        self.assertEqual(prog2["stop_reason"], "token_limit_exceeded")
        mock_caller_never.assert_not_called()

    def test_defect4_report_does_not_overwrite_manual_review_selection(self):
        """[Defect 4] 测试重新生成报告保存 review-proposal.json，但不覆盖已有 review.json 人工选择。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-04"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            is_retired = "retired/skill:SKILL.md" in usr_prompt
            match = is_ecom or is_retired
            quote = "电商" if is_retired else ("撰写电商商品详情与促销广告文案" if is_ecom else "")
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=match, quote=quote, usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)

        # 1. 首次生成报告
        render_report(self.root, batch_id)
        batch_dir = self.root / "data" / "local" / "topic-filter" / batch_id
        review_file = batch_dir / "review.json"
        proposal_file = batch_dir / "review-proposal.json"
        self.assertTrue(review_file.exists())
        self.assertTrue(proposal_file.exists())

        # 2. 模拟人工审核：只保留 1 项 (target/ecommerce)
        review_doc = json.loads(review_file.read_text(encoding="utf-8"))
        review_doc["selected_skill_ids"] = ["target/ecommerce:SKILL.md"]
        review_file.write_text(json.dumps(review_doc, ensure_ascii=False), encoding="utf-8")

        # 3. 再次生成报告：proposal 包含 2 项，但 review.json 依然保持人工选择的 1 项
        render_report(self.root, batch_id)
        proposal_after = json.loads(proposal_file.read_text(encoding="utf-8"))
        review_after = json.loads(review_file.read_text(encoding="utf-8"))

        self.assertEqual(len(proposal_after["selected_skill_ids"]), 2)
        self.assertEqual(review_after["selected_skill_ids"], ["target/ecommerce:SKILL.md"])

    def test_defect5_remaining_does_not_miss_unfinished_samples(self):
        """[Defect 5] 测试 --remaining 会补齐此前因中断而未完成的样本。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-05"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        # 人为将全部 4 个目标都标记为样本 (以便测试样本中断)
        batch_dir = self.root / "data" / "local" / "topic-filter" / batch_id
        targets_file = batch_dir / "targets.json"
        targets_doc = json.loads(targets_file.read_text(encoding="utf-8"))
        for t in targets_doc["targets"]:
            t["is_sample"] = True
        targets_file.write_text(json.dumps(targets_doc, ensure_ascii=False), encoding="utf-8")

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=False, usage={"total_tokens": 100}
            )

        # 样本运行因请求上限仅完成 1 条
        prog1 = run_assessments(self.root, batch_id, sample_only=True, max_total_requests=1, model_caller=mock_caller)
        self.assertEqual(prog1["completed"], 1)

        # 运行 --remaining：必须补齐剩余 3 条未完成的样本，使总完成数达到 4
        prog2 = run_assessments(self.root, batch_id, remaining_only=True, max_total_requests=10, model_caller=mock_caller)
        self.assertEqual(prog2["completed"], 4)

    def test_defect6_plan_apply_validates_results_fingerprint(self):
        """[Defect 6] 测试应用阶段强校验 review.json 的 results_fingerprint。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-06"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom="target/ecommerce:SKILL.md" in usr_prompt,
                quote="撰写电商商品详情与促销广告文案" if "target/ecommerce:SKILL.md" in usr_prompt else "",
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)
        render_report(self.root, batch_id)

        review_file = self.root / "data" / "local" / "topic-filter" / batch_id / "review.json"
        review_doc = json.loads(review_file.read_text(encoding="utf-8"))

        # 篡改结果指纹为过期指纹
        review_doc["results_fingerprint"] = "expired_fingerprint_0000"
        review_file.write_text(json.dumps(review_doc, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            plan_apply(self.root, batch_id, review_file)
        self.assertIn("结果指纹", str(ctx.exception))

    def test_defect7_cli_apply_is_idempotent(self):
        """[Defect 7] 测试命令行重复执行 apply 具备幂等性（连续执行均返回退出码 0）。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, main as cli_main
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-07"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            quote = "撰写电商商品详情与促销广告文案" if is_ecom else ""
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=is_ecom, quote=quote, usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)
        render_report(self.root, batch_id)

        # 首次 CLI 应用 -> 退出码 0
        code1 = cli_main(["--root", str(self.root), "apply", "--run-id", batch_id, "--apply"])
        self.assertEqual(code1, 0)

        # 二次 CLI 应用 -> 退出码依然为 0（幂等成功，不误判状态冲突）
        code2 = cli_main(["--root", str(self.root), "apply", "--run-id", batch_id, "--apply"])
        self.assertEqual(code2, 0)

    def test_defect8_execute_apply_recovery_from_snooze_write_interruption(self):
        """[Defect 8] 测试应用阶段故障注入：写入 snooze 后中断，下次能基于预期后哈希正常继续。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply, execute_apply
        from src.catalog.filter_rules import FilterRules
        from src.infra.files import write_json_atomic

        batch_id = "test-defect-08"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_snoozed = "snoozed/skill:SKILL.md" in usr_prompt
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=is_snoozed, quote="专为健身房运营设计的会员与私教课程排班系统" if is_snoozed else "",
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)
        render_report(self.root, batch_id)
        review_file = self.root / "data" / "local" / "topic-filter" / batch_id / "review.json"
        plan = plan_apply(self.root, batch_id, review_file)

        # 故障注入：模拟 snooze.json 已写入磁盘，但进程中断，apply-manifest.json 停留在 prepared
        snooze_path = self.root / "config" / "governance" / "snoozed.json"
        sn_data = json.loads(snooze_path.read_text(encoding="utf-8"))
        sn_data["snoozed"] = [s for s in sn_data.get("snoozed", []) if s.get("skill_id") not in set(plan["snoozes_to_remove"])]
        write_json_atomic(snooze_path, sn_data)

        manifest_file = self.root / "data" / "local" / "topic-filter" / batch_id / "apply-manifest.json"
        write_json_atomic(manifest_file, {"run_id": batch_id, "phase": "prepared"})

        # 执行 execute_apply：核对预期写入后摘要，顺利继续完成并成功离线同步
        manifest = execute_apply(self.root, batch_id)
        self.assertEqual(manifest["phase"], "completed")

    def test_defect9_limitations_string_preserved(self):
        """[Defect 9] 测试 limitations 为纯文本字符串时不被拆散为单个字符列表。"""
        from tools.filter_existing_catalog import extract_materials, build_topic_request
        from src.catalog.filter_rules import FilterRules

        raw_entry = {
            "skill_id": "test/lim:SKILL.md",
            "name": "局限性测试技能",
            "url": "https://example.com",
            "summary_zh": "测试简述",
            "limitations": "ABC",  # 字符串输入
            "key_features": ["特性1"],
        }
        mat = extract_materials(raw_entry)
        # 应提取为整段列表 ["ABC"]，而非 ["A", "B", "C"]
        self.assertEqual(mat["limitations"], ["ABC"])

        rules = FilterRules(self.rules_data)
        _, usr_prompt = build_topic_request(rules, {"materials": mat, "skill_id": "test/lim:SKILL.md"})
        self.assertIn("[limitations/0]: ABC", usr_prompt)
        self.assertNotIn("[limitations/1]", usr_prompt)

    def test_defect_new1_exception_pre_persists_request_and_budget_reservation(self):
        """[Defect New 1] 测试请求异常时在调用前即持久化请求记录与预留费用，重启不漏记。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments
        batch_id = "test-defect-new-01"
        prepare_run(self.root, batch_id)

        call_count = 0
        def fail_caller(cfg, sys_prompt, usr_prompt, request_id):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("network dropped unexpectedly")

        # 首次运行：上限 1 次，调用抛出异常
        run_assessments(self.root, batch_id, max_total_requests=1, model_caller=fail_caller)
        self.assertEqual(call_count, 1)

        # 验证 requests/ 目录中已有落盘请求记录，记录了 3000 Token 与异常原因
        req_files = list((self.root / "data" / "local" / "topic-filter" / batch_id / "requests").glob("*.json"))
        self.assertEqual(len(req_files), 1)
        r_doc = json.loads(req_files[0].read_text(encoding="utf-8"))
        self.assertEqual(r_doc["tokens_charged"], 3000)
        self.assertTrue(r_doc["unknown_usage"])
        self.assertIn("network dropped", r_doc["error"])

        # 二次运行（重启）：上限仍为 1 次，因已累计 1 次请求和 3000 Token，前置预算检查立即停止，绝不再次调用模型
        prog2 = run_assessments(self.root, batch_id, max_total_requests=1, model_caller=fail_caller)
        self.assertEqual(call_count, 1)  # 严格保持为 1，没有第二次尝试
        self.assertEqual(prog2["requests"], 1)
        self.assertEqual(prog2["tokens"], 3000)

    def test_defect_new2_render_report_preserves_unchecked_state_in_html(self):
        """[Defect New 2] 测试报告页面按已有审核选择初始化勾选状态，不强制全选覆盖。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-new-02"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            is_ecom = "target/ecommerce:SKILL.md" in usr_prompt
            is_retired = "retired/skill:SKILL.md" in usr_prompt
            match = is_ecom or is_retired
            quote = "电商" if is_retired else ("撰写电商商品详情与促销广告文案" if is_ecom else "")
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom=match, quote=quote, usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)

        # 1. 首次生成报告，默认选中 2 项
        render_report(self.root, batch_id)
        batch_dir = self.root / "data" / "local" / "topic-filter" / batch_id
        review_file = batch_dir / "review.json"

        # 2. 人工审核仅保留 1 项 (target/ecommerce)
        review_doc = json.loads(review_file.read_text(encoding="utf-8"))
        review_doc["selected_skill_ids"] = ["target/ecommerce:SKILL.md"]
        review_file.write_text(json.dumps(review_doc, ensure_ascii=False), encoding="utf-8")

        # 3. 再次生成报告：HTML 表格中 target/ecommerce 必须 checked，retired/skill 必须未勾选
        report_html_path = render_report(self.root, batch_id)
        html_text = Path(report_html_path).read_text(encoding="utf-8")

        # 验证工具栏当前已选中为 1 项
        self.assertIn('<strong id="selectedCount">1</strong> / 2 项', html_text)
        # 验证行内 checkbox 状态
        self.assertIn("value='target/ecommerce:SKILL.md' checked", html_text)
        self.assertIn("value='retired/skill:SKILL.md'  onchange", html_text)
        self.assertNotIn("value='retired/skill:SKILL.md' checked", html_text)

    def test_defect_new3_fingerprint_includes_evidence_and_report_retains_old_fingerprint(self):
        """[Defect New 3] 测试证据变动会改变结果指纹，且 render_report 不自动将旧 review.json 刷成最新。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply, compute_results_fingerprint
        from src.catalog.filter_rules import FilterRules

        batch_id = "test-defect-new-03"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom="target/ecommerce:SKILL.md" in usr_prompt,
                quote="撰写电商商品详情与促销广告文案" if "target/ecommerce:SKILL.md" in usr_prompt else "",
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)
        render_report(self.root, batch_id)

        batch_dir = self.root / "data" / "local" / "topic-filter" / batch_id
        results_dir = batch_dir / "results"
        review_file = batch_dir / "review.json"
        fp_before = compute_results_fingerprint(results_dir)

        # 模拟仅修改其中一项的证据内容 (topic assessment evidence quote)
        res_file = next(results_dir.glob("*.json"))
        res_doc = json.loads(res_file.read_text(encoding="utf-8"))
        if res_doc.get("topic_assessments"):
            res_doc["topic_assessments"][0]["evidence_quote"] = "被修改的新证据引文"
        else:
            res_doc["error"] = "新错误标记"
        res_file.write_text(json.dumps(res_doc, ensure_ascii=False), encoding="utf-8")

        fp_after = compute_results_fingerprint(results_dir)
        self.assertNotEqual(fp_before, fp_after)

        # 再次执行 render_report：review.json 的结果指纹必须保留旧指纹，绝不自动更新为 fp_after
        render_report(self.root, batch_id)
        review_doc_after = json.loads(review_file.read_text(encoding="utf-8"))
        self.assertEqual(review_doc_after["results_fingerprint"], fp_before)
        self.assertNotEqual(review_doc_after["results_fingerprint"], fp_after)

        # 尝试应用该旧审核清单：必须被拦截报错
        with self.assertRaises(ValueError) as ctx:
            plan_apply(self.root, batch_id, review_file)
        self.assertIn("结果指纹", str(ctx.exception))

    def test_defect_new4_apply_interrupted_tampered_overrides_rejected(self):
        """[Defect New 4] 测试 exclusions_written 阶段若外部篡改 overrides.json，执行应用时必须立即拒绝。"""
        from tools.filter_existing_catalog import prepare_run, run_assessments, render_report, plan_apply, execute_apply
        from src.catalog.filter_rules import FilterRules
        from src.infra.files import write_json_atomic

        batch_id = "test-defect-new-04"
        prepare_run(self.root, batch_id)
        rules = FilterRules(self.rules_data)

        def mock_caller(cfg, sys_prompt, usr_prompt, request_id):
            return self._mock_topic_response(
                rules.blocked_topics[0]["topic_id"], rules.blocked_topics[1]["topic_id"],
                match_ecom="target/ecommerce:SKILL.md" in usr_prompt,
                quote="撰写电商商品详情与促销广告文案" if "target/ecommerce:SKILL.md" in usr_prompt else "",
                usage={"total_tokens": 100}
            )

        run_assessments(self.root, batch_id, model_caller=mock_caller)
        render_report(self.root, batch_id)
        review_file = self.root / "data" / "local" / "topic-filter" / batch_id / "review.json"
        plan = plan_apply(self.root, batch_id, review_file)

        # 模拟执行到达 exclusions_written 阶段
        manifest_file = self.root / "data" / "local" / "topic-filter" / batch_id / "apply-manifest.json"
        write_json_atomic(manifest_file, {"run_id": batch_id, "phase": "exclusions_written"})

        # 外部篡改：从 overrides.json 删除了排除项或修改了内容，使其不等于预期写入后摘要
        overrides_path = self.root / "config" / "governance" / "overrides.json"
        ov_data = json.loads(overrides_path.read_text(encoding="utf-8"))
        ov_data["manual_exclusions"] = []  # 篡改为空
        write_json_atomic(overrides_path, ov_data)

        # 再次执行应用：必须被锁内核验拦截，抛出 RuntimeError，禁止放行或返回 completed
        with self.assertRaises(RuntimeError) as ctx:
            execute_apply(self.root, batch_id)
        self.assertIn("overrides.json 处于写入后阶段但哈希不匹配预期摘要", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

