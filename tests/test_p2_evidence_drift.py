"""Unit 5 / P2.2 单元测试：Finder 邻近证据修复及展示。

测试覆盖：
1. 指定区间精确匹配 (match_method="exact")；
2. 邻近有限行号漂移容错与定位恢复 (match_method="nearby_drift", +-3行)；
3. 超出允许漂移阈值严格拒绝；
4. 邻近多处重复位置歧义严格拒绝；
5. 标点、数字、否定词删改与段落拼接反例绝对防御；
6. 非法行号（布尔值、越界、反向）、未读文件与空引文严格防御；
7. 评估降级与短名单端到端全链路验证（解决“一直没有推荐”痛点）；
8. 报告展示实际核验行号与公共快照脱敏投影。
"""

from __future__ import annotations

import unittest

from src.finder.evaluation import (
    KIND_QUALITY_SIGNAL,
    KIND_REQUIRED,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_STRONG,
    STATUS_SUPPORTED,
    STATUS_UNKNOWN,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from src.finder.evidence import (
    DEFAULT_MAX_DRIFT_LINES,
    EvidenceVerificationResult,
    verify_evidence_snippet,
    verify_single_evidence,
)
from src.finder.report import (
    render_find_markdown_report,
    sanitize_report_for_public,
)
from src.shared.versions import EVIDENCE_VERIFIER_VERSION
from tests.fixtures.benchmark_samples import (
    EVIDENCE_BENCHMARK_DOCUMENT,
    EVIDENCE_BENCHMARK_SAMPLES,
)


class TestEvidenceVerificationDriftTolerance(unittest.TestCase):
    """测试 evidence.py 的分层核验与邻近容错漂移修复。"""

    def setUp(self) -> None:
        self.materials = {
            "SKILL.md": EVIDENCE_BENCHMARK_DOCUMENT,
        }

    def test_multiline_normal_exact_match(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["multiline_normal"]
        res = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.match_method, "exact")
        self.assertEqual(res.start_line, sample["start_line"])
        self.assertEqual(res.end_line, sample["end_line"])
        self.assertEqual(res.failure_code, "")
        self.assertEqual(res.verifier_version, EVIDENCE_VERIFIER_VERSION)

    def test_line_drift_positive_recovers_actual_lines(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["line_drift_positive"]
        # 1. 当关闭漂移容错时，精确比对必须失败
        res_exact_only = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
            allow_drift=False,
        )
        self.assertFalse(res_exact_only.is_valid)
        self.assertEqual(res_exact_only.failure_code, "text_mismatch")

        # 2. 当开启默认漂移容错（+-3行）时，成功唯一定位恢复
        res_drift = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertTrue(res_drift.is_valid)
        self.assertEqual(res_drift.match_method, "nearby_drift")
        # 恢复为文档中的真实行号 8-9
        self.assertEqual(res_drift.start_line, sample["actual_start_line"])
        self.assertEqual(res_drift.end_line, sample["actual_end_line"])
        # 同时完整保留大模型原始声称的行号 6-8
        self.assertEqual(res_drift.original_start_line, sample["start_line"])
        self.assertEqual(res_drift.original_end_line, sample["end_line"])

    def test_drift_exceeded_threshold_rejected(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["line_drift_positive"]
        # 原真实位置为 8-9，若声称位置为 1-2（漂移 6-7 行），超出 max_drift=3 时必须拒绝
        res = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": 1,
                "end_line": 2,
                "quote": sample["quote"],
            },
            self.materials,
            max_drift=3,
        )
        self.assertFalse(res.is_valid)

    def test_ambiguous_nearby_match_rejected(self) -> None:
        # 文档中第 14 行与第 26 行完全相同: "Note: duplicate disclaimer for license compliance."
        # 如果我们在包含两处的窗口中间提供行号（如第 20 行，且 max_drift 覆盖两者），必须检测出歧义并拒绝
        dup_quote = "Note: duplicate disclaimer for license compliance."
        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 20,
                "end_line": 20,
                "quote": dup_quote,
            },
            self.materials,
            allow_drift=True,
            max_drift=7,  # 覆盖 13 到 27 行，恰好包含第 14 与第 26 两处重复
        )
        self.assertFalse(res.is_valid)
        self.assertEqual(res.failure_code, "ambiguous_nearby_match")
        self.assertIn("歧义", res.failure_reason)

    def test_negation_fabricated_rejected(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["negation_contrast_fabricated"]
        # 原文包含 NOT，引文删掉了 NOT
        res = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
            allow_drift=True,
        )
        self.assertFalse(res.is_valid)

    def test_number_variant_fabricated_rejected(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["number_variant_fabricated"]
        # 原文是 3.11，引文被篡改为 2.7
        res = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
            allow_drift=True,
        )
        self.assertFalse(res.is_valid)

    def test_modified_code_string_whitespace_rejected(self) -> None:
        """Issue 5 回归测试：严禁折叠代码或字符串内部空白。
        原文为 x = "a  b"，模型引文被篡改为 x = "a b"，位置发生漂移后必须坚决拒绝。
        """
        code_doc = '```python\n# test module\nx = "a  b"\ny = 10\n```'
        materials = {"SKILL.md": code_doc}

        # 原文 x = "a  b" 位于第 3 行，声称位于第 1 行（漂移 2 行）
        # 引文被修改为 x = "a b"（双空格缩减为单空格）
        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 1,
                "end_line": 1,
                "quote": 'x = "a b"',
            },
            materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertFalse(res.is_valid)
        self.assertEqual(res.failure_code, "nearby_not_found")

    def test_code_indentation_change_rejected(self) -> None:
        """Issue 2 回归测试：代码匹配必须严格保留缩进。
        将 cleanup() 从条件块内移到块外（缩进改变）必须被拒绝。
        """
        code_doc = "```python\nif condition:\n    cleanup()\n```"
        materials = {"SKILL.md": code_doc}

        # 1. 精确行号下，缩进被消除（例如模型引用 'cleanup()'，无缩进）
        res_exact = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 2,
                "end_line": 3,
                "quote": "if condition:\ncleanup()",
            },
            materials,
            allow_drift=False,
        )
        self.assertFalse(res_exact.is_valid)
        self.assertEqual(res_exact.failure_code, "text_mismatch")

        # 2. 漂移容错下，缩进被消除同样必须被拒绝
        res_drift = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 1,
                "end_line": 2,
                "quote": "if condition:\ncleanup()",
            },
            materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertFalse(res_drift.is_valid)
        self.assertEqual(res_drift.failure_code, "nearby_not_found")

    def test_code_empty_line_deletion_rejected(self) -> None:
        """Issue 2 回归测试：代码匹配必须严格保留代码块内部的空行。
        删除代码中间的空行必须被拒绝。
        """
        code_doc = "```python\ndef run():\n    step1()\n\n    step2()\n```"
        materials = {"SKILL.md": code_doc}

        # 删除了 step1() 与 step2() 之间的空行
        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 2,
                "end_line": 5,
                "quote": "def run():\n    step1()\n    step2()",
            },
            materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertFalse(res.is_valid)

    def test_code_exact_indentation_and_blank_lines_accepted(self) -> None:
        """Issue 2 回归测试：保持代码正确缩进与空行的引文能够成功核验。"""
        code_doc = "```python\ndef run():\n    step1()\n\n    step2()\n```"
        materials = {"SKILL.md": code_doc}

        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 3,
                "end_line": 5,
                "quote": "    step1()\n\n    step2()",
            },
            materials,
            allow_drift=True,
            max_drift=2,
        )
        self.assertTrue(res.is_valid)

    def test_multiline_code_collapsed_to_single_line_rejected(self) -> None:
        """Issue 1 回归测试：多行代码 if ok:\n    run() 被改写为单行 if ok: run() 必须被拒绝。"""
        code_doc = "```python\nif ok:\n    run()\n```"
        materials = {"SKILL.md": code_doc}

        # 1. 精确匹配下拒绝
        res_exact = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 2,
                "end_line": 3,
                "quote": "if ok: run()",
            },
            materials,
            allow_drift=False,
        )
        self.assertFalse(res_exact.is_valid)

        # 2. 漂移容错下同样坚决拒绝
        res_drift = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 1,
                "end_line": 2,
                "quote": "if ok: run()",
            },
            materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertFalse(res_drift.is_valid)

    def test_tilde_code_block_indentation_change_rejected(self) -> None:
        """Issue 1 回归测试：~~~ 围栏代码块必须被正确识别，缩进删改必须被坚决拒绝。"""
        code_doc = "~~~python\nif ok:\n    run()\n~~~"
        materials = {"SKILL.md": code_doc}

        res = verify_single_evidence(
            {
                "source_path": "SKILL.md",
                "start_line": 2,
                "end_line": 3,
                "quote": "if ok:\nrun()",
            },
            materials,
            allow_drift=True,
            max_drift=3,
        )
        self.assertFalse(res.is_valid)

    def test_cross_file_spliced_fabricated_rejected(self) -> None:
        sample = EVIDENCE_BENCHMARK_SAMPLES["cross_file_spliced_fabricated"]
        # 拼接跳过了中间行
        res = verify_single_evidence(
            {
                "source_path": sample["source_path"],
                "start_line": sample["start_line"],
                "end_line": sample["end_line"],
                "quote": sample["quote"],
            },
            self.materials,
            allow_drift=True,
        )
        self.assertFalse(res.is_valid)

    def test_invalid_line_types_rejected(self) -> None:
        # 布尔值陷阱
        res_bool = verify_single_evidence(
            {"source_path": "SKILL.md", "start_line": True, "end_line": 5, "quote": "some text"},
            self.materials,
        )
        self.assertFalse(res_bool.is_valid)
        self.assertEqual(res_bool.failure_code, "invalid_line_type")

        # 越界
        res_oob = verify_single_evidence(
            {"source_path": "SKILL.md", "start_line": 999, "end_line": 1005, "quote": "some text"},
            self.materials,
        )
        self.assertFalse(res_oob.is_valid)
        self.assertEqual(res_oob.failure_code, "out_of_bounds")

        # 路径缺失
        res_missing = verify_single_evidence(
            {"source_path": "unknown.md", "start_line": 1, "end_line": 2, "quote": "some text"},
            self.materials,
        )
        self.assertFalse(res_missing.is_valid)
        self.assertEqual(res_missing.failure_code, "file_not_found")


class TestEndToEndEvaluationAndShortlistRecovery(unittest.TestCase):
    """端到端验证：引文行号轻微漂移能够被修复并成功进入推荐短名单（解决 0 推荐痛点）。"""

    def setUp(self) -> None:
        self.materials = {
            "SKILL.md": EVIDENCE_BENCHMARK_DOCUMENT,
        }
        self.criteria = [
            {
                "id": "http_scraping",
                "kind": KIND_REQUIRED,
                "description": "具备 HTTP 网页抓取能力",
            },
            {
                "id": "proxy_support",
                "kind": KIND_REQUIRED,
                "description": "支持代理轮换与会话持久化",
            },
        ]

    def test_drift_tolerance_rescues_strong_match_into_shortlist(self) -> None:
        """核心业务用例：

        大模型在评估时准确找到了能力证据（Built on top of httpx...），但行号偏差提供了 [6, 8]（实际为 [8, 9]）。
        - 关闭漂移容错：证据核验失败 -> 必需项降级为 unknown -> 候选由 strong 降级为 partial -> 短名单 0 项（一直没有推荐）；
        - 开启漂移容错：证据行号修复为 [8, 9] -> 必需项维持 supported -> 候选维持 strong -> 成功产出短名单推荐！
        """
        raw_evaluation = {
            "match": MATCH_STRONG,
            "documentation": "clear",
            "summary_zh": "高性能异步 HTTP 爬虫套件",
            "why_consider": "功能完备且支持重试与代理轮换",
            "criteria_results": [
                {
                    "criterion_id": "http_scraping",
                    "status": STATUS_SUPPORTED,
                    "explanation": "基于 httpx 实现异步爬取并支持自动重试",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 6,  # 模型漂移行号
                            "end_line": 8,
                            "quote": "Built on top of httpx and beautifulsoup4 for parsing HTML documents. Supports automatic retry with exponential backoff on 429 and 503.",
                        }
                    ],
                },
                {
                    "criterion_id": "proxy_support",
                    "status": STATUS_SUPPORTED,
                    "explanation": "说明中明确包含代理轮换支持",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 10,
                            "end_line": 10,
                            "quote": "Features include proxy rotation and session persistence.",
                        }
                    ],
                },
            ],
        }

        # 1. 模拟未修复前的行为（关闭容错）
        eval_without_repair = verify_and_adjust_evaluation(
            raw_evaluation,
            self.materials,
            self.criteria,
            allow_drift=False,
        )
        self.assertEqual(eval_without_repair["match"], MATCH_PARTIAL)
        item_without_repair = {
            "candidate": {"skill_id": "owner/repo:SKILL.md", "name": "web-scraper"},
            "evaluation": eval_without_repair,
        }
        shortlist_0, alts_0 = rank_find_results([item_without_repair], plan={"criteria": self.criteria})
        # 证实问题：强匹配被降级，推荐短名单为 0！
        self.assertEqual(len(shortlist_0), 0)
        self.assertEqual(len(alts_0), 1)

        # 2. 启用邻近容错修复后的行为
        eval_with_repair = verify_and_adjust_evaluation(
            raw_evaluation,
            self.materials,
            self.criteria,
            allow_drift=True,
            max_drift=3,
        )
        # 强匹配保住！
        self.assertEqual(eval_with_repair["match"], MATCH_STRONG)
        repaired_ev = eval_with_repair["criteria_results"][0]["evidence"][0]
        # 实际位置校正为 [8, 9]
        self.assertEqual(repaired_ev["start_line"], 8)
        self.assertEqual(repaired_ev["end_line"], 9)
        self.assertEqual(repaired_ev["original_start_line"], 6)
        self.assertEqual(repaired_ev["original_end_line"], 8)
        self.assertEqual(repaired_ev["match_method"], "nearby_drift")

        item_with_repair = {
            "candidate": {"skill_id": "owner/repo:SKILL.md", "name": "web-scraper", "repo_url": "https://github.com/owner/repo"},
            "evaluation": eval_with_repair,
        }
        shortlist_1, alts_1 = rank_find_results([item_with_repair], plan={"criteria": self.criteria})
        # 痛点解决：成功产生推荐短名单！
        self.assertEqual(len(shortlist_1), 1)
        self.assertEqual(len(alts_1), 0)
        self.assertEqual(shortlist_1[0]["candidate"]["skill_id"], "owner/repo:SKILL.md")

    def test_fabricated_evidence_still_downgraded_even_with_drift_enabled(self) -> None:
        """安全底线：即使开启漂移容错，真正捏造的证据（如去除 NOT）绝不能放行。"""
        fake_evaluation = {
            "match": MATCH_STRONG,
            "documentation": "clear",
            "summary_zh": "测试",
            "criteria_results": [
                {
                    "criterion_id": "http_scraping",
                    "status": STATUS_SUPPORTED,
                    "explanation": "声称支持验证码登录",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 12,
                            "end_line": 12,
                            "quote": "Notice: This tool does support authenticated login behind CAPTCHA.",
                        }
                    ],
                },
                {
                    "criterion_id": "proxy_support",
                    "status": STATUS_SUPPORTED,
                    "explanation": "真实支持代理",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 10,
                            "end_line": 10,
                            "quote": "Features include proxy rotation and session persistence.",
                        }
                    ],
                },
            ],
        }
        adjusted = verify_and_adjust_evaluation(
            fake_evaluation,
            self.materials,
            self.criteria,
            allow_drift=True,
        )
        self.assertEqual(adjusted["match"], MATCH_PARTIAL)
        self.assertEqual(adjusted["criteria_results"][0]["status"], STATUS_UNKNOWN)
        item = {"candidate": {"skill_id": "test:SKILL.md"}, "evaluation": adjusted}
        shortlist, _ = rank_find_results([item], plan={"criteria": self.criteria})
        self.assertEqual(len(shortlist), 0)

    def test_report_and_projection_display_actual_location(self) -> None:
        """测试脱敏快照与报告渲染正确包含实际定位与漂移审计。"""
        ev_data = {
            "match": MATCH_STRONG,
            "documentation": "clear",
            "summary_zh": "爬虫",
            "criteria_results": [
                {
                    "criterion_id": "http_scraping",
                    "status": STATUS_SUPPORTED,
                    "explanation": "支持异步抓取",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 8,
                            "end_line": 9,
                            "original_start_line": 6,
                            "original_end_line": 8,
                            "match_method": "nearby_drift",
                            "quote": "Built on top of httpx",
                        }
                    ],
                }
            ],
        }
        raw_report = {
            "run_id": "test-run",
            "topic": "爬虫",
            "status": "completed",
            "shortlist": [
                {
                    "candidate": {"skill_id": "o/r:SKILL.md", "name": "r", "repo_url": "https://github.com/o/r", "author": "o", "path": "SKILL.md"},
                    "evaluation": ev_data,
                }
            ],
            "alternatives": [],
        }
        # 1. 公共快照脱敏
        proj = sanitize_report_for_public(raw_report)
        card_ev = proj["shortlist"][0]["evaluation"]["criteria_results"][0]["evidence"][0]
        self.assertEqual(card_ev["start_line"], 8)
        self.assertEqual(card_ev["end_line"], 9)
        self.assertEqual(card_ev["original_start_line"], 6)
        self.assertEqual(card_ev["match_method"], "nearby_drift")

        # 2. Markdown 报告渲染
        md = render_find_markdown_report(raw_report)
        self.assertIn("Built on top of httpx", md)


if __name__ == "__main__":
    unittest.main()
