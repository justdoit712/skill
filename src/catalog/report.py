"""周报：区分新增、内容变化、分类或链接修正、推荐状态变化、上游移除与采集失败（§7.2）。

报告只描述本次运行相对上次索引的差异，不把"最近检查"当作"技能更新"（§6）。
输出路径由调用方给出。
"""

from __future__ import annotations

from src.infra.files import write_json_atomic, write_text_atomic

import json
from pathlib import Path

from .index import STATUS_RECOMMENDED, UPSTREAM_GONE, UPSTREAM_OK

NEW = "new"
CONTENT_CHANGED = "content_changed"
RECATEGORIZED = "recategorized"
STATUS_CHANGED = "status_changed"
UPSTREAM_REMOVED = "upstream_removed"
COLLECTION_FAILED = "collection_failed"


def _index_by_id(catalog: dict | None) -> dict[str, dict]:
    if not catalog:
        return {}
    return {entry["skill_id"]: entry for entry in catalog.get("entries", [])}


def _category_id(entry: dict) -> str | None:
    category = entry.get("main_category") or {}
    return category.get("id")


def build_report(
    catalog: dict,
    *,
    previous_catalog: dict | None = None,
    run_meta: dict | None = None,
    outcomes: list[dict] | None = None,
) -> dict:
    """计算本次运行相对上次索引的差异。

    outcomes 是发现阶段的每次查询结果（{ok, reason_code, error, query}），
    用于区分"采集失败"与"条目质量"。
    """
    current = _index_by_id(catalog)
    previous = _index_by_id(previous_catalog)

    added, changed, recategorized, status_changed, removed = [], [], [], [], []

    for skill_id, entry in current.items():
        old = previous.get(skill_id)
        if old is None:
            added.append(_brief(entry))
            continue

        if entry.get("content_fingerprint") and old.get("content_fingerprint") != entry.get(
            "content_fingerprint"
        ):
            changed.append(_brief(entry))

        if _category_id(old) != _category_id(entry):
            recategorized.append(
                {"skill_id": skill_id, "from": _category_id(old), "to": _category_id(entry)}
            )

        if old.get("status") != entry.get("status"):
            # §6：收藏条目的变化只记信息，不计入推荐状态变化
            if not (old.get("manual_pick") or entry.get("manual_pick")):
                status_changed.append(
                    {"skill_id": skill_id, "from": old.get("status"), "to": entry.get("status")}
                )

        if entry.get("upstream_status") == UPSTREAM_GONE and old.get("upstream_status") == UPSTREAM_OK:
            removed.append(_brief(entry))

    for skill_id, entry in previous.items():
        if skill_id not in current:
            removed.append(_brief(entry))

    failures = [
        {"query": o.get("query"), "reason_code": o.get("reason_code"), "error": o.get("error")}
        for o in (outcomes or [])
        if not o.get("ok")
    ]

    meta = dict(run_meta or {})
    return {
        "generated_at": catalog.get("generated_at"),
        "rules_version": catalog.get("rules_version"),
        "run": meta,
        "counts": {
            NEW: len(added),
            CONTENT_CHANGED: len(changed),
            RECATEGORIZED: len(recategorized),
            STATUS_CHANGED: len(status_changed),
            UPSTREAM_REMOVED: len(removed),
            COLLECTION_FAILED: len(failures),
        },
        NEW: added,
        CONTENT_CHANGED: changed,
        RECATEGORIZED: recategorized,
        STATUS_CHANGED: status_changed,
        UPSTREAM_REMOVED: removed,
        COLLECTION_FAILED: failures,
        "recommended_total": sum(
            1 for entry in current.values() if entry.get("status") == STATUS_RECOMMENDED and not entry.get("manual_pick")
        ),
        "manual_total": sum(
            1 for entry in current.values() if entry.get("manual_pick")
        ),
        "quota": meta.get("quota"),
    }


def _brief(entry: dict) -> dict:
    return {
        "skill_id": entry.get("skill_id"),
        "name": entry.get("name"),
        "url": entry.get("url"),
        "status": entry.get("status"),
        "manual_pick": bool(entry.get("manual_pick")),
    }


def render_report_markdown(report: dict) -> str:
    """把周报渲染为 Markdown，便于作为更新记录展示（§6）。"""
    meta = report.get("run") or {}
    lines = [
        f"# 运行报告 {report.get('generated_at') or ''}".rstrip(),
        "",
        f"- 规则版本：{report.get('rules_version') or '未记录'}",
    ]
    quota = report.get("quota") or {}
    if quota:
        lines.append(
            f"- 本周额度：上限 {quota.get('cap')}，已用 {quota.get('used')}，剩余 {quota.get('remaining')}"
        )
    if meta.get("dry_run"):
        lines.append("- 本次为 dry_run：未调用模型、未写账本、未提交、未部署")

    lines += ["", "## 变更统计", "", "| 类别 | 数量 |", "| --- | --- |"]
    for key, value in (report.get("counts") or {}).items():
        lines.append(f"| {key} | {value} |")

    def section(title: str, key: str, render) -> None:
        items = report.get(key) or []
        lines.extend(["", f"## {title}（{len(items)}）", ""])
        if not items:
            lines.append("无。")
            return
        for item in items:
            lines.append(render(item))

    section("新增", NEW, lambda i: f"- `{i['skill_id']}` — {i.get('status')}")
    section("内容变化", CONTENT_CHANGED, lambda i: f"- `{i['skill_id']}` — {i.get('status')}")
    section("分类修正", RECATEGORIZED, lambda i: f"- `{i['skill_id']}` — {i.get('from')} → {i.get('to')}")
    section("推荐状态变化", STATUS_CHANGED, lambda i: f"- `{i['skill_id']}` — {i.get('from')} → {i.get('to')}")
    section("上游移除", UPSTREAM_REMOVED, lambda i: f"- `{i['skill_id']}` — {i.get('url')}")
    section(
        "采集失败",
        COLLECTION_FAILED,
        lambda i: f"- `{i.get('query')}` — {i.get('reason_code')} {i.get('error') or ''}".rstrip(),
    )
    return "\n".join(lines) + "\n"


def write_report(report: dict, *, json_path: str | Path, markdown_path: str | Path | None = None) -> dict:
    """写出 JSON 报告，可选同时写出 Markdown 版本。"""
    json_file = Path(json_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(json_file, report)

    written = {"report_json": str(json_file)}
    if markdown_path is not None:
        md_file = Path(markdown_path)
        md_file.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(md_file, render_report_markdown(report))
        written["report_markdown"] = str(md_file)
    return written
