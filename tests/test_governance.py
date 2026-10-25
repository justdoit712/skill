"""治理与人工规则单元测试：人工挑优/排除、离线恢复收藏保护与已收录补丁冲突防护。

保留 3 项核心行为：
1. 人工规则生效（人工挑优升级 candidate 为 manual_pick）。
2. 补丁冲突保护（基线过期时应用补丁抛出 OwnedPatchConflictError）。
3. 恢复保留收藏（离线恢复与数据丰富化绝对保留人工收藏配置）。
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.catalog.budget import BudgetLedger
from src.catalog.entry_state import EntryUpdateEvent, update_entry
from src.catalog.index import CatalogContext, build_catalog
from src.catalog.maintenance import enrich_catalog, recover_completed_results, sync_config_to_catalog
from src.catalog.models import Candidate, PrescreenResult
from src.catalog.overrides import apply_manual_overrides_to_entry
from src.shared.owned import OWNED_SCHEMA_VERSION, OwnedPatchConflictError, apply_owned_patch
from tests import smoke
from src.catalog.filter_rules import (
    FilterRules,
    _rules_from_audit,
    build_topic_filter_audit,
    filter_new_evaluation,
    load_filter_rules,
    match_local_filter_rules,
    validate_filter_rules,
)


class GovernanceTest(unittest.TestCase):
    """人工治理规则与收藏保护契约。"""

    @smoke
    def test_retired_reset_recovery_rejects_without_side_effects(self):
        from src.catalog.local import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = root / "data/local/state/evaluations/interrupted.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({
                "status": "needs_recovery", "attempts": 2,
                "requests": [{"status": "sending"}], "error": "usage_unknown",
            }), encoding="utf-8")
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            for extra in ([], ["--check"], ["--sync-config"], ["--recover-catalog"], ["--enrich-catalog"]):
                with self.subTest(extra=extra), patch("builtins.print") as output, \
                        patch("src.catalog.budget.BudgetLedger.load") as load_ledger, \
                        patch("src.catalog.local.load_all_config") as load_config:
                    self.assertEqual(main(["--reset-recovery", *extra], root=root), 2)
                    load_ledger.assert_not_called()
                    load_config.assert_not_called()
                    message = "\n".join(str(call.args[0]) for call in output.call_args_list)
                    self.assertIn("manage_pool.py resume", message)
                    self.assertIn("--dry-run", message)
                    self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)

    @smoke
    def test_resume_preserves_history_and_requires_exclusive_lock(self):
        from src.catalog.budget import evaluation_filename
        from src.catalog.maintenance import resume_candidate
        from src.catalog.pool import CandidatePool, PoolItem, load_pool, save_pool
        from src.infra.files import LockConflict, file_lock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = Candidate("demo/repo:SKILL.md", "demo", "repo", content_fingerprint="sha256:demo")
            evaluation_id = "demo-evaluation"
            record_path = root / "data/local/state/evaluations" / evaluation_filename(evaluation_id)
            record_path.parent.mkdir(parents=True)
            record = {
                "evaluation_id": evaluation_id, "skill_id": candidate.skill_id,
                "content_fingerprint": candidate.content_fingerprint,
                "status": "needs_recovery", "attempts": 2, "max_attempts": 2,
                "requests": [{"status": "sending", "request_id": "req-1"}],
                "usage": {"total_tokens": 123}, "error": "usage_unknown",
            }
            record_path.write_text(json.dumps(record), encoding="utf-8")
            pool_path = root / "data/local/pool.json"
            save_pool(pool_path, CandidatePool(items=[PoolItem(
                seq=1, candidate=candidate, status="blocked",
                block_info={"evaluation_id": evaluation_id},
            )]))
            snapshot = lambda: {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            before = snapshot()
            kwargs = dict(evaluation_id=evaluation_id, reason="已核对请求", event_id="resume-demo", extra_attempts=1)
            preview = resume_candidate(root, **kwargs)
            self.assertTrue(preview["dry_run"])
            self.assertEqual(snapshot(), before)
            with file_lock(root / "data/.catalog.lock"):
                with self.assertRaises(LockConflict):
                    resume_candidate(root, **kwargs, apply=True)
            self.assertEqual(snapshot(), before)
            resume_candidate(root, **kwargs, apply=True)
            updated = json.loads(record_path.read_text(encoding="utf-8"))
            for field in ("requests", "usage", "attempts", "error"):
                self.assertEqual(updated[field], record[field])
            self.assertEqual(updated["max_attempts"], 3)
            self.assertEqual(len(updated["resume_history"]), 1)
            self.assertEqual(updated["resume_history"][0]["event_id"], "resume-demo")
            self.assertEqual(load_pool(pool_path).items[0].status, "pending")
            after = snapshot()
            self.assertTrue(resume_candidate(root, **kwargs, apply=True)["idempotent"])
            self.assertEqual(snapshot(), after)

    @smoke
    def test_governance_paths_reject_legacy_files_regardless_of_mtime(self):
        """四类加载器、总入口及显式文件参数都不能按时间回退到旧配置。"""
        from src.catalog.config import load_all_config
        from src.catalog.favorites import load_favorites
        from src.catalog.overrides import load_overrides
        from src.catalog.snooze import load_snooze
        from src.infra.governance_paths import GovernancePathError
        from src.infra.owned import get_owned_config_path, load_owned_config

        cases = (
            ("favorites.json", load_favorites, {"favorites_version": "1.0.0", "manual_picks": []}),
            ("overrides.json", load_overrides, {"overrides_version": "1.0.0", "manual_exclusions": []}),
            ("snoozed.json", load_snooze, {"snooze_version": "1.0.0", "default_snooze_days": 150, "snoozed": []}),
            ("owned-skills.json", load_owned_config, {"schema_version": "1.0.0", "items": []}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config"
            (base / "governance").mkdir(parents=True)
            for name, loader, payload in cases:
                with self.subTest(filename=name):
                    target, legacy = base / "governance" / name, base / name
                    self.assertEqual(loader(target), payload)
                    target.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(loader(target), payload)
                    if name == "owned-skills.json":
                        self.assertEqual(get_owned_config_path(base), target)
                        self.assertEqual(load_owned_config(base), payload)
                    legacy.write_text(json.dumps(payload), encoding="utf-8")
                    for old_time in (100, 200, 300):
                        os.utime(target, (200, 200))
                        os.utime(legacy, (old_time, old_time))
                        with self.assertRaisesRegex(GovernancePathError, "路径冲突"):
                            loader(target)
                        if name == "overrides.json":
                            for path in (target, legacy, base / "filter-rules.json"):
                                with self.assertRaises(GovernancePathError):
                                    load_filter_rules(path)
                        with patch("src.catalog.config.load_config") as prescreen:
                            with self.assertRaises(GovernancePathError):
                                load_all_config(base)
                            prescreen.assert_not_called()
                    target.unlink()
                    with self.assertRaisesRegex(GovernancePathError, "迁移"):
                        loader(target)
                    if name == "overrides.json":
                        with self.assertRaisesRegex(GovernancePathError, "迁移"):
                            load_filter_rules(target)
                    with self.assertRaises(GovernancePathError):
                        loader(legacy)
                    legacy.unlink()
                    self.assertEqual(loader(target), payload)

    @smoke
    def test_governance_conflicts_do_not_write_config_or_projections(self):
        """保存、补丁和离线同步不能绕过冲突，也不能写到另一个项目。"""
        from src.infra.governance_paths import GOVERNANCE_FILES, GovernancePathError
        from src.infra.owned import apply_and_save_owned_patch, load_owned_config, save_owned_config
        from tools.manage_owned import apply_changes, main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config"
            target = base / "governance" / "owned-skills.json"
            payload = {"schema_version": "1.0.0", "items": []}
            patch_payload = {"schema_version": "1.0.0", "changes": [{
                "skill_id": "demo/repo:SKILL.md", "before": None,
                "after": {"skill_id": "demo/repo:SKILL.md", "name": "demo", "added_at": "2026-10-25"},
            }]}
            patch_file = root / "patch.json"
            patch_file.write_text(json.dumps(patch_payload), encoding="utf-8")
            self.assertEqual(save_owned_config(payload, base), target)
            apply_and_save_owned_patch(patch_payload, target)
            self.assertEqual(len(load_owned_config(base)["items"]), 1)
            self.assertFalse((base / "owned-skills.json").exists())
            with patch("builtins.print"):
                self.assertEqual(main(["--list", "--root", str(root)]), 0)

            catalog_file, page_file = root / "data/catalog.json", root / "public/data/catalog.json"
            for file in (catalog_file, page_file):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('{"sentinel": true}', encoding="utf-8")
            before = {file: file.read_bytes() for file in (target, catalog_file, page_file)}
            for name in GOVERNANCE_FILES:
                legacy = base / name
                legacy.write_text("{}", encoding="utf-8")
                with self.subTest(filename=name):
                    with self.assertRaises(GovernancePathError):
                        sync_config_to_catalog(root)
                    with patch("tools.manage_owned.sync_config_offline") as rebuild:
                        self.assertEqual(apply_changes(patch_file, root, base, log_fn=lambda _: None), 1)
                        rebuild.assert_not_called()
                    if name == "owned-skills.json":
                        for destination in (base, target, legacy):
                            with self.assertRaises(GovernancePathError):
                                save_owned_config(payload, destination)
                        with self.assertRaises(GovernancePathError):
                            apply_and_save_owned_patch(patch_payload, base)
                        with patch("builtins.print"):
                            self.assertEqual(main(["--list", "--root", str(root)]), 1)
                    self.assertEqual({file: file.read_bytes() for file in before}, before)
                    self.assertEqual(legacy.read_text(encoding="utf-8"), "{}")
                legacy.unlink()

            with self.assertRaises(GovernancePathError):
                save_owned_config(payload, root / "custom.json")
            with self.assertRaises(ValueError):
                sync_config_to_catalog(root, owned_path=root / "other/config/governance/owned-skills.json")

    @smoke
    def test_apply_manual_picks_upgrades_candidate(self):
        """人工精选规则生效并将候选条目升级为 manual_pick。"""
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

    def test_patch_conflict_on_stale_base(self):
        """当本地基线与补丁的前置状态 (before) 不一致时，严格阻止合入并抛出冲突异常。"""
        base = {
            "schema_version": "1.0.0",
            "items": [
                {"skill_id": "owner/repo:skills/b/SKILL.md", "name": "version-2", "added_at": "2026-09-02"}
            ],
        }
        patch_payload = {
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
            apply_owned_patch(base, patch_payload)

    def test_offline_recovery_and_enrichment_preserve_favorites(self):
        """离线崩溃恢复与目录数据丰富化流程中，必须完整保留人工收藏状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            config = root / "config" / "governance"
            config.mkdir(parents=True)
            context = CatalogContext(rules_version="1.0.0")
            favorite = Candidate("demo/repo:skills/favorite/SKILL.md", "demo", "repo", name="favorite")
            entry = update_entry(
                None,
                favorite,
                EntryUpdateEvent(
                    kind="fresh_evaluation",
                    evaluation={"tags": ["营销规划"]},
                    decision={"decision": "candidate"},
                    evaluation_id="prior-favorite",
                ),
                context,
            )
            favorites = {
                "favorites_version": "1.0.0",
                "manual_picks": [
                    {"skill_id": favorite.skill_id, "reason": "keep", "added_at": "2026-10-01", "from": "candidate"},
                    {"skill_id": "demo/repo:skills/owned/SKILL.md", "reason": "keep owned", "added_at": "2026-10-01"},
                ],
            }
            exclusions = {"overrides_version": "2.0.0", "manual_exclusions": [], "blocked_topics": []}
            snoozed = {"snooze_version": "1.0.0", "snoozed": []}
            catalog = build_catalog([entry], context=context, overrides={**exclusions, "manual_picks": favorites["manual_picks"]}, snoozed=snoozed)
            (data / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            for name, payload in (
                ("favorites.json", favorites),
                ("overrides.json", exclusions),
                ("snoozed.json", snoozed),
                ("owned-skills.json", {"schema_version": OWNED_SCHEMA_VERSION, "items": [
                    {"skill_id": "demo/repo:skills/owned/SKILL.md", "name": "owned", "added_at": "2026-10-01"}]}),
            ):
                (config / name).write_text(json.dumps(payload), encoding="utf-8")

            sync_config_to_catalog(root)

            other = Candidate("demo/repo:skills/new/SKILL.md", "demo", "repo", name="new")
            ledger = BudgetLedger.load(data / "local" / "state", 10)
            ledger.reserve([{"evaluation_id": "new-result", "skill_id": other.skill_id}])
            ledger.complete(
                "new-result",
                {
                    "decision": "candidate",
                    "evaluation": {"tags": ["general"]},
                    "candidate": asdict(other),
                    "prescreen": asdict(PrescreenResult(other.skill_id, "queued")),
                },
            )

            (config / "favorites.json").write_text(json.dumps({"manual_picks": []}), encoding="utf-8")
            with patch("src.catalog.prescreen.load_config", return_value=SimpleNamespace(rules={"rules_version": "1.0.0"}, domain_names={})):
                recovered = recover_completed_results(root)
            self.assertEqual(recovered["restored"], 1)

            enrich_catalog(root)
            saved = json.loads((data / "catalog.json").read_text(encoding="utf-8"))
            page = json.loads((root / "public" / "data" / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["favorites"], favorites)
            self.assertEqual(page["favorites"], favorites)
            found = next(e for e in saved["entries"] if e["skill_id"] == favorite.skill_id)
            self.assertTrue(found["manual_pick"])
            self.assertEqual(found["manual_note"]["reason"], "keep")

    @smoke
    def test_filter_rules_300_schema_and_validation(self):
        """统一黑名单 3.0.0 规范校验：支持 keywords、local_match，拦截非法字段与重复项。"""
        valid_cfg = {
            "filter_rules_version": "3.0.0",
            "blocked_topics": [
                {
                    "name": "Excel 基础文档处理",
                    "description": "通用 Excel 处理，保留财务建模",
                    "keywords": ["Excel", "xlsx"],
                    "local_match": {
                        "names": ["excel", "xlsx"],
                        "purpose_phrases": ["通用电子表格处理"],
                        "preserve_phrases": ["财务建模", "financial modeling"],
                    },
                }
            ],
        }
        self.assertEqual(validate_filter_rules(valid_cfg), [])

        dup_name = {
            "blocked_topics": [
                {"name": "Excel 基础文档处理"},
                {"name": "excel 基础文档处理"},
            ]
        }
        self.assertTrue(any("重复" in err for err in validate_filter_rules(dup_name)))

        dup_kw = {
            "blocked_topics": [
                {"name": "Topic A", "keywords": ["kw1", "KW1"]},
            ]
        }
        self.assertTrue(any("重复" in err for err in validate_filter_rules(dup_kw)))

        illegal_lm = {
            "blocked_topics": [
                {"name": "Topic A", "local_match": {"unknown_field": []}},
            ]
        }
        self.assertTrue(any("未识别的字段" in err for err in validate_filter_rules(illegal_lm)))

    @smoke
    def test_local_filter_rules_matching_contract(self):
        """本地无 I/O 保守匹配契约：三条件约束、专业保留保护、含糊不删。"""
        rules = FilterRules({
            "filter_rules_version": "3.0.0",
            "blocked_topics": [
                {
                    "name": "Excel 基础文档处理",
                    "description": "主要用于 Excel 基础处理，保留财务建模",
                    "keywords": ["Excel", "xlsx"],
                    "local_match": {
                        "names": ["excel", "xlsx", "excel-tools"],
                        "purpose_phrases": ["通用电子表格处理", "Excel 文档读写与格式转换"],
                        "preserve_phrases": ["财务建模", "统计分析", "financial modeling"],
                    },
                },
                {
                    "name": "PDF 基础处理",
                    "description": "主要用于 PDF 拆分合并等通用处理，保留合同分析",
                    "keywords": ["pdf"],
                    "local_match": {
                        "names": ["pdf", "pdf-tools"],
                        "purpose_phrases": ["通用 PDF 处理", "PDF 拆分与合并"],
                        "preserve_phrases": ["合同分析", "contract analysis"],
                    },
                },
            ],
        })

        # 1. 明确通用工具命中
        res = match_local_filter_rules(
            rules,
            name="owner/repo:skills/excel-tools/SKILL.md",
            summary="这是一个用于通用电子表格处理的脚本",
        )
        self.assertEqual(res["result"], "match")
        self.assertEqual(res["matched_topic_name"], "Excel 基础文档处理")
        self.assertEqual(res["matched_name"], "excel-tools")

        # 2. 只有标签，无自身用途摘要 -> unknown（不排除）
        res = match_local_filter_rules(
            rules,
            name="excel",
            summary="",
            materials={"tags": ["Excel", "XLSX"]},
        )
        self.assertEqual(res["result"], "unknown")

        # 3. 包含保留词：财务建模 -> unknown（严禁误删）
        res = match_local_filter_rules(
            rules,
            name="excel",
            summary="用于通用电子表格处理，并支持企业级财务建模分析",
        )
        self.assertEqual(res["result"], "unknown")
        self.assertIn("专业用途保留短语", res["reason"])

        # 4. 英文保留词边界匹配：financial modeling
        res = match_local_filter_rules(
            rules,
            name="excel",
            summary="A tool for Excel 文档读写与格式转换 with financial modeling capabilities",
        )
        self.assertEqual(res["result"], "unknown")

        # 5. 名称不完全匹配：excel-financial-modeling 不应被 excel 命中
        res = match_local_filter_rules(
            rules,
            name="excel-financial-modeling",
            summary="通用电子表格处理",
        )
        self.assertEqual(res["result"], "no_match")

        # 6. PDF 基础拆分合并命中
        res = match_local_filter_rules(
            rules,
            name="pdf-tools",
            summary="提供通用 PDF 处理及 PDF 拆分与合并功能",
        )
        self.assertEqual(res["result"], "match")
        self.assertEqual(res["matched_topic_name"], "PDF 基础处理")

        # 7. PDF 专业合同分析保护
        res = match_local_filter_rules(
            rules,
            name="pdf-tools",
            summary="通用 PDF 处理，专业合同分析工具",
        )
        self.assertEqual(res["result"], "unknown")

    @smoke
    def test_filter_rules_fingerprint_idempotency_from_audit(self):
        """规则快照重建幂等性：空 keywords/local_match 与完整配置重建后指纹绝对一致。"""
        raw_config = {
            "filter_rules_version": "3.0.0",
            "blocked_topics": [
                {"name": "No Optional Fields Topic", "description": "无可选字段"},
                {"name": "Empty Lists Topic", "description": "空列表", "keywords": [], "local_match": {}},
                {
                    "name": "Full Topic",
                    "description": "完整字段",
                    "keywords": ["full", "complete"],
                    "local_match": {
                        "names": ["full"],
                        "purpose_phrases": ["full purpose"],
                        "preserve_phrases": ["full preserve"],
                    },
                },
            ],
        }
        rules = FilterRules(raw_config)
        assessments = [{"topic_id": rules.blocked_topics[2]["topic_id"], "result": "match", "evidence": "hit"}]
        audit = build_topic_filter_audit(rules, assessments)

        reconstructed = _rules_from_audit(audit)
        self.assertIsNotNone(reconstructed)
        self.assertEqual(reconstructed.fingerprint, rules.fingerprint)

        # 确保 filter_new_evaluation 依据重建后的规则成功工作
        ev = {"topic_filter_audit": audit}
        decision = filter_new_evaluation({"decision": "candidate"}, ev, rules)
        self.assertEqual(decision["decision"], "excluded")
        self.assertIn("Full Topic", decision["blocked_topics"])

    @smoke
    def test_negation_and_secondary_context_handling(self):
        """子串匹配上下文检测：否定词与次要用途修饰判定为 unknown，不予排除。"""
        rules = FilterRules({
            "blocked_topics": [
                {
                    "name": "Excel 基础文档处理",
                    "local_match": {
                        "names": ["excel"],
                        "purpose_phrases": ["通用电子表格处理"],
                    },
                }
            ]
        })
        # 1. 否定词修饰：不支持
        res_neg = match_local_filter_rules(rules, name="excel", summary="本项目不支持通用电子表格处理，仅作为特定计算器")
        self.assertEqual(res_neg["result"], "unknown")
        self.assertIn("否定或次要修饰", res_neg["reason"])

        # 2. 否定词修饰：并非 / does not
        res_neg2 = match_local_filter_rules(rules, name="excel", summary="This tool does not perform 通用电子表格处理")
        self.assertEqual(res_neg2["result"], "unknown")

        # 3. 次要用途修饰：仅顺带
        res_sec = match_local_filter_rules(rules, name="excel", summary="主要用于财务估值，仅顺带提供通用电子表格处理功能")
        self.assertEqual(res_sec["result"], "unknown")
        self.assertIn("否定或次要修饰", res_sec["reason"])

        # 4. 次要用途修饰：仅作为辅助
        res_sec2 = match_local_filter_rules(rules, name="excel", summary="聚焦于资产定价，通用电子表格处理仅作为辅助手段")
        self.assertEqual(res_sec2["result"], "unknown")

        # 5. 肯定主陈述：正常命中 match
        res_pos = match_local_filter_rules(rules, name="excel", summary="用于通用电子表格处理与格式转换")
        self.assertEqual(res_pos["result"], "match")

        for summary in (
            "通用电子表格处理不在本技能支持范围内，主要用于语法检查",
            "通用电子表格处理功能不提供",
            "通用电子表格处理并非主要用途",
            "通用电子表格处理 is not supported; this tool checks syntax",
            "通用电子表格处理 isn't available",
        ):
            with self.subTest(summary=summary):
                self.assertEqual(match_local_filter_rules(rules, name="excel", summary=summary)["result"], "unknown")
        # 后一句否定其他能力，不能推翻前一句明确的主要用途。
        self.assertEqual(match_local_filter_rules(
            rules, name="excel", summary="通用电子表格处理。不支持图片编辑。"
        )["result"], "match")

    @smoke
    def test_post_fetch_materials_text_protection(self):
        """抓取后专业用途保护检查正文 text：正文中含有 preserve_phrases 触发保护。"""
        rules = FilterRules({
            "blocked_topics": [
                {
                    "name": "Excel 基础文档处理",
                    "local_match": {
                        "names": ["excel"],
                        "purpose_phrases": ["通用电子表格处理"],
                        "preserve_phrases": ["财务建模", "现金流折现"],
                    },
                }
            ]
        })
        # 正文包含保留词“现金流折现”
        full_text = "# Excel Skill\n\n这是一个用于通用电子表格处理的模块。\n\n## 高级功能\n本工具深度支持企业财务建模与现金流折现分析。"
        res = match_local_filter_rules(
            rules,
            name="excel",
            summary="通用电子表格处理",
            materials={"text": full_text},
            stage="post_fetch",
        )
        self.assertEqual(res["result"], "unknown")
        self.assertIn("专业用途保留短语", res["reason"])

    @smoke
    def test_collection_subskill_isolation_and_evidence_persistence(self):
        """合集子目录技能隔离与提前排除依据持久化。"""
        rules = FilterRules({
            "blocked_topics": [
                {
                    "name": "Word 基础文档处理",
                    "local_match": {
                        "names": ["docx"],
                        "purpose_phrases": ["Word 文档处理"],
                    },
                }
            ]
        })
        sub_cand = Candidate(
            skill_id="owner/repo:skills/docx/SKILL.md",
            owner="owner",
            repo="repo",
            name="docx",
            path="skills/docx/SKILL.md",
            description="Awesome tools including Word 文档处理 and other utilities",  # 仓库简介
        )
        # 子目录技能在抓取前不可泄露仓库简介
        from src.catalog.local_candidate import is_collection_subskill
        self.assertTrue(is_collection_subskill(sub_cand.path))
        pre_summary = "" if is_collection_subskill(sub_cand.path) else sub_cand.description
        res_pre = match_local_filter_rules(rules, name=sub_cand.name, summary=pre_summary, stage="pre_fetch")
        self.assertEqual(res_pre["result"], "unknown")  # 不在抓取前误杀

        # 验证提前排除依据持久化至 entry
        local_match = {
            "result": "match",
            "matched_topic_id": rules.blocked_topics[0]["topic_id"],
            "matched_topic_name": "Word 基础文档处理",
            "matched_name": "docx",
            "matched_phrase": "Word 文档处理",
            "matched_purpose": "Word 文档处理",
            "stage": "post_fetch",
        }
        audit = build_topic_filter_audit(rules, [{"topic_id": local_match["matched_topic_id"], "result": "match", "evidence": "Word 文档处理"}])
        event = EntryUpdateEvent(
            kind="fresh_evaluation",
            decision={
                "decision": "excluded",
                "reason_codes": ["TOPIC_FILTERED"],
                "topic_filter_local": local_match,
                "topic_filter_audit": audit,
            },
        )
        entry = update_entry(None, sub_cand, event)
        self.assertEqual(entry["status"], "excluded")
        self.assertIn("TOPIC_FILTERED", entry["reason_codes"])
        self.assertEqual(entry.get("topic_filter_local"), local_match)
        self.assertEqual(entry.get("topic_filter_audit"), audit)

    @smoke
    def test_formal_filter_rules_cover_actual_samples(self):
        """正式 overrides.json (blocked_topics) 正确覆盖真实清理样例。"""
        formal_rules = load_filter_rules()
        self.assertTrue(formal_rules.has_evaluation_rules)

        # 样例 1: docx
        res1 = match_local_filter_rules(
            formal_rules,
            name="docx",
            summary="创建、编辑和分析Word文档，支持修订跟踪、批注和格式化，适用于设计简报和文案文档",
        )
        self.assertEqual(res1["result"], "match")
        self.assertEqual(res1["matched_topic_name"], "Word 基础文档处理")

        # 样例 2: docx 元数据
        res2 = match_local_filter_rules(
            formal_rules,
            name="docx",
            summary="这是一个用于在 OpenDesign 中注册和发现 Word 文档处理技能的元数据条目，指向 Anthropic 官方的 docx 技能包",
        )
        self.assertEqual(res2["result"], "match")
        self.assertEqual(res2["matched_topic_name"], "Word 基础文档处理")

        # 样例 3: doc
        res3 = match_local_filter_rules(
            formal_rules,
            name="doc",
            summary="通过OpenAI文档技能读写编辑.docx文档，保持格式和布局保真，适用于文档处理场景",
        )
        self.assertEqual(res3["result"], "match")
        self.assertEqual(res3["matched_topic_name"], "Word 基础文档处理")

        # 样例 4: minimax-docx
        res4 = match_local_filter_rules(
            formal_rules,
            name="minimax-docx",
            summary="声称提供专业DOCX文档创建与编辑功能，适用场景包括品牌报告、提案等文档生产，但缺乏实际指导",
        )
        self.assertEqual(res4["result"], "match")
        self.assertEqual(res4["matched_topic_name"], "Word 基础文档处理")

        # 样例 5: pdf
        res5 = match_local_filter_rules(
            formal_rules,
            name="pdf",
            summary="处理 PDF 文件：文本提取、PDF 创建和表单处理，适用于新闻稿、品牌单页文件和可打印设计成果",
        )
        self.assertEqual(res5["result"], "match")
        self.assertEqual(res5["matched_topic_name"], "PDF 基础处理")


if __name__ == "__main__":
    unittest.main()

