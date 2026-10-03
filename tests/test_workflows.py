"""workflow、文档与站点发布约束测试。

合并自原有分散的 test_workflows.py、test_public.py 与 test_docs.py。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET
import yaml

from tests import smoke

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLIC = ROOT / "public"
SITE_ROOT = "https://justdoit712.github.io/skill/"
PAGES = ["index.html", "styles.css", "robots.txt", "sitemap.xml"]
DOC_FILES = ["README.md", "LICENSE-CONTENT.md"]
DOC_FILES += [str(p.relative_to(ROOT)).replace("\\", "/") for p in sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+?)\)")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict):
    return doc.get("on", doc.get(True))


def step_runs(doc: dict, job: str) -> str:
    return "\n".join(s.get("run", "") for s in doc["jobs"][job]["steps"] if isinstance(s, dict))


@smoke
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

    def test_automation_switch_is_config_driven(self) -> None:
        gate = self.doc["jobs"]["gate"]
        runs = "\n".join(s.get("run", "") for s in gate["steps"] if isinstance(s, dict))
        self.assertIn("automation.json", runs, "gate 必须读取 automation.json")
        self.assertIn("scheduled_sync_enabled", runs, "gate 必须读取 scheduled_sync_enabled")
        self.assertIn("run_sync", gate["outputs"])
        self.assertEqual(self.doc["jobs"]["sync"]["needs"], "gate")
        self.assertEqual(self.doc["jobs"]["sync"]["if"], "needs.gate.outputs.run_sync == 'true'")
        self.assertIn("workflow_dispatch", runs, "手动触发必须绕过开关")

    def test_job_outputs_reference_existing_step_ids(self) -> None:
        for job_name, job in self.doc["jobs"].items():
            ids = {s.get("id") for s in job.get("steps", []) if isinstance(s, dict)}
            for key, value in (job.get("outputs") or {}).items():
                for ref in re.findall(r"steps\.([A-Za-z0-9_-]+)\.outputs", str(value)):
                    self.assertIn(ref, ids, f"{job_name}.outputs.{key} 引用了不存在的 step id: {ref}")

    def test_manual_trigger_with_dry_run(self) -> None:
        dispatch = self.trig["workflow_dispatch"]
        self.assertIn("dry_run", dispatch["inputs"])

    def test_concurrency_serialises_without_cancelling(self) -> None:
        conc = self.doc["concurrency"]
        self.assertEqual(conc["group"], "pages-deploy")
        self.assertFalse(conc["cancel-in-progress"], "被中断会留下未对账的预留")

    def test_permissions_are_split(self) -> None:
        self.assertEqual(self.doc["permissions"], {"contents": "read"})
        self.assertEqual(self.doc["jobs"]["sync"]["permissions"], {"contents": "write"})

    def test_top_level_entry_calls_the_pipeline(self) -> None:
        self.assertIn("python -m src.pipeline", self.runs)

    def test_two_phases_with_commit_between_them(self) -> None:
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
        self.assertIn("缺少 LLM_API_KEY", self.runs)

    def test_commit_scope_is_explicit(self) -> None:
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
        self.assertNotIn("concurrency", self.doc)

    def test_permissions_cover_pages(self) -> None:
        perms = self.doc["permissions"]
        for key in ("pages", "id-token"):
            self.assertEqual(perms.get(key), "write")

    def test_downloads_artifact_instead_of_checking_out(self) -> None:
        self.assertIn("download-artifact", self.runs + str(self.doc))
        uses = [s.get("uses", "") for s in self.doc["jobs"]["deploy"]["steps"] if isinstance(s, dict)]
        self.assertFalse(any(u.startswith("actions/checkout") for u in uses))
        self.assertFalse(any("sparse-checkout" in str(s) for s in self.doc["jobs"]["deploy"]["steps"]))


class PublishWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load("publish-pages.yml")
        cls.trig = triggers(cls.doc)
        cls.raw = (WORKFLOWS / "publish-pages.yml").read_text(encoding="utf-8")

    def test_manual_only(self) -> None:
        self.assertIn("workflow_dispatch", self.trig)
        self.assertNotIn("schedule", self.trig)
        self.assertNotIn("push", self.trig)

    def test_reuses_the_single_deploy_workflow(self) -> None:
        deploy = self.doc["jobs"]["deploy"]
        self.assertEqual(deploy["uses"], "./.github/workflows/deploy-pages.yml")
        self.assertEqual(deploy["needs"], "stage")

    def test_uploads_the_committed_artifact_without_rebuilding(self) -> None:
        self.assertIn("actions/upload-artifact", self.raw)
        self.assertNotIn("pip install", self.raw)
        self.assertNotRegex(self.raw, r"run:\s*[^\n]*python")
        self.assertIn("public/data/catalog.json", self.raw)

    def test_concurrency_shares_deploy_group(self) -> None:
        conc = self.doc.get("concurrency")
        self.assertIsNotNone(conc)
        self.assertEqual(conc["group"], "pages-deploy")
        self.assertFalse(conc["cancel-in-progress"])


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
        self.assertIn("config/owned-skills.json", paths)
        self.assertIn("workflow_dispatch", self.trig)

    def test_executes_offline_sync_and_reuses_deploy_pages(self) -> None:
        self.assertIn("python tools/run_local.py --sync-config", self.runs)
        self.assertIn("deploy-pages.yml", str(self.doc["jobs"]["deploy"]))

    def test_concurrency_shares_deploy_group(self) -> None:
        conc = self.doc.get("concurrency")
        self.assertIsNotNone(conc)
        self.assertEqual(conc["group"], "pages-deploy")
        self.assertFalse(conc["cancel-in-progress"])


@smoke
class DocLinkTest(unittest.TestCase):
    """合并自 test_docs.py：文档链接与文件存在性检查。"""
    def test_all_local_links_resolve(self) -> None:
        broken: list[str] = []
        checked = 0
        for rel in DOC_FILES:
            path = ROOT / rel
            if not path.exists():
                broken.append(f"{rel}（文档本身不存在）")
                continue
            base = path.parent
            for target in LINK.findall(path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                checked += 1
                clean_target = re.sub(r":\d+$", "", target.split("#")[0])
                if not (base / clean_target).exists():
                    broken.append(f"{rel} -> {target}")
        self.assertGreater(checked, 0, "没有检查到任何本地链接")
        self.assertEqual(broken, [], "存在失效的本地链接：\n" + "\n".join(broken))

    def test_docs_dir_has_no_unexpected_files(self) -> None:
        names = sorted(p.name for p in (ROOT / "docs").glob("*.md"))
        self.assertEqual(
            names,
            ["产品规范.md", "架构约定.md", "运行说明.md"],
            "docs/ 的文件集合发生变化；若是刻意调整，请同步更新本断言",
        )


@smoke
class NoUpstreamTracesTest(unittest.TestCase):
    """合并自 test_public.py：站点身份与内容范围。"""
    FORBIDDEN = {
        "旧域名": "miyucaicai",
        "上游署名": "anbeime",
        "站群标识": "TOPGO",
        "站群用词": "站群",
        "旧计数 182": "182",
        "旧计数 245": "245",
        "旧计数 2326": "2326",
        "旧英文页": "skills-en",
        "本地技能页": "local-skills",
        "聊天演示页": "chat-demo",
        "项目案例页": "projects.html",
    }

    def test_no_forbidden_traces(self) -> None:
        for name in PAGES:
            blob = (PUBLIC / name).read_text(encoding="utf-8")
            for label, needle in self.FORBIDDEN.items():
                with self.subTest(page=name, label=label):
                    self.assertNotIn(needle, blob, f"{name} 出现{label}")

    def test_no_orphan_promo_selectors_in_css(self) -> None:
        css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
        for selector in (".eco-grid", ".eco-link", ".contact-badge", ".qr-box"):
            self.assertNotIn(selector, css, f"styles.css 残留孤立样式 {selector}")

    def test_skills_page_is_merged_and_removed(self) -> None:
        self.assertFalse((PUBLIC / "skills.html").exists(), "skills.html 应已合并删除")


class IndexPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        js_files = sorted((PUBLIC / "js").glob("*.js"))
        js_content = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        cls.raw_html = raw_html
        cls.html = raw_html + "\n" + js_content

    def test_canonical_points_at_user_pages_subpath(self) -> None:
        self.assertIn(f'<link rel="canonical" href="{SITE_ROOT}">', self.html)

    def test_loads_index_generated_page_data(self) -> None:
        self.assertIn('fetch("data/catalog.json"', self.html)

    def test_has_search_and_filters(self) -> None:
        for element_id in ('id="q"', 'id="category"', 'id="source"', 'id="tab-candidate"'):
            self.assertIn(element_id, self.html)

    def test_no_custom_domain_cname(self) -> None:
        self.assertFalse((PUBLIC / "CNAME").exists())

    def test_relative_links_do_not_escape_public_dir(self) -> None:
        offenders = [
            m.group(1)
            for m in re.finditer(r'(?:href|src)="([^"]+)"', self.html)
            if m.group(1).startswith("../")
        ]
        self.assertEqual(offenders, [], f"存在跳出 public/ 的链接：{offenders}")


class RobotsAndSitemapTest(unittest.TestCase):
    def test_robots_uses_new_site_and_sitemap_url(self) -> None:
        robots = (PUBLIC / "robots.txt").read_text(encoding="utf-8")
        self.assertIn(f"Sitemap: {SITE_ROOT}sitemap.xml", robots)
        self.assertIn("justdoit712.github.io", robots)
        self.assertIn("User-agent: *", robots)

    def test_sitemap_is_valid_xml_and_only_lists_new_site(self) -> None:
        tree = ET.parse(PUBLIC / "sitemap.xml")
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [e.text.strip() for e in tree.getroot().findall(".//sm:loc", ns)]
        self.assertTrue(locs, "sitemap 至少要有一个 URL")
        for loc in locs:
            self.assertTrue(loc.startswith(SITE_ROOT), f"{loc} 不在发布地址下")

    def test_sitemap_has_no_deleted_pages(self) -> None:
        blob = (PUBLIC / "sitemap.xml").read_text(encoding="utf-8")
        for gone in ("skills.html", "skills-en.html", "local-skills.html", "projects.html", "llms.txt"):
            self.assertNotIn(gone, blob, f"sitemap 仍列出已删除页面 {gone}")


if __name__ == "__main__":
    unittest.main()
