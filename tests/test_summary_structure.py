"""结构化描述、形态分类、示例请求与全路径缓存继承的单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.catalog.enrich import (
    enrich_catalog,
    enrich_entry,
    extract_example_requests,
    extract_key_features,
    infer_skill_type,
)
from src.catalog.index import build_entry
from src.catalog.models import Candidate
from src.pipeline import previous_evaluation_snapshot, review_state
from src.shared.schema import normalize_skill_type, normalize_string_list


class SchemaUtilsTest(unittest.TestCase):
    def test_normalize_skill_type(self):
        self.assertEqual(normalize_skill_type("tool_script"), "tool_script")
        self.assertEqual(normalize_skill_type("  GUIDELINE  "), "guideline")
        self.assertEqual(normalize_skill_type("template"), "template")
        self.assertEqual(normalize_skill_type("reference"), "reference")
        # 非法或未知枚举一律归为 None，不猜测
        self.assertIsNone(normalize_skill_type("unknown"))
        self.assertIsNone(normalize_skill_type("other"))
        self.assertIsNone(normalize_skill_type("工具脚本"))
        self.assertIsNone(normalize_skill_type(""))
        self.assertIsNone(normalize_skill_type(None))
        self.assertIsNone(normalize_skill_type(123))

    def test_normalize_string_list(self):
        # 正常列表、去重、过滤空串与非字符串、去除空白
        raw = ["  生成报告  ", "", "  ", None, 123, {"a": 1}, "生成报告", "信函撰写", "备忘录"]
        res = normalize_string_list(raw, max_items=2, max_length=10)
        self.assertEqual(res, ["生成报告", "信函撰写"])

        # 超过 max_length 截断
        long_str = "a" * 150
        res2 = normalize_string_list([long_str], max_items=2, max_length=50)
        self.assertEqual(len(res2[0]), 50)

        # 单个字符串作为输入自动封装为单元素列表
        self.assertEqual(normalize_string_list("单个请求"), ["单个请求"])
        # None 或非集合返回空列表
        self.assertEqual(normalize_string_list(None), [])
        self.assertEqual(normalize_string_list(123), [])


class GoldStandardFixturesTest(unittest.TestCase):
    def test_docx_facts_and_no_hallucination(self):
        # docx: 严禁编造“无需安装Office”、“精确排版”、“无损批注”等未证明能力
        # 形态分类因证据不足必须为 None，不可猜为 tool_script 或 reference
        raw_docx_summary = (
            "创建、编辑、读取和转换 Word 文档（.docx/.dotx），"
            "支持格式排版、批注、修订跟踪与内容提取，适用于生成报告、信函、备忘录等办公文档。"
        )
        stype = infer_skill_type(
            name="docx",
            summary_zh=raw_docx_summary,
            tags=["Word", "docx", "办公自动化"],
            dependencies=["docx (npm, preinstalled)", "pandoc", "LibreOffice (soffice)"],
        )
        self.assertIsNone(stype, "docx 没有明确形态证据，严禁猜测，必须为 None")

        requests = extract_example_requests(raw_docx_summary)
        self.assertEqual(requests, ["“生成报告、信函、备忘录等办公文档”"])
        for r in requests:
            self.assertNotIn("无需安装", r)
            self.assertNotIn("精确", r)
            self.assertNotIn("无损", r)

        features = extract_key_features(raw_docx_summary)
        self.assertIn("支持格式排版、批注、修订跟踪与内容提取", features)
        self.assertIn("创建、编辑、读取和转换 Word 文档", features)

    def test_writing_guidelines_is_guideline(self):
        raw_summary = "该技能依据外部写作指南审查文档/散文的规范符合性，适用于要求检查写作风格或进行文档审查的场景。"
        stype = infer_skill_type(
            name="writing-guidelines",
            summary_zh=raw_summary,
            tags=["写作规范", "文档审查"],
        )
        self.assertEqual(stype, "guideline")
        requests = extract_example_requests(raw_summary)
        self.assertEqual(requests, ["“检查写作风格或进行文档审查”"])

    def test_claude_api_is_reference(self):
        raw_summary = (
            "提供 Claude API / Anthropic SDK 的参考指南，覆盖模型选择、定价、参数、流式、"
            "工具调用、MCP、托管代理、缓存、token 计数和模型迁移等，典型用于构建和调试 Claude LLM 应用。"
        )
        stype = infer_skill_type(
            name="claude-api",
            summary_zh=raw_summary,
            tags=["claude-api", "anthropic-sdk", "api-reference"],
        )
        self.assertEqual(stype, "reference")
        requests = extract_example_requests(raw_summary)
        self.assertEqual(requests, ["“构建和调试 Claude LLM 应用”"])
        features = extract_key_features(raw_summary)
        self.assertTrue(any("覆盖模型选择" in f for f in features))

    def test_existing_template_entry_without_summary_returns_none(self):
        # 现有 template 条目 summary_zh 为 null，孤立名称绝对不能作为分类证据
        stype = infer_skill_type(name="template", summary_zh=None, tags=[])
        self.assertIsNone(stype, "无描述文本时严禁仅凭名称推测为 template，必须返回 None")

    def test_explicit_template_fixture_classifies_as_template(self):
        # 只有在有明确描述证据时才判定为 template
        summary = "提供用于快速搭建新服务的项目模板与开发脚手架，适用于初始化微服务仓库。"
        stype = infer_skill_type(name="service-template", summary_zh=summary)
        self.assertEqual(stype, "template")


class CacheInheritanceTest(unittest.TestCase):
    def test_same_fingerprint_inherits_missing_structured_fields(self):
        cand = Candidate(
            skill_id="test/sample:SKILL.md",
            owner="test",
            repo="sample",
            path="SKILL.md",
            url="https://github.com/test/sample",
            content_fingerprint="sha256:aaaa",
        )
        previous = {
            "skill_id": "test/sample:SKILL.md",
            "content_fingerprint": "sha256:aaaa",
            "summary_zh": "既有中文简述",
            "skill_type": "guideline",
            "example_requests": ["“测试请求1”"],
            "key_features": ["亮点A", "亮点B"],
            "status": "recommended",
            "main_category": {"id": "dev", "name": "编程开发"},
            "tags": ["test"],
            "last_checked": "2026-09-20",
        }

        # 旧评估缓存缺少新字段
        cached_evaluation_old = {
            "summary_zh": "既有中文简述",
            "tags": ["test"],
        }

        changed = False
        evaluation_a = dict(cached_evaluation_old)
        if (
            previous
            and not changed
            and previous.get("content_fingerprint")
            and cand.content_fingerprint
            and previous["content_fingerprint"] == cand.content_fingerprint
        ):
            if "skill_type" not in evaluation_a or evaluation_a.get("skill_type") is None:
                evaluation_a["skill_type"] = previous.get("skill_type")
            if not evaluation_a.get("example_requests") and previous.get("example_requests"):
                evaluation_a["example_requests"] = previous.get("example_requests")
            if not evaluation_a.get("key_features") and previous.get("key_features"):
                evaluation_a["key_features"] = previous.get("key_features")

        entry_a = build_entry(cand, evaluation=evaluation_a)
        self.assertEqual(entry_a["skill_type"], "guideline")
        self.assertEqual(entry_a["example_requests"], ["“测试请求1”"])
        self.assertEqual(entry_a["key_features"], ["亮点A", "亮点B"])

    def test_content_changed_does_not_inherit_fields(self):
        # 上游内容指纹改变（changed=True）
        cand_changed = Candidate(
            skill_id="test/sample:SKILL.md",
            owner="test",
            repo="sample",
            path="SKILL.md",
            url="https://github.com/test/sample",
            content_fingerprint="sha256:bbbb",
        )
        previous = {
            "skill_id": "test/sample:SKILL.md",
            "content_fingerprint": "sha256:aaaa",
            "summary_zh": "旧版本简述",
            "skill_type": "guideline",
            "example_requests": ["“旧版本请求”"],
            "key_features": ["旧亮点"],
            "status": "recommended",
            "main_category": {"id": "dev", "name": "编程开发"},
            "tags": ["test"],
            "last_checked": "2026-09-20",
        }
        changed = True
        state = review_state(previous, changed, None)
        self.assertTrue(state["needs_review"])
        self.assertIsNotNone(state["pending_review"])
        self.assertEqual(state["pending_review"]["skill_type"], "guideline")
        self.assertEqual(state["pending_review"]["example_requests"], ["“旧版本请求”"])

        cached_evaluation_old = {"summary_zh": "旧版本简述"}
        evaluation_b = dict(cached_evaluation_old)
        if (
            previous
            and not changed
            and previous.get("content_fingerprint")
            and cand_changed.content_fingerprint
            and previous["content_fingerprint"] == cand_changed.content_fingerprint
        ):
            evaluation_b["skill_type"] = previous.get("skill_type")

        entry_b = build_entry(
            cand_changed,
            evaluation=evaluation_b,
            needs_review=state["needs_review"],
            pending_review=state["pending_review"],
        )
        # 新条目不应继承旧版本结构化字段
        self.assertIsNone(entry_b["skill_type"])
        self.assertEqual(entry_b["example_requests"], [])
        self.assertEqual(entry_b["key_features"], [])
        # 旧版本字段完整保留在 pending_review 快照中
        self.assertEqual(entry_b["pending_review"]["skill_type"], "guideline")
        self.assertEqual(entry_b["pending_review"]["example_requests"], ["“旧版本请求”"])

    def test_content_changed_with_no_outcome_does_not_copy_old_fields(self):
        # 关键用例：内容指纹改变，且本轮调用无 outcome（评估失败/跳过）
        cand_changed = Candidate(
            skill_id="test/sample:SKILL.md",
            owner="test",
            repo="sample",
            path="SKILL.md",
            url="https://github.com/test/sample",
            content_fingerprint="sha256:cccc",
        )
        previous = {
            "skill_id": "test/sample:SKILL.md",
            "content_fingerprint": "sha256:aaaa",
            "summary_zh": "旧版本简述",
            "skill_type": "guideline",
            "example_requests": ["“旧版本请求”"],
            "key_features": ["旧亮点"],
            "status": "recommended",
            "main_category": {"id": "dev", "name": "编程开发"},
            "tags": ["test"],
            "last_checked": "2026-09-20",
        }
        changed = True
        state = review_state(previous, changed, None)
        entry = build_entry(
            cand_changed,
            evaluation=None,
            needs_review=state["needs_review"],
            pending_review=state["pending_review"],
        )
        outcome = None
        # 模拟 local_run.py:213 的统一指纹判断逻辑
        if (
            previous
            and not changed
            and previous.get("content_fingerprint")
            and cand_changed.content_fingerprint
            and previous["content_fingerprint"] == cand_changed.content_fingerprint
            and not outcome
        ):
            for key in ("summary_zh", "skill_type", "example_requests", "key_features"):
                entry[key] = previous.get(key)

        # 指纹改变且无 outcome 时，严禁复制旧字段进新条目
        self.assertIsNone(entry["summary_zh"])
        self.assertIsNone(entry["skill_type"])
        self.assertEqual(entry["example_requests"], [])
        self.assertEqual(entry["key_features"], [])
        # 旧字段留在 pending_review 快照中
        self.assertEqual(entry["pending_review"]["summary_zh"], "旧版本简述")
        self.assertEqual(entry["pending_review"]["skill_type"], "guideline")
        self.assertEqual(entry["pending_review"]["example_requests"], ["“旧版本请求”"])


class EnrichmentSafetyAndIdempotencyTest(unittest.TestCase):
    def test_summary_zh_never_mutated(self):
        orig_summary = "原始中文简述，绝不能被任何自动化流程修改或润色。"
        entry = {
            "skill_id": "test/id",
            "name": "writing-guidelines",
            "summary_zh": orig_summary,
            "tags": ["规范"],
        }
        enriched = enrich_entry(entry)
        self.assertEqual(enriched["summary_zh"], orig_summary, "summary_zh 必须保持完全原貌")
        # 描述中无明确依据，不猜
        self.assertIsNone(enriched["skill_type"])

        # 二次执行幂等
        enriched2 = enrich_entry(enriched)
        self.assertEqual(enriched2, enriched)
        self.assertEqual(enriched2["summary_zh"], orig_summary)

    def test_enrich_catalog_roundtrip_and_stability(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            catalog_file = data_dir / "catalog.json"
            catalog_content = {
                "catalog_version": "1.0.0",
                "rules_version": "1.0.1",
                "counts": {"recommended": 2},
                "entries": [
                    {
                        "skill_id": "test/docx:SKILL.md",
                        "name": "docx",
                        "url": "https://example.com",
                        "author": "test",
                        "summary_zh": "创建、编辑、读取和转换 Word 文档，支持格式排版，适用于生成报告等办公文档。",
                        "status": "recommended",
                        "main_category": {"id": "docs", "name": "文档办公"},
                        "tags": ["Word"],
                        "dependencies_declared": [],
                        "platform_declared": None,
                        "source_type": "official",
                        "first_seen": "2026-09-20",
                        "last_checked": "2026-09-21",
                        "content_changed_at": None,
                        "upstream_status": "ok",
                        "license": None,
                        "needs_review": False,
                        "review_note": None,
                        "pending_review": None,
                        "flags": [],
                        "reason_codes": [],
                        "limitations": None,
                        "manual_pick": True,
                        "manual_note": {"reason": "收藏"},
                        "snooze": None,
                    },
                    {
                        "skill_id": "test/template:SKILL.md",
                        "name": "template",
                        "url": "https://example.com/template",
                        "author": "test",
                        "summary_zh": None,
                        "status": "excluded",
                        "main_category": None,
                        "tags": [],
                        "dependencies_declared": [],
                        "platform_declared": None,
                        "source_type": "official",
                        "first_seen": "2026-09-20",
                        "last_checked": "2026-09-21",
                        "content_changed_at": None,
                        "upstream_status": "ok",
                        "license": None,
                        "needs_review": False,
                        "review_note": None,
                        "pending_review": None,
                        "flags": [],
                        "reason_codes": [],
                        "limitations": None,
                        "manual_pick": False,
                        "manual_note": None,
                        "snooze": None,
                    },
                ],
            }
            catalog_file.write_text(json.dumps(catalog_content, ensure_ascii=False), encoding="utf-8")

            stats1 = enrich_catalog(root)
            self.assertEqual(stats1["total"], 2)
            self.assertEqual(stats1["with_summary"], 1)

            saved1 = json.loads(catalog_file.read_text(encoding="utf-8"))
            e_docx = saved1["entries"][0]
            e_tmpl = saved1["entries"][1]

            # docx 验证
            self.assertEqual(e_docx["summary_zh"], "创建、编辑、读取和转换 Word 文档，支持格式排版，适用于生成报告等办公文档。")
            self.assertIsNone(e_docx["skill_type"])
            self.assertEqual(e_docx["example_requests"], ["“生成报告等办公文档”"])
            self.assertIn("支持格式排版", e_docx["key_features"])
            self.assertTrue(e_docx["manual_pick"])
            self.assertEqual(e_docx["manual_note"]["reason"], "收藏")

            # template 验证：summary_zh 为 None 时，形态必须为 None
            self.assertIsNone(e_tmpl["summary_zh"])
            self.assertIsNone(e_tmpl["skill_type"])
            self.assertEqual(e_tmpl["example_requests"], [])
            self.assertEqual(e_tmpl["key_features"], [])
            self.assertEqual(e_tmpl["status"], "excluded")

            # 连续执行第二次，产物完全一致（幂等性）
            stats2 = enrich_catalog(root)
            saved2 = json.loads(catalog_file.read_text(encoding="utf-8"))
            self.assertEqual(saved1, saved2)


if __name__ == "__main__":
    unittest.main()
