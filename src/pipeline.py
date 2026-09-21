"""运行入口：把发现、预筛、额度、评估、索引与报告串成一次运行（§7.2）。

分成两个阶段，以对应 §7.2 的步骤 3 与 4-5：

- `--phase reserve`：发现 → 抓取 → 内容指纹 → 预筛 → **预留额度**，并把候选与
  已抓取材料落到 `data/state/`。此阶段的产物**必须先提交推送**，否则下次运行读不到
  已消耗额度，会造成重复计费。
- `--phase evaluate`：读取预留、逐条调用模型、**保留完整评估内容**、与既有索引合并、
  生成页面数据与周报。

`--dry-run` 只做发现与计算计划：不调用模型、不写账本、不提交、不部署（§7.3）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from .budget import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_RECOVERY,
    BudgetLedger,
    QuotaExceeded,
    now_local,
    week_id,
)
from .decide import decide
from .dedupe import content_fingerprint, dedupe
from .discover import discover, load_searches
from .evaluate import evaluate, evaluation_id, resolve_api_key
from .fetch import fetch_text
from .index import (
    STATUS_CANDIDATE,
    STATUS_RECOMMENDED,
    UPSTREAM_GONE,
    CatalogContext,
    build_catalog,
    build_entry,
    index_by_id,
    merge_entries,
    write_catalog,
)
from .models import Candidate, PrescreenResult
from .overrides import (
    apply_manual_overrides,
    apply_manual_overrides_to_entry,
    get_manual_exclusions,
    get_manual_picks,
    load_overrides,
    validate_overrides,
)
from .prescreen import DECISION_EXCLUDED, DECISION_QUEUED, load_config, prescreen
from .report import build_report, write_report

DEFAULT_LIMIT_EVALUATIONS = 50
TEXTS_DIRNAME = "texts"
QUEUE_FILENAME = "queue.json"
QUEUE_VERSION = "2.0.0"
FAILURE_RATE_ABORT = 1.0  # 全部来源失败时中止，不用空结果覆盖既有索引

# §5.2 待复核标记：醒目标记用固定文案，原因码取 rules.json 的 reason_codes.review
REVIEW_REASON_CONTENT_CHANGED = "CONTENT_CHANGED"
REVIEW_NOTE_CONTENT_CHANGED = (
    "上游内容已变化，待复核；当前评估对应的是变化前的版本，"
    "不代表对新版本的验证。"
)
REVIEW_NOTE_AWAITING_EVALUATION = "已列入待复核队列，等待额度或模型调用完成，不自动生成通过结论。"

# 每轮最多抽查多少条"已评估且内容未变"的条目：这是让已推荐内容变化最终被发现的
# 通道（§7.2 要求检查内容变化）。数量有界，避免抽查挤占新候选的评估名额。
MAX_RECHECKS_PER_RUN = 10


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_config(config_dir: str | Path = "config") -> dict:
    base = Path(config_dir)
    prescreen_cfg = load_config(base)
    model_path = base / "model.local.json"
    model_cfg = _load_json(model_path) if model_path.exists() else _load_json(base / "model.example.json")
    overrides_cfg = load_overrides(base / "overrides.json")
    return {
        "prescreen": prescreen_cfg,
        "taxonomy": prescreen_cfg.taxonomy,
        "rules": prescreen_cfg.rules,
        "searches": load_searches(str(base)),
        "sources": _load_json(base / "sources.json"),
        "model": model_cfg,
        "overrides": overrides_cfg,
        "source_types": {
            s["id"]: s.get("source_type") for s in _load_json(base / "sources.json").get("sources", [])
        },
    }


def precheck(cfg: dict) -> list[str]:
    """预检（§7.2 步骤 1）。返回问题列表；非空时必须中止，不得进入付费调用。"""
    problems: list[str] = []
    if not cfg["prescreen"].domain_names:
        problems.append("taxonomy.json 未加载到任何主分类")
    if not cfg["rules"].get("checks"):
        problems.append("rules.json 未定义检查项")
    if not cfg["searches"].get("per_domain"):
        problems.append("searches.json 未定义任何领域的查询词")
    if not cfg["model"].get("endpoint") or not cfg["model"].get("model"):
        problems.append("模型配置缺 endpoint 或 model")
    if "overrides" in cfg:
        problems.extend(validate_overrides(cfg["overrides"]))
    return problems


# --------------------------------------------------------------------------
# 准备：发现 → 抓取 → 指纹 → 预筛
# --------------------------------------------------------------------------


def _candidate_payload(candidate: Candidate, pres: PrescreenResult, fetch: dict) -> dict:
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


# --------------------------------------------------------------------------
# 队列：跨轮累积的待处理候选（§7.2 的 data/queue.json）
#
# §7.2 要求队列持久化。每轮只保留本轮快照会让超出额度的候选永远轮不到：
# 下一轮重新发现时按同样的顺序切前 N 个，后面的候选被反复跳过。因此队列跨轮累积，
# 并按 §5.2/§5.3 排序：已知内容变化或待复核的条目插队，其余条目公平轮转
# （先补齐从未处理过的，再按上次检查时间从早到晚），官方来源优先。
#
# 已评估且内容未变的条目不在队列主体里：它们只按有限名额被抽查，
# 用来发现"已推荐内容发生变化"，同时不占用新候选的评估机会。
# --------------------------------------------------------------------------


def _catalogued_entries(data_dir: Path) -> dict[str, dict]:
    """已收录条目的 skill_id → 条目，用于读取指纹与既有的待复核结论。

    队列项据此判断自己是新候选、待评估还是待复核，并且**与索引里的指纹比较**——
    条目在队列里停留多轮时，它自己记录的指纹可能已经过时（§7.2 的"检查内容变化"）。
    """
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


def _source_type_of(item: dict, source_types: dict[str, str]) -> str | None:
    for source_id in ((item.get("candidate") or {}).get("source_ids") or []):
        if source_types.get(source_id):
            return source_types[source_id]
    return None


def _skill_of(item: dict) -> str:
    return ((item.get("candidate") or {}).get("skill_id")) or ""


def _ordered_pending(
    items: list[dict],
    *,
    catalogued: dict,
    source_types: dict,
    recheck: set[str] | None = None,
    manual_picks: set[str] | None = None,
) -> list[dict]:
    """按 §5.2/§5.3 的优先级排序，并按 skill_id 去重（同一技能只排一次）。

    优先级：内容已有变化的条目 > 人工收藏条目 > 已收录待评估 > 新候选 > 本轮抽查的已评估条目。
    抽查是让"已推荐内容变化"最终能被发现的通道：已评估条目出队后不再是队列主体，
    否则积压会被同一批已评估条目反复占据，新候选永远轮不到评估。
    """
    recheck = recheck or set()
    manual_picks = manual_picks or set()

    def rank(item: dict) -> int:
        # 只在**已知**内容变化或待复核时插队；收藏条目排在普通抽查与新候选之前（§4.6）
        if item.get("content_changed") or item.get("needs_review"):
            return 0
        skill = _skill_of(item)
        if item.get("manual_pick") or (skill and skill in manual_picks):
            return 1
        return 2

    ordered: list[dict] = []
    seen: set[str] = set()
    for item in sorted(
        items,
        key=lambda i: (
            rank(i),
            0 if _source_type_of(i, source_types) == "official" else 1,
            # 先补齐从未处理过的条目，再按"上次检查时间"从早到晚推进积压：
            # 队列头部不会被同一批条目反复占用，后面的候选也能轮到（§5.3）
            0 if not i.get("last_attempted_at") else 1,
            i.get("last_attempted_at") or "",
            i.get("first_queued_at") or "",
            i.get("seq", 0),
        ),
    ):
        skill = _skill_of(item)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        ordered.append(item)
    return ordered


def prepare(
    cfg: dict,
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
    expand: bool = True,
    expand_limit: int | None = None,
    state_dir: Path | None = None,
    data_dir: Path | None = None,
    pending: list[dict] | None = None,
    ledger: BudgetLedger | None = None,
    sleep=time.sleep,
    discover_fn=discover,
    fetch_fn=fetch_text,
) -> dict:
    """发现 → 无内容预筛 → 按上限抓取 → 指纹 → 带内容复筛。不写账本、不调用模型。

    抓取只针对本批可能被评估的候选：先用**无内容**预筛筛掉明显不合格的，再抓取。
    只有真正抓到内容的候选才带指纹，也才可能进入评估批次（评估 ID 依赖内容指纹，§7.3）。
    limit_fetches 为 None 时跟随本批评估名额，因此默认不会为永不评估的候选白抓。

    pending 是上一轮遗留的持久队列；本轮名额优先分配给排在它前面的条目，因此
    超额候选会在后续运行中依次得到评估，而不是被每轮重复跳过。
    """
    candidates, outcomes = discover_fn(
        cfg["searches"],
        sources=cfg["sources"],
        expand=expand,
        expand_limit=expand_limit,
        max_queries=limit_queries,
        sleep=sleep,
    )
    merged = dedupe(candidates)

    # 第一轮预筛：不需要内容就能判定，先筛掉明显不合格的，避免浪费抓取
    first_pass = [(candidate, prescreen(candidate, cfg["prescreen"], None)) for candidate in merged]

    source_types = cfg.get("source_types") or {}
    catalogued_entries = _catalogued_entries(Path(data_dir)) if data_dir else {}
    catalogued = {skill_id: entry.get("content_fingerprint")
                  for skill_id, entry in catalogued_entries.items()}
    catalogued_review = {
        skill_id: bool(entry.get("needs_review"))
        for skill_id, entry in catalogued_entries.items()
    }
    # 队列里带指纹的条目同样视作"已见过"：数据目录缺失时（dry_run、单元测试）
    # 仍能按指纹判断内容变化，不会把变化误判成新候选
    for item in pending or []:
        skill = _skill_of(item)
        if skill and item.get("content_fingerprint"):
            catalogued.setdefault(skill, item["content_fingerprint"])
    # 抓取名额的估算需要队列长度，因此先按"已发现的合格候选 + 遗留队列"估一次，
    # 再据此决定本轮抽查名额（见 _accumulate_plan 的 cap 参数）
    cap_estimate = min(limit_evaluations, len(first_pass) + len(pending or []))
    cap = cap_estimate if limit_fetches is None else max(0, limit_fetches)

    plan = _accumulate_plan(
        first_pass, list(pending or []), catalogued=catalogued,
        catalogued_review=catalogued_review,
        source_types=source_types,
        cfg=cfg, ledger=ledger, cap=cap,
    )
    slots = min(limit_evaluations, len(plan["queue"]))

    staged: dict[str, str] = {}
    texts_dir = (state_dir / TEXTS_DIRNAME) if state_dir else None
    if texts_dir:
        texts_dir.mkdir(parents=True, exist_ok=True)

    enriched: list[tuple[Candidate, PrescreenResult, dict]] = []
    fetched_count = 0
    # 抓取名额只花在本轮需要评估的条目上（§5.3）
    for item in plan["queue"]:
        if fetched_count >= cap:
            break
        candidate = _candidate_from_payload(item["candidate"])

        fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
        text = fetched.text if (fetched.ok and fetched.text) else None
        note = {"ok": bool(text), "bytes": fetched.bytes_read, "reason_code": fetched.reason_code}
        if not fetched.ok:
            note["upstream_gone"] = fetched.reason_code == UPSTREAM_GONE
        if text:
            fresh_fingerprint = content_fingerprint(text)
            # §7：内容变化以**基线指纹**（上次实际评估到的版本）为准，
            # 不凭仓库最近提交时间判断，也不把首次抓取当作变化
            baseline = item.get("baseline_fingerprint")
            note["content_changed"] = bool(baseline and baseline != fresh_fingerprint)
            note["truncated"] = fetched.truncated
            item["content_fingerprint"] = fresh_fingerprint
            item["candidate"]["content_fingerprint"] = fresh_fingerprint
            item["content_changed"] = note["content_changed"]
            item["last_attempted_at"] = _stamp()
            fetched_count += 1
            candidate.content_fingerprint = fresh_fingerprint
            if texts_dir is not None:
                staged[candidate.skill_id] = text
        item["fetch"] = note
        item["prescreen"] = asdict(prescreen(candidate, cfg["prescreen"], text))
        enriched.append((candidate, _prescreen_from_payload(item["prescreen"]), note))

    # 超出抓取上限的合格候选：留在队列，等后续运行处理
    for item in plan["queue"][cap:]:
        candidate = _candidate_from_payload(item["candidate"])
        item["fetch"] = {
            "ok": False, "bytes": 0, "reason_code": None,
            "skipped": "超出本次抓取上限，留待后续运行",
        }
        enriched.append(
            (candidate, _prescreen_from_payload(item["prescreen"]), item["fetch"])
        )

    # 第一轮即被排除的候选仍进入索引，状态为 excluded
    for candidate, result in first_pass:
        if result.decision != DECISION_QUEUED:
            enriched.append(
                (candidate, result, {"ok": False, "bytes": 0, "reason_code": None,
                                     "skipped": "预筛排除，未抓取"})
            )

    if texts_dir is not None and staged:
        (texts_dir / "staged.json").write_text(
            json.dumps(staged, ensure_ascii=False), encoding="utf-8"
        )

    queued = [(c, p, f) for c, p, f in enriched if p.decision == DECISION_QUEUED]
    excluded = [(c, p, f) for c, p, f in enriched if p.decision != DECISION_QUEUED]
    # 没有内容指纹的候选不能进入评估批次，否则评估 ID 会退化为 nofingerprint
    batch = [(c, p, f) for c, p, f in queued if c.content_fingerprint]

    return {
        "outcomes": outcomes,
        "queued": queued,
        "batch": batch,
        "excluded": excluded,
        "queue": plan["queue"],
        "queue_pending": plan["queue_pending"],
        "carried_over": plan["carried_over"],
        "needs_review": plan["needs_review"],
        "discovery_total": len(candidates),
        "discovery_failed": sum(1 for o in outcomes if not o.ok),
        "fetch_cap": cap,
        "fetched": fetched_count,
        "evaluation_slots": min(slots, len(batch)),
    }


def _stamp() -> str:
    return now_local().replace(microsecond=0).isoformat()


def _skill_evaluation_id(skill_id: str, fingerprint: str, cfg: dict) -> str:
    """按 §7.3 的拼接规则生成某份内容的评估 ID（与 evaluate.evaluation_id 一致）。"""
    return "|".join(
        [
            skill_id,
            fingerprint,
            str(cfg["rules"].get("rules_version") or ""),
            str(cfg["model"].get("model_config_version") or ""),
        ]
    )


def _settled_for(ledger: BudgetLedger, skill_id: str, fingerprint: str, cfg: dict) -> bool:
    record = ledger.get(_skill_evaluation_id(skill_id, fingerprint, cfg)) or {}
    return record.get("status") == STATUS_COMPLETED


def _mark_settled(item: dict, cfg: dict, ledger: BudgetLedger) -> bool:
    """队列项对应的内容是否已经评估完 → 出队，不再占用名额（§5.3）。

    以队列里记录的指纹（上次抓取到的内容版本）为准，不依赖本轮是否再次抓取。
    """
    fingerprint = item.get("content_fingerprint")
    return bool(fingerprint and _settled_for(ledger, _skill_of(item), fingerprint, cfg))


def _accumulate_plan(
    first_pass: list[tuple[Candidate, PrescreenResult]],
    previous: list[dict],
    *,
    catalogued: dict,
    catalogued_review: dict,
    source_types: dict,
    cfg: dict | None = None,
    ledger: BudgetLedger | None = None,
    cap: int | None = None,
) -> dict:
    """把本轮合格候选并入持久队列，并给出按优先级排序后的队列。

    只有真正进入队列的合格候选才参与排序：被预筛排除的候选不占队列。
    索引记录的内容指纹优先于队列自己的记录——队列里的指纹可能已经过时。
    已评估且内容未变的条目不再作为队列主体（会饿死新候选），只按名额抽查。
    """
    previous_by_skill = {_skill_of(item): item for item in previous if _skill_of(item)}
    stamped = _stamp()

    def known_fingerprint(skill_id: str, *fallbacks: str | None) -> str | None:
        return catalogued.get(skill_id) or next((fp for fp in fallbacks if fp), None)

    def settled(skill_id: str, fingerprint: str | None) -> bool:
        return bool(
            ledger is not None
            and fingerprint
            and _settled_for(ledger, skill_id, fingerprint, cfg or {})
        )

    manual_picks_dict = get_manual_picks((cfg or {}).get("overrides") or {})
    manual_picks_set = set(manual_picks_dict.keys())
    max_manual_checks = int(((cfg or {}).get("rules") or {}).get("run_limits", {}).get("max_manual_checks_per_run", 10) or 10)

    fresh: dict[str, dict] = {}
    manual_recheck: list[str] = []
    recheck: list[str] = []
    rechecked: set[str] = set()
    for index, (candidate, prescreen_result) in enumerate(first_pass):
        if prescreen_result.decision != DECISION_QUEUED:
            continue
        old = previous_by_skill.get(candidate.skill_id) or {}
        baseline = known_fingerprint(
            candidate.skill_id, candidate.content_fingerprint, old.get("baseline_fingerprint")
        )
        if settled(candidate.skill_id, baseline):
            # 已评估且内容与基线一致：不作为队列主体。按名额抽查（§4.6、§5.3）
            if candidate.skill_id in manual_picks_set:
                if len(manual_recheck) >= max_manual_checks:
                    continue
                manual_recheck.append(candidate.skill_id)
                rechecked.add(candidate.skill_id)
            else:
                if len(recheck) >= MAX_RECHECKS_PER_RUN:
                    continue
                recheck.append(candidate.skill_id)
                rechecked.add(candidate.skill_id)
        item = _candidate_payload(
            candidate, prescreen_result,
            {"ok": False, "bytes": 0, "reason_code": None, "skipped": "尚未抓取"},
        )
        item["baseline_fingerprint"] = baseline
        item["content_fingerprint"] = old.get("content_fingerprint")
        item["candidate"]["content_fingerprint"] = item["content_fingerprint"]
        item["recheck"] = candidate.skill_id in rechecked
        item["manual_pick"] = candidate.skill_id in manual_picks_set
        # seq 只从队列继承；新候选按本轮发现顺序排在后面，避免抢到不该有的靠前顺位
        item["seq"] = old.get("seq", len(previous) + index)
        item["first_queued_at"] = old.get("first_queued_at") or stamped
        item["last_attempted_at"] = old.get("last_attempted_at")
        item["needs_review"] = bool(catalogued_review.get(candidate.skill_id))
        item["pending"] = True
        fresh[candidate.skill_id] = item

    # 抽查条目只能用"超出新候选份额"的抓取名额：否则每次运行都被已评估条目占满，
    # 新候选永远得不到评估（§5.3）。名额不足时直接不排本轮抽查，下轮再说。
    regular = [item for item in fresh.values() if not item.get("recheck")]
    ordered_regular = _ordered_pending(
        regular, catalogued=catalogued, source_types=source_types, manual_picks=manual_picks_set
    )
    rechecks = sorted(rechecked)
    if cap is not None and len(ordered_regular) >= cap:
        for skill_id in rechecks:
            fresh.pop(skill_id, None)
        rechecks = []

    kept = [
        item
        for skill_id, item in previous_by_skill.items()
        if skill_id and skill_id not in fresh
    ]
    for item in kept:
        item["pending"] = True
        item.setdefault("first_queued_at", stamped)
        item.setdefault("baseline_fingerprint", catalogued.get(_skill_of(item)))

    queue = _ordered_pending(
        kept + list(fresh.values()), catalogued=catalogued,
        source_types=source_types, recheck=rechecked,
    )
    return {
        "queue": queue,
        "queue_pending": len(queue),
        "carried_over": sum(1 for item in queue if _skill_of(item) not in fresh),
        "needs_review": sum(1 for item in queue if item.get("content_changed")),
        "recheck": sorted(rechecked),
    }


def _outcome_records(outcomes) -> list[dict]:
    return [
        {
            "query": o.query.q,
            "domain": o.query.domain_id,
            "ok": o.ok,
            "total_count": o.total_count,
            "candidates": len(o.candidates),
            "reason_code": o.reason_code,
            "error": o.error,
        }
        for o in outcomes
    ]


def _write_queue(plan: dict, state_dir: Path, week: str, generated_at: str, pending: list[dict]) -> None:
    """写出持久队列。queue 是待处理队列，excluded 只记录本轮预筛结论供审计。"""
    payload = {
        "queue_version": QUEUE_VERSION,
        "week": week,
        "generated_at": generated_at,
        "pending": pending,
        "excluded": [
            _candidate_payload(c, p, f) for c, p, f in plan["excluded"]
        ],
        "outcomes": _outcome_records(plan["outcomes"]),
        "evaluation_slots": plan["evaluation_slots"],
    }
    path = state_dir / QUEUE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_queue(state_dir: Path) -> dict:
    return _load_json(state_dir / QUEUE_FILENAME)


def _queue_pending(queue: dict | None) -> list[dict]:
    """读取待处理队列，兼容旧版快照格式（旧格式没有跨轮语义，只取本轮 queued）。"""
    if not queue:
        return []
    if queue.get("queue_version"):
        return list(queue.get("pending") or [])
    return [{"candidate": item.get("candidate"), "prescreen": item.get("prescreen"),
             "fetch": item.get("fetch"), "pending": True}
            for item in queue.get("queued") or []]


def _settled_pending(pending: list[dict], cfg: dict, ledger: BudgetLedger) -> list[dict]:
    """已了结的条目出队：这份内容已评估完、已达尝试上限、或结果不明需人工确认。

    使用额度上限或模型调用失败的条目**留在队列**，下周排队时占新周名额（§7.3），
    不因为在队列里就自动获得通过结论。
    """
    remaining: list[dict] = []
    for item in pending:
        # 抽查的条目内容未变：账本里已有这份内容的完成记录，直接出队（§5.3）
        if _mark_settled(item, cfg, ledger):
            continue
        candidate = _candidate_from_payload(item["candidate"])
        evaluation = evaluation_id(candidate, cfg["model"], cfg["rules"])
        record = ledger.get(evaluation) or {}
        status = record.get("status")
        done = status == STATUS_NEEDS_RECOVERY or (
            status == STATUS_FAILED
            and int(record.get("attempts") or 0)
            >= int(record.get("max_attempts") or ledger.max_attempts)
        )
        if not done:
            remaining.append(item)
    return remaining


def _candidate_from_payload(payload: dict) -> Candidate:
    return Candidate(**payload)


def _prescreen_from_payload(payload: dict) -> PrescreenResult:
    return PrescreenResult(**payload)


def _has_previous_catalog(data_dir: Path) -> bool:
    path = data_dir / "catalog.json"
    if not path.exists():
        return False
    try:
        return bool(_load_json(path).get("entries"))
    except (OSError, json.JSONDecodeError):
        return False


def _previous_entries(data_dir: Path) -> list[dict]:
    path = data_dir / "catalog.json"
    if not path.exists():
        return []
    try:
        return _load_json(path).get("entries") or []
    except (OSError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------
# 阶段一：预留
# --------------------------------------------------------------------------


def phase_reserve(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    state_dir: str | Path | None = None,
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
    expand: bool = True,
    expand_limit: int | None = None,
    sleep=time.sleep,
    discover_fn=discover,
    fetch_fn=fetch_text,
) -> dict:
    """发现、预筛并预留额度。产物必须先提交推送，之后才可付费调用。"""
    started = now_local()
    cfg = load_all_config(config_dir)
    data_path = Path(data_dir)
    state_path = Path(state_dir) if state_dir else data_path / "state"

    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems}

    # 抓取上限：命令行优先，其次取 config/rules.json 的 run_limits，最后跟随本批名额
    fetch_limit = limit_fetches
    if fetch_limit is None:
        fetch_limit = (cfg["rules"].get("run_limits") or {}).get("max_fetches_per_run")

    # §7.2：队列是持久数据，本轮必须读回上一轮遗留的待处理条目，
    # 否则超出额度的候选会在每轮被重复跳过，永远轮不到评估。
    pending = _queue_pending(_read_queue(state_path) if (state_path / QUEUE_FILENAME).exists() else None)
    # 账本先加载：既定的完成记录决定哪些候选不需要重新入队（§5.3）
    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    ledger.mark_in_progress_as_needs_recovery(started)

    plan = prepare(
        cfg, config_dir=config_dir, limit_queries=limit_queries,
        limit_evaluations=limit_evaluations, limit_fetches=fetch_limit,
        expand=expand, expand_limit=expand_limit,
        state_dir=state_path, data_dir=data_path, pending=pending, ledger=ledger,
        sleep=sleep, discover_fn=discover_fn, fetch_fn=fetch_fn,
    )

    # §7：来源失败时保留上次有效数据；全部来源失败时不得用空结果覆盖既有索引
    if plan["discovery_total"] == 0 and _has_previous_catalog(data_path):
        return {
            "ok": False,
            "stage": "discover",
            "error": "本轮未发现任何候选且既有索引非空，中止以避免清空目录",
            "discovery_failed": plan["discovery_failed"],
            "outcomes": _outcome_records(plan["outcomes"]),
        }

    batch = plan["batch"][: plan["evaluation_slots"]]
    entries = [
        {
            "evaluation_id": evaluation_id(candidate, cfg["model"], cfg["rules"]),
            "skill_id": candidate.skill_id,
            "content_fingerprint": candidate.content_fingerprint,
            "rules_version": cfg["rules"].get("rules_version"),
            "model_config_version": cfg["model"].get("model_config_version"),
        }
        for candidate, _, _ in batch
    ]

    try:
        reserved = ledger.reserve(entries, started)
    except QuotaExceeded as exc:
        return {"ok": False, "stage": "reserve", "error": str(exc), "quota": ledger.snapshot()}

    _write_queue(
        plan, state_path, ledger.week, started.replace(microsecond=0).isoformat(),
        plan["queue"],
    )

    return {
        "ok": True,
        "phase": "reserve",
        "week": ledger.week,
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "fetched": plan["fetched"],
        "fetch_cap": plan["fetch_cap"],
        "evaluation_slots": plan["evaluation_slots"],
        "reserved": len(reserved),
        # 队列与复核积压：报告必须显示积压，不擅自突破额度（§5.4）
        "queue_pending": plan["queue_pending"],
        "carried_over": plan["carried_over"],
        "needs_review_backlog": plan["needs_review"],
        "quota": ledger.snapshot(),
        "discovery_failed": plan["discovery_failed"],
        "state_dir": str(state_path),
    }


# --------------------------------------------------------------------------
# 阶段二：评估与生成
# --------------------------------------------------------------------------


def previous_evaluation_snapshot(previous: dict | None) -> dict | None:
    """原评估对应的版本快照（§5.2/§6）。

    待复核时必须能展示"原评估对应的版本及评估时间"，不能只留一个标记，
    也不能让旧结论冒充对新版本的验证。
    """
    if not previous:
        return None
    category = previous.get("main_category") or {}
    return {
        "content_fingerprint": previous.get("content_fingerprint"),
        "status": previous.get("status"),
        "summary_zh": previous.get("summary_zh"),
        "main_category": category.get("id"),
        "tags": list(previous.get("tags") or []),
        "limitations": previous.get("limitations"),
        "evaluated_at": previous.get("last_checked"),
        "rules_version": previous.get("evaluation_rules_version"),
    }


def review_state(previous: dict | None, changed: bool, verdict: str | None = None) -> dict:
    """§5.2 待复核状态：保留推荐状态并醒目标记，展示原评估版本。

    - 已推荐条目的内容发生变化：**保留推荐状态**、标记待复核、记录内容变化时间，
      并附上原评估快照；不把旧结论当作对新版本的验证。
    - 待复核期间的新结论尚未拿到（等待额度或调用失败）：保留标记，不自动生成通过结论。
    - 复核通过（新结论仍是 recommended）：清除标记并更新评估依据。
    - 复核不通过（新结论不再是 recommended）：保留标记用于说明降级原因。
    """
    previous = previous or {}
    already = bool(previous.get("needs_review"))
    was_recommended = previous.get("status") == STATUS_RECOMMENDED
    if not (already or (was_recommended and changed)):
        return {
            "needs_review": False,
            "content_changed_at": previous.get("content_changed_at"),
            "pending_review": None,
            "review_note": None,
        }

    if verdict == STATUS_RECOMMENDED:
        # 复核通过：评估依据已更新，标记与旧版本快照一并清除
        return {
            "needs_review": False,
            "content_changed_at": previous.get("content_changed_at") or (
                previous.get("last_checked") if changed else None
            ),
            "pending_review": None,
            "review_note": None,
        }

    note = (
        REVIEW_NOTE_CONTENT_CHANGED
        if changed and was_recommended
        else (previous.get("review_note") or REVIEW_NOTE_AWAITING_EVALUATION)
    )
    return {
        "needs_review": True,
        # 内容变更时间取首次发现变化的时间，不因之后的每次运行被刷新
        "content_changed_at": (
            previous.get("content_changed_at")
            or (previous.get("last_checked") if changed else None)
        ),
        "pending_review": previous_evaluation_snapshot(previous),
        "review_note": note,
    }


def admission_decision(
    decision: dict | None, pres: PrescreenResult, previous: dict | None, state: dict
) -> dict | None:
    """把评估结论与待复核状态合成为索引里的状态。

    - 复核通过（新评估仍是推荐）：更新评估依据并清除待复核标记（§5.2）
    - 复核不通过（新评估不再推荐）：降级至候选区并说明原因（§5.2）
    - 待复核期间的新结论尚未拿到：保留推荐状态，标记醒目的"内容已变化，待复核"
    - 预筛已明确排除：按排除处理，不因先前推荐而保留
    """
    previous = previous or {}
    was_recommended = previous.get("status") == STATUS_RECOMMENDED
    verdict = (decision or {}).get("decision")

    # 只有在**拿到了新结论**且新结论不是推荐时才降级；
    # 没有结论（等待额度、评估 ID 未落账本）不算复核不通过
    if was_recommended and state["needs_review"] and verdict is not None and verdict != STATUS_RECOMMENDED:
        codes = list((decision or {}).get("reason_codes") or [])
        if REVIEW_REASON_CONTENT_CHANGED not in codes:
            codes.append(REVIEW_REASON_CONTENT_CHANGED)
        out = dict(decision or {})
        out["decision"] = STATUS_CANDIDATE
        out["reason_codes"] = codes
        return out

    if verdict:
        return decision
    if pres.excluded:
        return {"decision": DECISION_EXCLUDED, "reason_codes": list(pres.reason_codes)}
    if was_recommended:
        # 变化后的新版本尚未评估（或评估 ID 尚未落到账本）：保留推荐状态，
        # 由醒目的待复核标记说明"当前评估对应变化前的版本"，不降级也不生成通过结论
        return {"decision": STATUS_RECOMMENDED, "reason_codes": []}
    return None


def phase_evaluate(
    *,
    config_dir: str | Path = "config",
    data_dir: str | Path = "data",
    public_dir: str | Path = "public",
    state_dir: str | Path | None = None,
    api_key: str | None = None,
    sleep=time.sleep,
    fetch_fn=fetch_text,
    evaluate_fn=evaluate,
) -> dict:
    """评估已预留的条目，保留完整评估内容，合并既有索引并生成页面数据与周报。"""
    started = now_local()
    cfg = load_all_config(config_dir)
    data_path, public_path = Path(data_dir), Path(public_dir)
    state_path = Path(state_dir) if state_dir else data_path / "state"

    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems}

    queue = _read_queue(state_path)
    cap = int(cfg["rules"].get("weekly_quota") or DEFAULT_LIMIT_EVALUATIONS)
    ledger = BudgetLedger.load(state_path, cap)
    reserved_ids = set(ledger.reserved)

    # 单次运行的 token 消耗兜底闸：达到后停止后续模型调用，
    # 已预留的名额不退还（§7.3：失败仍占本周额度）
    token_cap = int((cfg["model"].get("limits") or {}).get("max_total_tokens_per_run") or 0)
    tokens_used = 0
    token_stopped: list[str] = []

    staged: dict[str, str] = {}
    staged_path = state_path / TEXTS_DIRNAME / "staged.json"
    if staged_path.exists():
        try:
            staged = json.loads(staged_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            staged = {}

    results: dict[str, dict] = {}
    evaluated = 0
    skipped = 0
    settled_items: list[str] = []

    manual_picks_dict = get_manual_picks((cfg or {}).get("overrides") or {})
    manual_picks_set = set(manual_picks_dict.keys())
    manual_exclusions_dict = get_manual_exclusions((cfg or {}).get("overrides") or {})
    manual_exclusions_set = set(manual_exclusions_dict.keys())

    for item in queue.get("pending", []):
        candidate = _candidate_from_payload(item["candidate"])
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])

        if candidate.skill_id in manual_exclusions_set:
            # 人工排除条目直接跳过，不调用模型
            results[candidate.skill_id] = {"status": "skipped", "note": "人工排除黑名单条目直接跳过"}
            skipped += 1
            continue

        if candidate.skill_id in manual_picks_set:
            # §4.4: 人工收藏条目不调用模型重新评估（0 模型调用）
            results[candidate.skill_id] = {"status": "skipped", "note": "人工收藏条目不调用模型重新评估"}
            skipped += 1
            continue

        if _mark_settled(item, cfg, ledger):
            # 这份内容已经评估过：直接出队，不抓取、不占名额（§5.3）
            settled_items.append(_skill_of(item))
            continue

        if eid not in reserved_ids:
            continue  # 本批未预留（例如超出抓取上限），留给后续运行

        existing = ledger.get(eid) or {}
        if existing.get("status") == "completed":
            skipped += 1
            continue

        allowed, reason = ledger.can_attempt(eid)
        if not allowed:
            results[candidate.skill_id] = {"status": "skipped", "note": reason}
            skipped += 1
            continue

        if token_cap and tokens_used >= token_cap:
            token_stopped.append(candidate.skill_id)
            results[candidate.skill_id] = {
                "status": "stopped",
                "note": f"已达单次运行 token 上限 {token_cap}，本轮不再调用模型",
            }
            continue

        text = staged.get(candidate.skill_id)
        if text is None:
            fetched = fetch_fn(candidate.url or candidate.repo_url, sleep=sleep)
            if not fetched.ok or not fetched.text:
                ledger.fail(eid, fetched.reason_code or "NETWORK_ERROR", fetched.error or "抓取失败", started)
                results[candidate.skill_id] = {"status": "failed", "note": "抓取失败"}
                continue
            text = fetched.text
            fresh = content_fingerprint(text)
            if candidate.content_fingerprint and fresh != candidate.content_fingerprint:
                results.setdefault(candidate.skill_id, {})["fingerprint_changed"] = True

        ledger.begin_attempt(eid, started)
        outcome = evaluate_fn(
            candidate, text, model_cfg=cfg["model"], rules=cfg["rules"],
            taxonomy=cfg["taxonomy"], api_key=api_key, sleep=sleep,
        )
        call = outcome.get("call")
        if call is not None:
            tokens_used += int(getattr(call, "total_tokens", 0) or 0)
        if not outcome["ok"]:
            ledger.fail(eid, outcome["reason_code"], outcome["error"] or "", started)
            results[candidate.skill_id] = {"status": "failed", "note": outcome["error"]}
            continue

        evaluation = outcome["evaluation"]
        decision = decide(evaluation, cfg["rules"])
        # §6：中文简述、分类与依赖必须随索引保留，不能只留决策与原因码
        ledger.complete(
            eid,
            {
                "decision": decision["decision"],
                "reason_codes": decision["reason_codes"],
                "main_category": evaluation.get("main_category"),
                "evaluation": evaluation,
            },
            started,
        )
        results[candidate.skill_id] = {"status": "completed", "decision": decision["decision"]}
        evaluated += 1

    context = CatalogContext(
        rules_version=cfg["rules"].get("rules_version"),
        generated_at=started.replace(microsecond=0).isoformat(),
        domain_names=cfg["prescreen"].domain_names,
        source_types=cfg["source_types"],
    )

    # §7：本轮未出现的条目保留，不因来源失败而批量删除
    previous_entries = _previous_entries(data_path)
    previous_by_id = index_by_id(previous_entries)

    def entry_for(
        candidate: Candidate, pres: PrescreenResult, fetch_note: dict | None = None
    ) -> dict:
        eid = evaluation_id(candidate, cfg["model"], cfg["rules"])
        record = ledger.get(eid) or {}
        # 完整评估内容随 outcome 一起保存在评估记录里（§6 需要中文简述与分类）
        outcome = record.get("outcome") or {}
        evaluation = outcome.get("evaluation") or {}
        decision = (
            {"decision": outcome["decision"], "reason_codes": outcome.get("reason_codes", [])}
            if outcome.get("decision")
            else None
        )
        previous = previous_by_id.get(candidate.skill_id)
        changed = bool((fetch_note or {}).get("content_changed"))
        state = review_state(previous, changed, (decision or {}).get("decision"))
        return build_entry(
            candidate,
            prescreen_result=pres,
            decision=admission_decision(decision, pres, previous, state),
            evaluation=evaluation,
            context=context,
            first_seen=candidate.discovered_at or context.generated_at,
            last_checked=context.generated_at,
            # §6：最近检查时间不能冒充内容变更时间，两者分列且只在确实变化时记录
            content_changed_at=state["content_changed_at"],
            upstream_status=(
                UPSTREAM_GONE if (fetch_note or {}).get("upstream_gone") else "ok"
            ),
            needs_review=state["needs_review"],
            review_note=state["review_note"],
            pending_review=state["pending_review"],
        )

    fresh: list[dict] = []
    for item in queue.get("pending", []):
        fresh.append(
            entry_for(
                _candidate_from_payload(item["candidate"]),
                _prescreen_from_payload(item["prescreen"]),
                item.get("fetch"),
            )
        )
    for item in queue.get("excluded", []):
        fresh.append(
            entry_for(
                _candidate_from_payload(item["candidate"]),
                _prescreen_from_payload(item["prescreen"]),
            )
        )

    merged = merge_entries(previous_entries, fresh)
    apply_manual_overrides(merged, manual_picks_dict, manual_exclusions_dict)
    catalog = build_catalog(merged, context=context, overrides=(cfg or {}).get("overrides"))
    manifest = write_catalog(
        catalog,
        data_path=data_path / "catalog.json",
        public_path=public_path / "data" / "catalog.json",
    )

    report = build_report(
        catalog,
        previous_catalog={"entries": previous_entries},
        run_meta={"quota": ledger.snapshot(), "dry_run": False},
        outcomes=queue.get("outcomes") or [],
    )
    week = queue.get("week") or ledger.week
    written = write_report(
        report,
        json_path=data_path / "reports" / f"{week}.json",
        markdown_path=public_path / "reports" / f"{week}.md",
    )

    build_manifest = {"week": week, **manifest, **written}
    (state_path / "build-manifest.json").parent.mkdir(parents=True, exist_ok=True)
    (state_path / "build-manifest.json").write_text(
        json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # §7.2：队列是持久数据——已完成的条目出队，失败或待复核的留在队列里继续占用后续名额
    queued_before = list(queue.get("pending") or [])
    remaining = _settled_pending(queued_before, cfg, ledger)
    queue["pending"] = remaining
    queue["generated_at"] = context.generated_at
    (state_path / QUEUE_FILENAME).write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 抓取到的上游原文只在同一次运行内复用，运行结束即清理，不进入仓库（§1、§7.4）
    if staged_path.exists():
        staged_path.unlink()

    return {
        "ok": True,
        "phase": "evaluate",
        "week": week,
        "evaluated": evaluated,
        "skipped": skipped,
        "settled": len(settled_items),
        "tokens_used": tokens_used,
        "token_cap": token_cap or None,
        "token_stopped": token_stopped,
        "results": results,
        "entries_before": len(previous_entries),
        "entries_after": len(merged),
        "queue_pending": len(remaining),
        "queue_dropped": len(queued_before) - len(remaining),
        "quota": ledger.snapshot(),
        "manifest": manifest,
        "report_paths": written,
    }


# --------------------------------------------------------------------------
# 干跑
# --------------------------------------------------------------------------


def dry_run(
    *,
    config_dir: str | Path = "config",
    limit_queries: int | None = None,
    limit_evaluations: int = DEFAULT_LIMIT_EVALUATIONS,
    limit_fetches: int | None = None,
    expand: bool = True,
    expand_limit: int | None = None,
    sleep=time.sleep,
) -> dict:
    """只验证配置与计算计划：不调用模型、不写账本、不提交、不部署（§7.3）。"""
    cfg = load_all_config(config_dir)
    problems = precheck(cfg)
    if problems:
        return {"ok": False, "stage": "precheck", "problems": problems, "dry_run": True}

    fetch_limit = limit_fetches
    if fetch_limit is None:
        fetch_limit = (cfg["rules"].get("run_limits") or {}).get("max_fetches_per_run")

    plan = prepare(
        cfg, config_dir=config_dir, limit_queries=limit_queries,
        limit_evaluations=limit_evaluations, limit_fetches=fetch_limit,
        expand=expand, expand_limit=expand_limit,
        state_dir=None, sleep=sleep,
    )
    return {
        "ok": True,
        "dry_run": True,
        "week": week_id(),
        "candidates": plan["discovery_total"],
        "queued": len(plan["queued"]),
        "excluded": len(plan["excluded"]),
        "fetched": plan["fetched"],
        "fetch_cap": plan["fetch_cap"],
        "evaluation_slots": plan["evaluation_slots"],
        "model": cfg["model"].get("model"),
        "credentials_present": bool(resolve_api_key(cfg["model"])),
        "discovery_failed": plan["discovery_failed"],
        "outcomes": _outcome_records(plan["outcomes"]),
        "excluded_reasons": [
            {"skill_id": c.skill_id, "reason_codes": p.reason_codes} for c, p, _ in plan["excluded"]
        ],
        "notes": ["dry_run：未调用模型、未写账本、未提交、未部署"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skills 导航目录：一次完整运行")
    parser.add_argument("--phase", choices=["reserve", "evaluate", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="只验证配置与计算计划")
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--limit-evaluations", type=int, default=DEFAULT_LIMIT_EVALUATIONS)
    parser.add_argument("--limit-fetches", type=int, default=None,
                        help="本轮最多抓取多少条上游内容；留空则跟随本批评估名额，"
                             "或取 config/rules.json 的 run_limits.max_fetches_per_run")
    parser.add_argument("--expand-limit", type=int, default=None, help="本轮最多展开多少个仓库")
    parser.add_argument("--no-expand", action="store_true", help="不展开到具体技能（仅调试用）")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--public-dir", default="public")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    common = dict(
        config_dir=args.config_dir, data_dir=args.data_dir, state_dir=args.state_dir,
        limit_queries=args.limit_queries, limit_evaluations=args.limit_evaluations,
        limit_fetches=args.limit_fetches,
        expand=not args.no_expand, expand_limit=args.expand_limit,
    )

    if args.dry_run:
        result = dry_run(**{k: v for k, v in common.items() if k not in ("data_dir", "state_dir")})
    elif args.phase == "reserve":
        result = phase_reserve(**common)
    elif args.phase == "evaluate":
        result = phase_evaluate(
            config_dir=args.config_dir, data_dir=args.data_dir, public_dir=args.public_dir,
            state_dir=args.state_dir,
        )
    else:
        result = phase_reserve(**common)
        if result.get("ok"):
            evaluated = phase_evaluate(
                config_dir=args.config_dir, data_dir=args.data_dir,
                public_dir=args.public_dir, state_dir=args.state_dir,
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


if __name__ == "__main__":
    sys.exit(main())
