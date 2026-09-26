"""候选池管理：候选列表持久化、冻结顺序与断点续跑。

本地运行每次完整搜索需调用上百次 GitHub API 并展开数十个仓库（耗时十余分钟且易限流）。
本模块将初次发现的候选名单冻结在 data/local/pool.json 中，为每个候选赋予唯一 seq 序号，
并记录处理状态（pending / done / excluded / fetch_failed / not_skill / length_exceeded）。

下次启动时：
- 若池中待处理候选充足（>= 水位线），直接跳过网络搜索（0.1s 秒级启动）；
- 自动从第一个 pending 候选继续，跳过已处理项，实现无缝断点续跑；
- 支持 7 天 TTL 过期判定与增量补水。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any
from src.infra.files import write_json_atomic
from src.shared.runtime import now_local
from .dedupe import dedupe
from .models import Candidate

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_EXCLUDED = "excluded"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_NOT_SKILL = "not_skill"
STATUS_LENGTH_EXCEEDED = "length_exceeded"

VALID_STATUSES = {
    STATUS_PENDING,
    STATUS_DONE,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_LENGTH_EXCEEDED,
}


@dataclass
class PoolItem:
    """候选池中的单条条目，携带固定序号与执行状态。"""

    seq: int
    candidate: Candidate
    status: str = STATUS_PENDING
    checked_at: str | None = None


@dataclass
class CandidatePool:
    """本地候选池，持久化于 data/local/pool.json。"""

    pool_version: str = "1.0.0"
    built_at: str = ""
    updated_at: str = ""
    items: list[PoolItem] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        counts = {
            "total": len(self.items),
            "pending": 0,
            "done": 0,
            "excluded": 0,
            "fetch_failed": 0,
            "not_skill": 0,
            "length_exceeded": 0,
        }
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_PENDING)

    @property
    def candidates(self) -> list[PoolItem]:
        return self.items

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> PoolItem:
        return self.items[index]


def candidate_to_dict(c: Candidate) -> dict[str, Any]:
    return {
        "skill_id": c.skill_id,
        "owner": c.owner,
        "repo": c.repo,
        "path": c.path,
        "url": c.url,
        "repo_url": c.repo_url,
        "name": c.name,
        "description": c.description,
        "source_ids": list(c.source_ids),
        "discovery_methods": list(c.discovery_methods),
        "search_terms": list(c.search_terms),
        "domain_hints": list(c.domain_hints),
        "discovered_at": c.discovered_at,
        "content_fingerprint": c.content_fingerprint,
    }


def candidate_from_dict(d: dict[str, Any]) -> Candidate:
    return Candidate(
        skill_id=d.get("skill_id", ""),
        owner=d.get("owner", ""),
        repo=d.get("repo", ""),
        path=d.get("path", ""),
        url=d.get("url", ""),
        repo_url=d.get("repo_url", ""),
        name=d.get("name", ""),
        description=d.get("description", ""),
        source_ids=list(d.get("source_ids") or []),
        discovery_methods=list(d.get("discovery_methods") or []),
        search_terms=list(d.get("search_terms") or []),
        domain_hints=list(d.get("domain_hints") or []),
        discovered_at=d.get("discovered_at", ""),
        content_fingerprint=d.get("content_fingerprint"),
    )


def pool_to_dict(pool: CandidatePool) -> dict[str, Any]:
    return {
        "pool_version": pool.pool_version,
        "built_at": pool.built_at,
        "updated_at": pool.updated_at,
        "stats": pool.stats(),
        "candidates": [
            {
                "seq": item.seq,
                "status": item.status,
                "checked_at": item.checked_at,
                "candidate": candidate_to_dict(item.candidate),
            }
            for item in pool.items
        ],
    }


def pool_from_dict(data: dict[str, Any]) -> CandidatePool:
    items = [
        PoolItem(
            seq=int(c["seq"]),
            status=c.get("status", STATUS_PENDING),
            checked_at=c.get("checked_at"),
            candidate=candidate_from_dict(c["candidate"]),
        )
        for c in data.get("candidates", [])
    ]
    return CandidatePool(
        pool_version=data.get("pool_version", "1.0.0"),
        built_at=data.get("built_at", ""),
        updated_at=data.get("updated_at", ""),
        items=items,
    )


def load_pool(path: Path) -> CandidatePool | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "candidates" not in data:
            return None
        return pool_from_dict(data)
    except Exception:
        return None


def save_pool(path: Path, pool: CandidatePool) -> None:
    write_json_atomic(Path(path), pool_to_dict(pool))


def is_pool_expired(pool: CandidatePool, max_age_days: int = 7) -> bool:
    if not pool.built_at:
        return True
    try:
        built = datetime.fromisoformat(pool.built_at)
        now = now_local()
        if built.tzinfo is None:
            now = datetime.now()
        elif built.tzinfo != now.tzinfo:
            built = built.astimezone(now.tzinfo)
        return (now - built) > timedelta(days=max_age_days)
    except Exception:
        return True


def create_pool_from_candidates(
    candidates: list[Candidate],
    old_recommended: set[str] | None = None,
    source_types: dict[str, str] | None = None,
) -> CandidatePool:
    """根据候选列表初始化新候选池，冻结排序与 seq 编号。"""
    old_recommended = old_recommended or set()
    source_types = source_types or {}
    deduped = dedupe(candidates)
    # 优先寻找新增推荐，其次处理已有推荐；同组内官方来源优先。
    deduped.sort(
        key=lambda c: (
            c.skill_id in old_recommended,
            not any(source_types.get(s) == "official" for s in c.source_ids),
        )
    )
    now_str = now_local().isoformat()
    items = [
        PoolItem(
            seq=i,
            status=STATUS_PENDING,
            checked_at=None,
            candidate=candidate,
        )
        for i, candidate in enumerate(deduped)
    ]
    return CandidatePool(
        pool_version="1.0.0",
        built_at=now_str,
        updated_at=now_str,
        items=items,
    )


def append_new_candidates(
    pool: CandidatePool,
    new_candidates: list[Candidate],
    old_recommended: set[str] | None = None,
    source_types: dict[str, str] | None = None,
) -> int:
    """增量补水：仅将池中从未见过的候选追加到池尾，赋予新递增 seq。"""
    old_recommended = old_recommended or set()
    source_types = source_types or {}
    existing_ids = {item.candidate.skill_id for item in pool.items}
    deduped_new = [c for c in dedupe(new_candidates) if c.skill_id not in existing_ids]
    if not deduped_new:
        return 0
    deduped_new.sort(
        key=lambda c: (
            c.skill_id in old_recommended,
            not any(source_types.get(s) == "official" for s in c.source_ids),
        )
    )
    next_seq = (max((item.seq for item in pool.items), default=-1)) + 1
    for c in deduped_new:
        pool.items.append(
            PoolItem(
                seq=next_seq,
                status=STATUS_PENDING,
                checked_at=None,
                candidate=c,
            )
        )
        next_seq += 1
    pool.updated_at = now_local().isoformat()
    return len(deduped_new)


def get_pending_candidates(pool: CandidatePool) -> list[PoolItem]:
    """获取所有待处理候选。"""
    return [item for item in pool.items if item.status == STATUS_PENDING]


def update_candidate_status(
    pool: CandidatePool,
    seq: int,
    status: str,
    checked_at: str | None = None,
) -> None:
    """更新指定候选的状态与检查时间戳。"""
    if status not in VALID_STATUSES:
        raise ValueError(f"未知候选状态：{status}")
    if 0 <= seq < len(pool.items) and pool.items[seq].seq == seq:
        item = pool.items[seq]
    else:
        item = next((it for it in pool.items if it.seq == seq), None)
    if item is None:
        raise KeyError(f"未找到 seq 为 {seq} 的候选")
    item.status = status
    item.checked_at = checked_at or now_local().isoformat()
    pool.updated_at = now_local().isoformat()
