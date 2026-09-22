"""Catalog 业务包：统一状态机、读写排他协调、双阶段流水线与本地运行。

导出核心状态机与存储协调器：
- EntryUpdateEvent, update_entry, review_state, admission_decision
- mutate_catalog, catalog_lock, LockConflict, recover_catalog_projections
- prepare (Phase 1 reserve), phase_evaluate (Phase 2 evaluate)
- run_local, prepare_pool, process_candidate, apply_result, save_and_render
- sync_config_offline, enrich_catalog_offline
"""

from __future__ import annotations

from .entry_state import (
    EntryUpdateEvent,
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    STATUS_RECOMMENDED,
    admission_decision,
    previous_evaluation_snapshot,
    review_state,
    update_entry,
)
from .local import (
    STOP_LABELS,
    apply_result,
    prepare_pool,
    run_local,
    save_and_render,
)
from .maintenance import (
    enrich_catalog_offline,
    sync_config_offline,
)
from .store import (
    LockConflict,
    catalog_lock,
    mutate_catalog,
    recover_catalog_projections,
)
from .sync_evaluate import phase_evaluate
from .sync_reserve import phase_reserve, prepare

__all__ = [
    "EntryUpdateEvent",
    "STATUS_CANDIDATE",
    "STATUS_EXCLUDED",
    "STATUS_PENDING",
    "STATUS_RECOMMENDED",
    "admission_decision",
    "previous_evaluation_snapshot",
    "review_state",
    "update_entry",
    "STOP_LABELS",
    "apply_result",
    "prepare_pool",
    "run_local",
    "save_and_render",
    "enrich_catalog_offline",
    "sync_config_offline",
    "LockConflict",
    "catalog_lock",
    "mutate_catalog",
    "recover_catalog_projections",
    "prepare",
    "phase_reserve",
    "phase_evaluate",
]
