"""Actions 跨轮待处理候选队列管理（data/queue.json）。

依据 §7.2 要求队列跨轮累积持久化，避免每轮仅处理前 N 个导致尾部候选饥饿。
按 §5.2/§5.3 优先级排序：已知内容变化或待复核插队，官方来源优先，公平轮转。
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from src.infra.files import read_json, write_json_atomic
from src.models import Candidate, PrescreenResult
from src.budget import BudgetLedger, STATUS_COMPLETED, STATUS_FAILED, STATUS_NEEDS_RECOVERY

QUEUE_FILENAME = "queue.json"
QUEUE_VERSION = "1.0.0"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_payload(candidate: Candidate, pres: PrescreenResult, fetch: dict) -> dict:
    """序列化候选及其预筛、抓取元信息为队列载荷。"""
    return {
        "candidate": {
            "skill_id": candidate.skill_id,
            "owner": candidate.owner,
            "repo": candidate.repo,
            "path": candidate.path,
            "url": candidate.url,
            "repo_url": candidate.repo_url,
            "name": candidate.name,
            "description": candidate.description,
            "source_ids": candidate.source_ids,
            "discovery_methods": candidate.discovery_methods,
            "search_terms": candidate.search_terms,
            "discovered_at": candidate.discovered_at,
            "content_fingerprint": candidate.content_fingerprint,
        },
        "prescreen": asdict(pres),
        "fetch": fetch,
    }


def candidate_from_payload(payload: dict) -> Candidate:
    return Candidate(**payload)


def prescreen_from_payload(payload: dict) -> PrescreenResult:
    return PrescreenResult(**payload)


def catalogued_entries(data_dir: Path) -> dict[str, dict]:
    """已收录条目的 skill_id → 条目映射。"""
    path = data_dir / "catalog.json"
    if not path.exists():
        return {}
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        entry["skill_id"]: entry
        for entry in payload.get("entries", [])
        if entry.get("skill_id")
    }


def source_type_of(item: dict, source_types: dict[str, str]) -> str | None:
    for source_id in ((item.get("candidate") or {}).get("source_ids") or []):
        if source_types.get(source_id):
            return source_types[source_id]
    return None


def skill_of(item: dict) -> str:
    return ((item.get("candidate") or {}).get("skill_id")) or ""


def skill_evaluation_id(skill_id: str, fingerprint: str, cfg: dict) -> str:
    return "|".join(
        [
            skill_id,
            fingerprint,
            str(cfg["rules"].get("rules_version") or ""),
            str(cfg["model"].get("model_config_version") or ""),
        ]
    )


def settled_for(ledger: BudgetLedger, skill_id: str, fingerprint: str, cfg: dict) -> bool:
    record = ledger.get(skill_evaluation_id(skill_id, fingerprint, cfg)) or {}
    return record.get("status") == STATUS_COMPLETED


def mark_settled(item: dict, cfg: dict, ledger: BudgetLedger) -> bool:
    """队列项对应的内容是否已经评估完并出队。"""
    fingerprint = item.get("content_fingerprint")
    return bool(fingerprint and settled_for(ledger, skill_of(item), fingerprint, cfg))


def ordered_pending(
    items: list[dict],
    *,
    catalogued: dict,
    source_types: dict,
    recheck: set[str] | None = None,
    manual_picks: set[str] | None = None,
) -> list[dict]:
    """按 §5.2/§5.3 的优先级排序并去重。"""
    recheck = recheck or set()
    manual_picks = manual_picks or set()

    def rank(item: dict) -> int:
        if item.get("content_changed") or item.get("needs_review"):
            return 0
        skill = skill_of(item)
        if item.get("manual_pick") or (skill and skill in manual_picks):
            return 1
        return 2

    ordered: list[dict] = []
    seen: set[str] = set()
    for item in sorted(
        items,
        key=lambda i: (
            rank(i),
            0 if source_type_of(i, source_types) == "official" else 1,
            0 if not i.get("last_attempted_at") else 1,
            i.get("last_attempted_at") or "",
            i.get("first_queued_at") or "",
            i.get("seq", 0),
        ),
    ):
        skill = skill_of(item)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        ordered.append(item)
    return ordered


def read_queue(state_dir: Path) -> dict:
    path = state_dir / QUEUE_FILENAME
    if not path.exists():
        return {}
    return read_json(path, default={})


def queue_pending(queue: dict | None) -> list[dict]:
    """读取待处理队列，兼容旧版格式。"""
    if not queue:
        return []
    if queue.get("queue_version"):
        return list(queue.get("pending") or [])
    return [
        {
            "candidate": item.get("candidate"),
            "prescreen": item.get("prescreen"),
            "fetch": item.get("fetch"),
            "pending": True,
        }
        for item in queue.get("queued") or []
    ]


def settled_pending(pending: list[dict], cfg: dict, ledger: BudgetLedger) -> list[dict]:
    """过滤已了结的条目，保留需继续排队的未结清条目。"""
    remaining: list[dict] = []
    for item in pending:
        if mark_settled(item, cfg, ledger):
            continue
        candidate = candidate_from_payload(item["candidate"])
        eid = skill_evaluation_id(candidate.skill_id, candidate.content_fingerprint or "", cfg)
        record = ledger.get(eid) or {}
        status = record.get("status")
        done = status == STATUS_NEEDS_RECOVERY or (
            status == STATUS_FAILED
            and int(record.get("attempts") or 0)
            >= int(record.get("max_attempts") or ledger.max_attempts)
        )
        if not done:
            remaining.append(item)
    return remaining


def write_queue(
    state_dir: Path,
    plan: dict,
    *,
    generated_at: str,
    pending: list[dict],
) -> None:
    """原子保存跨轮待办队列。"""
    payload = {
        "queue_version": QUEUE_VERSION,
        "week": plan["week"],
        "generated_at": generated_at,
        "pending": pending,
        "excluded": [candidate_payload(c, p, f) for c, p, f in plan.get("excluded", [])],
        "outcomes": plan.get("outcomes", []),
        "evaluation_slots": plan.get("evaluation_slots", 0),
    }
    write_json_atomic(state_dir / QUEUE_FILENAME, payload)


__all__ = [
    "QUEUE_FILENAME",
    "QUEUE_VERSION",
    "candidate_payload",
    "candidate_from_payload",
    "prescreen_from_payload",
    "catalogued_entries",
    "source_type_of",
    "skill_of",
    "skill_evaluation_id",
    "settled_for",
    "mark_settled",
    "ordered_pending",
    "read_queue",
    "queue_pending",
    "settled_pending",
    "write_queue",
]
