"""索引：把候选、预筛结论与评估结果合并为唯一索引，并生成页面数据（§6、§7.2）。

§7.2：data/catalog.json 是唯一索引，public/data/ 由它生成，不维持两套独立数据。
§6：不编造用途、平台兼容性和变更时间；**最近检查时间不能冒充技能更新时间**。

输出路径全部由调用方给出，便于演练写入临时目录，不污染真实索引。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .decide import (
    DECISION_CANDIDATE,
    DECISION_EXCLUDED,
    DECISION_PROCESSING_FAILURE,
    DECISION_RECOMMENDED,
)
from .models import Candidate, PrescreenResult

CATALOG_VERSION = "1.0.0"

STATUS_PENDING = "pending"
STATUS_RECOMMENDED = DECISION_RECOMMENDED
STATUS_CANDIDATE = DECISION_CANDIDATE
STATUS_EXCLUDED = DECISION_EXCLUDED
STATUS_PROCESSING_FAILURE = DECISION_PROCESSING_FAILURE

UPSTREAM_OK = "ok"
UPSTREAM_GONE = "gone"
UPSTREAM_UNREACHABLE = "unreachable"

ACTIVE_STATUSES = (STATUS_RECOMMENDED, STATUS_CANDIDATE)
PAGE_STATUSES = (STATUS_RECOMMENDED, STATUS_CANDIDATE, STATUS_PENDING)


@dataclass
class CatalogContext:
    """生成索引所需的、与单个条目无关的信息。"""

    rules_version: str = ""
    generated_at: str = ""
    domain_names: dict[str, str] = field(default_factory=dict)
    source_types: dict[str, str] = field(default_factory=dict)


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pick_main_category(evaluation: dict | None) -> str | None:
    """主分类只来自评估结论。

    预筛命中的领域只是线索，记录在 candidate_domains 里，不冒充已判定的分类——
    §6 要求不编造用途，关键词命中不足以支撑分类结论。
    """
    if not evaluation:
        return None
    value = evaluation.get("main_category")
    return str(value) if value else None


def build_entry(
    candidate: Candidate,
    *,
    prescreen_result: PrescreenResult | None = None,
    decision: dict | None = None,
    evaluation: dict | None = None,
    context: CatalogContext | None = None,
    first_seen: str | None = None,
    last_checked: str | None = None,
    content_changed_at: str | None = None,
    upstream_status: str = UPSTREAM_OK,
    license_id: str | None = None,
    needs_review: bool = False,
    review_note: str | None = None,
    pending_review: dict | None = None,
    manual_pick: bool = False,
    manual_note: dict | None = None,
) -> dict:
    """构造一个索引条目。

    §6 要求「上游未说明的不得推测为全平台兼容」，因此平台与依赖字段未声明时
    一律为 None 或空列表，并保留 declared 语义。

    needs_review / review_note / pending_review 由调用方按 §5.2 的待复核规则给出：
    条目自身不判断内容是否变化，只负责如实保存结论与原评估快照。
    """
    ctx = context or CatalogContext()
    prescreen_result = prescreen_result or PrescreenResult(skill_id=candidate.skill_id, decision="queued")
    decision = decision or {}
    evaluation = evaluation or {}

    status = decision.get("decision") or STATUS_PENDING
    # 预筛已给出明确排除结论的条目，不因为没有评估结果而退回 pending：
    # 那会把"已排除"与"未评估"混为一谈。
    if not decision.get("decision") and prescreen_result.excluded:
        status = STATUS_EXCLUDED

    main_category_id = _pick_main_category(evaluation)
    main_category = None
    if main_category_id:
        main_category = {"id": main_category_id, "name": ctx.domain_names.get(main_category_id, "")}

    reason_codes = list(decision.get("reason_codes") or [])
    for code in prescreen_result.reason_codes:
        if code not in reason_codes:
            reason_codes.append(code)

    source_type = next(
        (ctx.source_types.get(sid) for sid in candidate.source_ids if ctx.source_types.get(sid)),
        None,
    )

    return {
        "skill_id": candidate.skill_id,
        "name": candidate.name or candidate.repo,
        "url": candidate.url,
        "repo_url": candidate.repo_url,
        "author": candidate.owner,
        # §6：中文简述来自评估；未评估时留空并明示状态，不编造
        "summary_zh": evaluation.get("summary_zh"),
        "main_category": main_category,
        "candidate_domains": list(prescreen_result.domains),
        "tags": list(evaluation.get("tags") or []),
        # §6：上游声明的适用平台、特殊工具、运行环境与依赖
        "platform_declared": evaluation.get("platform_declared"),
        "dependencies_declared": list(evaluation.get("dependencies_declared") or []),
        "source_type": source_type,
        "discovery": {
            "source_ids": list(candidate.source_ids),
            "methods": list(candidate.discovery_methods),
            "terms": list(candidate.search_terms),
        },
        "status": status,
        "manual_pick": bool(manual_pick),
        "manual_note": manual_note,
        "needs_review": bool(needs_review) if not manual_pick else False,
        "review_note": review_note,
        # §5.2/§6：待复核时要能展示「原评估对应的版本」——旧指纹、旧简述、旧分类与复核原因
        "pending_review": pending_review,
        "flags": list(prescreen_result.flags),
        "reason_codes": reason_codes,
        "limitations": evaluation.get("limitations"),
        "evaluation_rules_version": evaluation.get("rules_version") or ctx.rules_version or None,
        "first_seen": first_seen or candidate.discovered_at or None,
        "last_checked": last_checked or ctx.generated_at or None,
        # §6：最近检查时间不能冒充技能更新时间，两者分列
        "content_changed_at": content_changed_at,
        "content_fingerprint": candidate.content_fingerprint,
        "upstream_status": upstream_status,
        # §6 未列此字段，但公开上游技能的简述与评估属衍生内容，署名与许可需可查
        "license": license_id,
        "excluded": status == STATUS_EXCLUDED,
    }


def index_by_id(entries: list[dict] | None) -> dict[str, dict]:
    return {e["skill_id"]: e for e in (entries or []) if e.get("skill_id")}


def merge_entries(previous_entries: list[dict] | None, new_entries: list[dict]) -> list[dict]:
    """把本轮结果合并进既有索引（§7：网络失败时保留上次有效数据，不做批量删除）。

    - 本轮重新发现的条目**覆盖**旧条目，但沿用其 first_seen
    - 本轮**未出现**的条目原样保留：没有证据说明它已消失，不得当作下架
    - 顺序：先本轮结果，后保留条目
    """
    previous = index_by_id(previous_entries)
    merged: list[dict] = []

    for entry in new_entries:
        old = previous.pop(entry["skill_id"], None)
        if old:
            entry = dict(entry)
            entry["first_seen"] = old.get("first_seen") or entry.get("first_seen")
        merged.append(entry)

    merged.extend(previous.values())
    return merged


def build_catalog(
    entries: list[dict], *, context: CatalogContext, overrides: dict | None = None
) -> dict:
    """汇总为唯一索引。"""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1

    res = {
        "catalog_version": CATALOG_VERSION,
        "rules_version": context.rules_version,
        "generated_at": context.generated_at or _iso_now(),
        "counts": counts,
        "entries": entries,
    }
    if overrides:
        res["overrides"] = overrides
    return res


def build_page_data(catalog: dict) -> dict:
    """由索引生成页面数据（§6：默认展示推荐区，支持候选区与收藏区切换）。"""
    manual: list[dict] = []
    recommended: list[dict] = []
    candidates: list[dict] = []
    pending = 0
    failed = 0
    category_counts: dict[str, int] = {}

    for entry in catalog.get("entries", []):
        category = entry.get("main_category") or {}
        if category.get("id"):
            category_counts[category["id"]] = category_counts.get(category["id"], 0) + 1

        # §4.3 判定顺序，命中即停，保证互斥
        if entry.get("manual_pick"):
            manual.append(_display(entry))
        elif entry.get("status") == STATUS_RECOMMENDED:
            recommended.append(_display(entry))
        elif entry.get("status") == STATUS_CANDIDATE:
            candidates.append(_display(entry))
        elif entry.get("status") == STATUS_PENDING:
            pending += 1
        elif entry.get("status") == STATUS_PROCESSING_FAILURE:
            failed += 1

    res = {
        "generated_at": catalog.get("generated_at"),
        "rules_version": catalog.get("rules_version"),
        "counts": {
            "recommended": len(recommended),
            "candidate": len(candidates),
            "manual": len(manual),
            # §6：未评估或失败状态如实展示，不隐藏也不冒充已评估
            "pending": pending,
            "processing_failure": failed,
            "total_evaluated": len(recommended) + len(candidates) + len(manual),
        },
        "categories": [
            {"id": key, "count": value} for key, value in sorted(category_counts.items())
        ],
        "manual": manual,
        "recommended": recommended,
        "candidates": candidates,
    }
    if catalog.get("overrides"):
        res["overrides"] = catalog["overrides"]
    return res


def _display(entry: dict) -> dict:
    """页面展示所需的字段子集。"""
    return {
        "skill_id": entry["skill_id"],
        "name": entry["name"],
        "url": entry["url"],
        "author": entry["author"],
        "summary_zh": entry["summary_zh"],
        "main_category": entry["main_category"],
        "tags": entry["tags"],
        "platform_declared": entry["platform_declared"],
        "dependencies_declared": entry["dependencies_declared"],
        "source_type": entry["source_type"],
        "status": entry["status"],
        "manual_pick": bool(entry.get("manual_pick")),
        "manual_note": entry.get("manual_note"),
        "needs_review": entry["needs_review"],
        "review_note": entry["review_note"],
        # §6：待复核要清楚区分上游当前版本与原评估版本
        "pending_review": entry.get("pending_review"),
        "reason_codes": entry["reason_codes"],
        "limitations": entry["limitations"],
        "first_seen": entry["first_seen"],
        "last_checked": entry["last_checked"],
        "content_changed_at": entry["content_changed_at"],
        "upstream_status": entry["upstream_status"],
        "license": entry["license"],
    }


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def write_catalog(
    catalog: dict, *, data_path: str | Path, public_path: str | Path
) -> dict:
    """写出唯一索引与页面数据，返回构建清单（含各自摘要）。

    §7.2 步骤 6：发布时携带索引摘要，因此这里返回摘要供调用方记录。
    """
    data_file = Path(data_path)
    public_file = Path(public_path)

    page_data = build_page_data(catalog)

    return {
        "catalog_path": str(data_file),
        "catalog_digest": _write_json(data_file, catalog),
        "page_path": str(public_file),
        "page_digest": _write_json(public_file, page_data),
        "counts": page_data["counts"],
        "generated_at": catalog.get("generated_at"),
    }
