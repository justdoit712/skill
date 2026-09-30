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
    sources_file = root / "config" / "discovery" / "sources.json"
    if not sources_file.exists():
        sources_file = root / "config" / "sources.json"
    sources = read_json(sources_file, default={})
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
        if previous:
            # A later observation or evaluation must never be replaced by an older result,
            # regardless of whether the content fingerprint is identical or changed.
            from datetime import datetime

            prev_rules = previous.get("evaluation_rules_version")
            rec_rules = record.get("rules_version")
            if prev_rules and rec_rules:
                try:
                    p_tuple = tuple(int(x) for x in str(prev_rules).split(".") if x.isdigit())
                    r_tuple = tuple(int(x) for x in str(rec_rules).split(".") if x.isdigit())
                    if p_tuple > r_tuple:
                        continue
                except Exception:
                    pass

            prev_time = previous.get("evaluated_at") or previous.get("last_checked")
            rec_time = outcome.get("evaluated_at") or record.get("updated_at")
            try:
                if prev_time and rec_time:
                    prev_dt = datetime.fromisoformat(prev_time)
                    rec_dt = datetime.fromisoformat(rec_time)
                    if prev_dt.tzinfo is not None and rec_dt.tzinfo is None:
                        rec_dt = rec_dt.replace(tzinfo=prev_dt.tzinfo)
                    elif prev_dt.tzinfo is None and rec_dt.tzinfo is not None:
                        prev_dt = prev_dt.replace(tzinfo=rec_dt.tzinfo)
                    if prev_dt >= rec_dt:
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
    # Only align an exact current evaluation, never an older result for the same skill.
    pool_file = root / "data" / "local" / "pool.json"
    pool = None
    pool_dirty = False
    if pool_file.exists():
        from .pool import load_pool, save_pool, STATUS_PENDING, STATUS_DONE
        from .config import load_all_config
        from .evaluation import evaluation_id
        from .budget import evaluation_filename

        cfg = load_all_config(root / "config")
        pool = load_pool(pool_file)
        for item in pool.items:
            candidate = item.candidate
            if item.status != STATUS_PENDING or not candidate.content_fingerprint:
                continue
            eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
            # Local records take precedence, including incomplete local records.
            name = evaluation_filename(eid)
            local_record = data / "local" / "state" / "evaluations" / name
            record = read_json(local_record if local_record.exists()
                               else data / "state" / "evaluations" / name, default={})
            if (record.get("evaluation_id") == eid
                    and record.get("skill_id") == candidate.skill_id
                    and record.get("content_fingerprint") == candidate.content_fingerprint
                    and record.get("status") == "completed"
                    and (record.get("outcome") or {}).get("evaluation")
                    and (entries.get(candidate.skill_id) or {}).get("last_evaluation_id") == eid):
                item.status = STATUS_DONE
                pool_dirty = True

    # Preserve the already committed manual policy; config synchronization is separate.
    values = list(entries.values())
    apply_manual_overrides(values, catalog.get("favorites") or catalog.get("overrides") or {},
                           catalog.get("overrides") or {})
    apply_snooze_overrides(values, catalog.get("snoozed"))
    if restored:
        updated = build_catalog(values, context=context, favorites=catalog.get("favorites"), overrides=catalog.get("overrides"), snoozed=catalog.get("snoozed"), owned=catalog.get("owned"))
        write_catalog(updated, data_path=data / "catalog.json", public_path=root / "public" / "data" / "catalog.json")
    else:
        recover_catalog_projections(root)
    # Publish first. Any pool failure remains visible and the operation can be replayed.
    if pool_dirty:
        save_pool(pool_file, pool)
    return {"restored": restored, "skipped": skipped, "model_calls": 0}


def sync_config_offline(
    root_dir: str | Path = ".",
    *,
    catalog_path: str | Path | None = None,
    public_catalog_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    snoozed_path: str | Path | None = None,
    owned_path: str | Path | None = None,
) -> dict[str, Any]:
    """纯离线同步配置规则到目录与页面公开数据（0-Token、无模型依赖）。"""
    return sync_config_to_catalog(
        root_dir=root_dir,
        catalog_path=catalog_path,
        public_catalog_path=public_catalog_path,
        overrides_path=overrides_path,
        snoozed_path=snoozed_path,
        owned_path=owned_path,
    )


def enrich_catalog_offline(
    root_dir: str | Path = ".",
) -> dict[str, Any]:
    """纯离线结构化增强（0-Token、无模型依赖）。"""
    return enrich_catalog(root_dir)


__all__ = [
    "sync_config_offline",
    "enrich_catalog_offline",
    "reconcile_pool",
    "resume_candidate",
]


@catalog_task
def sync_config_to_catalog(
    root_dir: str | Path = ".",
    catalog_path: str | Path | None = None,
    public_catalog_path: str | Path | None = None,
    favorites_path: str | Path | None = None,
    overrides_path: str | Path | None = None,
    snoozed_path: str | Path | None = None,
    owned_path: str | Path | None = None,
) -> dict:
    """仅同步 favorites.json, overrides.json 和 snoozed.json 到已有的 catalog.json。

    严格遵循设计原则：
    - 不联网、0 模型调用、不消耗 Token、不写账本；
    - 独立分流，完全不依赖 LLM API Key 或模型凭据；
    - 绝不修改条目的原评估内容（中文简述、分类、评估证据等）和原检查时间；
    - 自动剔除过期或已撤销条目上的残留 snooze 标记。
    """
    from .favorites import get_manual_picks, load_favorites, validate_favorites
    from .overrides import apply_manual_overrides, get_manual_exclusions, load_overrides, validate_overrides
    from .snooze import apply_snooze_overrides, get_active_snoozed, load_snooze, validate_snooze
    from src.infra.owned import load_owned_config

    root = Path(root_dir)
    data_file = Path(catalog_path) if catalog_path else root / "data" / "catalog.json"
    public_file = Path(public_catalog_path) if public_catalog_path else root / "public" / "data" / "catalog.json"
    def _res(fname: str, sub: str) -> Path:
        sub_p = root / "config" / sub / fname
        flat_p = root / "config" / fname
        if flat_p.exists() and sub_p.exists():
            try:
                return flat_p if flat_p.stat().st_mtime >= sub_p.stat().st_mtime else sub_p
            except OSError:
                return flat_p
        if flat_p.exists():
            return flat_p
        return sub_p

    favorites_file = Path(favorites_path) if favorites_path else _res("favorites.json", "governance")
    overrides_file = Path(overrides_path) if overrides_path else _res("overrides.json", "governance")
    snooze_file = Path(snoozed_path) if snoozed_path else _res("snoozed.json", "governance")
    owned_file = Path(owned_path) if owned_path else _res("owned-skills.json", "governance")

    if not data_file.exists():
        raise FileNotFoundError(f"主索引文件不存在：{data_file}")

    catalog = json.loads(data_file.read_text(encoding="utf-8"))
    entries = catalog.get("entries") or []
    known_skill_ids = {e.get("skill_id") for e in entries if e.get("skill_id")}
    # 已收录技能可能已从主索引隐藏，仍是可信的收藏身份，拆分配置不能丢掉它们。
    owned_cfg = load_owned_config(owned_file)
    known_skill_ids.update(item["skill_id"] for item in owned_cfg.get("items", []))
    # 历史人工策略可能指向已不在主索引中的技能；认可已提交的身份，不放行新拼写错误。
    for saved_config, collection in (
        (catalog.get("favorites") or {}, "manual_picks"),
        (catalog.get("overrides") or {}, "manual_picks"),
        (catalog.get("overrides") or {}, "manual_exclusions"),
        (catalog.get("snoozed") or {}, "snoozed"),
    ):
        items = saved_config if isinstance(saved_config, list) else saved_config.get(collection, [])
        known_skill_ids.update(item["skill_id"] for item in items if item.get("skill_id"))

    favorites = load_favorites(favorites_file)
    fav_errors = validate_favorites(favorites, known_skill_ids)
    if fav_errors:
        raise ValueError("favorites.json 校验失败：" + "；".join(fav_errors))

    overrides = load_overrides(overrides_file)
    override_errors = validate_overrides(overrides, known_skill_ids)
    if override_errors:
        raise ValueError("overrides.json 校验失败：" + "；".join(override_errors))

    active_picks = get_manual_picks(favorites)
    active_exclusions = get_manual_exclusions(overrides)

    conflict_ids = set(active_picks.keys()).intersection(set(active_exclusions.keys()))
    if conflict_ids:
        raise ValueError("跨文件互斥校验失败：skill_id 同时存在于收藏区与排除区：" + "；".join(sorted(conflict_ids)))

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
    apply_manual_overrides(entries, favorites, active_exclusions)
    # 应用活跃冷冻（自动清理非活跃的残留 snooze）
    apply_snooze_overrides(entries, snooze_cfg)

    catalog["favorites"] = favorites
    catalog["overrides"] = overrides
    catalog["snoozed"] = snooze_cfg
    catalog["owned"] = owned_cfg

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
    manifest["owned_count"] = len(owned_cfg.get("items", []))
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
        favorites=catalog.get("favorites"),
        overrides=catalog.get("overrides"),
        snoozed=catalog.get("snoozed"),
        owned=catalog.get("owned"),
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


@catalog_task
def reconcile_pool(root_dir: str | Path = ".", *, apply: bool = False) -> dict[str, Any]:
    """离线对账：精确比对本地候选池与账本记录，对齐异常及已授权恢复状态。

    不联网、不调用模型、不修改用量或删除账本。
    默认只预览 (--dry-run)；应用时持有目录任务锁，按“迁移清单 -> 备份池 -> 写入池 -> 完成清单”推进。
    """
    import shutil
    from uuid import uuid4
    from .pool import (
        load_pool,
        save_pool,
        STATUS_PENDING,
        STATUS_LENGTH_EXCEEDED,
        STATUS_BLOCKED,
        update_candidate_status,
    )
    from .budget import evaluation_filename
    from .config import load_all_config
    from .evaluation import evaluation_id
    from src.infra.files import write_json_atomic
    from src.shared.runtime import now_local

    root = Path(root_dir).resolve()
    pool_path = root / "data" / "local" / "pool.json"
    if not pool_path.exists():
        return {"status": "no_pool", "reconciled": 0, "apply": apply, "changes": [], "unverified": []}

    pool = load_pool(pool_path)
    cfg = load_all_config(root / "config")

    records_by_eid: dict[str, dict] = {}
    records_by_identity: dict[tuple, dict] = {}
    for state_dir in (root / "data" / "local" / "state", root / "data" / "state"):
        eval_dir = state_dir / "evaluations"
        if eval_dir.exists():
            for p in eval_dir.glob("*.json"):
                try:
                    rec = read_json(p)
                    eid = rec.get("evaluation_id")
                    if eid:
                        # 与运行器一致：先读本地记录，Actions 仅作缺失时的回退。
                        records_by_eid.setdefault(eid, rec)
                    sk_id = rec.get("skill_id")
                    fp = rec.get("content_fingerprint")
                    rv = rec.get("rules_version")
                    mv = rec.get("model_config_version")
                    if sk_id and fp:
                        records_by_identity.setdefault((sk_id, fp, str(rv or ""), str(mv or "")), rec)
                except Exception:
                    continue

    changes: list[dict] = []
    unverified: list[dict] = []

    for item in pool.items:
        candidate = item.candidate
        fp = candidate.content_fingerprint
        rules_v = str(cfg["rules"].get("rules_version") or "")
        model_v = str(cfg["model"].get("model_config_version") or "")
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"]) if fp else None
        record = (records_by_eid.get(eid) or records_by_identity.get((candidate.skill_id, fp, rules_v, model_v))) if (eid and fp) else None

        if item.status == STATUS_PENDING:
            if not fp:
                unverified.append({
                    "seq": item.seq,
                    "skill_id": candidate.skill_id,
                    "reason": "missing_fingerprint",
                })
                continue

            if not record:
                continue

            # 身份冲突检查：记录所属 skill_id 与当前候选不一致
            if record.get("skill_id") and record.get("skill_id") != candidate.skill_id:
                unverified.append({
                    "seq": item.seq,
                    "skill_id": candidate.skill_id,
                    "evaluation_id": eid,
                    "reason": "identity_conflict",
                })
                continue

            # 材料冲突检查：记录内容指纹与候选当前指纹不一致
            if record.get("content_fingerprint") and record.get("content_fingerprint") != fp:
                unverified.append({
                    "seq": item.seq,
                    "skill_id": candidate.skill_id,
                    "evaluation_id": eid,
                    "reason": "material_conflict",
                })
                continue

            error = record.get("error") or {}
            reason_code = error.get("reason_code") or record.get("reason_code")
            requests_list = record.get("requests") or []
            has_length = (
                reason_code == "LENGTH_EXCEEDED"
                or any(r.get("reason_code") == "LENGTH_EXCEEDED" or r.get("status") == "length_exceeded" for r in requests_list)
            )

            if has_length:
                changes.append({
                    "seq": item.seq,
                    "skill_id": candidate.skill_id,
                    "content_fingerprint": fp,
                    "evaluation_id": eid,
                    "previous_status": item.status,
                    "target_status": STATUS_LENGTH_EXCEEDED,
                    "reason": "LENGTH_EXCEEDED",
                })
                if apply:
                    update_candidate_status(pool, item.seq, STATUS_LENGTH_EXCEEDED)
            elif record.get("status") in ("failed", "needs_recovery"):
                retryable = error.get("retryable") or record.get("retryable")
                attempts = int(record.get("attempts") or 0)
                max_attempts = int(record.get("max_attempts") or 2)
                if not retryable or attempts >= max_attempts or record.get("status") == "needs_recovery":
                    block_info = {
                        "evaluation_id": eid,
                        "reason": "RECONCILED_FAILURE",
                        "reason_code": reason_code,
                        "error_kind": error.get("error_kind") or record.get("error_kind") or (
                            "LEGACY_PARSE_UNKNOWN" if reason_code == "PARSE_ERROR" else None
                        ),
                        "stage": record.get("stage"),
                        "http_status": error.get("http_status"),
                        "blocked_at": now_local().isoformat(),
                        "source": "reconcile",
                        "model": record.get("model") or cfg["model"].get("model"),
                        "model_config_version": model_v,
                    }
                    changes.append({
                        "seq": item.seq,
                        "skill_id": candidate.skill_id,
                        "content_fingerprint": fp,
                        "evaluation_id": eid,
                        "previous_status": item.status,
                        "target_status": STATUS_BLOCKED,
                        "block_info": block_info,
                        "reason": reason_code or "NON_RETRYABLE_FAILURE",
                    })
                    if apply:
                        item.block_info = block_info
                        update_candidate_status(pool, item.seq, STATUS_BLOCKED)
            else:
                unverified.append({
                    "seq": item.seq,
                    "skill_id": candidate.skill_id,
                    "evaluation_id": eid,
                    "reason": "missing_reason",
                })

        elif item.status == STATUS_BLOCKED:
            # 若条目在池中为 blocked，但账本中已被显式授予恢复且 attempts < max_attempts，则对齐为 pending
            if record and record.get("resume_history") and record.get("retryable"):
                attempts = int(record.get("attempts") or 0)
                max_attempts = int(record.get("max_attempts") or 2)
                if attempts < max_attempts:
                    changes.append({
                        "seq": item.seq,
                        "skill_id": candidate.skill_id,
                        "content_fingerprint": fp,
                        "evaluation_id": eid,
                        "previous_status": item.status,
                        "target_status": STATUS_PENDING,
                        "reason": "RESUMED_LEDGER_RECORD",
                    })
                    if apply:
                        item.block_info = None
                        update_candidate_status(pool, item.seq, STATUS_PENDING)

    backup_path = None
    manifest_id = None
    manifest_path = None
    manifest_dir = root / "data" / "local" / "state" / "migrations"
    unfinished = []
    for path in sorted(manifest_dir.glob("reconcile_*.json")):
        manifest = read_json(path)
        if manifest.get("status") != "in_progress":
            continue
        # The pool above contains either the committed target or a newly validated
        # transition from the current ledger. Do not replay stale saved decisions.
        for change in manifest.get("changes", []):
            matches = [item for item in pool.items
                       if item.candidate.skill_id == change.get("skill_id")]
            if (len(matches) != 1
                    or matches[0].candidate.content_fingerprint != change.get("content_fingerprint")
                    or evaluation_id(matches[0].candidate, cfg["model"], cfg["rules"]) != change.get("evaluation_id")
                    or matches[0].status != change.get("target_status")):
                if apply:
                    raise ValueError(f"未完成迁移与当前状态冲突，需核实：{path.name}")
                break
        else:
            unfinished.append((path, manifest))

    if apply and changes:
        timestamp_str = now_local().strftime('%Y%m%d_%H%M%S')
        manifest_id = f"reconcile_{timestamp_str}_{uuid4().hex[:8]}"
        manifest_dir = root / "data" / "local" / "state" / "migrations"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_file = manifest_dir / f"{manifest_id}.json"

        backup_path = pool_path.parent / f"pool.backup.{manifest_id}.json"
        shutil.copyfile(pool_path, backup_path)

        # 阶段 1：持久化操作记录（in_progress）
        manifest_data = {
            "manifest_version": "1.0.0",
            "manifest_id": manifest_id,
            "operation": "reconcile_pool",
            "created_at": now_local().isoformat(),
            "completed_at": None,
            "status": "in_progress",
            "apply": True,
            "backup_path": str(backup_path),
            "pool_path": str(pool_path),
            "changes": changes,
            "unverified": unverified,
        }
        write_json_atomic(manifest_file, manifest_data)
        manifest_path = str(manifest_file)

        # 阶段 2：写入业务状态（候选池）
        save_pool(pool_path, pool)

        # 阶段 3：标记迁移完成
        manifest_data["status"] = "completed"
        manifest_data["completed_at"] = now_local().isoformat()
        write_json_atomic(manifest_file, manifest_data)

    if apply:
        for path, manifest in unfinished:
            manifest["status"] = "completed"
            manifest["completed_at"] = now_local().isoformat()
            write_json_atomic(path, manifest)

    return {
        "status": "ok",
        "reconciled": len(changes),
        "apply": apply,
        "backup_path": str(backup_path) if backup_path else None,
        "manifest_id": manifest_id,
        "manifest_path": manifest_path,
        "recovered_manifests": [str(path) for path, _ in unfinished] if apply else [],
        "changes": changes,
        "unverified": unverified,
    }


@catalog_task
def resume_candidate(
    root_dir: str | Path = ".",
    *,
    evaluation_id: str,
    reason: str,
    extra_attempts: int = 1,
    event_id: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """显式恢复接口：解除特定条目的 blocked 状态并授予额外尝试额度 (§7.2)。

    恢复事件追加到本地账本，保留历史请求、用量与 attempts，绝不将尝试清零。
    支持稳定 event_id 保证中断重放幂等，独立记录迁移清单。
    """
    import shutil
    from uuid import uuid4
    from .pool import load_pool, save_pool, STATUS_PENDING, STATUS_BLOCKED
    from .budget import evaluation_filename
    from src.infra.files import write_json_atomic
    from src.shared.runtime import now_local

    if not evaluation_id or not evaluation_id.strip():
        raise ValueError("必须指定 --evaluation-id")
    if not reason or not reason.strip():
        raise ValueError("必须指定恢复原因 --reason")
    if extra_attempts < 1:
        raise ValueError("额外尝试次数必须 >= 1")
    if event_id:
        import re
        if not re.match(r"^[a-zA-Z0-9_-]{1,128}$", event_id):
            raise ValueError("事件 ID 仅允许字母、数字、下划线和连字符，且最长 128 字符")

    root = Path(root_dir).resolve()
    pool_path = root / "data" / "local" / "pool.json"
    if not pool_path.exists():
        raise FileNotFoundError("候选池文件不存在")

    pool = load_pool(pool_path)
    matched_item = None
    for item in pool.items:
        b_info = item.block_info or {}
        if b_info.get("evaluation_id") == evaluation_id:
            matched_item = item
            break

    hash_fn = evaluation_filename(evaluation_id)
    local_rec_path = root / "data" / "local" / "state" / "evaluations" / hash_fn
    actions_rec_path = root / "data" / "state" / "evaluations" / hash_fn
    is_from_actions = not local_rec_path.exists() and actions_rec_path.exists()
    rec_path = local_rec_path if local_rec_path.exists() else actions_rec_path if actions_rec_path.exists() else None
    if not rec_path:
        raise FileNotFoundError(f"未找到评估记录文件：{hash_fn}")

    record = read_json(rec_path)
    if record.get("evaluation_id") != evaluation_id:
        raise ValueError("身份冲突：账本评估 ID 与请求不一致")
    if matched_item is None:
        matches = [item for item in pool.items
                   if item.candidate.skill_id == record.get("skill_id")
                   and item.candidate.content_fingerprint == record.get("content_fingerprint")]
        if len(matches) != 1:
            raise ValueError("候选身份缺失或存在歧义，无法恢复")
        matched_item = matches[0]

    # 来源审计约束：Actions 来源必须具备可信身份与材料指纹
    if is_from_actions:
        if not record.get("skill_id") or not record.get("content_fingerprint"):
            raise ValueError("缺少可信依据：Actions 来源记录缺少必要身份或材料指纹，拒绝恢复")

    # 身份冲突检查
    if record.get("skill_id") and record.get("skill_id") != matched_item.candidate.skill_id:
        raise ValueError(f"身份冲突：记录 skill_id ({record.get('skill_id')}) 与候选 ({matched_item.candidate.skill_id}) 不一致")

    # 材料冲突检查
    if record.get("content_fingerprint") and matched_item.candidate.content_fingerprint:
        if record.get("content_fingerprint") != matched_item.candidate.content_fingerprint:
            raise ValueError(f"材料冲突：候选材料指纹 ({matched_item.candidate.content_fingerprint}) 与记录指纹 ({record.get('content_fingerprint')}) 不一致")

    # 结果状态约束：已完成条目禁止恢复
    if record.get("status") == "completed":
        raise ValueError("条目评估已完成 (completed)，无需且不可恢复")

    # 幂等性与事件 ID 处理
    manifest_dir = root / "data" / "local" / "state" / "migrations"
    in_progress_event_id = None
    if manifest_dir.exists():
        for mf in manifest_dir.glob("resume_*.json"):
            try:
                mdata = read_json(mf)
                if mdata and mdata.get("status") == "in_progress" and mdata.get("evaluation_id") == evaluation_id:
                    in_progress_event_id = mdata.get("event_id")
                    break
            except Exception:
                continue

    target_event_id = event_id or in_progress_event_id or f"evt_{now_local().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_event_id):
        raise ValueError("event_id 只能包含字母、数字、下划线和连字符，最长 128 字符")
    manifest_file = manifest_dir / f"resume_{target_event_id}.json"
    previous_manifest = read_json(manifest_file, default={})
    if previous_manifest and previous_manifest.get("evaluation_id") != evaluation_id:
        raise ValueError("event_id 已关联其他评估")
    existing_event = next((e for e in record.get("resume_history", []) if e.get("event_id") == target_event_id), None)
    is_idempotent_replay = existing_event is not None
    authorized = existing_event or previous_manifest
    if authorized and (authorized.get("reason") != reason.strip()
                       or authorized.get("extra_attempts") != extra_attempts):
        raise ValueError("同一 event_id 的原因和额外尝试次数必须与原授权一致")
    if matched_item.status not in (STATUS_BLOCKED, STATUS_PENDING):
        raise ValueError("候选状态已变化，不能重放旧恢复事件")

    if is_idempotent_replay:
        # 重放此前已授权的事件：不重复增加额度
        current_max_attempts = int(existing_event.get("previous_max_attempts") or record.get("max_attempts") or 2)
        new_max_attempts = int(record.get("max_attempts") or existing_event.get("new_max_attempts"))
    else:
        if matched_item.status != STATUS_BLOCKED:
            raise ValueError(f"候选 #{matched_item.seq} 当前状态为 {matched_item.status}，非 blocked")
        current_max_attempts = int(record.get("max_attempts") or 2)
        new_max_attempts = current_max_attempts + extra_attempts

    if is_idempotent_replay and matched_item.status == STATUS_PENDING:
        # 已经完整恢复过的幂等重放
        if apply and previous_manifest.get("status") == "in_progress":
            previous_manifest["status"] = "completed"
            previous_manifest["completed_at"] = now_local().isoformat()
            write_json_atomic(manifest_file, previous_manifest)
        return {
            "dry_run": not apply,
            "idempotent": True,
            "event_id": target_event_id,
            "evaluation_id": evaluation_id,
            "seq": matched_item.seq,
            "skill_id": matched_item.candidate.skill_id,
            "status": STATUS_PENDING,
            "reason": reason.strip(),
            "extra_attempts": extra_attempts,
            "new_max_attempts": new_max_attempts,
        }

    resume_event = {
        "event_id": target_event_id,
        "resumed_at": now_local().isoformat(),
        "reason": reason.strip(),
        "extra_attempts": extra_attempts,
        "previous_attempts": record.get("attempts", 0),
        "previous_max_attempts": current_max_attempts,
        "new_max_attempts": new_max_attempts,
        "source_ledger": "actions" if is_from_actions else "local",
    }

    if not apply:
        return {
            "dry_run": True,
            "event_id": target_event_id,
            "evaluation_id": evaluation_id,
            "seq": matched_item.seq,
            "skill_id": matched_item.candidate.skill_id,
            "current_status": matched_item.status,
            "target_status": STATUS_PENDING,
            "reason": reason.strip(),
            "extra_attempts": extra_attempts,
            "current_max_attempts": current_max_attempts,
            "new_max_attempts": new_max_attempts,
        }

    # 阶段 1：持久化操作清单（in_progress）
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_id = f"resume_{target_event_id}"
    manifest_file = manifest_dir / f"{manifest_id}.json"

    backup_path = pool_path.parent / f"pool.backup.{manifest_id}.json"
    if not previous_manifest:
        shutil.copyfile(pool_path, backup_path)

    manifest_data = {
        "manifest_version": "1.0.0",
        "manifest_id": manifest_id,
        "event_id": target_event_id,
        "operation": "resume_candidate",
        "created_at": now_local().isoformat(),
        "completed_at": None,
        "status": "in_progress",
        "apply": True,
        "evaluation_id": evaluation_id,
        "skill_id": matched_item.candidate.skill_id,
        "content_fingerprint": matched_item.candidate.content_fingerprint,
        "source_ledger": "actions" if is_from_actions else "local",
        "previous_status": matched_item.status,
        "target_status": STATUS_PENDING,
        "previous_max_attempts": current_max_attempts,
        "new_max_attempts": new_max_attempts,
        "extra_attempts": extra_attempts,
        "reason": reason.strip(),
        "backup_path": str(backup_path),
    }
    if previous_manifest:
        manifest_data = previous_manifest
    else:
        write_json_atomic(manifest_file, manifest_data)

    # 阶段 2：写入业务状态（账本记录，若来源于 Actions 则落盘至本地账本，绝不篡改 Actions 原件）
    if not is_idempotent_replay:
        record.setdefault("resume_history", []).append(resume_event)
    record["max_attempts"] = new_max_attempts
    record["retryable"] = True
    record["status"] = "failed"

    local_rec_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(local_rec_path, record)

    # 阶段 3：更新候选池状态
    matched_item.status = STATUS_PENDING
    matched_item.block_info = None
    save_pool(pool_path, pool)

    # 阶段 4：标记操作清单完成
    manifest_data["status"] = "completed"
    manifest_data["completed_at"] = now_local().isoformat()
    write_json_atomic(manifest_file, manifest_data)

    return {
        "dry_run": False,
        "idempotent": is_idempotent_replay,
        "manifest_id": manifest_id,
        "manifest_path": str(manifest_file),
        "event_id": target_event_id,
        "evaluation_id": evaluation_id,
        "seq": matched_item.seq,
        "skill_id": matched_item.candidate.skill_id,
        "status": STATUS_PENDING,
        "reason": reason.strip(),
        "extra_attempts": extra_attempts,
        "new_max_attempts": new_max_attempts,
        "backup_path": str(backup_path),
    }
