"""测试生产数据与测试环境的物理隔离防护契约。

验证规则：
1. 自动化测试环境中，调用 execute_find_skill() 绝不允许使用默认或指向真实工程的 root_dir，必须显式传入临时隔离目录；
2. 自动化测试环境中，调用 CLI main() 绝不允许默认写入真实工程目录；
3. 自动化测试环境中，调用 run_local() 绝不允许指向真实工程目录；
4. 真实工程中的 data/local/find-skills/ 目录绝不允许存在残留的空测试目录或未完成碎片；
5. 在显式提供临时目录时，execute_find_skill() 正常执行且产物严格隔离在临时目录内。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.catalog.local import run_local
from src.finder.run import execute_find_skill, main
from src.infra.llm import ModelCallResult
from src.shared.runtime import is_test_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1].resolve()


class TestDataIsolationGuard(unittest.TestCase):
    """验证测试环境与生产/用户运行数据的物理隔离。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.isolated_root = Path(self.temp_dir.name)
        (self.isolated_root / "config").mkdir(parents=True)
        (self.isolated_root / "config" / "model.local.json").write_text(
            json.dumps({"endpoint": "https://fake", "model": "fake-model", "auth": {"api_key": "fake-key"}}),
            encoding="utf-8",
        )
        (self.isolated_root / "config" / "find-skill.json").write_text(
            json.dumps({"limit": 1, "max_evaluations": 1, "max_tokens": 10000}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_environment_detector_identifies_unittest_runner(self) -> None:
        """测试环境检测函数能准确识别 unittest 正在运行。"""
        self.assertTrue(is_test_environment())

    def test_finder_rejects_default_root_dir_under_test(self) -> None:
        """测试环境下省略 root_dir（默认 "."）必须立即熔断报错，禁止写入工程目录。"""
        with self.assertRaises(RuntimeError) as ctx:
            execute_find_skill("测试需求")
        self.assertIn("测试环境中禁止直接写入工程生产 data/local 目录", str(ctx.exception))

    def test_finder_rejects_explicit_project_root_under_test(self) -> None:
        """测试环境下显式传入工程根目录必须立即熔断报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            execute_find_skill("测试需求", root_dir=PROJECT_ROOT)
        self.assertIn("测试环境中禁止直接写入工程生产 data/local 目录", str(ctx.exception))

    def test_cli_main_rejects_unisolated_root_under_test(self) -> None:
        """测试环境下 CLI main() 未显式提供隔离 root 时必须熔断报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            main(["测试需求"])
        self.assertIn("测试环境中禁止直接写入工程生产 data/local 目录", str(ctx.exception))

    def test_run_local_rejects_project_root_under_test(self) -> None:
        """测试环境下 run_local() 传入工程根目录必须立即熔断报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            run_local(PROJECT_ROOT, {})
        self.assertIn("测试环境中禁止直接写入工程生产 data/local 目录", str(ctx.exception))

    def test_finder_succeeds_with_isolated_temp_directory(self) -> None:
        """显式传入临时隔离目录时，查找正常执行，且产物完全限制在临时目录内。"""
        mock_call = MagicMock()
        mock_call.return_value = ModelCallResult(
            ok=True,
            content=json.dumps({
                "intent": "隔离测试",
                "queries": ["test"],
                "criteria": [{"id": "c1", "kind": "required", "description": "d1"}],
            }),
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

        with patch("src.finder.run.call_model", mock_call), patch(
            "src.finder.run.search_github_repos_for_query", return_value=(True, [], None)
        ), patch("src.finder.run.expand_and_collect_candidates", return_value=([], [])):
            report = execute_find_skill("隔离测试需求", root_dir=self.isolated_root)

        self.assertEqual(report["status"], "completed")
        # 验证产物在临时目录中生成
        isolated_find_dir = self.isolated_root / "data" / "local" / "find-skills"
        self.assertTrue(isolated_find_dir.exists())
        runs = list(isolated_find_dir.iterdir())
        self.assertEqual(len(runs), 1)
        self.assertTrue((runs[0] / "report.json").exists())

        # 验证工程真实目录绝对未受到任何影响
        real_project_find_dir = PROJECT_ROOT / "data" / "local" / "find-skills"
        if real_project_find_dir.exists():
            for p in real_project_find_dir.iterdir():
                self.assertNotEqual(p.name, runs[0].name)

    def test_project_data_local_find_skills_has_zero_polluted_empty_dirs(self) -> None:
        """真实工程的 data/local/find-skills 目录中每一个运行目录都必须包含有效 report.json，无空测试残留。"""
        real_find_dir = PROJECT_ROOT / "data" / "local" / "find-skills"
        if not real_find_dir.exists():
            return
        for run_path in real_find_dir.iterdir():
            if not run_path.is_dir():
                continue
            report_file = run_path / "report.json"
            self.assertTrue(
                report_file.exists(),
                f"发现未清理的测试或空运行目录残余：{run_path.name}，工程真实目录只允许保留包含 report.json 的真实运行记录！",
            )
            # 报告文件必须是合法的 JSON
            data = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertIn("topic", data)
            self.assertIn("status", data)


if __name__ == "__main__":
    unittest.main()
