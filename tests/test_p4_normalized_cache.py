"""Unit 9 / P4: 受限规范化缓存与复用审计单元测试。

验证规范化观察与受限复用闭环：
1. 换行编码纯函数规范化（仅允许 CRLF/CR -> LF，不合并空行、不清理缩进、不删除注释）；
2. 辅助指纹与内容匹配（换行符不同产出相同指纹，增减空行/缩进改变指纹）；
3. 原文证据重验闭环（新材料中证据引文缺失或漂移即安全回退）；
4. 记录合格性校验（终态要求、用量未知防护、版本严格匹配）；
5. 观察模式 vs 启用模式端到端行为（观察模式不阻断模型调用，启用模式安全复用且 0 Token 0 模型调用）；
6. 拒绝原因与审计事实记录（不可篡改的复用审计、拒绝原因统计分类）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.catalog.index import CatalogContext
from src.infra.llm import ModelCallResult
from src.catalog.local import (
    LocalCollection,
    find_normalized_candidate_record,
    process_candidate,
)
from src.catalog.pool import (
    CandidatePool,
    PoolItem,
    STATUS_DONE,
    STATUS_PENDING,
)
from src.shared.identity import candidate_from_repo
from src.shared.metrics import CacheMetricFacts, build_run_metrics
from src.shared.normalization import (
    NORMALIZATION_VERSION,
    create_reuse_audit,
    extract_evidence_quotes,
    inspect_record_for_normalized_reuse,
    normalize_material_text,
    normalized_content_fingerprint,
    verify_cached_evidence_in_text,
)
from src.shared.versions import LLM_OUTPUT_CONTRACT_VERSION
from src.shared.usage import UsageTotals


class TestNormalizationPureFunctions(unittest.TestCase):
    """测试规范化与指纹计算纯函数。"""

    def test_normalize_material_text_crlf_and_cr(self):
        """仅统一换行符为 LF，保持其它字符完全不变。"""
        self.assertIsNone(normalize_material_text(None))
        self.assertEqual(normalize_material_text("hello\r\nworld"), "hello\nworld")
        self.assertEqual(normalize_material_text("hello\rworld"), "hello\nworld")
        self.assertEqual(normalize_material_text("hello\nworld"), "hello\nworld")

    def test_normalize_material_text_preserves_blank_lines_and_indent(self):
        """绝对不合并空行、不去除缩进、不去除注释。"""
        raw = "line1\r\n\r\n  line2  \r\n# comment\r\n"
        expected = "line1\n\n  line2  \n# comment\n"
        self.assertEqual(normalize_material_text(raw), expected)

    def test_normalized_content_fingerprint_matching_newlines(self):
        """不同操作系统检出的相同文本（CRLF vs LF）产出完全相同的规范化辅助指纹。"""
        text_crlf = "# Title\r\n\r\nDescription line\r\n"
        text_lf = "# Title\n\nDescription line\n"
        text_cr = "# Title\r\rDescription line\r"

        fp_crlf = normalized_content_fingerprint(text_crlf)
        fp_lf = normalized_content_fingerprint(text_lf)
        fp_cr = normalized_content_fingerprint(text_cr)

        self.assertIsNotNone(fp_crlf)
        self.assertTrue(fp_crlf.startswith("sha256:norm:v1:"))
        self.assertEqual(fp_crlf, fp_lf)
        self.assertEqual(fp_crlf, fp_cr)

    def test_normalized_content_fingerprint_differs_on_extra_blank_line(self):
        """增加空行必须产出不同辅助指纹，安全回退到非缓存评估。"""
        text1 = "# Title\n\nDescription\n"
        text2 = "# Title\n\n\nDescription\n"  # 增加了一个空行
        self.assertNotEqual(
            normalized_content_fingerprint(text1),
            normalized_content_fingerprint(text2),
        )

    def test_normalized_content_fingerprint_differs_on_indent_or_content_change(self):
        """缩进改动、注释变动必须产出不同指纹。"""
        text1 = "line 1\n  line 2\n"
        text2 = "line 1\n    line 2\n"  # 改变了缩进
        text3 = "line 1\n  line 2\n# extra comment\n"
        self.assertNotEqual(
            normalized_content_fingerprint(text1),
            normalized_content_fingerprint(text2),
        )
        self.assertNotEqual(
            normalized_content_fingerprint(text1),
            normalized_content_fingerprint(text3),
        )


class TestEvidenceVerificationAndInspection(unittest.TestCase):
    """测试原文证据提取、重验与记录资格检查。"""

    def setUp(self):
        self.sample_doc = (
            "# Awesome Skill\n\n"
            "This skill automates testing for Python and Go services.\n"
            "Usage: /test run --all\n"
            "Requires Docker and Python 3.10+.\n"
        )
        self.valid_outcome = {
            "decision": "recommended",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": 3, "end_line": 3, "quote": "This skill automates testing for Python and Go services."}
                        ],
                    },
                    "instruction_completeness": {
                        "verdict": "pass",
                        "evidence": {
                            "quote": "Usage: /test run --all",
                            "start_line": 4,
                            "end_line": 4,
                        },
                    },
                },
                "quality_checks": {
                    "execution_capability": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": 5, "end_line": 5, "quote": "Requires Docker and Python 3.10+."}
                        ],
                    }
                },
            },
        }

    def test_extract_evidence_quotes(self):
        """能够从结构化检查、质量门槛和复核中完整提取引文。"""
        quotes = extract_evidence_quotes(self.valid_outcome)
        self.assertEqual(len(quotes), 3)
        quote_texts = [q["quote"] for q in quotes]
        self.assertIn("This skill automates testing for Python and Go services.", quote_texts)
        self.assertIn("Usage: /test run --all", quote_texts)
        self.assertIn("Requires Docker and Python 3.10+.", quote_texts)

    def test_verify_cached_evidence_in_text_success(self):
        """原文完整且换行符等价时，证据核验成功。"""
        # 测试在 CRLF 原文中核验 LF 引文
        crlf_text = self.sample_doc.replace("\n", "\r\n")
        ok, err = verify_cached_evidence_in_text(self.valid_outcome, crlf_text)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_verify_cached_evidence_in_text_missing_quote(self):
        """新材料删除了某条引文时，必须核验失败。"""
        altered_text = (
            "# Awesome Skill\n\n"
            "This is a rewritten skill.\n"
            "Usage: /test run --all\n"
            "Requires Docker and Python 3.10+.\n"
        )
        ok, err = verify_cached_evidence_in_text(self.valid_outcome, altered_text)
        self.assertFalse(ok)
        self.assertIn("证据引文在新材料中缺失", err)

    def test_inspect_record_for_normalized_reuse_conditions(self):
        """核对状态、版本、用量完整性等关键准入条件。"""
        rec = {
            "evaluation_id": "test_id",
            "status": "completed",
            "rules_version": "1.1.1",
            "model_config_version": "1.0.0",
            "output_contract_version": "1.0.0",
            "normalization_version": "1.0.0",
            "outcome": self.valid_outcome,
            "requests": [{"usage": {"total_tokens": 100}}],
        }
        # 1. 正常合格
        ok, err = inspect_record_for_normalized_reuse(
            rec,
            self.sample_doc,
            expected_rules_version="1.1.1",
            expected_model_config_version="1.0.0",
        )
        self.assertTrue(ok)
        self.assertIsNone(err)

        # 2. 状态未完成
        uncompleted_rec = dict(rec, status="failed")
        ok, err = inspect_record_for_normalized_reuse(uncompleted_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("status_not_completed", err)

        # 3. 存在未知用量
        unknown_usage_rec = dict(rec, requests=[{"usage": None}])
        ok, err = inspect_record_for_normalized_reuse(unknown_usage_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertEqual(err, "contains_unknown_usage")

        # 4. 规则版本不一致
        ok, err = inspect_record_for_normalized_reuse(
            rec,
            self.sample_doc,
            expected_rules_version="1.2.0",
        )
        self.assertFalse(ok)
        self.assertIn("rules_version_mismatch", err)

        # 5. 模型配置版本不一致
        ok, err = inspect_record_for_normalized_reuse(
            rec,
            self.sample_doc,
            expected_model_config_version="2.0.0",
        )
        self.assertFalse(ok)
        self.assertIn("model_config_mismatch", err)

    def test_verify_cached_evidence_rejects_empty_quotes(self):
        """无任何有效引文的评估，必须被拒绝复用。"""
        empty_outcome = {
            "decision": "recommended",
            "evaluation": {"checks": {}},
        }
        ok, err = verify_cached_evidence_in_text(empty_outcome, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("缺少有效证据引用", err)

    def test_verify_cached_evidence_line_bounds_and_ambiguity(self):
        """引文行号越界、非布尔整数类型校验失败时必须被拒绝。"""
        # 1. 行号越界 (sample_doc 只有 5 行，标注第 20 行)
        out_of_bounds_outcome = {
            "decision": "recommended",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": 20, "end_line": 20, "quote": "Usage: /test run --all"}
                        ],
                    }
                }
            },
        }
        ok, err = verify_cached_evidence_in_text(out_of_bounds_outcome, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("证据行号超出材料行数边界", err)

        # 2. 非布尔整数校验：布尔值行号必须被拒绝
        bool_line_outcome = {
            "decision": "recommended",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": True, "end_line": 3, "quote": "Usage: /test run --all"}
                        ],
                    }
                }
            },
        }
        ok, err = verify_cached_evidence_in_text(bool_line_outcome, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("证据行号类型非法", err)

        # 3. 字符串行号或缺失行号必须被直接拒绝
        str_line_outcome = {
            "decision": "recommended",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": "1", "end_line": "3", "quote": "Usage: /test run --all"}
                        ],
                    }
                }
            },
        }
        ok, err = verify_cached_evidence_in_text(str_line_outcome, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("证据行号类型非法", err)

    def test_inspect_record_rejects_null_tokens_and_pending_review(self):
        """测试对 total_tokens 为 null 以及待复核状态的坚决拦截。"""
        base_rec = {
            "evaluation_id": "test_id",
            "status": "completed",
            "rules_version": "1.1.1",
            "model_config_version": "1.0.0",
            "output_contract_version": "1.0.0",
            "normalization_version": "1.0.0",
            "outcome": self.valid_outcome,
            "requests": [{"usage": {"total_tokens": 100}}],
        }

        # 1. total_tokens 为 None
        null_tokens_rec = dict(base_rec, requests=[{"usage": {"total_tokens": None}}])
        ok, err = inspect_record_for_normalized_reuse(null_tokens_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertEqual(err, "contains_unknown_usage")

        # 2. requests 中 usage 缺少 total_tokens
        missing_tokens_rec = dict(base_rec, requests=[{"usage": {}}])
        ok, err = inspect_record_for_normalized_reuse(missing_tokens_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertEqual(err, "contains_unknown_usage")

        # 3. 待初评 / 待复核字段
        pending_eval_rec = dict(base_rec, pending_evaluation={"some": "state"})
        ok, err = inspect_record_for_normalized_reuse(pending_eval_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertEqual(err, "pending_review_or_evaluation")

        pending_rev_rec = dict(base_rec, pending_review={"some": "state"})
        ok, err = inspect_record_for_normalized_reuse(pending_rev_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertEqual(err, "pending_review_or_evaluation")

        # 4. 处于未决 review 状态
        pending_outcome = dict(self.valid_outcome, review_status="pending_review")
        pending_outcome_rec = dict(base_rec, outcome=pending_outcome)
        ok, err = inspect_record_for_normalized_reuse(pending_outcome_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("pending_review_status", err)

    def test_inspect_record_version_validation(self):
        """测试契约版本与规范化版本不匹配或缺失时拦截。"""
        rec = {
            "evaluation_id": "test_id",
            "status": "completed",
            "rules_version": "1.1.1",
            "model_config_version": "1.0.0",
            "output_contract_version": "1.0.0",
            "normalization_version": "1.0.0",
            "outcome": self.valid_outcome,
            "requests": [{"usage": {"total_tokens": 100}}],
        }
        # 1. 契约版本不匹配
        ok, err = inspect_record_for_normalized_reuse(
            rec,
            self.sample_doc,
            expected_contract_version="2.0.0",
        )
        self.assertFalse(ok)
        self.assertIn("contract_version_mismatch", err)

        # 2. 规范化算法版本不匹配
        ok, err = inspect_record_for_normalized_reuse(
            rec,
            self.sample_doc,
            expected_normalization_version="2.0.0",
        )
        self.assertFalse(ok)
        self.assertIn("normalization_version_mismatch", err)

        # 3. 缺失 output_contract_version
        missing_cv_rec = dict(rec)
        del missing_cv_rec["output_contract_version"]
        ok, err = inspect_record_for_normalized_reuse(missing_cv_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("contract_version_mismatch", err)

        # 4. 缺失 normalization_version
        missing_nv_rec = dict(rec)
        del missing_nv_rec["normalization_version"]
        ok, err = inspect_record_for_normalized_reuse(missing_nv_rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("normalization_version_mismatch", err)

    def test_inspect_record_review_evidence_and_pending_review_nested(self):
        """测试独立复核顶层检查与 quality_checks 证据的提取核验，以及嵌套待复核状态拦截。"""
        from copy import deepcopy
        review_outcome = {
            "decision": "recommended",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": 3, "end_line": 3, "quote": "This skill automates testing for Python and Go services."}
                        ],
                    }
                },
                "quality_audit": {
                    "review_status": "passed",
                    "review": {
                        "scope_match": {
                            "verdict": "pass",
                            "citations": [
                                {"start_line": 4, "end_line": 4, "quote": "Usage: /test run --all"}
                            ],
                        },
                        "quality_checks": {
                            "practical_value": {
                                "verdict": "pass",
                                "citations": [
                                    {"start_line": 5, "end_line": 5, "quote": "Requires Docker and Python 3.10+."}
                                ],
                            }
                        },
                    },
                },
            },
        }

        # 1. 成功核验复核引文
        quotes = extract_evidence_quotes(review_outcome)
        quote_texts = [q["quote"] for q in quotes]
        self.assertIn("Usage: /test run --all", quote_texts)
        self.assertIn("Requires Docker and Python 3.10+.", quote_texts)
        ok, err = verify_cached_evidence_in_text(review_outcome, self.sample_doc)
        self.assertTrue(ok)

        # 2. 嵌套在 outcome.evaluation.quality_audit 中的 pending_review 状态必须被拦截
        pending_review_outcome = deepcopy(review_outcome)
        pending_review_outcome["evaluation"]["quality_audit"]["review_status"] = "pending_review"
        rec = {
            "evaluation_id": "test_id",
            "status": "completed",
            "rules_version": "1.1.1",
            "model_config_version": "1.0.0",
            "output_contract_version": "1.0.0",
            "normalization_version": "1.0.0",
            "outcome": pending_review_outcome,
            "requests": [{"usage": {"total_tokens": 100}}],
        }
        ok, err = inspect_record_for_normalized_reuse(rec, self.sample_doc)
        self.assertFalse(ok)
        self.assertIn("pending_review_status", err)

    def test_domain_checks_evidence_extraction_and_verification(self):
        """Issue 2 回归测试：初评及复核的 domain_checks 必须纳入证据提取与核验。
        基础引文有效、但领域引文不存在时，必须坚决拒绝复用。
        """
        from copy import deepcopy

        outcome_with_domain = {
            "decision": "recommended",
            "evaluation": {
                "scope_match": {
                    "verdict": "pass",
                    "citations": [
                        {"start_line": 3, "end_line": 3, "quote": "This skill automates testing for Python and Go services."}
                    ],
                },
                "domain_checks": {
                    "security_audit": {
                        "value": "pass",
                        "citations": [
                            {"start_line": 1, "end_line": 1, "quote": "THIS TEXT DOES NOT EXIST IN DOCUMENT AT ALL"}
                        ],
                    }
                },
            },
        }

        # 1. 验证 domain_checks 引文已被成功提取
        quotes = extract_evidence_quotes(outcome_with_domain)
        quote_texts = [q["quote"] for q in quotes]
        self.assertIn("THIS TEXT DOES NOT EXIST IN DOCUMENT AT ALL", quote_texts)

        # 2. 核验因领域引文不存在而失败
        ok, err = verify_cached_evidence_in_text(outcome_with_domain, self.sample_doc)
        self.assertFalse(ok)

        # 3. 复核中的 domain_checks 缺失同样导致拒绝
        valid_domain_outcome = deepcopy(outcome_with_domain)
        valid_domain_outcome["evaluation"]["domain_checks"]["security_audit"]["citations"] = [
            {"start_line": 4, "end_line": 4, "quote": "Usage: /test run --all"}
        ]
        # 初评领域引文有效，但复核领域引文无效
        valid_domain_outcome["evaluation"]["quality_audit"] = {
            "review_status": "passed",
            "review": {
                "domain_checks": {
                    "finance_rule": {
                        "value": "pass",
                        "citations": [
                            {"start_line": 1, "end_line": 1, "quote": "NON_EXISTENT_REVIEW_DOMAIN_QUOTE"}
                        ],
                    }
                }
            }
        }
        review_quotes = extract_evidence_quotes(valid_domain_outcome)
        self.assertIn("NON_EXISTENT_REVIEW_DOMAIN_QUOTE", [q["quote"] for q in review_quotes])
        ok, err = verify_cached_evidence_in_text(valid_domain_outcome, self.sample_doc)
        self.assertFalse(ok)

    def test_code_evidence_with_leading_indentation_preserved_and_verified(self):
        """Issue 1 回归测试：保留缩进的代码引文（如 '    run()'）在提取时不应被 strip() 去除前导空格，
        在新材料仅变更换行符时，证据核验与规范化复用必须成功通过。
        """
        code_doc_lf = "```python\ndef main():\n    run()\n```\n"
        code_doc_crlf = code_doc_lf.replace("\n", "\r\n")

        outcome = {
            "decision": "recommended",
            "evaluation": {
                "scope_match": {
                    "verdict": "pass",
                    "citations": [
                        {"start_line": 3, "end_line": 3, "quote": "    run()"}
                    ],
                }
            },
        }

        # 1. 验证提取出的引文完整保留了 4 个空格缩进
        quotes = extract_evidence_quotes(outcome)
        self.assertEqual(quotes[0]["quote"], "    run()")

        # 2. 验证在 CRLF 版本的原文中核验通过
        ok, err = verify_cached_evidence_in_text(outcome, code_doc_crlf)
        self.assertTrue(ok, f"核验失败：{err}")


class TestNormalizedCachePipelineIntegration(unittest.TestCase):
    """测试目录管道中的受限规范化缓存观察与复用。"""

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.root = self.test_dir
        self.local = self.test_dir / "data" / "local"
        self.local.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.local / "runs" / "test_run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.pool_path = self.local / "pool.json"

        # 建立已存在评估的 evaluations 目录
        self.eval_dir = self.local / "state" / "evaluations"
        self.eval_dir.mkdir(parents=True, exist_ok=True)

        self.skill_content_lf = (
            "# My Skill\n\n"
            "A test utility for demonstration.\n"
            "Use via /run demo.\n"
        )
        self.norm_fp = normalized_content_fingerprint(self.skill_content_lf)

        # 构造一条合法的历史评估记录
        self.skill_id = "test_owner/test_repo:SKILL.md"
        self.existing_eid = f"{self.skill_id}|sha256:old_fingerprint|1.1.1|1.0.0"
        self.existing_outcome = {
            "decision": "recommended",
            "summary_zh": "测试工具说明",
            "main_category": "development",
            "evaluation": {
                "checks": {
                    "scope_match": {
                        "verdict": "pass",
                        "citations": [
                            {"start_line": 3, "end_line": 3, "quote": "A test utility for demonstration."}
                        ],
                    }
                }
            },
        }
        self.existing_record = {
            "evaluation_id": self.existing_eid,
            "skill_id": self.skill_id,
            "content_fingerprint": "sha256:old_fingerprint",
            "normalized_content_fingerprint": self.norm_fp,
            "normalization_version": NORMALIZATION_VERSION,
            "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
            "rules_version": "1.1.1",
            "model_config_version": "1.0.0",
            "status": "completed",
            "outcome": self.existing_outcome,
            "requests": [{"usage": {"total_tokens": 150}}],
        }
        # 保存到磁盘
        record_file = self.eval_dir / "existing_rec.json"
        record_file.write_text(json.dumps(self.existing_record, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_state(self, *, enable_normalized_cache: bool = False):
        candidate = candidate_from_repo(
            "test_owner",
            "test_repo",
            path="SKILL.md",
            url="https://github.com/test_owner/test_repo/blob/HEAD/SKILL.md",
        )
        pool_item = PoolItem(
            seq=1,
            candidate=candidate,
            status=STATUS_PENDING,
        )
        pool = CandidatePool(items=[pool_item])

        ledger_records: dict[str, dict] = {}
        mock_ledger = MagicMock()
        mock_ledger.evaluations_dir = self.eval_dir
        mock_ledger.begin_attempt.return_value = 1

        def _ledger_get(eid):
            return ledger_records.get(eid)

        def _ledger_reserve(entries):
            for entry in entries:
                eid = entry["evaluation_id"]
                ledger_records[eid] = dict(entry, status="reserved")

        def _ledger_complete(eid, outcome):
            rec = ledger_records.setdefault(eid, {"evaluation_id": eid})
            rec["status"] = "completed"
            rec["outcome"] = outcome

        def _ledger_save_record(eid, rec):
            ledger_records[eid] = dict(rec)

        mock_ledger.get.side_effect = _ledger_get
        mock_ledger.reserve.side_effect = _ledger_reserve
        mock_ledger.complete.side_effect = _ledger_complete
        mock_ledger.save_record.side_effect = _ledger_save_record

        report = {
            "run_id": "test_run",
            "checked": 0,
            "evaluations": 0,
            "cached": 0,
            "fetch_failed": 0,
            "prescreen_excluded": 0,
            "static_skipped": 0,
            "new_recommended": 0,
            "failed_requests": 0,
            "failed_evaluations": 0,
            "budget_tokens": 0,
            "unknown_usage_reserved_tokens": 0,
            "calls": [],
            "recommendations": [],
            "stop_causes": [],
            "stop_reason": None,
            "cache_observation": {
                "version": NORMALIZATION_VERSION,
                "enabled": enable_normalized_cache,
                "observed_count": 0,
                "potential_hits": 0,
                "actual_reused": 0,
                "rejection_reasons": {},
            },
        }

        context = CatalogContext(
            rules_version="1.1.1",
            domain_names=[],
            source_types={},
        )

        cfg = {
            "model": {"model": "test-model", "model_config_version": "1.0.0"},
            "rules": {"rules_version": "1.1.1", "checks": []},
            "prescreen": MagicMock(domain_names=[]),
            "source_types": {},
            "taxonomy": {"categories": []},
        }
        settings = {
            "target_recommended": 10,
            "max_total_tokens": 100000,
            "enable_normalized_cache": enable_normalized_cache,
        }

        # 抓取模拟：返回 CRLF 版本的文本（与历史评估的 LF 内容仅有换行差异）
        crlf_content = self.skill_content_lf.replace("\n", "\r\n")
        fetch_result = MagicMock(ok=True, text=crlf_content, truncated=False)
        fetch_fn = MagicMock(return_value=fetch_result)

        call_result = ModelCallResult(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            attempts=1,
            ok=True,
        )
        mock_eval_fn = MagicMock(return_value={
            "ok": True,
            "evaluation": self.existing_outcome["evaluation"],
            "call": call_result,
        })

        state = LocalCollection(
            root=self.root,
            local=self.local,
            settings=settings,
            cfg=cfg,
            discover_fn=MagicMock(),
            fetch_fn=fetch_fn,
            evaluate_fn=mock_eval_fn,
            log=MagicMock(),
            sleep=MagicMock(),
            run_id="test_run",
            run_dir=self.run_dir,
            usage=UsageTotals(),
            report=report,
            ledger=mock_ledger,
            context=context,
            entries={},
            old_recommended=set(),
            baseline={"entries": []},
            active_snoozed=set(),
            manual_exclusions=set(),
            manual_picks={},
            pool_path=self.pool_path,
            pool=pool,
            dirty=False,
            consecutive_failures=0,
            active_eid=None,
            active_call=None,
            unknown_reserve=0,
            max_attempts=2,
            max_retries=1,
            pending_items=[pool_item],
            owned_ids=set(),
            skipped_owned_ids=set(),
        )
        return state, pool_item, mock_eval_fn

    def test_observation_mode_records_potential_hit_without_blocking_evaluation(self):
        """观察模式（默认）：记录潜在命中，但不阻断模型调用，actual_reused 为 0。"""
        state, item, mock_eval = self._make_state(enable_normalized_cache=False)
        ok = process_candidate(state, item)
        self.assertTrue(ok)

        obs = state.report["cache_observation"]
        self.assertEqual(obs["potential_hits"], 1)
        self.assertEqual(obs["actual_reused"], 0)
        # 模型仍然被调用了
        mock_eval.assert_called_once()

    def test_enabled_mode_reuses_record_with_zero_tokens_and_audit(self):
        """启用模式：安全复用记录，模型调用为 0，Token 为 0，并记录复用审计。"""
        state, item, mock_eval = self._make_state(enable_normalized_cache=True)
        ok = process_candidate(state, item)
        self.assertTrue(ok)

        obs = state.report["cache_observation"]
        self.assertEqual(obs["potential_hits"], 1)
        self.assertEqual(obs["actual_reused"], 1)
        self.assertEqual(state.report["cached"], 1)

        # 关键断言：模型调用绝对未发生！
        mock_eval.assert_not_called()
        self.assertEqual(state.usage.total_tokens, 0)

        # 账本保存了 completed 状态与复用审计
        state.ledger.complete.assert_called_once()
        _, complete_kwargs = state.ledger.complete.call_args[0], state.ledger.complete.call_args[1]
        completed_outcome = state.ledger.complete.call_args[0][1]
        self.assertTrue(completed_outcome.get("cached"))
        audit = completed_outcome.get("reuse_audit")
        self.assertIsNotNone(audit)
        self.assertEqual(audit["source_evaluation_id"], self.existing_eid)
        self.assertEqual(audit["normalization_version"], NORMALIZATION_VERSION)
        self.assertEqual(audit["status"], "reused")

    def test_safe_fallback_on_content_drift(self):
        """若文本被改动导致证据引文缺失，规范化复用被拒绝并记录原因，安全调用模型。"""
        state, item, mock_eval = self._make_state(enable_normalized_cache=True)
        # 修改抓取返回：证据引文被删除
        state.fetch_fn.return_value = MagicMock(
            ok=True,
            text="# My Skill\n\nCompletely different content without the quote.\n",
            truncated=False,
        )

        ok = process_candidate(state, item)
        self.assertTrue(ok)

        obs = state.report["cache_observation"]
        self.assertEqual(obs["actual_reused"], 0)
        # 模型被调用作为安全回退
        mock_eval.assert_called_once()

    def test_transaction_ordering_fault_injection_ledger_failure_preserves_pending_status(self):
        """测试账本完成阶段注入故障时，事务落盘顺序保证候选池绝不提前被标记为 STATUS_DONE。"""
        state, item, mock_eval = self._make_state(enable_normalized_cache=True)
        # 故障注入：在 ledger.complete 时抛出 I/O 故障
        state.ledger.complete.side_effect = IOError("Disk full during ledger write")

        with self.assertRaises(IOError):
            process_candidate(state, item)

        # 验证核心保证：候选池状态未被提前置为 DONE，依然是 PENDING
        self.assertEqual(item.status, STATUS_PENDING)

    def test_real_budget_ledger_reuse_identity_and_version_persistence(self):
        """Issue 3 回归测试：使用真实 BudgetLedger 复现复用链路，
        断言：
        1. 账本落盘记录顶层与 outcome 中的 candidate, materials, evaluation.source_fingerprint
           均更新为当前新材料的真实身份（新指纹），杜绝旧指纹残留；
        2. 顶层及 outcome 中的 normalization_version 与 output_contract_version 均实际落盘。
        """
        from src.catalog.budget import BudgetLedger

        real_ledger = BudgetLedger.load(self.local / "state", cap=10, max_attempts=2)
        state, item, mock_eval = self._make_state(enable_normalized_cache=True)
        state.ledger = real_ledger

        # 执行复用
        ok = process_candidate(state, item)
        self.assertTrue(ok)

        # 检查候选池状态
        self.assertEqual(item.status, STATUS_DONE)

        # 检查真实账本落盘记录
        new_eid = f"{self.skill_id}|{item.candidate.content_fingerprint}|1.1.1|1.0.0"
        ledger_record = real_ledger.get(new_eid)
        self.assertIsNotNone(ledger_record, f"账本记录未落盘: {new_eid}")

        # 顶层字段核验
        self.assertEqual(ledger_record["status"], "completed")
        self.assertEqual(ledger_record["content_fingerprint"], item.candidate.content_fingerprint)
        self.assertNotEqual(ledger_record["content_fingerprint"], "sha256:old_fingerprint")
        self.assertEqual(ledger_record["normalized_content_fingerprint"], item.candidate.normalized_content_fingerprint)
        self.assertEqual(ledger_record["normalization_version"], NORMALIZATION_VERSION)
        self.assertEqual(ledger_record["output_contract_version"], LLM_OUTPUT_CONTRACT_VERSION)

        # outcome 内部字段核验
        outcome = ledger_record["outcome"]
        self.assertTrue(outcome["cached"])
        self.assertEqual(outcome["candidate"]["content_fingerprint"], item.candidate.content_fingerprint)
        self.assertEqual(outcome["evaluation"]["source_fingerprint"], item.candidate.content_fingerprint)
        self.assertEqual(outcome["materials"]["documents"][0]["fingerprint"], item.candidate.content_fingerprint)
        self.assertEqual(outcome["normalization_version"], NORMALIZATION_VERSION)
        self.assertEqual(outcome["output_contract_version"], LLM_OUTPUT_CONTRACT_VERSION)


class TestMetricsFactMapping(unittest.TestCase):
    """测试 build_run_metrics 对规范化缓存事实的映射。"""

    def test_metrics_facts_from_report(self):
        report = {
            "cached": 2,
            "cache_observation": {
                "version": NORMALIZATION_VERSION,
                "enabled": True,
                "observed_count": 5,
                "potential_hits": 3,
                "actual_reused": 1,
                "rejection_reasons": {"rules_version_mismatch": 2},
            },
        }
        metrics = build_run_metrics(report, kind="catalog")
        cache_metrics = metrics["cache"]
        self.assertEqual(cache_metrics["exact_hits"], 1)  # 总缓存 2 减去规范化复用 1 = 精确命中 1
        self.assertEqual(cache_metrics["normalized_potential_hits"], 3)
        self.assertEqual(cache_metrics["actual_reused"], 1)
        self.assertEqual(cache_metrics["rejection_reasons"], {"rules_version_mismatch": 2})

    def test_single_normalized_reuse_does_not_double_count_exact_hits(self):
        """Issue 2 回归测试：仅有 1 次规范化复用时，exact_hits 必须为 0，杜绝重复计数。"""
        report = {
            "cached": 1,
            "cache_observation": {
                "version": NORMALIZATION_VERSION,
                "enabled": True,
                "observed_count": 1,
                "potential_hits": 1,
                "actual_reused": 1,
                "rejection_reasons": {},
            },
        }
        metrics = build_run_metrics(report, kind="catalog")
        cache_metrics = metrics["cache"]
        self.assertEqual(cache_metrics["exact_hits"], 0)
        self.assertEqual(cache_metrics["actual_reused"], 1)


if __name__ == "__main__":
    unittest.main()
