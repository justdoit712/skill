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
import re
from typing import Any
from src.infra.files import write_json_atomic
from src.shared.owned import is_skill_owned
from src.shared.runtime import now_local
from .dedupe import dedupe
from .models import Candidate

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_EXCLUDED = "excluded"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_NOT_SKILL = "not_skill"
STATUS_LENGTH_EXCEEDED = "length_exceeded"
STATUS_BLOCKED = "blocked"
STATUS_STATIC_SKIPPED = "static_skipped"

VALID_STATUSES = {
    STATUS_PENDING,
    STATUS_DONE,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_LENGTH_EXCEEDED,
    STATUS_BLOCKED,
    STATUS_STATIC_SKIPPED,
}

POOL_TERMINAL_STATUSES = frozenset({
    STATUS_DONE,
    STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_NOT_SKILL,
    STATUS_LENGTH_EXCEEDED,
    STATUS_BLOCKED,
    STATUS_STATIC_SKIPPED,
})

REASON_SNOOZED = "snoozed"
REASON_MANUAL_EXCLUDED = "manual_excluded"
REASON_OWNED = "owned"


def classify_pending_candidate(
    candidate: Candidate | str,
    active_snoozed: set[str],
    manual_exclusions: set[str] | dict,
    owned_ids: set[str],
) -> tuple[bool, str | None]:
    """对处于 pending 状态的候选进行有效性判断，给出互斥主原因。

    统一优先级：
    1. active_snoozed -> (False, "snoozed")
    2. manual_exclusions -> (False, "manual_excluded")
    3. owned_ids -> (False, "owned")
    4. 其余 -> (True, None)
    """
    sid = candidate.skill_id if hasattr(candidate, "skill_id") else str(candidate)
    if sid in active_snoozed:
        return False, REASON_SNOOZED
    if sid in manual_exclusions:
        return False, REASON_MANUAL_EXCLUDED
    if is_skill_owned(sid, owned_ids):
        return False, REASON_OWNED
    return True, None


@dataclass
class PoolItem:
    """候选池中的单条条目，携带固定序号与执行状态。"""

    seq: int
    candidate: Candidate
    status: str = STATUS_PENDING
    checked_at: str | None = None
    block_info: dict | None = None


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
            "blocked": 0,
            "static_skipped": 0,
        }
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_PENDING)

    def count_actionable(
        self,
        active_snoozed: set[str],
        manual_exclusions: set[str] | dict,
        owned_ids: set[str],
    ) -> tuple[int, dict[str, int]]:
        """返回 (有效可处理数, 各跳过原因统计字典)。"""
        counts = {
            REASON_SNOOZED: 0,
            REASON_MANUAL_EXCLUDED: 0,
            REASON_OWNED: 0,
        }
        actionable = 0
        for item in self.items:
            if item.status == STATUS_PENDING:
                is_act, reason = classify_pending_candidate(
                    item.candidate, active_snoozed, manual_exclusions, owned_ids
                )
                if is_act:
                    actionable += 1
                elif reason:
                    counts[reason] = counts.get(reason, 0) + 1
        counts["actionable"] = actionable
        return actionable, counts

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
        "source_ids": list(getattr(c, "source_ids", [])),
        "discovery_methods": list(getattr(c, "discovery_methods", [])),
        "search_terms": list(getattr(c, "search_terms", [])),
        "domain_hints": list(getattr(c, "domain_hints", [])),
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
                "block_info": item.block_info,
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
            block_info=c.get("block_info"),
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


def prioritize_pending_batch(
    pending_items: list[PoolItem],
    *,
    batch_size: int = 20,
    manual_picks: set[str] | None = None,
    tier_map: dict[str, str] | None = None,
    enabled: bool = False,
    anti_starvation_ratio: float = 0.2,
) -> list[PoolItem]:
    """对待处理候选分批并在批内进行优先级排序（兼顾公平性与高优项响应）。

    严格安全保证：
    - enabled=False 时（默认），严格保持原始 seq FIFO 顺序，默认不改行为；
    - 批内排序依据准备好的正文静态分级（tier_map），结合人工挑选与高危合规优先复核；
    - 批内保证至少 20% 防饥饿配额分配给未被提权项（按原始 seq 顺序保留）；
    - 跨批次保持严格窗口隔离，绝不因后批高优项导致前批候选无限饥饿。
    """
    if not enabled or not pending_items:
        return list(pending_items)

    manual_picks = manual_picks or set()
    tier_map = tier_map or {}

    def item_rank(item: PoolItem) -> int:
        cand = item.candidate
        if cand.skill_id in manual_picks:
            return 0
        tier = tier_map.get(cand.skill_id)
        if tier == "tier_suspect":
            return 0
        if tier == "tier_normal":
            return 1
        if tier == "tier_unassessed":
            return 2
        if tier == "tier_clear_placeholder":
            return 4

        # 若无正文分级，后备元数据敏感词识别
        haystack = f"{cand.name} {cand.description} {cand.path}"
        for pattern in (
            r"(?i)实盘|自动下单|下单执行|交易执行|券商接口|auto[-_ ]?trad|place[-_ ]?order|order[-_ ]?execution|live[-_ ]?trad|broker[-_ ]?api",
            r"(?i)临床诊断|治疗决策|开处方|clinical[-_ ]?(decision|diagnos)|treatment[-_ ]?decision|prescription[-_ ]?engine",
            r"(?i)凭据外传|窃取密钥|exfiltrat|steal[-_ ]?credential|harvest[-_ ]?credential",
        ):
            if re.search(pattern, haystack):
                return 0
        return 2

    result: list[PoolItem] = []
    for i in range(0, len(pending_items), batch_size):
        chunk = pending_items[i : i + batch_size]
        n = len(chunk)
        if n <= 1:
            result.extend(chunk)
            continue

        boosted = [it for it in chunk if item_rank(it) <= 1]
        regular = [it for it in chunk if item_rank(it) > 1]

        # 计算防饥饿配额（批内至少保留 20% 配额分配给未提权项）
        quota = max(1, round(n * anti_starvation_ratio)) if (n >= 5 and regular) else 0

        if not boosted or not regular or quota == 0:
            sorted_chunk = sorted(chunk, key=lambda it: (item_rank(it), it.seq))
            result.extend(sorted_chunk)
        else:
            max_boosted = max(1, n - quota)
            sorted_boosted = sorted(boosted, key=lambda it: (item_rank(it), it.seq))
            sorted_regular = sorted(regular, key=lambda it: it.seq)

            top_boosted = sorted_boosted[:max_boosted]
            reserved_regular = sorted_regular[:quota]
            remaining = sorted_boosted[max_boosted:] + sorted_regular[quota:]
            remaining_sorted = sorted(remaining, key=lambda it: (item_rank(it), it.seq))

            result.extend(top_boosted + reserved_regular + remaining_sorted)

    return result
