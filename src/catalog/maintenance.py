"""目录离线运维与轻量任务（落实 P3 设计规范）。

不依赖网络、0-Token、零模型依赖：
- sync_config_offline: 将 config/*.json (overrides, snoozed) 同步到 data 与 public/data
- enrich_catalog_offline: 从现有数据中提取形态、示例请求与亮点，不修改原中文简述
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json
from .index import CatalogContext, build_catalog
from .store import catalog_task, write_catalog, recover_catalog_projections
from .enrich import enrich_entry
from src.infra.files import read_json


@catalog_task
def recover_completed_results(root_dir: str | Path = ".") -> dict:
    """Replay completed records and rebuild the page without network or credentials.

    New records carry their original candidate/prescreen facts. Legacy records
    can be recovered while their original queue or local pool is still present.
    Unknown/in-progress records are never retried by this command.
    """
    from .entry_state import EntryUpdateEvent, update_entry
    from .models import Candidate, PrescreenResult
    from .prescreen import load_config
    from .index import index_by_id
    from .overrides import apply_manual_overrides
    from .snooze import apply_snooze_overrides

    root = Path(root_dir)
    data = root / "data"
    catalog = read_json(data / "catalog.json", default={"entries": []})
    entries = index_by_id(catalog.get("entries", []))
    rules = load_config(root / "config")
    sources = read_json(root / "config" / "sources.json", default={})
    context = CatalogContext(rules_version=rules.rules["rules_version"],
                             domain_names=rules.domain_names,
                             source_types={s["id"]: s.get("source_type") for s in sources.get("sources", [])})
    originals = {}
    for path, key in ((data / "state" / "queue.json", "pending"),
                      (data / "local" / "pool.json", "items")):
        for item in read_json(path, default={}).get(key, []):
            candidate = item.get("candidate", {})
            originals[candidate.get("skill_id")] = item
    records = []
    for state in (data / "state", data / "local" / "state"):
        for path in (state / "evaluations").glob("*.json"):
            record = read_json(path)
            if record.get("status") == "completed" and (record.get("outcome") or {}).get("evaluation"):
                records.append(record)
    restored, skipped = 0, []
    for record in sorted(records, key=lambda r: r.get("updated_at", "")):
        outcome = record["outcome"]
        original = originals.get(record.get("skill_id"), {})
        candidate_data = outcome.get("candidate") or original.get("candidate")
        prescreen_data = outcome.get("prescreen") or original.get("prescreen")
        if not candidate_data or not prescreen_data:
            skipped.append({"evaluation_id": record.get("evaluation_id"), "reason": "legacy_identity_missing"})
            continue
        candidate = Candidate(**candidate_data)
        previous = entries.get(candidate.skill_id)
        if previous and previous.get("last_evaluation_id") == record.get("evaluation_id"):
            continue
        if previous and previous.get("content_fingerprint") != candidate.content_fingerprint:
            # A later observation must never be replaced by an older result.
            from datetime import datetime
            checked, evaluated = previous.get("last_checked"), record.get("updated_at")
            try:
                if checked and evaluated and datetime.fromisoformat(checked) >= datetime.fromisoformat(evaluated):
                    continue
            except (ValueError, TypeError):
                skipped.append({"evaluation_id": record.get("evaluation_id"), "reason": "ambiguous_version_order"})
                continue
        context.generated_at = record.get("updated_at", "")
        event = EntryUpdateEvent(kind="cached_evaluation", prescreen_result=PrescreenResult(**prescreen_data),
            evaluation=outcome["evaluation"], decision=outcome,
            fetched_fingerprint=candidate.content_fingerprint, evaluation_id=record.get("evaluation_id"),
            evaluated_at=outcome.get("evaluated_at"), rules_version=record.get("rules_version"))
        entries[candidate.skill_id] = update_entry(previous, candidate, event, context)
        restored += 1
    # Preserve the already committed manual policy; config synchronization is separate.
    values = list(entries.values())
    apply_manual_overrides(values, catalog.get("overrides") or {})
    apply_snooze_overrides(values, catalog.get("snoozed"))
    if restored:
        updated = build_catalog(values, context=context, overrides=catalog.get("overrides"), snoozed=catalog.get("snoozed"))
        write_catalog(updated, data_path=data / "catalog.json", public_path=root / "public" / "data" / "catalog.json")
    else:
        recover_catalog_projections(root)
    return {"restored": restored, "skipped": skipped, "model_calls": 0}


def sync_config_offline(
    root_dir: str | Path = ".",
    *,
    catalog_path: str | Path | None = None,
    public_catalog_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    snoozed_path: str | Path | None = None,
) -> dict[str, Any]:
    """纯离线同步配置规则到目录与页面公开数据（0-Token、无模型依赖）。"""
    return sync_config_to_catalog(
        root_dir=root_dir,
        catalog_path=catalog_path,
        public_catalog_path=public_catalog_path,
        overrides_path=overrides_path,
        snoozed_path=snoozed_path,
    )


def enrich_catalog_offline(
    root_dir: str | Path = ".",
) -> dict[str, Any]:
    """纯离线结构化增强（0-Token、无模型依赖）。"""
    return enrich_catalog(root_dir)


__all__ = [
    "sync_config_offline",
    "enrich_catalog_offline",
]


@catalog_task
def sync_config_to_catalog(
    root_dir: str | Path = ".",
    catalog_path: str | Path | None = None,
    public_catalog_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    snoozed_path: str | Path | None = None,
) -> dict:
    """仅同步 overrides.json 和 snoozed.json 到已有的 catalog.json。

    严格遵循设计原则：
    - 不联网、0 模型调用、不消耗 Token、不写账本；
    - 独立分流，完全不依赖 LLM API Key 或模型凭据；
    - 绝不修改条目的原评估内容（中文简述、分类、评估证据等）和原检查时间；
    - 自动剔除过期或已撤销条目上的残留 snooze 标记。
    """
    from .overrides import apply_manual_overrides, get_manual_exclusions, get_manual_picks, load_overrides, validate_overrides
    from .snooze import apply_snooze_overrides, get_active_snoozed, load_snooze, validate_snooze

    root = Path(root_dir)
    data_file = Path(catalog_path) if catalog_path else root / "data" / "catalog.json"
    public_file = Path(public_catalog_path) if public_catalog_path else root / "public" / "data" / "catalog.json"
    overrides_file = Path(overrides_path) if overrides_path else root / "config" / "overrides.json"
    snooze_file = Path(snoozed_path) if snoozed_path else root / "config" / "snoozed.json"

    if not data_file.exists():
        raise FileNotFoundError(f"主索引文件不存在：{data_file}")

    catalog = json.loads(data_file.read_text(encoding="utf-8"))
    entries = catalog.get("entries") or []
    known_skill_ids = {e.get("skill_id") for e in entries if e.get("skill_id")}

    overrides = load_overrides(overrides_file)
    override_errors = validate_overrides(overrides, known_skill_ids)
    if override_errors:
        raise ValueError("overrides.json 校验失败：" + "；".join(override_errors))

    active_picks = get_manual_picks(overrides)
    active_exclusions = get_manual_exclusions(overrides)

    snooze_cfg = load_snooze(snooze_file)
    snooze_errors = validate_snooze(
        snooze_cfg,
        known_skill_ids=known_skill_ids,
        active_pick_ids=set(active_picks.keys()),
        active_exclusion_ids=set(active_exclusions.keys()),
    )
    if snooze_errors:
        raise ValueError("snoozed.json 校验失败：" + "；".join(snooze_errors))

    # 应用人工收藏与黑名单
    apply_manual_overrides(entries, overrides)
    # 应用活跃冷冻（自动清理非活跃的残留 snooze）
    apply_snooze_overrides(entries, snooze_cfg)

    catalog["overrides"] = overrides
    catalog["snoozed"] = snooze_cfg

    # 重新计算各分类统计
    rec = sum(1 for e in entries if e.get("status") == "recommended" and not e.get("manual_pick"))
    cand = sum(1 for e in entries if e.get("status") == "candidate" and not e.get("manual_pick"))
    manual = sum(1 for e in entries if e.get("manual_pick"))
    excl = sum(1 for e in entries if e.get("status") == "excluded")
    catalog["counts"] = {
        "recommended": rec,
        "candidate": cand,
        "manual": manual,
        "excluded": excl,
    }

    manifest = write_catalog(catalog, data_path=data_file, public_path=public_file)
    manifest["counts"]["excluded"] = excl
    active_snoozed_count = len(get_active_snoozed(snooze_cfg))
    manifest["active_snoozed"] = active_snoozed_count
    return manifest


@catalog_task
def enrich_catalog(root: Path | str) -> dict[str, Any]:
    """对主索引文件执行离线结构化增强，并同步更新页面数据。

    完全离线、幂等、不改变 summary_zh，安全可重复执行。
    """
    root_path = Path(root).resolve()
    catalog_file = root_path / "data" / "catalog.json"
    public_file = root_path / "public" / "data" / "catalog.json"

    if not catalog_file.exists():
        raise FileNotFoundError(f"未找到主索引文件：{catalog_file}")

    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    raw_entries = catalog.get("entries", [])

    enriched_entries: list[dict] = []
    with_summary = 0
    with_skill_type = 0
    with_example_requests = 0
    with_key_features = 0

    for item in raw_entries:
        enriched = enrich_entry(item)
        enriched_entries.append(enriched)

        if enriched.get("summary_zh"):
            with_summary += 1
        if enriched.get("skill_type"):
            with_skill_type += 1
        if enriched.get("example_requests"):
            with_example_requests += 1
        if enriched.get("key_features"):
            with_key_features += 1

    ctx = CatalogContext(
        rules_version=catalog.get("rules_version"),
        generated_at=catalog.get("generated_at"),
    )
    new_catalog = build_catalog(
        enriched_entries,
        context=ctx,
        overrides=catalog.get("overrides"),
        snoozed=catalog.get("snoozed"),
    )

    write_catalog(new_catalog, data_path=catalog_file, public_path=public_file)

    return {
        "total": len(enriched_entries),
        "with_summary": with_summary,
        "with_skill_type": with_skill_type,
        "with_example_requests": with_example_requests,
        "with_key_features": with_key_features,
        "catalog_path": str(catalog_file),
        "page_path": str(public_file),
    }
