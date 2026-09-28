"""故障注入与异常恢复审计闭环专项验收测试（落实 P0 实施规范）。

覆盖场景：
1. 账本完成、池尚未保存（recover_completed_results 自愈对齐池与目录）
2. 池已保存、报告尚未生成（离线安全重建投影）
3. 恢复授权已保存、池尚未改回 pending（幂等重放，额度不重复增加）
4. 迁移清单已保存、状态与备份可追溯
5. 锁冲突、身份歧义、材料冲突与来源审计
"""

import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from src.catalog.budget import evaluation_filename
from src.catalog.config import load_all_config
from src.catalog.evaluation import evaluation_id
from src.catalog.maintenance import reconcile_pool, resume_candidate, recover_completed_results
from src.catalog.models import Candidate, PrescreenResult
from src.catalog.pool import (
    create_pool_from_candidates,
    load_pool,
    save_pool,
    STATUS_PENDING,
    STATUS_DONE,
    STATUS_BLOCKED,
    STATUS_LENGTH_EXCEEDED,
)
from src.catalog.store import catalog_session
from src.infra.files import write_json_atomic, read_json, LockConflict

ROOT = Path(__file__).resolve().parents[1]


class FaultRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        for path in (ROOT / "config").rglob("*.json"):
            if not path.name.endswith(".local.json"):
                rel = path.relative_to(ROOT / "config")
                dest = self.root / "config" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        self.cfg = load_all_config(self.root / "config")
        self.cfg["model"].update(endpoint="https://fake.invalid/v1/chat/completions", model="test-model")

    def make_candidate(self, i: int, fp: str = "fp_default") -> Candidate:
        path = f"skills/tool-{i}/SKILL.md"
        return Candidate(
            skill_id=f"example/skills:{path}",
            owner="example",
            repo="skills",
            path=path,
            name=f"tool-{i}",
            url=f"https://github.com/example/skills/blob/HEAD/{path}",
            content_fingerprint=fp,
        )

    def prepare_recovery(self, completed=False):
        candidate = self.make_candidate(99)
        eid = evaluation_id(candidate, self.cfg["model"], self.cfg["rules"])
        pool = create_pool_from_candidates([candidate])
        if not completed:
            pool.items[0].status = STATUS_BLOCKED
            pool.items[0].block_info = {"evaluation_id": eid}
        pool_path = self.root / "data/local/pool.json"
        save_pool(pool_path, pool)
        record = {
            "evaluation_id": eid, "skill_id": candidate.skill_id,
            "content_fingerprint": candidate.content_fingerprint,
            "status": "completed" if completed else "failed",
            "attempts": 2, "max_attempts": 2, "retryable": False,
            "requests": [{"usage_unknown": True}],
        }
        if completed:
            record["outcome"] = {
                "decision": "recommended", "candidate": candidate.__dict__,
                "prescreen": PrescreenResult(skill_id=candidate.skill_id, decision="accepted").__dict__,
                "evaluation": {"name": candidate.name, "summary_zh": "有效技能", "main_category": "programming"},
                "evaluated_at": "2026-09-28T08:00:00+08:00",
            }
            record["updated_at"] = "2026-09-28T08:00:00+08:00"
        record_path = self.root / "data/local/state/evaluations" / evaluation_filename(eid)
        write_json_atomic(record_path, record)
        return candidate, eid, pool_path, record_path

    def test_old_evaluation_does_not_complete_changed_material_or_versions(self):
        candidate, eid, pool_path, record_path = self.prepare_recovery(completed=True)
        recover_completed_results(self.root)
        for change in ("material", "rules", "model"):
            with self.subTest(change=change):
                pool = load_pool(pool_path)
                pool.items[0].status = STATUS_PENDING
                pool.items[0].candidate.content_fingerprint = "new" if change == "material" else candidate.content_fingerprint
                save_pool(pool_path, pool)
                cfg = deepcopy(self.cfg)
                if change == "rules":
                    cfg["rules"]["rules_version"] = "999"
                if change == "model":
                    cfg["model"]["model_config_version"] = "999"
                with patch("src.catalog.config.load_all_config", return_value=cfg):
                    recover_completed_results(self.root)
                self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)

    def test_pool_write_failure_is_visible_and_recoverable(self):
        _, _, pool_path, record_path = self.prepare_recovery(completed=True)
        before = record_path.read_bytes()
        with patch("src.catalog.pool.save_pool", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                recover_completed_results(self.root)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)
        recover_completed_results(self.root)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_DONE)
        self.assertEqual(before, record_path.read_bytes())

    def test_resume_manifest_completion_failure_replays_after_pool_saved(self):
        _, eid, pool_path, record_path = self.prepare_recovery()
        def fail_completion(path, value, *args, **kwargs):
            if Path(path).name.startswith("resume_") and value.get("status") == "completed":
                raise OSError("manifest failure")
            return write_json_atomic(path, value, *args, **kwargs)
        with patch("src.infra.files.write_json_atomic", side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, "manifest failure"):
                resume_candidate(self.root, evaluation_id=eid, reason="repair", apply=True)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)
        before = record_path.read_bytes()
        manifest_path = next((self.root / "data/local/state/migrations").glob("resume_*.json"))
        preview = resume_candidate(self.root, evaluation_id=eid, reason="repair", apply=False)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(read_json(manifest_path)["status"], "in_progress")
        result = resume_candidate(self.root, evaluation_id=eid, reason="repair", apply=True)
        self.assertTrue(result["idempotent"])
        self.assertEqual(read_json(manifest_path)["status"], "completed")
        self.assertEqual(before, record_path.read_bytes())
        again = resume_candidate(self.root, evaluation_id=eid, reason="repair", event_id=result["event_id"], apply=True)
        self.assertTrue(again["idempotent"])

    def test_resume_pool_failure_replays_without_extra_authorization(self):
        _, eid, pool_path, record_path = self.prepare_recovery()
        with patch("src.catalog.pool.save_pool", side_effect=OSError("pool failure")):
            with self.assertRaises(OSError):
                resume_candidate(self.root, evaluation_id=eid, reason="repair", apply=True)
        before = read_json(record_path)
        self.assertEqual(before["max_attempts"], 3)
        result = resume_candidate(self.root, evaluation_id=eid, reason="repair", apply=True)
        self.assertTrue(result["idempotent"])
        self.assertEqual(before, read_json(record_path))
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)

    def test_reconcile_write_failures_close_original_manifest(self):
        _, _, pool_path, record_path = self.prepare_recovery()
        pool = load_pool(pool_path)
        pool.items[0].status = STATUS_PENDING
        pool.items[0].block_info = None
        save_pool(pool_path, pool)
        def fail_completion(path, value, *args, **kwargs):
            if Path(path).name.startswith("reconcile_") and value.get("status") == "completed":
                raise OSError("manifest failure")
            return write_json_atomic(path, value, *args, **kwargs)
        before = record_path.read_bytes()
        with patch("src.infra.files.write_json_atomic", side_effect=fail_completion):
            with self.assertRaises(OSError):
                reconcile_pool(self.root, apply=True)
        manifest_path = next((self.root / "data/local/state/migrations").glob("reconcile_*.json"))
        self.assertEqual(read_json(manifest_path)["status"], "in_progress")
        result = reconcile_pool(self.root, apply=True)
        self.assertIn(str(manifest_path), result["recovered_manifests"])
        self.assertEqual(read_json(manifest_path)["status"], "completed")
        self.assertEqual(before, record_path.read_bytes())

    def test_reconcile_pool_write_failure_revalidates_then_closes_manifest(self):
        _, _, pool_path, record_path = self.prepare_recovery()
        pool = load_pool(pool_path)
        pool.items[0].status = STATUS_PENDING
        pool.items[0].block_info = None
        save_pool(pool_path, pool)
        with patch("src.catalog.pool.save_pool", side_effect=OSError("pool failure")):
            with self.assertRaises(OSError):
                reconcile_pool(self.root, apply=True)
        original = next((self.root / "data/local/state/migrations").glob("reconcile_*.json"))
        backup = Path(read_json(original)["backup_path"])
        backup_bytes = backup.read_bytes()
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)
        reconcile_pool(self.root, apply=True)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_BLOCKED)
        self.assertEqual(read_json(original)["status"], "completed")
        self.assertEqual(backup.read_bytes(), backup_bytes)

    def test_catalog_write_failure_does_not_complete_pool(self):
        _, _, pool_path, _ = self.prepare_recovery(completed=True)
        with patch("src.catalog.maintenance.write_catalog", side_effect=OSError("catalog failure")):
            with self.assertRaises(OSError):
                recover_completed_results(self.root)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_PENDING)
        recover_completed_results(self.root)
        self.assertEqual(load_pool(pool_path).items[0].status, STATUS_DONE)

    def test_cli_replays_explicit_event(self):
        import io
        from contextlib import redirect_stdout
        from tools import manage_pool
        _, eid, _, record_path = self.prepare_recovery()
        args = ["resume", "--evaluation-id", eid, "--reason", "repair",
                "--event-id", "cli_replay", "--apply"]
        output = io.StringIO()
        with patch.object(manage_pool, "ROOT", self.root), redirect_stdout(output):
            self.assertEqual(manage_pool.main(args), 0)
            self.assertEqual(manage_pool.main(args), 0)
        record = read_json(record_path)
        self.assertEqual(record["max_attempts"], 3)
        self.assertEqual(len(record["resume_history"]), 1)
        self.assertIn("未增加额度", output.getvalue())

    def test_replay_rejects_changed_authorization(self):
        _, eid, _, record_path = self.prepare_recovery()
        resume_candidate(self.root, evaluation_id=eid, reason="repair", event_id="stable", apply=True)
        before = record_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "原授权一致"):
            resume_candidate(self.root, evaluation_id=eid, reason="repair", event_id="stable", extra_attempts=5, apply=True)
        self.assertEqual(before, record_path.read_bytes())

    def test_ledger_completed_pool_unsaved_recovered_by_recover_completed_results(self):
        """故障场景 1：账本已完成评估，但候选池尚未标记 done（如断电中断）。

        recover_completed_results 应将 catalog 产物与 pool.json 自动对齐至 done，0 次模型调用。
        """
        c0 = self.make_candidate(0, fp="fp_completed")
        pool = create_pool_from_candidates([c0])
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        hash_fn = evaluation_filename(eid)
        rec_path = self.root / "data" / "local" / "state" / "evaluations" / hash_fn
        outcome = {
            "decision": "recommended",
            "evaluated_at": "2026-09-28T08:00:00+08:00",
            "candidate": c0.__dict__,
            "prescreen": PrescreenResult(skill_id=c0.skill_id, decision="accepted").__dict__,
            "evaluation": {
                "name": "tool-0",
                "summary_zh": "工具0描述",
                "decision": "recommended",
                "main_category": "programming",
            },
        }
        write_json_atomic(rec_path, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "status": "completed",
            "outcome": outcome,
            "updated_at": "2026-09-28T08:00:00+08:00",
        })

        # 初始状态：池中仍为 pending
        loaded_before = load_pool(pool_file)
        self.assertEqual(loaded_before.items[0].status, STATUS_PENDING)

        # 执行离线恢复
        res = recover_completed_results(self.root)
        self.assertEqual(res["restored"], 1)
        self.assertEqual(res["model_calls"], 0)

        # 验证 catalog.json 存在条目
        catalog = read_json(self.root / "data" / "catalog.json")
        self.assertEqual(len(catalog.get("entries", [])), 1)
        self.assertEqual(catalog["entries"][0]["skill_id"], c0.skill_id)

        # 验证候选池自愈：条目状态自动对齐为 done
        loaded_after = load_pool(pool_file)
        self.assertEqual(loaded_after.items[0].status, STATUS_DONE)

    def test_pool_saved_report_unwritten_recovers_projections(self):
        """故障场景 2：候选池已更新且账本存在，但前端公开投影尚未生成。

        recover_completed_results 纯离线重建 public/data/catalog.json。
        """
        c0 = self.make_candidate(0, fp="fp_proj")
        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        hash_fn = evaluation_filename(eid)
        outcome = {
            "decision": "recommended",
            "evaluated_at": "2026-09-28T08:00:00+08:00",
            "candidate": c0.__dict__,
            "prescreen": PrescreenResult(skill_id=c0.skill_id, decision="accepted").__dict__,
            "evaluation": {
                "name": "tool-0",
                "summary_zh": "工具0描述",
                "decision": "recommended",
                "main_category": "programming",
            },
        }
        write_json_atomic(self.root / "data" / "local" / "state" / "evaluations" / hash_fn, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "status": "completed",
            "outcome": outcome,
            "updated_at": "2026-09-28T08:00:00+08:00",
        })

        public_file = self.root / "public" / "data" / "catalog.json"
        self.assertFalse(public_file.exists())

        res = recover_completed_results(self.root)
        self.assertEqual(res["model_calls"], 0)
        self.assertTrue(public_file.exists())
        public_data = read_json(public_file)
        self.assertEqual(public_data["counts"]["recommended"], 1)
        self.assertEqual(len(public_data.get("recommended", [])), 1)

    def test_resume_authorization_saved_pool_still_blocked_idempotent_replay(self):
        """故障场景 3：恢复授权已记录入账本，但候选池尚未改回 pending。

        使用相同 event_id（或再次重放）恢复时，检测到幂等重放，绝不重复增加额度。
        """
        c0 = self.make_candidate(0, fp="fp_resume")
        pool = create_pool_from_candidates([c0])
        pool.items[0].status = STATUS_BLOCKED
        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        pool.items[0].block_info = {"evaluation_id": eid, "reason": "NON_RETRYABLE_FAILURE"}
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        hash_fn = evaluation_filename(eid)
        rec_path = self.root / "data" / "local" / "state" / "evaluations" / hash_fn
        write_json_atomic(rec_path, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
            "retryable": False,
        })

        # 首次显式恢复（指定固定 event_id）
        fixed_event_id = "test_evt_1001"
        res1 = resume_candidate(
            self.root,
            evaluation_id=eid,
            reason="修复提示词",
            extra_attempts=1,
            event_id=fixed_event_id,
            apply=True,
        )
        self.assertEqual(res1["new_max_attempts"], 3)
        self.assertEqual(res1["event_id"], fixed_event_id)

        # 模拟故障中断：账本已是 max_attempts=3，但池文件被外部/未落盘还原为 blocked
        loaded = load_pool(pool_file)
        loaded.items[0].status = STATUS_BLOCKED
        loaded.items[0].block_info = {"evaluation_id": eid, "reason": "NON_RETRYABLE_FAILURE"}
        save_pool(pool_file, loaded)

        # 重新执行恢复重放（相同 event_id）
        res2 = resume_candidate(
            self.root,
            evaluation_id=eid,
            reason="修复提示词",
            extra_attempts=1,
            event_id=fixed_event_id,
            apply=True,
        )
        self.assertTrue(res2["idempotent"])
        self.assertEqual(res2["new_max_attempts"], 3, "重放绝不应将 3 再次增至 4")

        # 验证账本 resume_history 仅有一条记录
        rec_after = read_json(rec_path)
        self.assertEqual(rec_after["max_attempts"], 3)
        self.assertEqual(len(rec_after["resume_history"]), 1)

        # 验证池已被正确修复为 pending
        loaded2 = load_pool(pool_file)
        self.assertEqual(loaded2.items[0].status, STATUS_PENDING)

    def test_reconcile_manifest_persisted_with_replay_and_backup(self):
        """故障场景 4：迁移清单在写入候选池前独立落盘，完整保留操作前后状态与备份。"""
        c0 = self.make_candidate(0, fp="fp_rec")
        c1 = self.make_candidate(1, fp="fp_rec")
        pool = create_pool_from_candidates([c0, c1])
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        eid0 = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        eid1 = evaluation_id(c1, self.cfg["model"], self.cfg["rules"])

        write_json_atomic(self.root / "data" / "local" / "state" / "evaluations" / evaluation_filename(eid0), {
            "evaluation_id": eid0,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "status": "failed",
            "error": {"reason_code": "LENGTH_EXCEEDED"},
        })
        write_json_atomic(self.root / "data" / "local" / "state" / "evaluations" / evaluation_filename(eid1), {
            "evaluation_id": eid1,
            "skill_id": c1.skill_id,
            "content_fingerprint": c1.content_fingerprint,
            "status": "failed",
            "retryable": False,
            "attempts": 2,
            "max_attempts": 2,
            "error": {"reason_code": "PARSE_ERROR"},
        })

        applied = reconcile_pool(self.root, apply=True)
        self.assertEqual(applied["reconciled"], 2)
        self.assertIsNotNone(applied["manifest_id"])
        self.assertIsNotNone(applied["manifest_path"])

        # 检查清单内容
        manifest = read_json(applied["manifest_path"])
        self.assertEqual(manifest["operation"], "reconcile_pool")
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(len(manifest["changes"]), 2)
        self.assertTrue(Path(manifest["backup_path"]).exists())

        # 验证重复对账幂等（reconciled=0）
        reapplied = reconcile_pool(self.root, apply=True)
        self.assertEqual(reapplied["reconciled"], 0)

    def test_lock_conflict_raises_and_preserves_state(self):
        """故障场景 5.1：会话锁冲突时直接抛出 LockConflict，不产生破损写入。"""
        import threading
        c0 = self.make_candidate(0, fp="fp_lock")
        pool = create_pool_from_candidates([c0])
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        locked_event = threading.Event()
        release_event = threading.Event()

        def external_task_holder():
            with catalog_session(self.root / "data"):
                locked_event.set()
                release_event.wait(timeout=5)

        thread = threading.Thread(target=external_task_holder)
        thread.start()
        self.assertTrue(locked_event.wait(timeout=3))

        try:
            with self.assertRaises(LockConflict):
                reconcile_pool(self.root, apply=True)
        finally:
            release_event.set()
            thread.join(timeout=3)

    def test_identity_conflict_rejected(self):
        """故障场景 5.2：记录的 skill_id 与候选不一致时，严格拒绝恢复与对账。"""
        c0 = self.make_candidate(0, fp="fp_id")
        pool = create_pool_from_candidates([c0])
        pool.items[0].status = STATUS_BLOCKED
        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        pool.items[0].block_info = {"evaluation_id": eid, "reason": "BLOCKED"}
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        # 写入一条所属 skill_id 不同的账本
        rec_path = self.root / "data" / "local" / "state" / "evaluations" / evaluation_filename(eid)
        write_json_atomic(rec_path, {
            "evaluation_id": eid,
            "skill_id": "other_owner/other_repo:skills/wrong/SKILL.md",
            "content_fingerprint": c0.content_fingerprint,
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
        })

        # 恢复时应拒绝
        with self.assertRaisesRegex(ValueError, "身份冲突"):
            resume_candidate(self.root, evaluation_id=eid, reason="尝试恢复", apply=True)

        # 候选池状态依然保持 blocked，未被篡改
        self.assertEqual(load_pool(pool_file).items[0].status, STATUS_BLOCKED)

    def test_material_conflict_rejected(self):
        """故障场景 5.3：候选材料指纹与记录不一致（内容已变更）时，严格拒绝恢复。"""
        c0 = self.make_candidate(0, fp="fp_new_content")
        pool = create_pool_from_candidates([c0])
        pool.items[0].status = STATUS_BLOCKED
        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        pool.items[0].block_info = {"evaluation_id": eid, "reason": "BLOCKED"}
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        rec_path = self.root / "data" / "local" / "state" / "evaluations" / evaluation_filename(eid)
        write_json_atomic(rec_path, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "content_fingerprint": "fp_old_outdated",
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
        })

        with self.assertRaisesRegex(ValueError, "材料冲突"):
            resume_candidate(self.root, evaluation_id=eid, reason="尝试恢复", apply=True)

    def test_actions_source_audit_and_constraints(self):
        """故障场景 5.4：Actions 远端来源审计与原件保护。

        缺少指纹时拒绝恢复；可信时恢复写入本地账本，绝不篡改 Actions 原始文件。
        """
        c0 = self.make_candidate(0, fp="fp_act")
        pool = create_pool_from_candidates([c0])
        pool.items[0].status = STATUS_BLOCKED
        eid = evaluation_id(c0, self.cfg["model"], self.cfg["rules"])
        pool.items[0].block_info = {"evaluation_id": eid, "reason": "BLOCKED"}
        pool_file = self.root / "data" / "local" / "pool.json"
        save_pool(pool_file, pool)

        actions_rec_path = self.root / "data" / "state" / "evaluations" / evaluation_filename(eid)

        # 缺少指纹时，拒绝恢复
        write_json_atomic(actions_rec_path, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
        })
        with self.assertRaisesRegex(ValueError, "缺少可信依据"):
            resume_candidate(self.root, evaluation_id=eid, reason="尝试恢复", apply=True)

        # 补齐指纹
        write_json_atomic(actions_rec_path, {
            "evaluation_id": eid,
            "skill_id": c0.skill_id,
            "content_fingerprint": c0.content_fingerprint,
            "status": "failed",
            "attempts": 2,
            "max_attempts": 2,
        })
        actions_content_before = actions_rec_path.read_text(encoding="utf-8")

        res = resume_candidate(self.root, evaluation_id=eid, reason="修复远端失败", apply=True)
        self.assertEqual(res["new_max_attempts"], 3)

        # 验证 Actions 原件绝未被修改
        self.assertEqual(actions_rec_path.read_text(encoding="utf-8"), actions_content_before)

        # 验证本地账本记录存在且标明来源于 actions
        local_rec_path = self.root / "data" / "local" / "state" / "evaluations" / evaluation_filename(eid)
        self.assertTrue(local_rec_path.exists())
        local_rec = read_json(local_rec_path)
        self.assertEqual(local_rec["resume_history"][0]["source_ledger"], "actions")


if __name__ == "__main__":
    unittest.main()
