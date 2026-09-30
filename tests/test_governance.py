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
    filter_discovered_candidates,
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
                                  ("filter-rules.json", {"evaluation": {"blocked_tags": ["营销规划"]}}),
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
    """阶段过滤规则校验、词界匹配与0模型调用契约。"""
    def test_validate_filter_rules(self):
        valid = {
            "filter_rules_version": "1.0.0",
            "discovery": {"blocked_keywords": ["expo", "App Store"]},
            "evaluation": {"blocked_tags": ["营销规划"]},
        }
        self.assertEqual(validate_filter_rules(valid), [])

    def test_discovery_keyword_word_boundaries(self):
        rules = FilterRules(blocked_keywords=["expo", "App Store"])
        # expo 边界测试：expo 命中，export 不命中
        self.assertTrue(rules.matches_discovery("expo", "skills/expo/SKILL.md"))
        self.assertTrue(rules.matches_discovery("demo", "skills/expo-cli/SKILL.md"))
        self.assertFalse(rules.matches_discovery("export-tool", "skills/export/SKILL.md"))

        # App Store 变体测试：App Store, AppStore, app-store 命中，apple storefront 不命中
        self.assertTrue(rules.matches_discovery("App Store Connect", "skills/tool/SKILL.md"))
        self.assertTrue(rules.matches_discovery("my-tool", "skills/AppStore/SKILL.md"))
        self.assertTrue(rules.matches_discovery("app-store-helper", "skills/test/SKILL.md"))
        self.assertFalse(rules.matches_discovery("Apple Storefront", "skills/apple-storefront/SKILL.md"))

    def test_filter_discovered_candidates(self):
        rules = FilterRules(blocked_keywords=["expo", "App Store"])
        candidates = [
            {"name": "expo-tool", "skill_path": "skills/expo-tool/SKILL.md"},
            {"name": "export-tool", "skill_path": "skills/export-tool/SKILL.md"},
            {"name": "app-store-sync", "skill_path": "skills/sync/SKILL.md"},
            {"name": "good-tool", "skill_path": "skills/good/SKILL.md"},
        ]
        report = {}
        kept = filter_discovered_candidates(candidates, existing_ids=set(), filter_rules=rules, report=report)
        self.assertEqual(len(kept), 2)
        self.assertEqual([c["name"] for c in kept], ["export-tool", "good-tool"])
        self.assertEqual(len(report.get("discovery_filtered", [])), 2)

    def test_evaluation_blocked_tags(self):
        rules = FilterRules(blocked_tags=["营销规划"])
        # 精确标签匹配
        self.assertTrue(rules.matches_evaluation(["营销规划"]))
        self.assertTrue(rules.matches_evaluation(["技术工具", "营销规划"]))
        # 前缀/子串不误伤
        self.assertFalse(rules.matches_evaluation(["营销规划工具"]))
        self.assertFalse(rules.matches_evaluation(["营销"]))
        self.assertFalse(rules.matches_evaluation([]))

    def test_stage_filters_only_affect_new_entries(self):
        from src.catalog.local import prepare_pool
        from src.catalog.pool import create_pool_from_candidates, save_pool
        from src.catalog.models import Candidate, PrescreenResult
        from src.catalog.entry_state import EntryUpdateEvent, update_entry
        from src.catalog.index import CatalogContext
        from src.catalog.budget import BudgetLedger
        from src.catalog.evaluation import evaluation_id
        from src.catalog.sync_evaluate import _evaluate_queue, _build_evaluated_catalog
        from src.shared.identity import content_fingerprint
        from src.shared.runtime import now_local

        rules = FilterRules(blocked_keywords=["expo"], blocked_tags=["营销规划"])
        text = "---\nname: demo\ndescription: A useful skill\n---\n# Demo\n" + "Complete a useful task. " * 20
        context = CatalogContext(rules_version="1.0.0")
        names = ("recommended", "candidate", "favorite", "pending", "new", "history", "retry")
        candidates = [Candidate(f"demo/repo:skills/expo-{name}/SKILL.md", "demo", "repo",
            name=f"expo-{name}", path=f"skills/expo-{name}/SKILL.md", content_fingerprint=content_fingerprint(text)) for name in names]
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
                self.assertEqual({i.candidate.skill_id for i in pool.items}, {c.skill_id for c in candidates[:4]})
                self.assertEqual([r["skill_id"] for r in report["discovery_filtered"]], [candidates[4].skill_id])

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
            evaluation = Mock(return_value={"ok": True, "evaluation": {"tags": ["营销规划"]}})
            fetch = Mock(side_effect=AssertionError("unexpected network"))
            _evaluate_queue(queue, cfg, ledger, {c.skill_id: text for c in candidates}, now_local(), 0,
                fetch, evaluation, None, lambda *a: None, previous_entries=previous)
            outcomes = {c.name: (ledger.get(evaluation_id(c, cfg["model"], cfg["rules"])) or {}).get("outcome", {}) for c in candidates}
            for name in ("recommended", "candidate", "history"):
                self.assertEqual(outcomes[f"expo-{name}"]["decision"], "recommended")
                self.assertNotIn("TAG_FILTERED", outcomes[f"expo-{name}"]["reason_codes"])
            for name in ("pending", "new", "retry"):
                outcome = outcomes[f"expo-{name}"]
                self.assertEqual(outcome["decision"], "excluded")
                self.assertEqual(outcome["blocked_tag"], "营销规划")
                self.assertEqual(outcome["original_decision"]["decision"], "recommended")
            evaluation_calls = evaluation.call_count
            rebuilt = _build_evaluated_catalog(previous, queue, cfg, ledger, context)
            self.assertEqual(next(e for e in rebuilt["entries"] if e["skill_id"] == candidates[0].skill_id)["status"], "recommended")
            favorite = next(e for e in rebuilt["entries"] if e["skill_id"] == candidates[2].skill_id)
            self.assertEqual(favorite["status"], "candidate")
            self.assertTrue(favorite["manual_pick"])
            # 后续缓存复用不因当前规则改变而重新检查；账本结论保持原样。
            cfg["filter_rules"] = FilterRules()
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
