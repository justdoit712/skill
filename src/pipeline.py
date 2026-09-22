"""Skills 导航目录流水线入口与向后兼容适配层（薄 CLI 壳）。

阶段一（发现与预留）与阶段二（评估与写出）已解耦至：
- Phase 1: src.catalog.sync_reserve (prepare, phase_reserve, dry_run)
- Phase 2: src.catalog.sync_evaluate (phase_evaluate)
- 状态机: src.catalog.entry_state (update_entry, review_state, admission_decision)
- 队列: src.catalog.queue (ordered_pending, mark_settled)
- 配置: src.catalog.config (load_all_config, precheck)
"""

from __future__ import annotations

import argparse
import json
import sys

from src.catalog.config import load_all_config, precheck
from src.catalog.entry_state import (
    EntryUpdateEvent,
    STATUS_CANDIDATE,
    STATUS_EXCLUDED,
    STATUS_PENDING,
    STATUS_RECOMMENDED,
    UPSTREAM_GONE,
    UPSTREAM_OK,
    admission_decision,
    previous_evaluation_snapshot,
    review_state,
    update_entry,
)
from src.catalog.queue import (
    QUEUE_FILENAME,
    candidate_from_payload,
    mark_settled,
    ordered_pending as _ordered_pending,
    ordered_pending,
    prescreen_from_payload,
    read_queue,
    settled_pending,
    skill_of,
    write_queue,
)
from src.catalog.sync_evaluate import phase_evaluate
from src.catalog.sync_reserve import (
    DEFAULT_LIMIT_EVALUATIONS,
    TEXTS_DIRNAME,
    _accumulate_plan,
    _outcome_records,
    dry_run,
    phase_reserve,
    prepare,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skills 导航目录：一次完整运行")
    parser.add_argument("--phase", choices=["reserve", "evaluate", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="只验证配置与计算计划")
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--limit-evaluations", type=int, default=DEFAULT_LIMIT_EVALUATIONS)
    parser.add_argument(
        "--limit-fetches",
        type=int,
        default=None,
        help="本轮最多抓取多少条上游内容；留空则跟随本批评估名额，或取 config/rules.json 的 run_limits.max_fetches_per_run",
    )
    parser.add_argument("--expand-limit", type=int, default=None, help="本轮最多展开多少个仓库")
    parser.add_argument("--no-expand", action="store_true", help="不展开到具体技能（仅调试用）")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--public-dir", default="public")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    common = dict(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        state_dir=args.state_dir,
        limit_queries=args.limit_queries,
        limit_evaluations=args.limit_evaluations,
        limit_fetches=args.limit_fetches,
        expand=not args.no_expand,
        expand_limit=args.expand_limit,
    )

    if args.dry_run:
        result = dry_run(**{k: v for k, v in common.items() if k not in ("data_dir", "state_dir")})
    elif args.phase == "reserve":
        result = phase_reserve(**common)
    elif args.phase == "evaluate":
        result = phase_evaluate(
            config_dir=args.config_dir,
            data_dir=args.data_dir,
            public_dir=args.public_dir,
            state_dir=args.state_dir,
        )
    else:
        result = phase_reserve(**common)
        if result.get("ok"):
            evaluated = phase_evaluate(
                config_dir=args.config_dir,
                data_dir=args.data_dir,
                public_dir=args.public_dir,
                state_dir=args.state_dir,
            )
            result = {"ok": evaluated.get("ok"), "reserve": result, "evaluate": evaluated}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print(f"运行中止于 {result.get('stage')}：{result.get('problems') or result.get('error')}")
    elif result.get("dry_run"):
        print(
            f"dry_run 通过：候选 {result['candidates']}、合格 {result['queued']}、"
            f"预筛排除 {result['excluded']}、抓取 {result['fetched']}/{result['fetch_cap']}、"
            f"本批名额 {result['evaluation_slots']}、采集失败 {result['discovery_failed']}、凭据 "
            f"{'已就绪' if result['credentials_present'] else '缺失'}"
        )
    else:
        print(json.dumps(result, ensure_ascii=False)[:400])
    return 0 if result.get("ok") else 1


__all__ = [
    "DEFAULT_LIMIT_EVALUATIONS",
    "TEXTS_DIRNAME",
    "QUEUE_FILENAME",
    "load_all_config",
    "precheck",
    "dry_run",
    "prepare",
    "phase_reserve",
    "phase_evaluate",
    "review_state",
    "admission_decision",
    "previous_evaluation_snapshot",
    "update_entry",
    "EntryUpdateEvent",
    "STATUS_RECOMMENDED",
    "STATUS_CANDIDATE",
    "STATUS_EXCLUDED",
    "STATUS_PENDING",
    "UPSTREAM_OK",
    "UPSTREAM_GONE",
    "_ordered_pending",
    "_accumulate_plan",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
