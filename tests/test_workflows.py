"""workflow 约束：把 §7.2 的链路要求固化成断言，防止后续改动悄悄破坏。"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict):
    # YAML 1.1 会把裸 on 解析成布尔 True
    return doc.get("on", doc.get(True))


def step_runs(doc: dict, job: str) -> str:
    return "\n".join(s.get("run", "") for s in doc["jobs"][job]["steps"] if isinstance(s, dict))


class SyncWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load("sync-skills.yml")
        cls.trig = triggers(cls.doc)
        cls.runs = step_runs(cls.doc, "sync")

    def test_weekly_schedule_is_sunday_1000_beijing(self) -> None:
        crons = [s["cron"] for s in self.trig["schedule"]]
        self.assertEqual(crons, ["0 2 * * 0"], "定时必须是每周日 10:00 北京时间")
        self.assertNotIn("0 2 * * *", crons, "不得保留旧的每日表达式")

    def test_manual_trigger_with_dry_run(self) -> None:
        dispatch = self.trig["workflow_dispatch"]
        self.assertIn("dry_run", dispatch["inputs"])

    def test_concurrency_serialises_without_cancelling(self) -> None:
        conc = self.doc["concurrency"]
        self.assertEqual(conc["group"], "skills-sync")
        self.assertFalse(conc["cancel-in-progress"], "被中断会留下未对账的预留")

    def test_permissions_are_split(self) -> None:
        self.assertEqual(self.doc["permissions"], {"contents": "read"})
        self.assertEqual(self.doc["jobs"]["sync"]["permissions"], {"contents": "write"})

    def test_top_level_entry_calls_the_pipeline(self) -> None:
        self.assertIn("python -m src.pipeline", self.runs)

    def test_two_phases_with_commit_between_them(self) -> None:
        """§7.2 步骤 3：额度预留必须先 commit/push，之后才可付费调用。

        否则 runner 中断或 push 失败时，下次运行读不到已消耗额度，会重复计费。
        """
        steps = self.doc["jobs"]["sync"]["steps"]
        names = [s.get("name", "") for s in steps if isinstance(s, dict)]
        reserve = next(i for i, n in enumerate(names) if "阶段一" in n)
        commit_state = next(i for i, n in enumerate(names) if "推送额度预留" in n)
        call_model = next(i for i, n in enumerate(names) if "阶段二" in n)
        self.assertLess(reserve, commit_state, "预留必须在提交之前")
        self.assertLess(commit_state, call_model, "提交推送必须在调用模型之前")

        commit_run = steps[commit_state].get("run", "")
        self.assertIn("git push", commit_run)
        self.assertIn("exit 1", commit_run, "推送失败必须中止，不得继续付费调用")

    def test_model_key_only_reaches_the_evaluate_phase(self) -> None:
        steps = self.doc["jobs"]["sync"]["steps"]
        for step in steps:
            if not isinstance(step, dict):
                continue
            has_key = "LLM_API_KEY" in json.dumps(step.get("env", {}))
            if has_key:
                self.assertIn("阶段二", step.get("name", ""), "凭据只应注入评估阶段")

    def test_precheck_stops_before_paid_calls(self) -> None:
        """缺凭据时必须在调用模型之前中止。"""
        self.assertIn("缺少 LLM_API_KEY", self.runs)

    def test_commit_scope_is_explicit(self) -> None:
        """§7.2 步骤 5：禁止无差别 git add。"""
        self.assertNotIn("git add .", self.runs)
        self.assertNotIn("git add -A", self.runs)
        self.assertNotIn("git add --all", self.runs)
        self.assertRegex(self.runs, r"git add -- \"\$p\"")
        for expected in ("data/catalog.json", "public/data"):
            self.assertIn(expected, self.runs, f"提交范围应显式包含 {expected}")

    def test_verifies_index_and_page_are_same_source(self) -> None:
        self.assertIn("同源校验", self.runs)

    def test_uploads_public_artifact(self) -> None:
        names = [
            s.get("with", {}).get("name")
            for s in self.doc["jobs"]["sync"]["steps"]
            if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/upload-artifact")
        ]
        self.assertIn("pages-public", names)

    def test_deploy_is_a_reusable_call_not_a_second_entry(self) -> None:
        deploy = self.doc["jobs"]["deploy"]
        self.assertEqual(deploy["uses"], "./.github/workflows/deploy-pages.yml")
        self.assertEqual(deploy["needs"], "sync")


class DeployWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load("deploy-pages.yml")
        cls.trig = triggers(cls.doc)
        cls.runs = step_runs(cls.doc, "deploy")

    def test_only_callable(self) -> None:
        self.assertIn("workflow_call", self.trig)
        self.assertNotIn("push", self.trig, "不得自行监听 push，否则会有两个部署入口")
        self.assertNotIn("schedule", self.trig)

    def test_does_not_declare_its_own_concurrency(self) -> None:
        """顶层已持有固定组，重复申请会相互阻塞（§7.2）。"""
        self.assertNotIn("concurrency", self.doc)

    def test_permissions_cover_pages(self) -> None:
        perms = self.doc["permissions"]
        for key in ("pages", "id-token"):
            self.assertEqual(perms.get(key), "write")

    def test_downloads_artifact_instead_of_checking_out(self) -> None:
        self.assertIn("download-artifact", self.runs + str(self.doc))
        uses = [s.get("uses", "") for s in self.doc["jobs"]["deploy"]["steps"] if isinstance(s, dict)]
        self.assertFalse(
            any(u.startswith("actions/checkout") for u in uses),
            "不得重新检出仓库里的旧 public/",
        )
        self.assertFalse(
            any("sparse-checkout" in str(s) for s in self.doc["jobs"]["deploy"]["steps"]),
            "不得使用稀疏检出",
        )


class SyncConfigDeployWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load("sync-config-deploy.yml")
        cls.trig = triggers(cls.doc)
        cls.runs = step_runs(cls.doc, "sync-config")

    def test_triggers_on_config_push_and_dispatch(self) -> None:
        self.assertIn("push", self.trig)
        paths = self.trig["push"]["paths"]
        self.assertIn("config/overrides.json", paths)
        self.assertIn("config/snoozed.json", paths)
        self.assertIn("workflow_dispatch", self.trig)

    def test_executes_offline_sync_and_reuses_deploy_pages(self) -> None:
        self.assertIn("python tools/run_local.py --sync-config", self.runs)
        self.assertIn("deploy-pages.yml", str(self.doc["jobs"]["deploy"]))


if __name__ == "__main__":
    unittest.main()

