"""候选池持久化、断点状态与补水逻辑测试。"""

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from src.catalog.dedupe import candidate_from_repo
from src.catalog.models import Candidate
from src.catalog.pool import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_PENDING,
    CandidatePool,
    PoolItem,
    append_new_candidates,
    candidate_from_dict,
    candidate_to_dict,
    create_pool_from_candidates,
    get_pending_candidates,
    is_pool_expired,
    load_pool,
    save_pool,
    update_candidate_status,
)


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _sample_candidate(self, i: int, owner: str = "test-owner") -> Candidate:
        path = f"skills/tool-{i}/SKILL.md"
        return candidate_from_repo(
            owner,
            "skills",
            path=path,
            url=f"https://github.com/{owner}/skills/blob/HEAD/{path}",
            name=f"tool-{i}",
        )

    def test_candidate_dict_roundtrip(self):
        c = self._sample_candidate(1)
        c.description = "desc"
        c.source_ids = ["official"]
        c.discovery_methods = ["repo_file_structure"]
        c.search_terms = ["code review"]
        c.domain_hints = ["programming"]
        c.discovered_at = "2026-09-21T00:00:00+08:00"
        c.content_fingerprint = "abc12345"

        d = candidate_to_dict(c)
        restored = candidate_from_dict(d)

        self.assertEqual(restored.skill_id, c.skill_id)
        self.assertEqual(restored.owner, c.owner)
        self.assertEqual(restored.repo, c.repo)
        self.assertEqual(restored.path, c.path)
        self.assertEqual(restored.url, c.url)
        self.assertEqual(restored.repo_url, c.repo_url)
        self.assertEqual(restored.name, c.name)
        self.assertEqual(restored.description, c.description)
        self.assertEqual(restored.source_ids, c.source_ids)
        self.assertEqual(restored.discovery_methods, c.discovery_methods)
        self.assertEqual(restored.search_terms, c.search_terms)
        self.assertEqual(restored.domain_hints, c.domain_hints)
        self.assertEqual(restored.discovered_at, c.discovered_at)
        self.assertEqual(restored.content_fingerprint, c.content_fingerprint)

    def test_pool_creation_and_stats(self):
        candidates = [self._sample_candidate(i) for i in range(5)]
        pool = create_pool_from_candidates(candidates)

        self.assertEqual(len(pool), 5)
        self.assertEqual(pool.pending_count, 5)
        self.assertEqual([item.seq for item in pool.items], [0, 1, 2, 3, 4])
        self.assertEqual([item.status for item in pool.items], [STATUS_PENDING] * 5)
        self.assertEqual(
            pool.stats(),
            {
                "total": 5,
                "pending": 5,
                "done": 0,
                "excluded": 0,
                "fetch_failed": 0,
                "not_skill": 0,
                "length_exceeded": 0,
                "blocked": 0,
            },
        )

    def test_pool_save_and_load(self):
        candidates = [self._sample_candidate(i) for i in range(3)]
        pool = create_pool_from_candidates(candidates)
        pool_file = self.dir / "pool.json"

        save_pool(pool_file, pool)
        self.assertTrue(pool_file.exists())

        loaded = load_pool(pool_file)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded.pool_version, "1.0.0")
        self.assertEqual(loaded[0].candidate.skill_id, candidates[0].skill_id)
        self.assertEqual(loaded[1].seq, 1)

    def test_update_candidate_status(self):
        candidates = [self._sample_candidate(i) for i in range(4)]
        pool = create_pool_from_candidates(candidates)

        update_candidate_status(pool, 0, STATUS_DONE)
        update_candidate_status(pool, 1, STATUS_EXCLUDED)
        update_candidate_status(pool, 2, STATUS_FETCH_FAILED)
        update_candidate_status(pool, 3, STATUS_NOT_SKILL)

        self.assertEqual(pool[0].status, STATUS_DONE)
        self.assertIsNotNone(pool[0].checked_at)
        self.assertEqual(pool[1].status, STATUS_EXCLUDED)
        self.assertEqual(pool[2].status, STATUS_FETCH_FAILED)
        self.assertEqual(pool[3].status, STATUS_NOT_SKILL)
        self.assertEqual(pool.pending_count, 0)
        self.assertEqual(
            pool.stats(),
            {
                "total": 4,
                "pending": 0,
                "done": 1,
                "excluded": 1,
                "fetch_failed": 1,
                "not_skill": 1,
                "length_exceeded": 0,
                "blocked": 0,
            },
        )

        with self.assertRaises(ValueError):
            update_candidate_status(pool, 0, "unknown_status")

        with self.assertRaises(KeyError):
            update_candidate_status(pool, 999, STATUS_DONE)

    def test_get_pending_candidates(self):
        candidates = [self._sample_candidate(i) for i in range(3)]
        pool = create_pool_from_candidates(candidates)

        update_candidate_status(pool, 0, STATUS_DONE)
        pending = get_pending_candidates(pool)

        self.assertEqual(len(pending), 2)
        self.assertEqual([p.seq for p in pending], [1, 2])
        self.assertEqual([p.candidate.name for p in pending], ["tool-1", "tool-2"])

    def test_is_pool_expired(self):
        pool = CandidatePool(pool_version="1.0.0")
        self.assertTrue(is_pool_expired(pool, max_age_days=7))

        pool.built_at = datetime.now().isoformat()
        self.assertFalse(is_pool_expired(pool, max_age_days=7))

        eight_days_ago = datetime.now() - timedelta(days=8)
        pool.built_at = eight_days_ago.isoformat()
        self.assertTrue(is_pool_expired(pool, max_age_days=7))

        pool.built_at = "not-a-valid-iso-date"
        self.assertTrue(is_pool_expired(pool, max_age_days=7))

    def test_append_new_candidates(self):
        initial = [self._sample_candidate(i) for i in range(3)]
        pool = create_pool_from_candidates(initial)
        self.assertEqual(len(pool), 3)

        # 追加混合候选：已有的 tool-1、tool-2，和新增的 tool-3、tool-4
        new_batch = [self._sample_candidate(i) for i in [1, 2, 3, 4]]
        added_count = append_new_candidates(pool, new_batch)

        self.assertEqual(added_count, 2)
        self.assertEqual(len(pool), 5)
        self.assertEqual([item.seq for item in pool.items], [0, 1, 2, 3, 4])
        self.assertEqual(pool[3].candidate.name, "tool-3")
        self.assertEqual(pool[4].candidate.name, "tool-4")
        self.assertEqual(pool[3].status, STATUS_PENDING)

        # 再次追加完全相同的候选，增加量应为 0
        added_again = append_new_candidates(pool, new_batch)
        self.assertEqual(added_again, 0)
        self.assertEqual(len(pool), 5)

    def test_resume_simulation(self):
        """模拟两轮运行：第一轮处理前 2 条中断，第二轮直接从第 3 条（seq=2）继续。"""
        candidates = [self._sample_candidate(i) for i in range(5)]
        pool_file = self.dir / "pool.json"

        # 第一轮构建池并处理 seq 0, 1
        pool = create_pool_from_candidates(candidates)
        save_pool(pool_file, pool)

        pending = get_pending_candidates(pool)
        self.assertEqual(pending[0].seq, 0)
        update_candidate_status(pool, 0, STATUS_DONE)
        update_candidate_status(pool, 1, STATUS_DONE)
        save_pool(pool_file, pool)

        # 模拟重启：从磁盘加载池
        reloaded = load_pool(pool_file)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.pending_count, 3)

        next_pending = get_pending_candidates(reloaded)
        self.assertEqual([p.seq for p in next_pending], [2, 3, 4])
        self.assertEqual(next_pending[0].candidate.name, "tool-2")


    def test_blocked_status_and_block_info(self):
        """blocked 状态应正常存储 block_info 并通过序列化往返。"""
        candidates = [self._sample_candidate(i) for i in range(3)]
        pool = create_pool_from_candidates(candidates)
        pool_file = self.dir / "pool.json"

        block_info = {
            "evaluation_id": "test-eid-001",
            "reason": "NON_RETRYABLE_FAILURE",
            "reason_code": "MODEL_ERROR",
            "http_status": 200,
            "blocked_at": "2026-09-26T10:00:00+08:00",
            "source": "local_ledger",
        }
        pool.items[1].block_info = block_info
        update_candidate_status(pool, 1, STATUS_BLOCKED)

        # 验证内存状态
        self.assertEqual(pool[1].status, STATUS_BLOCKED)
        self.assertEqual(pool[1].block_info["reason_code"], "MODEL_ERROR")
        self.assertEqual(pool.pending_count, 2)
        stats = pool.stats()
        self.assertEqual(stats["blocked"], 1)
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(
            stats,
            {
                "total": 3,
                "pending": 2,
                "done": 0,
                "excluded": 0,
                "fetch_failed": 0,
                "not_skill": 0,
                "length_exceeded": 0,
                "blocked": 1,
            },
        )

        # 验证序列化往返
        save_pool(pool_file, pool)
        loaded = load_pool(pool_file)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[1].status, STATUS_BLOCKED)
        self.assertEqual(loaded[1].block_info["evaluation_id"], "test-eid-001")
        self.assertEqual(loaded[1].block_info["reason"], "NON_RETRYABLE_FAILURE")
        # blocked 条目不算 pending
        self.assertEqual(loaded.pending_count, 2)
        # blocked 条目的 block_info 为 None 时也能反序列化
        loaded[0].block_info = None
        update_candidate_status(loaded, 0, STATUS_BLOCKED)
        save_pool(pool_file, loaded)
        reloaded = load_pool(pool_file)
        self.assertIsNone(reloaded[0].block_info)


if __name__ == "__main__":
    unittest.main()
