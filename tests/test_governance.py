"""治理与人工规则单元测试：人工挑优/排除、离线恢复收藏保护与已收录补丁冲突防护。

保留 3 项核心行为：
1. 人工规则生效（人工挑优升级 candidate 为 manual_pick）。
2. 补丁冲突保护（基线过期时应用补丁抛出 OwnedPatchConflictError）。
3. 恢复保留收藏（离线恢复与数据丰富化绝对保留人工收藏配置）。
"""

from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
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


class GovernanceTest(unittest.TestCase):
    """人工治理规则与收藏保护契约。"""

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
            exclusions = {"overrides_version": "1.0.0", "manual_exclusions": []}
            snoozed = {"snooze_version": "1.0.0", "snoozed": []}
            catalog = build_catalog([entry], context=context, overrides={**exclusions, "manual_picks": favorites["manual_picks"]}, snoozed=snoozed)
            (data / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            for name, payload in (
                ("favorites.json", favorites),
                ("overrides.json", exclusions),
                ("snoozed.json", snoozed),
                ("filter-rules.json", {"blocked_topics": []}),
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


if __name__ == "__main__":
    unittest.main()
