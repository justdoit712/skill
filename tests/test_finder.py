"""定向查找核心调度、诊断与输出契约测试（Skill Finder Core Contracts）。

合并收敛自 test_skill_finder.py, test_find_evaluate.py, test_finder_projection.py,
test_finder_owned.py, test_finder_diagnostics.py, test_finder_output_contract_bounds.py,
以及相关回归测试用例。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import smoke
from src.finder.config import DEFAULT_LIMIT
from src.finder.evidence import verify_evidence_snippet, verify_single_evidence
from src.finder.evaluation import (
    DOC_CLEAR,
    DOC_INSUFFICIENT,
    KIND_REQUIRED,
    MATCH_NONE,
    MATCH_STRONG,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from src.finder.report import (
    render_find_markdown_report,
    sanitize_report_for_public,
    should_update_public_snapshot,
)
from src.finder.run import (
    STATUS_ALL_CANDIDATES_OWNED,
    STATUS_COMPLETED,
    STATUS_STOPPED,
    STATUS_USAGE_UNKNOWN,
    classify_evaluation_error,
    execute_find_skill,
)
from src.infra.llm import (
    ModelCallResult,
    REASON_LENGTH_EXCEEDED,
    REASON_NETWORK_ERROR,
)
from src.shared.models import Candidate
from src.shared.output_contracts import FINDER_EVALUATION_CONTRACT

ROOT = Path(__file__).resolve().parents[1]


# =====================================================================
# 1. 调度与用量熔断测试 (Lifecycle & Usage Unknown)
# =====================================================================

@smoke
class FinderLifecycleAndUsageTest(unittest.TestCase):
    """用量熔断、预算控制与中断恢复。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.finder.run.call_model")
    def test_usage_unknown_in_planning_stops_immediately(self, mock_call) -> None:
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage=None,
        )
        report = execute_find_skill("测试需求", root_dir=self.root)
        self.assertEqual(report["status"], STATUS_STOPPED)
        self.assertEqual(report["stop_reason"], STATUS_USAGE_UNKNOWN)
        self.assertEqual(mock_call.call_count, 1)

    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_no_candidates_found_stops_cleanly(self, mock_call, mock_search) -> None:
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "test", "queries": ["q1"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage={"total_tokens": 100},
        )
        mock_search.return_value = (True, [], None)
        report = execute_find_skill("测试需求", root_dir=self.root, max_rounds=1)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["shortlist_count"], 0)


# =====================================================================
# 2. 诊断与错误分类测试 (Diagnostics & Accounting)
# =====================================================================

@smoke
class FinderDiagnosticsTest(unittest.TestCase):
    """Finder 异常诊断分类与用量复算对账。"""

    def test_classify_auth_error_and_plain_403(self):
        res403 = ModelCallResult(
            ok=False,
            http_status=403,
            error='HTTP 403: {"error":{"message":"Free quota exhausted."}}',
        )
        code403, _ = classify_evaluation_error(res403)
        self.assertEqual(code403, "auth_error")

        res401 = ModelCallResult(ok=False, http_status=401, error="HTTP 401: Invalid API Key")
        code401, _ = classify_evaluation_error(res401)
        self.assertEqual(code401, "auth_error")

    def test_classify_network_timeout(self):
        res = ModelCallResult(ok=False, reason_code=REASON_NETWORK_ERROR, error="ReadTimeout")
        code, _ = classify_evaluation_error(res)
        self.assertEqual(code, "network_timeout")

    def test_classify_length_exceeded(self):
        res = ModelCallResult(ok=False, reason_code=REASON_LENGTH_EXCEEDED, finish_reason="length")
        code, _ = classify_evaluation_error(res)
        self.assertEqual(code, "length_exceeded")


# =====================================================================
# 3. 输出契约硬约束测试 (Output Contract Bounds)
# =====================================================================

@smoke
class FinderOutputContractsTest(unittest.TestCase):
    """阶段 4：输出契约定义与长度上限硬约束。"""

    def test_output_contract_schema_bounds(self):
        schema = FINDER_EVALUATION_CONTRACT["json_schema"]["schema"]
        props = schema["properties"]

        self.assertEqual(props["dependencies"]["maxItems"], 5)
        self.assertEqual(props["limitations"]["maxItems"], 5)

        quote_prop = props["criteria_results"]["items"]["properties"]["evidence"]["items"]["properties"]["quote"]
        self.assertEqual(quote_prop["maxLength"], 1200)


# =====================================================================
# 4. 已收录名单过滤集成测试 (Owned Skills Integration)
# =====================================================================

class FinderOwnedIntegrationTest(unittest.TestCase):
    """定向查找对已收录名单的前置过滤与零消耗跳过。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )
        (self.root / "config" / "find-skill.json").write_text(
            json.dumps({"limit": 5, "max_evaluations": 10, "max_tokens": 50000}),
            encoding="utf-8",
        )
        (self.root / "config" / "owned-skills.json").write_text(
            json.dumps({
                "schema_version": "1.0.0",
                "items": [
                    {
                        "skill_id": "test-owner/test-repo:skills/pdf/SKILL.md",
                        "name": "pdf",
                        "source_url": "https://github.com/test-owner/test-repo",
                        "added_at": "2026-09-24",
                    }
                ],
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch("src.finder.run.fetch_candidate_materials")
    @patch("src.finder.run.expand_and_collect_candidates")
    @patch("src.finder.run.search_github_repos_for_query")
    @patch("src.finder.run.call_model")
    def test_all_candidates_owned_zero_evaluations(self, mock_call, mock_search, mock_expand, mock_fetch):
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({"intent": "pdf", "queries": ["pdf"], "criteria": [{"id": "c1", "kind": "required", "description": "d1"}]}),
            usage={"total_tokens": 100},
        )
        mock_search.return_value = (True, [{"owner": "test-owner", "repo": "test-repo", "url": "u", "description": "d"}], None)
        mock_expand.return_value = ([
            Candidate(
                skill_id="test-owner/test-repo:skills/pdf/SKILL.md",
                owner="test-owner", repo="test-repo", path="skills/pdf/SKILL.md",
                name="pdf", url="https://github.com/test-owner/test-repo/blob/main/skills/pdf/SKILL.md",
                repo_url="https://github.com/test-owner/test-repo",
            )
        ], [{"owner": "test-owner", "repo": "test-repo", "ok": True}])

        report = execute_find_skill("pdf", root_dir=self.root, max_rounds=1)
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(mock_fetch.call_count, 0)
        self.assertEqual(report["evaluated_count"], 0)
        self.assertEqual(report["search"]["skipped_owned"], 1)


# =====================================================================
# 5. 安全投影与隔离测试 (Projection & Decoupling)
# =====================================================================

@smoke
class FinderProjectionTest(unittest.TestCase):
    """公共快照白名单脱敏与四态发布条件。"""

    def test_sanitize_report_for_public_removes_local_paths(self):
        raw_report = {
            "query": "test query",
            "status": "completed",
            "stop_reason": "target_reached",
            "report_paths": {
                "json": "C:/Users/Admin/AppData/Local/find-skill/report.json",
                "markdown": "C:/Users/Admin/AppData/Local/find-skill/report.md",
            },
            "candidates": [
                {
                    "skill_id": "owner/repo:SKILL.md",
                    "evaluation": {"match": "strong"},
                    "local_cache_path": "D:/Code/cache/data.json",
                }
            ],
            "shortlist": [{"skill_id": "owner/repo:SKILL.md"}],
        }
        public_view = sanitize_report_for_public(raw_report)
        self.assertNotIn("report_paths", public_view)
        for cand in public_view.get("candidates", []):
            self.assertNotIn("local_cache_path", cand)

    def test_should_update_public_snapshot_conditions(self):
        # 成功且有短名单 -> 更新
        self.assertTrue(should_update_public_snapshot({"status": "completed", "shortlist": [{"id": 1}]}))
        # 异常失败 -> 不更新保留旧快照
        self.assertFalse(should_update_public_snapshot({"status": "error"}))


# =====================================================================
# 6. 证据核验与四级排序测试 (Evidence & Ranking)
# =====================================================================

@smoke
class FinderEvidenceAndRankingTest(unittest.TestCase):
    """客观引文比对、行号漂移容错与四级稳定排序。"""

    def setUp(self):
        self.materials = {
            "SKILL.md": (
                "# Prompt Craftsman\n"
                "A specialized AI Agent skill for designing production prompts.\n"
                "Provides structured templates and constraints.\n"
                "Line 4: Empathetic response techniques included.\n"
            )
        }

    def test_verify_evidence_snippet_exact(self):
        ok, reason = verify_evidence_snippet(
            source_path="SKILL.md",
            start_line=2,
            end_line=2,
            quote="A specialized AI Agent skill for designing production prompts.",
            materials=self.materials,
        )
        self.assertTrue(ok)

    def test_verify_evidence_snippet_drift_recovery(self):
        # 引文实际在第 2 行，模型误报为第 3 行 -> +-3 行内应自动漂移修复
        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 3,
                "end_line": 3,
                "quote": "A specialized AI Agent skill for designing production prompts.",
            },
            self.materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.start_line, 2)
        self.assertEqual(res.match_method, "nearby_drift")

    def test_rank_find_results_stable_order(self):
        # strong clear > partial clear > none
        cands = [
            {"skill_id": "c1", "evaluation": {"match": "none", "documentation": DOC_CLEAR, "criteria_results": []}},
            {"skill_id": "c2", "evaluation": {"match": MATCH_STRONG, "documentation": DOC_CLEAR, "criteria_results": [{"status": "supported"}]}},
            {"skill_id": "c3", "evaluation": {"match": "partial", "documentation": DOC_CLEAR, "criteria_results": [{"status": "supported"}]}},
        ]
        shortlist, alternatives = rank_find_results(cands)
        self.assertEqual([c["skill_id"] for c in shortlist], ["c2"])
        self.assertEqual([c["skill_id"] for c in alternatives], ["c3"])


if __name__ == "__main__":
    unittest.main()
