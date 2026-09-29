"""测试 Phase 6：本地实时投影与受控双并发安全底座。

涵盖验收场景：
1. 本地实时投影：每评估完成 1 个候选，立即向 report.json 与 report.md 投影最新 shortlist/alternatives 与消耗进度；
2. 预算预留机制：发起模型请求前预留 8,000 Token 与 1 次尝试配额，预算不足时安全熔断，防止瞬间击穿阈值；
3. 在途检查点映射：pending_evaluations 精准跟踪在途请求；
4. 受控双并发执行：concurrency=2 时由 ThreadPoolExecutor(max_workers=2) 受控并发执行，线程安全账本零竞态；
5. 目标达成即停：并发场景下一个线程命中目标后立即设置 stop_event，阻断后续候选浪费调用。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from src.finder.run import (
    FinderRunState,
    STATUS_COMPLETED,
    STATUS_TARGET_REACHED,
    STATUS_TOKEN_LIMIT,
    execute_find_skill,
)
from src.infra.llm import ModelCallResult
from src.shared.models import Candidate

SAMPLE_PLAN = {
    "intent": "查找心理疏导技能",
    "queries": ["comforting"],
    "criteria": [
        {"id": "comfort", "kind": "required", "description": "具备情绪安抚与心理疏导能力"},
    ],
}

STRONG_EVAL_CONTENT = json.dumps({
    "match": "strong",
    "documentation": "clear",
    "summary_zh": "优秀安抚技能",
    "why_consider": "核心安抚能力完备",
    "criteria_results": [
        {
            "criterion_id": "comfort",
            "status": "supported",
            "explanation": "明确具备情绪抚慰能力",
            "evidence": [
                {
                    "source_path": "SKILL.md",
                    "start_line": 2,
                    "end_line": 2,
                    "quote": "Empathetic emotional soothing and compassionate listening.",
                }
            ],
        }
    ],
    "limitations": [],
})

NONE_EVAL_CONTENT = json.dumps({
    "match": "none",
    "documentation": "clear",
    "summary_zh": "纯格式工具，不相关",
    "criteria_results": [
        {
            "criterion_id": "comfort",
            "status": "unsupported",
            "explanation": "不具备安抚能力",
            "evidence": [],
        }
    ],
    "limitations": [],
})

SAMPLE_MATERIAL = (
    "# Empathy Skill\n"
    "Empathetic emotional soothing and compassionate listening.\n"
    "Notice: Non-clinical tool.\n"
)


class TestFinderProgressiveAndConcurrency(unittest.TestCase):
    """测试阶段 6 本地实时投影与受控双并发。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake.invalid", "model": "fake", "auth": {"api_key": "fake"}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_progressive_realtime_projection_during_execution(self) -> None:
        """测试每完成 1 个候选评估，即刻在内存与磁盘投影最新的 shortlist 与 report.md。"""
        observed_projections = []

        def mock_call(cfg, system, user, *args, **kwargs):
            if "技能检索" in system or "queries" in system or "QueryPlan" in system:
                return ModelCallResult(ok=True, content=json.dumps(SAMPLE_PLAN), usage={"total_tokens": 100})

            # 在第二个候选评估时，检查磁盘上是否已经有第 1 个候选的实时 shortlist 投影
            run_dirs = list((self.root / "data" / "local" / "find-skills").glob("*"))
            if run_dirs:
                rep_json = run_dirs[0] / "report.json"
                rep_md = run_dirs[0] / "report.md"
                if rep_json.exists() and rep_md.exists():
                    data = json.loads(rep_json.read_text(encoding="utf-8"))
                    md_text = rep_md.read_text(encoding="utf-8")
                    observed_projections.append((data.get("shortlist_count", 0), len(data.get("evaluations", [])), md_text))

            if "cand1" in user:
                return ModelCallResult(ok=True, content=STRONG_EVAL_CONTENT, usage={"total_tokens": 150})
            return ModelCallResult(ok=True, content=NONE_EVAL_CONTENT, usage={"total_tokens": 150})

        cands = [
            Candidate(skill_id="test/c1:SKILL.md", owner="test", repo="c1", path="SKILL.md",
                      url="u1", repo_url="ru1", name="cand1", description="", discovered_at="2026-01-01T00:00:00Z"),
            Candidate(skill_id="test/c2:SKILL.md", owner="test", repo="c2", path="SKILL.md",
                      url="u2", repo_url="ru2", name="cand2", description="", discovered_at="2026-01-01T00:00:00Z"),
        ]

        report = execute_find_skill(
            "emotional comfort",
            root_dir=self.root,
            limit=2,
            max_evaluations=2,
            max_clarification_turns=0,
            call_model_fn=mock_call,
            search_github_repos_fn=lambda *a, **k: (True, [{"owner": "test", "repo": "c1"}], None),
            expand_and_collect_candidates_fn=lambda *a, **k: (cands, []),
            fetch_candidate_materials_fn=lambda *a, **k: (True, {"SKILL.md": SAMPLE_MATERIAL}, None),
            concurrency=1,
            log=lambda *a: None,
        )

        self.assertEqual(report["evaluated_count"], 2)
        # 证明在评估第 2 个候选期间，磁盘已经存在第 1 个候选的 shortlist 投影与 report.md
        self.assertTrue(any(proj[0] == 1 and proj[1] == 1 for proj in observed_projections),
                        f"未能观察到实时的中间短名单投影: {observed_projections}")

    def test_budget_reservation_prevents_threshold_overshoot(self) -> None:
        """测试预算预留机制：当剩余 Token 预算不足以承受安全缓冲 (8,000 Token) 时，预留失败并停止。"""
        state = FinderRunState("test", {"max_tokens": 10000, "max_evaluations": 5})
        state.usage.total_tokens = 5000

        # 1. 第一次预留 8,000 Token: 5000 + 0 + 8000 = 13000 > 10000 -> 预留被拦截
        ok = state.check_and_reserve_budget("cand1", tokens=8000)
        self.assertFalse(ok)
        self.assertEqual(state.reserved_tokens, 0)
        self.assertEqual(state.reserved_attempts, 0)

        # 2. 如果 max_tokens 足够 (如 20,000)
        state.report["parameters"]["max_tokens"] = 20000
        ok2 = state.check_and_reserve_budget("cand1", tokens=8000)
        self.assertTrue(ok2)
        self.assertEqual(state.reserved_tokens, 8000)
        self.assertEqual(state.reserved_attempts, 1)

        # 3. 再次预留 8,000: 5000 + 8000 + 8000 = 21000 > 20000 -> 再次拦截
        ok3 = state.check_and_reserve_budget("cand2", tokens=8000)
        self.assertFalse(ok3)

        # 4. 释放后又可正常预留
        state.release_reserved_budget(tokens=8000)
        self.assertEqual(state.reserved_tokens, 0)
        self.assertEqual(state.reserved_attempts, 0)

    def test_controlled_dual_concurrency_execution(self) -> None:
        """测试 concurrency=2 时的双并发调度，账本与用量无竞态且准确对账。"""
        call_times = []

        def mock_call(cfg, system, user, *args, **kwargs):
            if "技能检索" in system or "queries" in system or "QueryPlan" in system:
                return ModelCallResult(ok=True, content=json.dumps(SAMPLE_PLAN), usage={"total_tokens": 100})
            call_times.append(time.time())
            time.sleep(0.05)  # 模拟轻微网络耗时
            return ModelCallResult(ok=True, content=STRONG_EVAL_CONTENT, usage={"total_tokens": 200})

        cands = [
            Candidate(skill_id=f"test/c{i}:SKILL.md", owner="test", repo=f"c{i}", path="SKILL.md",
                      url=f"u{i}", repo_url=f"ru{i}", name=f"cand{i}", description="", discovered_at="2026-01-01T00:00:00Z")
            for i in range(4)
        ]

        report = execute_find_skill(
            "emotional comfort",
            root_dir=self.root,
            limit=4,
            max_evaluations=4,
            max_clarification_turns=0,
            call_model_fn=mock_call,
            search_github_repos_fn=lambda *a, **k: (True, [{"owner": "test", "repo": "c0"}], None),
            expand_and_collect_candidates_fn=lambda *a, **k: (cands, []),
            fetch_candidate_materials_fn=lambda *a, **k: (True, {"SKILL.md": SAMPLE_MATERIAL}, None),
            concurrency=2,  # 启用受控双并发
            log=lambda *a: None,
        )

        self.assertEqual(report["evaluated_count"], 4)
        self.assertEqual(len(report["shortlist"]), 4)
        self.assertEqual(report["parameters"]["concurrency"], 2)
        # 用量准确对账 (1 次规划 100 Token + 4 次评估各 200 Token = 900 Token)
        self.assertEqual(report["usage"]["total_tokens"], 900)
        self.assertEqual(report["usage"]["requests"], 5)

    def test_concurrent_target_reached_stops_early(self) -> None:
        """测试并发执行时，一旦达到 limit 目标，立即停止后续评估。"""
        evaluated_cands = []

        def mock_call(cfg, system, user, *args, **kwargs):
            if "技能检索" in system or "queries" in system or "QueryPlan" in system:
                return ModelCallResult(ok=True, content=json.dumps(SAMPLE_PLAN), usage={"total_tokens": 100})
            for cname in ("cand0", "cand1", "cand2", "cand3"):
                if cname in user:
                    evaluated_cands.append(cname)
            return ModelCallResult(ok=True, content=STRONG_EVAL_CONTENT, usage={"total_tokens": 200})

        cands = [
            Candidate(skill_id=f"test/c{i}:SKILL.md", owner="test", repo=f"c{i}", path="SKILL.md",
                      url=f"u{i}", repo_url=f"ru{i}", name=f"cand{i}", description="", discovered_at="2026-01-01T00:00:00Z")
            for i in range(4)
        ]

        report = execute_find_skill(
            "emotional comfort",
            root_dir=self.root,
            limit=1,  # 仅需 1 个即可停止
            max_evaluations=4,
            max_clarification_turns=0,
            call_model_fn=mock_call,
            search_github_repos_fn=lambda *a, **k: (True, [{"owner": "test", "repo": "c0"}], None),
            expand_and_collect_candidates_fn=lambda *a, **k: (cands, []),
            fetch_candidate_materials_fn=lambda *a, **k: (True, {"SKILL.md": SAMPLE_MATERIAL}, None),
            concurrency=2,
            log=lambda *a: None,
        )

        self.assertEqual(report["stop_reason"], STATUS_TARGET_REACHED)
        self.assertEqual(len(report["shortlist"]), 1)
        # 绝不把全部 4 个候选都浪费跑完
        self.assertLess(len(evaluated_cands), 4)


if __name__ == "__main__":
    unittest.main()
