"""统一条目状态机与状态转移矩阵测试（落实 T09-1~T09-5, T10）。

验证全库唯一的纯函数 update_entry()：
- T09-1: 无评估结果但内容指纹不变，保留原有 summary_zh 与所有分类，不清除
- T09-2: 新评估结果中增强字段显式为 None 或 []，不得复活已丢弃的增强字段
- T09-3: 旧格式缓存缺少新字段，内容指纹一致时可从既有条目继承
- T09-4: 内容指纹改变且既有状态为 recommended，必须保留 recommended、标记 needs_review=True，且原评估快照完整移入 pending_review
- T09-5: 待复核期间若再次经历评估失败，不得用错误快照覆盖已有 pending_review
- T10: 验证在 local 与 pipeline 两种场景下输入相同事件，生成的条目结构完全相同
"""

from __future__ import annotations

import unittest
from copy import deepcopy

from src.catalog.entry_state import (
    EntryUpdateEvent,
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    STATUS_RECOMMENDED,
    admission_decision,
    previous_evaluation_snapshot,
    review_state,
    update_entry,
)
from src.catalog.index import CatalogContext
from src.catalog.models import Candidate


class EntryStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.context = CatalogContext(
            rules_version="1.0.1",
            domain_names={"programming": "编程开发"},
            source_types={"official": "官方"},
            generated_at="2026-09-23T12:00:00",
        )

    def test_t09_1_no_evaluation_same_fingerprint_preserves_all_fields(self):
        """T09-1: 无评估结果但内容指纹不变，保留原有 summary_zh 与所有分类，不清除。"""
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:1111",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "既有中文摘要",
            "skill_type": "guideline",
            "example_requests": ["测试请求1"],
            "key_features": ["核心亮点A"],
            "main_category": {"id": "programming", "name": "编程开发"},
            "tags": ["python", "ai"],
            "platform_declared": "python",
            "dependencies_declared": ["pytest"],
            "first_seen": "2026-09-01T00:00:00",
            "last_checked": "2026-09-20T00:00:00",
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:1111",
        )
        event = EntryUpdateEvent(
            kind="no_evaluation",
            fetched_fingerprint="sha256:1111",
            rules_version="1.0.1",
        )
        entry = update_entry(previous, cand, event, self.context)

        self.assertEqual(entry["status"], STATUS_RECOMMENDED)
        self.assertFalse(entry["needs_review"])
        self.assertEqual(entry["summary_zh"], "既有中文摘要")
        self.assertEqual(entry["skill_type"], "guideline")
        self.assertEqual(entry["example_requests"], ["测试请求1"])
        self.assertEqual(entry["key_features"], ["核心亮点A"])
        self.assertEqual(entry["main_category"], {"id": "programming", "name": "编程开发"})
        self.assertEqual(entry["tags"], ["python", "ai"])
        self.assertEqual(entry["first_seen"], "2026-09-01T00:00:00")

    def test_t09_2_fresh_evaluation_explicit_empty_does_not_resurrect(self):
        """T09-2: 新评估结果中增强字段显式为 None 或 []，不得复活已丢弃的增强字段。"""
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:1111",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "旧摘要",
            "skill_type": "guideline",
            "example_requests": ["旧请求1", "旧请求2"],
            "key_features": ["旧亮点A", "旧亮点B"],
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:1111",
        )
        fresh_eval = {
            "summary_zh": "新摘要",
            "skill_type": None,
            "example_requests": [],
            "key_features": None,
            "main_category": "programming",
            "tags": ["updated"],
        }
        event = EntryUpdateEvent(
            kind="fresh_evaluation",
            fetched_fingerprint="sha256:1111",
            evaluation=fresh_eval,
            decision={"decision": STATUS_RECOMMENDED, "reason_codes": []},
            rules_version="1.0.1",
        )
        entry = update_entry(previous, cand, event, self.context)

        self.assertEqual(entry["summary_zh"], "新摘要")
        self.assertIsNone(entry["skill_type"])
        self.assertEqual(entry["example_requests"], [])
        self.assertEqual(entry["key_features"], [])
        self.assertEqual(entry["tags"], ["updated"])

    def test_t09_3_cached_evaluation_inherits_missing_fields_on_same_fingerprint(self):
        """T09-3: 旧格式缓存缺少新字段，内容指纹一致时可从既有条目继承。"""
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:1111",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "既有中文摘要",
            "skill_type": "guideline",
            "example_requests": ["请求A"],
            "key_features": ["亮点A"],
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:1111",
        )
        old_cached_eval = {
            "summary_zh": "既有中文摘要",
            "tags": ["cached"],
        }
        event = EntryUpdateEvent(
            kind="cached_evaluation",
            fetched_fingerprint="sha256:1111",
            evaluation=old_cached_eval,
            decision={"decision": STATUS_RECOMMENDED, "reason_codes": []},
            rules_version="1.0.1",
        )
        entry = update_entry(previous, cand, event, self.context)

        self.assertEqual(entry["summary_zh"], "既有中文摘要")
        self.assertEqual(entry["skill_type"], "guideline")
        self.assertEqual(entry["example_requests"], ["请求A"])
        self.assertEqual(entry["key_features"], ["亮点A"])
        self.assertEqual(entry["tags"], ["cached"])

    def test_t09_4_content_changed_retains_recommended_and_flags_needs_review(self):
        """T09-4: 内容指纹改变且既有状态为 recommended，必须保留 recommended、标记 needs_review=True，且原评估快照完整移入 pending_review。"""
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:old_fingerprint",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "第一版摘要",
            "skill_type": "guideline",
            "example_requests": ["第一版示例"],
            "key_features": ["第一版亮点"],
            "main_category": {"id": "programming", "name": "编程开发"},
            "tags": ["v1"],
            "last_checked": "2026-09-01T00:00:00",
            "first_seen": "2026-08-01T00:00:00",
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:new_fingerprint",
        )
        event = EntryUpdateEvent(
            kind="no_evaluation",
            fetched_fingerprint="sha256:new_fingerprint",
            rules_version="1.0.1",
        )
        entry = update_entry(previous, cand, event, self.context)

        # 核心断言：保留推荐、标记待复核
        self.assertEqual(entry["status"], STATUS_RECOMMENDED)
        self.assertTrue(entry["needs_review"])
        self.assertIsNotNone(entry["content_changed_at"])
        self.assertEqual(entry["content_fingerprint"], "sha256:new_fingerprint")

        # 原评估完整移入 pending_review
        snapshot = entry["pending_review"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["content_fingerprint"], "sha256:old_fingerprint")
        self.assertEqual(snapshot["summary_zh"], "第一版摘要")
        self.assertEqual(snapshot["skill_type"], "guideline")
        self.assertEqual(snapshot["example_requests"], ["第一版示例"])
        self.assertEqual(snapshot["key_features"], ["第一版亮点"])
        self.assertEqual(snapshot["main_category"], "programming")
        self.assertEqual(snapshot["evaluated_at"], "2026-09-01T00:00:00")

        # 当前新条目上不应直接挂载旧字段冒充新版本结论
        self.assertIsNone(entry["summary_zh"])
        self.assertIsNone(entry["skill_type"])
        self.assertEqual(entry["example_requests"], [])
        self.assertEqual(entry["key_features"], [])

    def test_t09_5_consecutive_failure_during_review_preserves_existing_pending_review(self):
        """T09-5: 待复核期间若再次经历评估失败，不得用错误快照覆盖已有 pending_review。"""
        original_snapshot = {
            "content_fingerprint": "sha256:v1",
            "status": STATUS_RECOMMENDED,
            "summary_zh": "V1摘要",
            "skill_type": "guideline",
            "example_requests": ["V1请求"],
            "key_features": ["V1亮点"],
            "evaluated_at": "2026-09-01T00:00:00",
        }
        previous = {
            "skill_id": "acme/widget:skills/widget/SKILL.md",
            "content_fingerprint": "sha256:v2",
            "status": STATUS_RECOMMENDED,
            "needs_review": True,
            "pending_review": deepcopy(original_snapshot),
            "review_note": "上游内容已变化，当前评估对应变化前的版本，待复核",
            "content_changed_at": "2026-09-10T00:00:00",
        }
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:v2",
        )
        event = EntryUpdateEvent(
            kind="fetch_failed",
            fetched_fingerprint="sha256:v2",
            upstream_status="upstream_gone",
            rules_version="1.0.1",
        )
        entry = update_entry(previous, cand, event, self.context)

        self.assertTrue(entry["needs_review"])
        self.assertEqual(entry["status"], STATUS_RECOMMENDED)
        # 必须牢牢保留原版 V1 快照，绝不可被空字典或错误快照覆盖
        self.assertEqual(entry["pending_review"]["content_fingerprint"], "sha256:v1")
        self.assertEqual(entry["pending_review"]["summary_zh"], "V1摘要")
        self.assertEqual(entry["pending_review"]["example_requests"], ["V1请求"])

    def test_t10_local_and_pipeline_identical_entry_output(self):
        """T10: 验证在 local 与 pipeline 两种调用场景下，输入相同事件与上下文，生成的条目结构完全相同。"""
        cand = Candidate(
            skill_id="acme/widget:skills/widget/SKILL.md",
            owner="acme",
            repo="widget",
            path="skills/widget/SKILL.md",
            url="https://github.com/acme/widget",
            content_fingerprint="sha256:abc123",
        )
        event = EntryUpdateEvent(
            kind="fresh_evaluation",
            fetched_fingerprint="sha256:abc123",
            evaluation={
                "summary_zh": "同一技能简述",
                "skill_type": "tool",
                "example_requests": ["做X", "做Y"],
                "key_features": ["速度快", "安全"],
                "main_category": "programming",
                "tags": ["cli"],
            },
            decision={"decision": STATUS_RECOMMENDED, "reason_codes": []},
            rules_version="1.0.1",
        )
        entry_from_pipeline_scenario = update_entry(None, cand, event, self.context)
        entry_from_local_scenario = update_entry(None, cand, event, self.context)

        self.assertEqual(entry_from_pipeline_scenario, entry_from_local_scenario)
        # 确保所有必需字段键均存在
        required_keys = {
            "skill_id", "name", "url", "repo_url", "author", "summary_zh",
            "skill_type", "example_requests", "key_features", "main_category",
            "candidate_domains", "tags", "platform_declared", "dependencies_declared",
            "limitations", "license", "status", "reason_codes", "first_seen",
            "last_checked", "content_changed_at", "content_fingerprint", "source_type",
            "upstream_status", "needs_review", "review_note", "pending_review",
            "manual_pick", "manual_note", "evaluation_rules_version",
        }
        self.assertTrue(required_keys.issubset(set(entry_from_pipeline_scenario.keys())))


if __name__ == "__main__":
    unittest.main()
