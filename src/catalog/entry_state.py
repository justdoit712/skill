"""统一条目状态机与状态转移矩阵。

消除本地 local_run:publish() 与 Actions pipeline:entry_for() 的割裂双重分支，
提供全库唯一的条目更新纯函数：update_entry()。

架构红线：
- 纯函数：绝不进行文件读写、网络调用或模型通信
- 严格状态矩阵：版本继承、快照保护、待复核标记、显式空值防复活
- 保证同一事件在本地与 Actions 下生成完全一致的条目数据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.shared.models import CandidateIdentity
from src.shared.schema import normalize_skill_type, normalize_string_list

STATUS_RECOMMENDED = "recommended"
STATUS_CANDIDATE = "candidate"
STATUS_EXCLUDED = "excluded"
STATUS_PENDING = "pending"

REVIEW_NOTE_CONTENT_CHANGED = "上游内容已变化，当前评估对应变化前的版本，待复核"
REVIEW_NOTE_AWAITING_EVALUATION = "待复核期间的新结论尚未拿到（等待额度或调用失败）"
REVIEW_REASON_CONTENT_CHANGED = "CONTENT_CHANGED"

UPSTREAM_OK = "ok"
UPSTREAM_GONE = "upstream_gone"


@dataclass(frozen=True)
class EntryUpdateEvent:
    """条目更新事件（替代松散字典与非空判断）。"""

    kind: str  # "fresh_evaluation" | "cached_evaluation" | "no_evaluation" | "fetch_failed"
    prescreen_result: Any = None
    evaluation: Optional[dict] = None
    decision: Optional[dict] = None
    upstream_status: str = UPSTREAM_OK
    fetched_fingerprint: Optional[str] = None
    rules_version: str = "1.0.1"
    model_config_version: str = "1.0.0"


def previous_evaluation_snapshot(previous: dict | None) -> dict | None:
    """原评估对应的版本快照（§5.2/§6）。

    待复核时必须能展示“原评估对应的版本及评估时间”，不能只留一个标记，
    也不能让旧结论冒充对新版本的验证。
    """
    if not previous:
        return None
    category = previous.get("main_category") or {}
    category_id = category.get("id") if isinstance(category, dict) else str(category or "")
    return {
        "content_fingerprint": previous.get("content_fingerprint"),
        "status": previous.get("status"),
        "summary_zh": previous.get("summary_zh"),
        "skill_type": previous.get("skill_type"),
        "example_requests": list(previous.get("example_requests") or []),
        "key_features": list(previous.get("key_features") or []),
        "main_category": category_id or None,
        "tags": list(previous.get("tags") or []),
        "limitations": previous.get("limitations"),
        "evaluated_at": previous.get("last_checked"),
        "rules_version": previous.get("evaluation_rules_version"),
    }


def review_state(previous: dict | None, changed: bool, verdict: str | None = None) -> dict:
    """§5.2 待复核状态判定：保留推荐状态并醒目标记，展示原评估版本。"""
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
        # 复核通过：清除标记与旧版本快照
        return {
            "needs_review": False,
            "content_changed_at": previous.get("content_changed_at") or (
                previous.get("last_checked") if changed else None
            ),
            "pending_review": None,
            "review_note": None,
        }

    # 待复核期间：如果已经是 needs_review 且有 pending_review，必须牢牢保留原快照，绝不能被中间错误覆盖
    existing_snapshot = previous.get("pending_review") or previous_evaluation_snapshot(previous)
    note = (
        REVIEW_NOTE_CONTENT_CHANGED
        if changed and was_recommended and not already
        else (previous.get("review_note") or REVIEW_NOTE_AWAITING_EVALUATION)
    )
    return {
        "needs_review": True,
        "content_changed_at": (
            previous.get("content_changed_at")
            or (previous.get("last_checked") if changed else None)
        ),
        "pending_review": existing_snapshot,
        "review_note": note,
    }


def admission_decision(
    decision: dict | None, pres: Any, previous: dict | None, state: dict
) -> dict | None:
    """把评估结论与待复核状态合成为索引里的准入结论。"""
    previous = previous or {}
    was_recommended = previous.get("status") == STATUS_RECOMMENDED
    verdict = (decision or {}).get("decision")

    # 只有在拿到了新结论且新结论不是推荐时才降级；没有结论不算复核不通过
    if was_recommended and state.get("needs_review") and verdict is not None and verdict != STATUS_RECOMMENDED:
        codes = list((decision or {}).get("reason_codes") or [])
        if REVIEW_REASON_CONTENT_CHANGED not in codes:
            codes.append(REVIEW_REASON_CONTENT_CHANGED)
        out = dict(decision or {})
        out["decision"] = STATUS_CANDIDATE
        out["reason_codes"] = codes
        return out

    if verdict:
        return decision
    if pres and getattr(pres, "excluded", False):
        return {"decision": STATUS_EXCLUDED, "reason_codes": list(getattr(pres, "reason_codes", []))}
    if was_recommended:
        return {"decision": STATUS_RECOMMENDED, "reason_codes": []}
    return None


def update_entry(
    previous_entry: Optional[dict],
    candidate: CandidateIdentity,
    event: EntryUpdateEvent,
    context: Any = None,
) -> dict:
    """全库唯一的条目更新纯函数。

    参数：
    - previous_entry：既有目录条目字典（若为全新发现项则为 None）
    - candidate：候选身份（包含 owner/repo/path/name 等）
    - event：条目更新事件（包含类型、预筛结论、评估结果、上游状态等）
    - context：目录上下文（包含 domain_names, source_types, generated_at 等）
    """
    previous = previous_entry or {}
    current_fp = event.fetched_fingerprint or candidate.content_fingerprint

    # 判断上游内容是否发生变更
    changed = False
    if previous.get("content_fingerprint") and current_fp:
        changed = previous["content_fingerprint"] != current_fp

    verdict = (event.decision or {}).get("decision")
    state = review_state(previous, changed, verdict)
    adm_decision = admission_decision(event.decision, event.prescreen_result, previous, state)

    generated_at = getattr(context, "generated_at", "") or ""
    domain_names = getattr(context, "domain_names", {}) or {}
    source_types = getattr(context, "source_types", {}) or {}

    evaluation = event.evaluation
    eval_dict: dict[str, Any] = {}

    if evaluation is not None:
        eval_dict = dict(evaluation)
        # 旧格式缓存补全（cached_evaluation）：当内容未变时，若旧缓存缺少字段，则从既有条目继承
        if (
            event.kind == "cached_evaluation"
            and previous
            and not changed
            and previous.get("content_fingerprint")
            and current_fp
            and previous["content_fingerprint"] == current_fp
        ):
            if "skill_type" not in eval_dict or eval_dict.get("skill_type") is None:
                eval_dict["skill_type"] = previous.get("skill_type")
            if not eval_dict.get("example_requests") and previous.get("example_requests"):
                eval_dict["example_requests"] = previous.get("example_requests")
            if not eval_dict.get("key_features") and previous.get("key_features"):
                eval_dict["key_features"] = previous.get("key_features")
    else:
        # 没有新评估（no_evaluation / fetch_failed 等）：
        # 若内容未变且既有条目有效，必须完整保留原有字段，绝不用空评估覆盖
        is_pres_excluded = bool(event.prescreen_result and getattr(event.prescreen_result, "excluded", False))
        if (
            previous
            and not changed
            and previous.get("content_fingerprint")
            and current_fp
            and previous["content_fingerprint"] == current_fp
            and not is_pres_excluded
        ):
            for key in (
                "summary_zh",
                "skill_type",
                "example_requests",
                "key_features",
                "main_category",
                "tags",
                "platform_declared",
                "dependencies_declared",
                "evaluation_rules_version",
                "limitations",
                "license",
            ):
                eval_dict[key] = previous.get(key)

    # 确定 status 与 reason_codes
    status = (adm_decision or {}).get("decision") or STATUS_PENDING
    if not (adm_decision or {}).get("decision") and event.prescreen_result and getattr(event.prescreen_result, "excluded", False):
        status = STATUS_EXCLUDED

    # 确定主分类
    main_category = None
    raw_cat = eval_dict.get("main_category")
    cat_id = None
    if isinstance(raw_cat, dict):
        cat_id = raw_cat.get("id")
    elif isinstance(raw_cat, str) and raw_cat:
        cat_id = raw_cat
    if cat_id:
        main_category = {"id": cat_id, "name": domain_names.get(cat_id, "")}

    # 汇总原因码
    reason_codes = list((adm_decision or {}).get("reason_codes") or [])
    if event.prescreen_result:
        for code in getattr(event.prescreen_result, "reason_codes", []):
            if code not in reason_codes:
                reason_codes.append(code)

    source_ids = getattr(candidate, "source_ids", []) or []
    source_type = next((source_types.get(sid) for sid in source_ids if source_types.get(sid)), None)

    candidate_domains = list(getattr(event.prescreen_result, "domains", [])) if event.prescreen_result else []

    first_seen = previous.get("first_seen") or candidate.discovered_at or generated_at
    last_checked = generated_at or previous.get("last_checked")

    entry: dict[str, Any] = {
        "skill_id": candidate.skill_id,
        "name": candidate.name or candidate.repo,
        "url": candidate.url,
        "repo_url": candidate.repo_url,
        "author": candidate.owner,
        "summary_zh": eval_dict.get("summary_zh"),
        "skill_type": normalize_skill_type(eval_dict.get("skill_type")),
        "example_requests": normalize_string_list(eval_dict.get("example_requests"), max_items=2, max_length=100),
        "key_features": normalize_string_list(eval_dict.get("key_features"), max_items=3, max_length=60),
        "main_category": main_category,
        "candidate_domains": candidate_domains,
        "tags": list(eval_dict.get("tags") or []),
        "platform_declared": eval_dict.get("platform_declared"),
        "dependencies_declared": list(eval_dict.get("dependencies_declared") or []),
        "limitations": eval_dict.get("limitations"),
        "license": eval_dict.get("license"),
        "status": status,
        "reason_codes": reason_codes,
        "first_seen": first_seen,
        "last_checked": last_checked,
        "content_changed_at": state["content_changed_at"],
        "content_fingerprint": current_fp,
        "source_type": source_type,
        "upstream_status": event.upstream_status,
        "needs_review": state["needs_review"],
        "review_note": state["review_note"],
        "pending_review": state["pending_review"],
        "manual_pick": bool(previous.get("manual_pick", False)),
        "manual_note": previous.get("manual_note"),
    }

    if eval_dict.get("evaluation_rules_version"):
        entry["evaluation_rules_version"] = eval_dict["evaluation_rules_version"]
    elif event.rules_version and evaluation is not None:
        entry["evaluation_rules_version"] = event.rules_version

    return entry


__all__ = [
    "EntryUpdateEvent",
    "STATUS_RECOMMENDED",
    "STATUS_CANDIDATE",
    "STATUS_EXCLUDED",
    "STATUS_PENDING",
    "REVIEW_NOTE_CONTENT_CHANGED",
    "REVIEW_NOTE_AWAITING_EVALUATION",
    "REVIEW_REASON_CONTENT_CHANGED",
    "UPSTREAM_OK",
    "UPSTREAM_GONE",
    "previous_evaluation_snapshot",
    "review_state",
    "admission_decision",
    "update_entry",
]
