"""主目录流水线与已收录（Owned）功能集成回归测试（P2）。

覆盖场景：
1. 本地采集（prepare_pool / process_candidate）：
   - 计算可处理候选水位时排除已收录项；
   - 抓取与评估替身设为“调用即失败”，验证已收录候选 0 抓取、0 模型调用；
   - 候选池中已收录项保持 STATUS_PENDING（不写成排除或已完成）。
2. Actions 阶段一（sync_reserve / phase_reserve）：
   - 候选与待办中已收录项不占抓取名额，不占额度预留；
   - 待办队列保留已收录项为休眠待办（不丢失）；
   - 报告区分待办总数、可调度数量和已收录跳过数。
3. Actions 阶段二（sync_evaluate / phase_evaluate）：
   - 阶段一预留后新增已收录标记，阶段二跳过模型调用；
   - 保留原主目录条目事实，不用“本轮未评估”覆盖原评估快照。
4. 离线同步与公共投影（sync_config_offline / build_page_data）：
   - 主索引保存 owned 快照；
   - 公开页面默认数组与分类筛选排除已收录项；
   - counts 包含 owned 与 owned_in_catalog；
   - owned_entries 保留原始展示事实与原分区信息；
   - 绝不泄露私人字段。
5. 取消标记恢复：
   - 取消标记后，休眠待办在下一轮重新成为可调度候选。
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.catalog.dedupe import candidate_from_repo
from src.catalog.index import (
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    STATUS_RECOMMENDED,
    build_catalog,
    build_entry,
    build_page_data,
)
from src.catalog.local import prepare_pool, process_candidate, LocalCollection
from src.catalog.maintenance import sync_config_offline, sync_config_to_catalog, recover_completed_results
from src.catalog.pool import (
    STATUS_DONE,
    STATUS_PENDING as POOL_STATUS_PENDING,
    create_pool_from_candidates,
    load_pool,
    save_pool,
)
from src.catalog.sync_reserve import phase_reserve
from src.catalog.sync_evaluate import phase_evaluate
from src.infra.files import read_json, write_json_atomic
from src.pipeline import phase_reserve as pipeline_reserve, phase_evaluate as pipeline_evaluate
from src.shared.owned import normalize_owned_id
from tests.test_pipeline import (
    PipelineHarness,
    fake_discover,
    fake_evaluate,
    fake_fetch,
    passing_evaluation,
)


class OwnedCatalogPipelineIntegrationTest(PipelineHarness):
    """测试主目录流水线与已收录（Owned）名单集成的端到端行为。"""

    def _write_owned_config(self, cfg_dir: Path, items: list[dict]) -> None:
        data = {
            "schema_version": "1.0.0",
            "items": items,
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        for p in (cfg_dir / "governance" / "owned-skills.json", cfg_dir / "owned-skills.json"):
            if p.parent.exists():
                p.write_text(content, encoding="utf-8")

    def test_local_pool_watermark_and_skip_owned(self):
        """本地采集：水位线计算排除已收录项，已收录项 0 抓取 0 评估且保留 PENDING 状态。"""
        cfg_dir = self.temp_config()
        self._write_owned_config(
            cfg_dir,
            [
                {
                    "skill_id": "acme/owned:SKILL.md",
                    "name": "owned-skill",
                    "added_at": "2026-09-24",
                }
            ],
        )

        cand_owned = candidate_from_repo("acme", "owned", path="SKILL.md", url="https://github.com/acme/owned/blob/main/SKILL.md")
        cand_normal = candidate_from_repo("acme", "normal", path="SKILL.md", url="https://github.com/acme/normal/blob/main/SKILL.md")

        local_dir = self.data / "local"
        local_dir.mkdir(parents=True, exist_ok=True)
        pool = create_pool_from_candidates([cand_owned, cand_normal], set(), {})
        save_pool(local_dir / "pool.json", pool)

        # 水位线：pool 中 2 个 PENDING，但 acme/owned:SKILL.md 已收录，因此可处理只有 1
        cfg = {
            "snoozed": {},
            "overrides": {},
            "owned": read_json(cfg_dir / "owned-skills.json"),
            "searches": {"per_domain": {}},
            "sources": {"sources": []},
            "source_types": {},
        }
        # 调用 prepare_pool，设置水位线 2，当可处理 (1) < 2 时触发增量补水
        discovered_new = candidate_from_repo("acme", "fresh", path="SKILL.md", url="https://github.com/acme/fresh/blob/main/SKILL.md")
        supplemented_pool = prepare_pool(
            self.root,
            local_dir,
            cfg,
            {"pool_watermark": 2, "pool_max_age_days": 7, "refresh_pool": False},
            set(),
            discover_fn=fake_discover([discovered_new]),
            log=lambda _: None,
        )
        # 补水成功，新候选加入
        self.assertIn("acme/fresh:SKILL.md", {it.candidate.skill_id for it in supplemented_pool.items})

        # 验证 process_candidate：若调用 fetch 或 evaluate 则直接失败
        def strict_fetch(url, **kwargs):
            if "acme/owned" in url:
                raise AssertionError(f"绝不应该抓取已收录候选正文：{url}")
            return fake_fetch()(url, **kwargs)

        def strict_evaluate(candidate, text, **kwargs):
            if candidate.skill_id == "acme/owned:SKILL.md":
                raise AssertionError(f"绝不应该调用模型评估已收录条目：{candidate.skill_id}")
            return fake_evaluate()(candidate, text, **kwargs)

        # 模拟运行收集状态
        state = LocalCollection(
            root=self.root,
            local=local_dir,
            settings={"target_recommended": 1, "max_total_tokens": 100000, "max_retries": 1},
            cfg={"model": {"model": "test"}, "rules": {"rules_version": "1.0.0"}, "prescreen": None, "taxonomy": None, "source_types": {}, "owned": cfg["owned"]},
            discover_fn=fake_discover([]),
            fetch_fn=strict_fetch,
            evaluate_fn=strict_evaluate,
            log=lambda _: None,
            sleep=lambda _: None,
            run_id="test-run",
            run_dir=local_dir / "runs" / "test",
            usage=None,
            report={"new_recommended": 0, "budget_tokens": 0, "evaluations": 0, "checked": 0, "skipped_owned": 0},
            ledger=None,
            context=None,
            entries={},
            old_recommended=set(),
            baseline=None,
            active_snoozed=set(),
            manual_exclusions=set(),
            manual_picks={},
            pool_path=local_dir / "pool.json",
            pool=supplemented_pool,
            dirty=False,
            consecutive_failures=0,
            active_eid=None,
            active_call=None,
            unknown_reserve=0,
            max_attempts=1,
            max_retries=0,
            pending_items=supplemented_pool.items,
            owned_ids={"acme/owned:SKILL.md"},
            skipped_owned_ids=set(),
        )

        owned_item = next(it for it in supplemented_pool.items if it.candidate.skill_id == "acme/owned:SKILL.md")
        proceed = process_candidate(state, owned_item)
        self.assertTrue(proceed)
        self.assertEqual(state.report["skipped_owned"], 1)
        # 候选池状态未被破坏为 EXCLUDED 或 DONE，仍然保留 POOL_STATUS_PENDING
        self.assertEqual(owned_item.status, POOL_STATUS_PENDING)

    def test_actions_phase_reserve_skips_owned_and_preserves_dormant_queue(self):
        """Actions 阶段一：已收录条目 0 抓取、0 预留额度，且休眠保留在待办队列中。"""
        cfg_dir = self.temp_config()
        self._write_owned_config(
            cfg_dir,
            [
                {
                    "skill_id": "acme/owned:SKILL.md",
                    "name": "owned-skill",
                    "added_at": "2026-09-24",
                }
            ],
        )

        cand_owned = candidate_from_repo("acme", "owned", path="SKILL.md", url="https://github.com/acme/owned/blob/main/SKILL.md")
        cand_normal = candidate_from_repo("acme", "normal", path="SKILL.md", url="https://github.com/acme/normal/blob/main/SKILL.md")

        def strict_fetch(url, **kwargs):
            if "acme/owned" in url:
                raise AssertionError(f"阶段一禁止抓取已收录条目：{url}")
            return fake_fetch()(url, **kwargs)

        res_reserve = phase_reserve(
            config_dir=cfg_dir,
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover([cand_owned, cand_normal]),
            fetch_fn=strict_fetch,
        )
        self.assertTrue(res_reserve["ok"])
        # 仅预留 normal 1 个，owned 不占预算名额
        self.assertEqual(res_reserve["reserved"], 1)
        self.assertEqual(res_reserve["schedulable_count"], 1)
        self.assertEqual(res_reserve["skipped_owned"], 1)

        # 检查持久化待办队列 queue.json：必须同时保留 normal 与 owned（休眠状态）
        queue = read_json(self.state / "queue.json")
        pending_ids = {it["candidate"]["skill_id"] for it in queue["pending"]}
        self.assertIn("acme/normal:SKILL.md", pending_ids)
        self.assertIn("acme/owned:SKILL.md", pending_ids)
        owned_queue_item = next(it for it in queue["pending"] if it["candidate"]["skill_id"] == "acme/owned:SKILL.md")
        self.assertEqual(owned_queue_item["fetch"]["skipped"], "已收录，暂不调度")

    def test_actions_phase_evaluate_skips_newly_owned_and_preserves_catalog_fact(self):
        """阶段二：阶段一预留后才标记已收录，阶段二跳过模型评估且保留旧条目评估事实。"""
        cfg_dir = self.temp_config()
        # 初始未标记已收录
        self._write_owned_config(cfg_dir, [])

        cand_a = candidate_from_repo("acme", "item-a", path="SKILL.md", url="https://github.com/acme/item-a/blob/main/SKILL.md")
        cand_b = candidate_from_repo("acme", "item-b", path="SKILL.md", url="https://github.com/acme/item-b/blob/main/SKILL.md")

        # 预设历史条目快照到 catalog.json
        old_entry = self.previous_entry("acme/item-a:SKILL.md")
        old_entry["summary_zh"] = "原始高质量历史中文摘要"
        old_entry["status"] = STATUS_RECOMMENDED
        old_entry["main_category"] = {"id": "finance", "name": "金融"}
        self.write_previous_catalog([old_entry])

        # 阶段一：两个候选均被调度预留
        res_reserve = phase_reserve(
            config_dir=cfg_dir,
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover([cand_a, cand_b]),
            fetch_fn=fake_fetch(),
        )
        self.assertTrue(res_reserve["ok"])
        self.assertEqual(res_reserve["reserved"], 2)

        # 阶段一结束，用户在此期间将 item-a 标记为已收录
        self._write_owned_config(
            cfg_dir,
            [
                {
                    "skill_id": "acme/item-a:SKILL.md",
                    "name": "item-a",
                    "added_at": "2026-09-24",
                }
            ],
        )

        # 阶段二：评估
        eval_spy = fake_evaluate()
        res_eval = phase_evaluate(
            config_dir=cfg_dir,
            data_dir=self.data,
            public_dir=self.public,
            state_dir=self.state,
            evaluate_fn=eval_spy,
            fetch_fn=fake_fetch(),
        )
        self.assertTrue(res_eval["ok"])
        # 模型评估仅调用了 item-b，item-a 被安全跳过（0 次模型调用）
        self.assertEqual(eval_spy.calls, ["acme/item-b:SKILL.md"])
        self.assertEqual(res_eval["evaluated"], 1)
        self.assertEqual(res_eval["skipped"], 1)

        # 验证 catalog 数据：item-a 的原评估字段完整保留，未被覆盖为无评估空条目
        catalog = self.read_catalog()
        entries_by_id = {e["skill_id"]: e for e in catalog["entries"]}
        item_a = entries_by_id["acme/item-a:SKILL.md"]
        self.assertEqual(item_a["summary_zh"], "原始高质量历史中文摘要")
        self.assertEqual(item_a["status"], STATUS_RECOMMENDED)

    def test_offline_sync_and_public_projections(self):
        """离线配置同步与公共投影：已收录从浏览数组排除、counts 正确、owned_entries 携带原分区。"""
        cfg_dir = self.temp_config()
        overrides_content = json.dumps({
            "manual_picks": [{"skill_id": "acme/manual:SKILL.md", "reason": "测试收藏", "added_at": "2026-09-24"}],
            "manual_exclusions": [],
        }, ensure_ascii=False)
        for p in (cfg_dir / "governance" / "overrides.json", cfg_dir / "overrides.json"):
            if p.parent.exists():
                p.write_text(overrides_content, encoding="utf-8")
        snooze_content = json.dumps({"snooze_version": "1.0.0", "snoozed": []}, ensure_ascii=False)
        for p in (cfg_dir / "governance" / "snoozed.json", cfg_dir / "snoozed.json"):
            if p.parent.exists():
                p.write_text(snooze_content, encoding="utf-8")
        self._write_owned_config(
            cfg_dir,
            [
                {
                    "skill_id": "acme/rec:SKILL.md",
                    "name": "rec-owned",
                    "added_at": "2026-09-24",
                },
                {
                    # 仅在定向查找发现、不在主索引中的条目
                    "skill_id": "other/finder-only:SKILL.md",
                    "name": "finder-only",
                    "added_at": "2026-09-24",
                }
            ],
        )

        e_rec = self.previous_entry("acme/rec:SKILL.md")
        e_rec["status"] = STATUS_RECOMMENDED
        e_rec["main_category"] = {"id": "finance", "name": "金融"}

        e_cand = self.previous_entry("acme/cand:SKILL.md")
        e_cand["status"] = STATUS_CANDIDATE
        e_cand["main_category"] = {"id": "crypto", "name": "加密货币"}

        e_manual = self.previous_entry("acme/manual:SKILL.md")
        e_manual["status"] = STATUS_RECOMMENDED
        e_manual["manual_pick"] = True
        e_manual["main_category"] = {"id": "finance", "name": "金融"}

        self.write_previous_catalog([e_rec, e_cand, e_manual])

        manifest = sync_config_offline(self.root)
        self.assertEqual(manifest["owned_count"], 2)

        catalog = self.read_catalog()
        self.assertIn("owned", catalog)
        self.assertEqual(len(catalog["owned"]["items"]), 2)

        page = read_json(self.public / "data" / "catalog.json")
        rec_ids = [it["skill_id"] for it in page["recommended"]]
        cand_ids = [it["skill_id"] for it in page["candidates"]]
        manual_ids = [it["skill_id"] for it in page["manual"]]

        # 已收录的 acme/rec:SKILL.md 不出现在 recommended、candidates、manual
        self.assertNotIn("acme/rec:SKILL.md", rec_ids)
        self.assertIn("acme/cand:SKILL.md", cand_ids)
        self.assertIn("acme/manual:SKILL.md", manual_ids)

        # counts 验证
        self.assertEqual(page["counts"]["recommended"], 0)
        self.assertEqual(page["counts"]["candidate"], 1)
        self.assertEqual(page["counts"]["manual"], 1)
        self.assertEqual(page["counts"]["owned"], 2)  # 名单总数包含 finder-only
        self.assertEqual(page["counts"]["owned_in_catalog"], 1)  # 仅 acme/rec 存在于主索引

        # 分类统计排除已收录条目（finance 仅剩 manual 1 个，crypto 1 个）
        cats = {c["id"]: c["count"] for c in page["categories"]}
        self.assertEqual(cats.get("finance"), 1)
        self.assertEqual(cats.get("crypto"), 1)

        # owned_entries 携带原始展示快照与原分区
        self.assertEqual(len(page["owned_entries"]), 1)
        owned_snap = page["owned_entries"][0]
        self.assertEqual(owned_snap["skill_id"], "acme/rec:SKILL.md")
        self.assertEqual(owned_snap["original_partition"], "recommended")
        self.assertEqual(owned_snap["status"], STATUS_RECOMMENDED)

        # 绝对白名单：无私人泄漏
        self.assertNotIn("managed_url", page["owned"]["items"][0])
        self.assertNotIn("note", page["owned"]["items"][0])
        self.assertNotIn("private_details", page["owned"]["items"][0])

    def test_unmarking_owned_restores_candidate_to_schedulable(self):
        """取消已收录标记后，休眠待办在下一轮恢复为可调度状态。"""
        cfg_dir = self.temp_config()
        # 初始标记为已收录
        self._write_owned_config(
            cfg_dir,
            [
                {
                    "skill_id": "acme/dormant:SKILL.md",
                    "name": "dormant",
                    "added_at": "2026-09-24",
                }
            ],
        )

        cand = candidate_from_repo("acme", "dormant", path="SKILL.md", url="https://github.com/acme/dormant/blob/main/SKILL.md")

        # 阶段一：被跳过，留在 queue.json
        res1 = phase_reserve(
            config_dir=cfg_dir,
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover([cand]),
            fetch_fn=fake_fetch(),
        )
        self.assertEqual(res1["reserved"], 0)
        self.assertEqual(res1["skipped_owned"], 1)

        # 用户在网页端或配置中取消标记（移出 owned-skills.json）
        self._write_owned_config(cfg_dir, [])

        # 再次执行阶段一（假定增量发现为空，仅处理待办）
        res2 = phase_reserve(
            config_dir=cfg_dir,
            data_dir=self.data,
            state_dir=self.state,
            discover_fn=fake_discover([]),
            fetch_fn=fake_fetch(),
        )
        self.assertTrue(res2["ok"])
        # 成功唤醒待办，获得调度资格并完成额度预留！
        self.assertEqual(res2["reserved"], 1)
        self.assertEqual(res2["schedulable_count"], 1)
        self.assertEqual(res2["skipped_owned"], 0)


if __name__ == "__main__":
    unittest.main()
