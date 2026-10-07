"""存量目录黑名单一次性过滤维护工具。

直接启动：按 blocked-tags.json 中的完整标签离线匹配，写入人工排除并同步页面。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable
from uuid import uuid4

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.filter_rules import FilterRules
from src.catalog.favorites import get_manual_picks, load_favorites
from src.catalog.maintenance import sync_config_offline
from src.catalog.overrides import get_manual_exclusions, load_overrides
from src.catalog.snooze import get_active_snoozed, load_snooze, now_shanghai_date
from src.catalog.store import catalog_session
from src.infra.files import file_lock, read_json, write_json_atomic
from src.infra.owned import load_owned_config
from src.shared.owned import is_skill_owned, normalize_owned_id
from src.shared.runtime import now_local

TOOL_VERSION = "2.0.0"
MATCH_MODE = "exact_tags_v1"


def safe_print(msg: str) -> None:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))
        except Exception:
            pass


safe_log = safe_print


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _reconstruct_filter_rules(blocked_topics: list[dict[str, Any]]) -> FilterRules:
    clean = [
        {"name": t["name"], "description": t.get("description", "")}
        for t in blocked_topics
        if isinstance(t, dict) and "name" in t
    ]
    return FilterRules({"blocked_topics": clean})



def _resolve_config_path(root: Path, filename: str, subdir: str) -> Path:
    sub_p = root / "config" / subdir / filename
    flat_p = root / "config" / filename
    if flat_p.exists() and sub_p.exists():
        try:
            return flat_p if flat_p.stat().st_mtime >= sub_p.stat().st_mtime else sub_p
        except OSError:
            return flat_p
    if flat_p.exists():
        return flat_p
    return sub_p


def _get_run_dir(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", run_id):
        raise ValueError("批次 ID 仅允许字母、数字、下划线和连字符，长度 1～101")
    return root / "data" / "local" / "topic-filter" / run_id


def _normalize_string_list(val: Any) -> list[str]:
    """将字符串或列表规范化为字符串列表，防止字符串被按字符拆解。"""
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return []


def extract_materials(entry: dict[str, Any]) -> dict[str, Any]:
    """保留已有标签及供人工审核的目录描述。"""
    return {
        "name": str(entry.get("name") or "").strip(),
        "url": str(entry.get("url") or "").strip(),
        "summary_zh": str(entry.get("summary_zh") or "").strip(),
        "key_features": _normalize_string_list(entry.get("key_features")),
        "example_requests": _normalize_string_list(entry.get("example_requests")),
        "limitations": _normalize_string_list(entry.get("limitations")),
        "main_category": (
            f"{entry['main_category'].get('id', '')}: {entry['main_category'].get('name', '')}".strip(" :")
            if isinstance(entry.get("main_category"), dict)
            else str(entry.get("main_category") or "").strip()
        ),
        "tags": _normalize_string_list(entry.get("tags")),
    }


def load_tag_rules(path: Path) -> FilterRules:
    """名单只包含实际标签，不做主题映射或关键词匹配。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    tags = data.get("blocked_tags") if isinstance(data, dict) else None
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise ValueError("blocked_tags 必须是非空字符串组成的数组")
    names = sorted({tag.strip().casefold() for tag in tags})
    return FilterRules({"blocked_topics": [{"name": name} for name in names]})


def require_tag_run(run_data: dict) -> None:
    if run_data.get("match_mode") != MATCH_MODE:
        raise ValueError("这是旧的模型筛选批次，请使用新的 run-id 执行 prepare；旧记录保留不变")


def prepare_run(root_dir: str | Path, run_id: str | None = None) -> dict[str, Any]:
    """生成不可变批次快照：选择范围、校验规则与治理保护。"""
    root = Path(root_dir).resolve()
    run_id = run_id or f"topic-filter-{now_local().strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    run_dir = _get_run_dir(root, run_id)
    if run_dir.exists():
        raise ValueError(f"批次目录已存在：{run_id}。请使用新批次 ID。")

    catalog_path = root / "data" / "catalog.json"
    public_catalog_path = root / "public" / "data" / "catalog.json"
    filter_rules_path = _resolve_config_path(root, "blocked-tags.json", "governance")
    favorites_path = _resolve_config_path(root, "favorites.json", "governance")
    overrides_path = _resolve_config_path(root, "overrides.json", "governance")
    snooze_path = _resolve_config_path(root, "snoozed.json", "governance")
    owned_path = _resolve_config_path(root, "owned-skills.json", "governance")

    if not catalog_path.exists():
        raise FileNotFoundError(f"主目录文件不存在：{catalog_path}")

    with catalog_session(catalog_path.parent):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        entries = catalog.get("entries") or []

        # 校验 filter rules
        filter_rules = load_tag_rules(filter_rules_path)
        if not filter_rules.has_evaluation_rules:
            raise ValueError(f"标签黑名单为空，请先填写要屏蔽的现有标签：{filter_rules_path}")

        # 加载治理保护
        fav_data = load_favorites(favorites_path)
        manual_picks = get_manual_picks(fav_data)

        ov_data = load_overrides(overrides_path)
        manual_exclusions = get_manual_exclusions(ov_data)

        sn_data = load_snooze(snooze_path)
        active_snoozes = get_active_snoozed(sn_data)

        owned_cfg = load_owned_config(owned_path)
        owned_ids = set()
        for item in owned_cfg.get("items", []):
            raw_sid = item.get("skill_id", "")
            try:
                owned_ids.add(normalize_owned_id(raw_sid))
            except ValueError:
                owned_ids.add(raw_sid)

        # 离线页面投影与主目录一致性检查
        consistency_warnings: list[str] = []
        if public_catalog_path.exists():
            try:
                pub_cat = json.loads(public_catalog_path.read_text(encoding="utf-8"))
                pub_counts = pub_cat.get("counts") or {}
                main_counts = catalog.get("counts") or {}
                if (
                    pub_counts.get("recommended") != main_counts.get("recommended")
                    or pub_counts.get("candidate") != main_counts.get("candidate")
                ):
                    consistency_warnings.append(
                        f"主目录与公共页面投影数量不一致：主目录 recommended={main_counts.get('recommended')}, "
                        f"candidate={main_counts.get('candidate')}；公共投影 recommended={pub_counts.get('recommended')}, "
                        f"candidate={pub_counts.get('candidate')}。"
                    )
            except Exception as e:
                consistency_warnings.append(f"读取公共页面投影核验失败：{e}")

        seen_ids = set()
        targets = []
        inconsistencies: list[dict[str, Any]] = []
        excluded_reasons: dict[str, int] = {
            "not_in_scope_status": 0,
            "manual_excluded": 0,
            "favorited": 0,
            "owned": 0,
        }
        snoozed_count = 0

        for entry in entries:
            sid = entry.get("skill_id")
            if not sid or not isinstance(sid, str) or not sid.strip():
                raise ValueError(f"目录包含非法无 ID 条目：{entry}")
            sid = sid.strip()
            if sid in seen_ids:
                raise ValueError(f"主目录存在重复 skill_id：{sid}")
            seen_ids.add(sid)

            status = entry.get("status")
            if status not in ("recommended", "candidate"):
                excluded_reasons["not_in_scope_status"] += 1
                continue

            if sid in manual_exclusions:
                excluded_reasons["manual_excluded"] += 1
                continue

            if sid in manual_picks or entry.get("manual_pick"):
                if bool(sid in manual_picks) != bool(entry.get("manual_pick")):
                    inconsistencies.append({
                        "skill_id": sid,
                        "in_favorites_file": sid in manual_picks,
                        "entry_manual_pick": bool(entry.get("manual_pick")),
                    })
                excluded_reasons["favorited"] += 1
                continue

            if is_skill_owned(sid, owned_ids):
                excluded_reasons["owned"] += 1
                continue

            is_snoozed_item = sid in active_snoozes
            if is_snoozed_item:
                snoozed_count += 1

            mat = extract_materials(entry)
            mat_fp = _sha256_hex(_canonical_json(mat))[:16]

            target_row = {
                "skill_id": sid,
                "name": mat["name"],
                "url": mat["url"],
                "original_status": status,
                "is_snoozed": is_snoozed_item,
                "materials": mat,
                "materials_fingerprint": mat_fp,
            }
            targets.append(target_row)

        rec_targets = [t for t in targets if t["original_status"] == "recommended"]
        cand_targets = [t for t in targets if t["original_status"] == "candidate"]

        targets.sort(key=lambda t: t["skill_id"])
        targets_canonical = _canonical_json([
            {"skill_id": t["skill_id"], "mat_fp": t["materials_fingerprint"]}
            for t in targets
        ])
        targets_fp = _sha256_hex(targets_canonical)[:16]

        targets_doc = {
            "run_id": run_id,
            "created_at": now_local().isoformat(),
            "targets_fingerprint": targets_fp,
            "total_targets": len(targets),
            "recommended_count": len(rec_targets),
            "candidate_count": len(cand_targets),
            "snoozed_count": snoozed_count,
            "targets": targets,
        }
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "results").mkdir()
        write_json_atomic(run_dir / "targets.json", targets_doc)

        run_doc = {
            "run_id": run_id,
            "created_at": now_local().isoformat(),
            "tool_version": TOOL_VERSION,
            "match_mode": MATCH_MODE,
            "status": "prepared",
            "rules_fingerprint": filter_rules.fingerprint,
            "targets_fingerprint": targets_fp,
            "config_paths": {
                "catalog": str(catalog_path),
                "public_catalog": str(public_catalog_path),
                "filter_rules": str(filter_rules_path),
                "favorites": str(favorites_path),
                "overrides": str(overrides_path),
                "snooze": str(snooze_path),
                "owned": str(owned_path),
            },
            "blocked_topics": filter_rules.blocked_topics,
            "excluded_from_scope": excluded_reasons,
            "inconsistencies": inconsistencies,
            "consistency_warnings": consistency_warnings,
            "progress": {
                "total": len(targets),
                "completed": 0,
                "matched": 0,
                "no_match": 0,
                "unknown": 0,
                "failed": 0,
                "needs_recovery": 0,
                "requests": 0,
                "tokens": 0,
            },
        }
        write_json_atomic(run_dir / "run.json", run_doc)

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "total_targets": len(targets),
        "recommended_count": len(rec_targets),
        "candidate_count": len(cand_targets),
        "snoozed_count": snoozed_count,
        "topics_count": len(filter_rules.blocked_topics),
        "excluded_summary": excluded_reasons,
    }


def _task_hash(run_id: str, skill_id: str, mat_fp: str, rules_fp: str) -> str:
    return _sha256_hex(f"{MATCH_MODE}|{run_id}|{skill_id}|{mat_fp}|{rules_fp}")[:16]


def match_tags(rules: FilterRules, target: dict) -> tuple[str, list[dict]]:
    tags = target["materials"].get("tags", [])
    by_name = {tag.strip().casefold(): (i, tag) for i, tag in enumerate(tags) if tag.strip()}
    assessments = []
    for rule in rules.blocked_topics:
        found = by_name.get(rule["name"].casefold())
        assessments.append({
            "topic_id": rule["topic_id"], "topic_name": rule["name"],
            "result": "match" if found else ("no_match" if by_name else "unknown"),
            "evidence_source": f"tags/{found[0]}" if found else "",
            "evidence_quote": found[1] if found else "",
            "evidence": "已有标签与黑名单完整匹配" if found else ("标签未命中" if by_name else "无标签，保留"),
        })
    result = "match" if any(a["result"] == "match" for a in assessments) else ("no_match" if by_name else "unknown")
    return result, assessments


def run_assessments(root_dir: str | Path, run_id: str) -> dict[str, Any]:
    """离线扫描全部快照；重复执行保留已完成结果，无任何模型请求。"""
    run_dir = _get_run_dir(Path(root_dir).resolve(), run_id)
    if not (run_dir / "run.json").exists():
        raise FileNotFoundError(f"请先执行 prepare：{run_id}")
    require_tag_run(read_json(run_dir / "run.json"))
    with file_lock(run_dir / ".batch.lock"):
        run_data = read_json(run_dir / "run.json")
        require_tag_run(run_data)
        targets = read_json(run_dir / "targets.json")["targets"]
        rules = _reconstruct_filter_rules(run_data["blocked_topics"])
        if rules.fingerprint != run_data["rules_fingerprint"]:
            raise ValueError("批次标签快照已变化，请重新 prepare")
        for target in targets:
            if _sha256_hex(_canonical_json(target["materials"]))[:16] != target["materials_fingerprint"]:
                raise ValueError("批次材料快照已变化，请重新 prepare")
        results_dir = run_dir / "results"
        results_dir.mkdir(exist_ok=True)
        progress = dict(total=len(targets), completed=0, matched=0, no_match=0,
                        unknown=0, failed=0, needs_recovery=0, requests=0, tokens=0)
        for target in targets:
            task_hash = _task_hash(run_id, target["skill_id"], target["materials_fingerprint"], rules.fingerprint)
            path = results_dir / f"{task_hash}.json"
            result, assessments = match_tags(rules, target)
            expected = {"task_hash": task_hash, "skill_id": target["skill_id"],
                        "status": "completed", "result": result, "topic_assessments": assessments}
            prior = read_json(path) if path.exists() else {}
            if any(prior.get(key) != value for key, value in expected.items()):
                write_json_atomic(path, {**expected, "completed_at": now_local().isoformat()})
            progress["completed"] += 1
            progress["matched" if result == "match" else result] += 1
        run_data["progress"] = progress
        run_data["status"] = "completed"
        write_json_atomic(run_dir / "run.json", run_data)
        return progress


def compute_results_fingerprint(results_dir: Path) -> str:
    """计算当前 results 目录下所有任务结果的集合指纹（涵盖状态、判定结果、主题明细与证据）。"""
    res_sig_list = []
    for p in sorted(results_dir.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            sid = r.get("skill_id")
            status = r.get("status")
            res = r.get("result")
            error = r.get("error")
            assessments = []
            for ta in r.get("topic_assessments") or []:
                assessments.append({
                    "topic_id": ta.get("topic_id"),
                    "result": ta.get("result"),
                    "evidence": ta.get("evidence"),
                    "evidence_quote": ta.get("evidence_quote"),
                    "evidence_source": ta.get("evidence_source"),
                })
            assessments.sort(key=lambda a: str(a.get("topic_id") or ""))
            res_sig_list.append({
                "skill_id": sid,
                "status": status,
                "result": res,
                "error": error,
                "topic_assessments": assessments,
            })
        except Exception:
            pass
    res_sig_list.sort(key=lambda x: str(x.get("skill_id") or ""))
    return _sha256_hex(_canonical_json(res_sig_list))[:16]


def render_report(root_dir: str | Path, run_id: str) -> str:
    """生成本地静态 HTML 审核报告，支持交互式选择和 review.json 导出。"""
    root = Path(root_dir).resolve()
    run_dir = _get_run_dir(root, run_id)
    if not (run_dir / "run.json").exists() or not (run_dir / "targets.json").exists():
        raise FileNotFoundError(f"批次快照未就绪：{run_id}")

    run_data = read_json(run_dir / "run.json")
    require_tag_run(run_data)
    targets_data = read_json(run_dir / "targets.json")
    targets_map = {t["skill_id"]: t for t in targets_data.get("targets", [])}

    results_dir = run_dir / "results"
    results: list[dict[str, Any]] = []
    for p in sorted(results_dir.glob("*.json")):
        try:
            results.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass

    # 关联结果与 target
    matched_items = []
    unknown_items = []
    no_match_items = []
    failed_items = []

    for r in results:
        sid = r.get("skill_id")
        t = targets_map.get(sid, {})
        status = r.get("status")
        res = r.get("result")

        merged = {
            **r,
            "name": t.get("name", sid),
            "url": t.get("url", ""),
            "original_status": t.get("original_status", ""),
            "is_snoozed": t.get("is_snoozed", False),
            "materials": t.get("materials", {}),
        }

        if status == "completed":
            if res == "match":
                matched_items.append(merged)
            elif res == "unknown":
                unknown_items.append(merged)
            elif res == "no_match":
                no_match_items.append(merged)
        else:
            failed_items.append(merged)

    # 规则、目标与结果集合指纹
    results_fp = compute_results_fingerprint(results_dir)
    rules_fp = run_data.get("rules_fingerprint", "")
    targets_fp = targets_data.get("targets_fingerprint", "")

    progress = run_data.get("progress") or {}
    is_finished = progress.get("completed", 0) == len(targets_map)

    # 默认建议清单
    default_review = {
        "run_id": run_id,
        "rules_fingerprint": rules_fp,
        "targets_fingerprint": targets_fp,
        "results_fingerprint": results_fp,
        "selected_skill_ids": sorted([m["skill_id"] for m in matched_items]),
        "exported_at": now_local().isoformat(),
    }
    # 默认建议清单始终存盘至 review-proposal.json
    write_json_atomic(run_dir / "review-proposal.json", default_review)

    # 人工审核选择 review.json 仅在尚未存在时初始化，绝不自动覆盖已有的人工审核文件，也不自动更新其指纹 (Issue 3)
    review_path = run_dir / "review.json"
    if not review_path.exists():
        write_json_atomic(review_path, default_review)
        persisted_selected = set(default_review.get("selected_skill_ids") or [])
    else:
        try:
            curr_rev = json.loads(review_path.read_text(encoding="utf-8"))
            persisted_selected = set(curr_rev.get("selected_skill_ids") or [])
        except Exception:
            persisted_selected = set(default_review.get("selected_skill_ids") or [])

    # 分区统计与逐标签统计
    rec_removed_count = sum(1 for m in matched_items if m["original_status"] == "recommended")
    cand_removed_count = sum(1 for m in matched_items if m["original_status"] == "candidate")
    snoozed_matched_count = sum(1 for m in matched_items if m.get("is_snoozed"))

    topic_match_counts: dict[str, int] = {}
    for m in matched_items:
        for ta in m.get("topic_assessments", []):
            if ta.get("result") == "match":
                tname = ta.get("topic_name") or ta.get("topic_id") or "未知主题"
                topic_match_counts[tname] = topic_match_counts.get(tname, 0) + 1

    processed_sids = {r.get("skill_id") for r in results}
    pending_items = [t for sid, t in targets_map.items() if sid not in processed_sids]

    # 构造 HTML
    def _esc(val: Any) -> str:
        return html.escape(str(val if val is not None else ""))

    matched_rows_html = []
    for idx, item in enumerate(matched_items):
        sid = item["skill_id"]
        topic_badges = []
        evidence_snippets = []
        for ta in item.get("topic_assessments", []):
            if ta.get("result") == "match":
                topic_badges.append(f"<span class='badge badge-match'>{_esc(ta.get('topic_name'))}</span>")
                evidence_snippets.append(
                    f"<div><strong>[{_esc(ta.get('evidence_source'))}]</strong> <em>“{_esc(ta.get('evidence_quote'))}”</em> — {_esc(ta.get('evidence'))}</div>"
                )

        badge_status = f"<span class='badge badge-status'>{_esc(item['original_status'])}</span>"
        if item.get("is_snoozed"):
            badge_status += " <span class='badge badge-snooze'>冷冻中</span>"

        url_html = f"<a href='{_esc(item['url'])}' target='_blank' rel='noopener'>{_esc(sid)}</a>" if item['url'].startswith(("http://", "https://")) else _esc(sid)

        # 页面根据已有审核选择初始化勾选状态 (Issue 2)
        is_checked = "checked" if sid in persisted_selected else ""

        matched_rows_html.append(f"""
        <tr data-skill-id='{_esc(sid)}'>
          <td style='text-align: center;'><input type='checkbox' class='item-checkbox' value='{_esc(sid)}' {is_checked} onchange='updateSelectionCount()'></td>
          <td><strong>{_esc(item['name'])}</strong><br><small>{url_html}</small><br>{badge_status}</td>
          <td>{' '.join(topic_badges)}</td>
          <td class='evidence-cell'>{''.join(evidence_snippets)}</td>
          <td><small>{_esc(item.get('materials', {}).get('summary_zh'))}</small></td>
        </tr>
        """)

    unknown_rows_html = []
    for item in unknown_items:
        reasons = [f"[{_esc(ta.get('topic_name'))}] {_esc(ta.get('evidence'))}" for ta in item.get("topic_assessments", []) if ta.get("result") == "unknown"]
        unknown_rows_html.append(f"""
        <tr>
          <td><strong>{_esc(item['name'])}</strong> ({_esc(item['skill_id'])})</td>
          <td>{_esc(item['original_status'])}</td>
          <td><small>{'<br>'.join(reasons) or _esc(item.get('error'))}</small></td>
        </tr>
        """)

    failed_rows_html = []
    for item in failed_items:
        failed_rows_html.append(f"""
        <tr>
          <td><strong>{_esc(item.get('name', item['skill_id']))}</strong> ({_esc(item['skill_id'])})</td>
          <td><span class='badge badge-status'>{_esc(item.get('status'))}</span></td>
          <td><small style='color:#cf222e;'>{_esc(item.get('error') or '筛选未完成，请重新执行 run')}</small></td>
        </tr>
        """)

    pending_rows_html = []
    for item in pending_items:
        pending_rows_html.append(f"""
        <tr>
          <td><strong>{_esc(item.get('name', item['skill_id']))}</strong> ({_esc(item['skill_id'])})</td>
          <td>{_esc(item.get('original_status'))}</td>
          <td><small>尚未筛选</small></td>
        </tr>
        """)

    no_match_rows_html = []
    for item in no_match_items:
        no_match_rows_html.append(f"""
        <tr>
          <td><strong>{_esc(item['name'])}</strong> ({_esc(item['skill_id'])})</td>
          <td>{_esc(item['original_status'])}</td>
          <td><small>{_esc(item.get('materials', {}).get('summary_zh'))}</small></td>
        </tr>
        """)

    topic_badges_summary = " ".join(
        f"<span class='badge badge-match'>{_esc(name)} ({count})</span>"
        for name, count in sorted(topic_match_counts.items(), key=lambda x: -x[1])
    )

    status_warning_html = ""
    if not is_finished:
        status_warning_html = f"""
        <div class='alert alert-warning'>
          ⚠️ <strong>警告：当前批次尚未完全遍历完成</strong>（当前进度 {progress.get('completed', 0)}/{progress.get('total', 0)}）。
          请勿在所有目标进入终态前执行生产应用。
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>存量黑名单过滤审核报告 - {run_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.5; color: #24292f; background: #f6f8fa; margin: 0; padding: 24px; }}
  .container {{ max-width: 1200px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 8px; border: 1px solid #d0d7de; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  h1, h2, h3 {{ color: #1f2328; border-bottom: 1px solid #d0d7de; padding-bottom: 8px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin: 20px 0; }}
  .stat-card {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; text-align: center; }}
  .stat-val {{ font-size: 26px; font-weight: bold; color: #0969da; }}
  .stat-val.match {{ color: #cf222e; }}
  .stat-val.unknown {{ color: #9a6700; }}
  .badge {{ display: inline-block; padding: 2px 6px; font-size: 12px; font-weight: 500; border-radius: 12px; }}
  .badge-match {{ background: #ffebe9; color: #cf222e; border: 1px solid #ff8182; }}
  .badge-status {{ background: #ddf4ff; color: #0969da; }}
  .badge-snooze {{ background: #fff8c5; color: #9a6700; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }}
  th, td {{ border: 1px solid #d0d7de; padding: 10px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f6f8fa; }}
  .evidence-cell {{ font-size: 13px; color: #57606a; }}
  .evidence-cell div {{ margin-bottom: 6px; }}
  .toolbar {{ display: flex; justify-content: space-between; align-items: center; margin: 16px 0; padding: 12px; background: #f6f8fa; border-radius: 6px; border: 1px solid #d0d7de; }}
  button {{ background: #1f883d; color: #fff; border: 1px solid rgba(27,31,36,0.15); padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }}
  button:hover {{ background: #1a7f37; }}
  .btn-outline {{ background: #fff; color: #24292f; border: 1px solid #d0d7de; margin-right: 8px; }}
  .btn-outline:hover {{ background: #f3f4f6; }}
  .alert {{ padding: 16px; border-radius: 6px; margin: 16px 0; }}
  .alert-warning {{ background: #fff8c5; border: 1px solid #d4a72c; color: #7d4e00; }}
  details {{ margin: 16px 0; padding: 12px; background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; }}
  summary {{ cursor: pointer; font-weight: 600; }}
</style>
</head>
<body>
<div class="container">
  <h1>🛡️ 存量目录黑名单一次性过滤审核报告</h1>
  <p>批次标识：<strong>{_esc(run_id)}</strong> | 规则指纹：<code>{_esc(rules_fp[:12])}</code> | 目标指纹：<code>{_esc(targets_fp[:12])}</code></p>
  
  {status_warning_html}

  <div class="stats-grid">
    <div class="stat-card"><div class="stat-val">{len(targets_map)}</div>目标总数</div>
    <div class="stat-card"><div class="stat-val">{progress.get('completed', 0)}</div>已筛选完成</div>
    <div class="stat-card"><div class="stat-val match">{len(matched_items)}</div>命中黑名单 (去重)</div>
    <div class="stat-card"><div class="stat-val match">{rec_removed_count} / {cand_removed_count}</div>推荐移除 / 候选移除</div>
    <div class="stat-card"><div class="stat-val">{snoozed_matched_count}</div>冷冻命中项</div>
    <div class="stat-card"><div class="stat-val unknown">{len(unknown_items)}</div>无标签，保留</div>
    <div class="stat-card"><div class="stat-val">{len(no_match_items)}</div>未命中</div>
    <div class="stat-card"><div class="stat-val">{len(failed_items)}</div>失败/需恢复</div>
    <div class="stat-card"><div class="stat-val">{progress.get('tokens', 0):,}</div>模型调用 Token（始终为 0）</div>
  </div>

  <div style="margin: 16px 0; padding: 12px; background: #f6f8fa; border-radius: 6px; border: 1px solid #d0d7de;">
    <strong>逐标签命中统计：</strong> {topic_badges_summary or '暂无命中'}
  </div>

  <h2>1. 命中黑名单待排除技能清单（已按审核选择勾选，共 {len(matched_items)} 条）</h2>
  <p>以下技能的已有标签与黑名单完整匹配。请核对命中标签；不想屏蔽的条目可取消勾选。</p>

  <div class="toolbar">
    <div>
      <button type="button" class="btn-outline" onclick="selectAll(true)">全选</button>
      <button type="button" class="btn-outline" onclick="selectAll(false)">取消全选</button>
      <span>当前已选中: <strong id="selectedCount">{len([m for m in matched_items if m['skill_id'] in persisted_selected])}</strong> / {len(matched_items)} 项</span>
    </div>
    <button type="button" onclick="downloadReviewJson()">📥 导出并下载 review.json</button>
  </div>

  <table>
    <thead>
      <tr>
        <th style="width: 40px; text-align: center;">选择</th>
        <th style="width: 250px;">技能信息</th>
        <th style="width: 180px;">命中标签</th>
        <th style="width: 380px;">证据回溯与解释</th>
        <th>原中文简述</th>
      </tr>
    </thead>
    <tbody>
      {''.join(matched_rows_html) if matched_rows_html else "<tr><td colspan='5' style='text-align:center;'>本次未检测到任何命中项</td></tr>"}
    </tbody>
  </table>

  <h2>2. 无标签技能清单 ({len(unknown_items)} 条)</h2>
  <p>没有可匹配的标签，保留原目录状态。</p>
  <table>
    <thead>
      <tr>
        <th>技能名称 (ID)</th>
        <th>原分区</th>
        <th>存疑原因诊断</th>
      </tr>
    </thead>
    <tbody>
      {''.join(unknown_rows_html) if unknown_rows_html else "<tr><td colspan='3' style='text-align:center;'>无存疑项目</td></tr>"}
    </tbody>
  </table>

  <h2>3. 失败与需恢复项目清单 ({len(failed_items)} 条)</h2>
  <p>未完成的条目保留原状态，可重新执行 run 完成离线筛选。</p>
  <table>
    <thead>
      <tr>
        <th>技能名称 (ID)</th>
        <th>状态</th>
        <th>错误/诊断信息</th>
      </tr>
    </thead>
    <tbody>
      {''.join(failed_rows_html) if failed_rows_html else "<tr><td colspan='3' style='text-align:center;'>无失败项目</td></tr>"}
    </tbody>
  </table>

  {f'''<h2>4. 未处理目标清单 ({len(pending_items)} 条)</h2>
  <table>
    <thead><tr><th>技能名称 (ID)</th><th>原分区</th><th>说明</th></tr></thead>
    <tbody>{"".join(pending_rows_html)}</tbody>
  </table>''' if pending_items else ''}

  <h2>5. 未命中技能抽查 ({len(no_match_items)} 条)</h2>
  <details>
    <summary>展开查看全部 {len(no_match_items)} 条未命中项以供抽查（点击展开）</summary>
    <table>
      <thead>
        <tr>
          <th>技能名称 (ID)</th>
          <th>原分区</th>
          <th>中文简述</th>
        </tr>
      </thead>
      <tbody>
        {''.join(no_match_rows_html) if no_match_rows_html else "<tr><td colspan='3' style='text-align:center;'>无未命中项目</td></tr>"}
      </tbody>
    </table>
  </details>

  <h2>6. 执行说明与后续步骤</h2>
  <ol>
    <li>在上方表格核对命中项，取消勾选任何误判条目。</li>
    <li>点击 <strong>“导出并下载 review.json”</strong>，将文件保存到 <code>data/local/topic-filter/{_esc(run_id)}/review.json</code>。</li>
    <li>执行预览核对：<code>.\\.venv\\Scripts\\python.exe tools/filter_existing_catalog.py apply --run-id {_esc(run_id)} --review data/local/topic-filter/{_esc(run_id)}/review.json --dry-run</code></li>
    <li>确认无误后执行正式应用：<code>.\\.venv\\Scripts\\python.exe tools/filter_existing_catalog.py apply --run-id {_esc(run_id)} --review data/local/topic-filter/{_esc(run_id)}/review.json --apply</code></li>
  </ol>
</div>

<script>
  const runId = {_canonical_json(run_id)};
  const rulesFingerprint = {_canonical_json(rules_fp)};
  const targetsFingerprint = {_canonical_json(targets_fp)};
  const resultsFingerprint = {_canonical_json(results_fp)};

  function updateSelectionCount() {{
    const checked = document.querySelectorAll('.item-checkbox:checked').length;
    document.getElementById('selectedCount').innerText = checked;
  }}

  function selectAll(val) {{
    document.querySelectorAll('.item-checkbox').forEach(cb => cb.checked = val);
    updateSelectionCount();
  }}

  function downloadReviewJson() {{
    const selected = [];
    document.querySelectorAll('.item-checkbox:checked').forEach(cb => selected.push(cb.value));
    selected.sort();
    const doc = {{
      run_id: runId,
      rules_fingerprint: rulesFingerprint,
      targets_fingerprint: targetsFingerprint,
      results_fingerprint: resultsFingerprint,
      selected_skill_ids: selected,
      exported_at: new Date().toISOString()
    }};
    const blob = new Blob([JSON.stringify(doc, null, 2)], {{ type: 'application/json' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'review.json';
    a.click();
    URL.revokeObjectURL(url);
  }}
</script>
</body>
</html>
"""
    report_file = run_dir / "report.html"
    report_file.write_text(html_content, encoding="utf-8")
    return str(report_file)


# ----------------------------------------------------------------------
# 5. 应用阶段：预览、多文件写入与离线同步 (Apply)
# ----------------------------------------------------------------------

def plan_apply(
    root_dir: str | Path,
    run_id: str,
    review_path: str | Path,
) -> dict[str, Any]:
    """生成应用计划与差异预览，持锁核对当前状态与冲突。"""
    root = Path(root_dir).resolve()
    run_dir = _get_run_dir(root, run_id)
    review_file = Path(review_path).resolve()
    if not review_file.exists():
        raise FileNotFoundError(f"审核选择文件不存在：{review_file}")

    review_data = read_json(review_file)
    run_data = read_json(run_dir / "run.json")
    require_tag_run(run_data)
    targets_data = read_json(run_dir / "targets.json")

    # 1. 强校验 review 与批次身份绑定
    if review_data.get("run_id") != run_id:
        raise ValueError(f"review.json 中的 run_id ({review_data.get('run_id')}) 与当前批次 ({run_id}) 不匹配")
    if review_data.get("rules_fingerprint") != run_data.get("rules_fingerprint"):
        raise ValueError("review.json 的规则指纹与当前批次规则快照不一致")
    if review_data.get("targets_fingerprint") != targets_data.get("targets_fingerprint"):
        raise ValueError("review.json 的目标指纹与当前批次目标快照不一致")

    results_dir = run_dir / "results"
    curr_results_fp = compute_results_fingerprint(results_dir)
    if review_data.get("results_fingerprint") != curr_results_fp:
        raise ValueError(
            f"review.json 的结果指纹 ({review_data.get('results_fingerprint')}) "
            f"与当前批次结果指纹 ({curr_results_fp}) 不一致，审核选择已过期或结果已变化，请重新导出审核清单"
        )

    selected_ids = set(review_data.get("selected_skill_ids") or [])

    # 读取 results，确保 selected 是合法 match 项的子集
    valid_matches: dict[str, dict] = {}
    for p in results_dir.glob("*.json"):
        r = json.loads(p.read_text(encoding="utf-8"))
        if r.get("status") == "completed" and r.get("result") == "match":
            valid_matches[r["skill_id"]] = r

    illegal_selected = selected_ids - set(valid_matches.keys())
    if illegal_selected:
        raise ValueError(f"review.json 包含未命中或非法的技能 ID：{', '.join(sorted(illegal_selected))}")

    # 确保当前批次所有目标均已进入终态
    results_map: dict[str, dict] = {}
    for p in results_dir.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            results_map[r["skill_id"]] = r
        except Exception:
            pass

    unfinished_targets = []
    for t in targets_data.get("targets", []):
        sid = t["skill_id"]
        res = results_map.get(sid)
        if not res or res.get("status") in ("pending", "started", "needs_recovery"):
            unfinished_targets.append(sid)

    if unfinished_targets:
        raise ValueError(
            f"当前批次尚有 {len(unfinished_targets)} 条目标未进入终态（未完成、运行中或需恢复），禁止执行应用！\n"
            f"请重新执行 run 完成离线筛选（例如: {unfinished_targets[0]}）。"
        )

    # 2. 持锁核对当前目录与治理配置
    catalog_path = Path(run_data["config_paths"]["catalog"])
    overrides_path = Path(run_data["config_paths"]["overrides"])
    snooze_path = Path(run_data["config_paths"]["snooze"])
    favorites_path = Path(run_data["config_paths"]["favorites"])
    owned_path = Path(run_data["config_paths"]["owned"])

    with catalog_session(catalog_path.parent):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        entries_map = {e["skill_id"]: e for e in catalog.get("entries", []) if e.get("skill_id")}

        fav_data = load_favorites(favorites_path)
        manual_picks = get_manual_picks(fav_data)

        ov_data = load_overrides(overrides_path)
        existing_exclusions = ov_data.get("manual_exclusions") or []
        active_exclusions = get_manual_exclusions(ov_data)

        sn_data = load_snooze(snooze_path)
        existing_snoozes = sn_data.get("snoozed") or []
        active_snoozes = get_active_snoozed(sn_data)

        owned_cfg = load_owned_config(owned_path)
        owned_ids = {item["skill_id"] for item in owned_cfg.get("items", [])}

        conflicts = []
        to_add = []
        to_reactivate = []
        already_active_excluded = []
        snoozes_to_remove = set()

        # 建立退休 exclusion 的映射 (sid -> item)
        retired_map: dict[str, dict] = {}
        for ex in existing_exclusions:
            sid = ex.get("skill_id")
            if sid and ex.get("retired_at"):
                retired_map[sid] = ex

        today_str = now_shanghai_date()

        for sid in sorted(selected_ids):
            entry = entries_map.get(sid)
            if not entry:
                conflicts.append({"skill_id": sid, "reason": "not_in_catalog", "message": "该技能已从主目录中消失"})
                continue

            # 校验送审材料是否仍与快照一致
            target_meta = next((t for t in targets_data.get("targets", []) if t["skill_id"] == sid), None)
            if target_meta:
                curr_mat = extract_materials(entry)
                curr_fp = _sha256_hex(_canonical_json(curr_mat))[:16]
                if curr_fp != target_meta.get("materials_fingerprint"):
                    conflicts.append({
                        "skill_id": sid,
                        "reason": "content_changed",
                        "message": "该技能送审材料内容已发生变更，原判定依据已失效",
                    })
                    continue

            # 已有人工排除（包括此前已应用该批次的情况，优先识别避免误判状态冲突）
            if sid in active_exclusions:
                already_active_excluded.append({
                    "skill_id": sid,
                    "existing_reason": active_exclusions[sid].get("reason"),
                })
                continue

            if entry.get("status") not in ("recommended", "candidate"):
                conflicts.append({"skill_id": sid, "reason": "status_changed", "message": f"状态已不再是推荐/候选（当前为 {entry.get('status')}）"})
                continue

            if sid in manual_picks or entry.get("manual_pick"):
                conflicts.append({"skill_id": sid, "reason": "starred_conflict", "message": "该技能当前已被收藏，受保护禁止排除"})
                continue

            if is_skill_owned(sid, owned_ids):
                conflicts.append({"skill_id": sid, "reason": "owned_conflict", "message": "该技能为官方已收录资产，受保护禁止排除"})
                continue

            # 命中标签名称
            res_item = valid_matches[sid]
            matched_names = [ta["topic_name"] for ta in res_item.get("topic_assessments", []) if ta.get("result") == "match"]
            topics_desc = "、".join(matched_names) or "标签黑名单"
            reason_str = f"存量标签过滤；批次 {run_id}；命中：{topics_desc}"

            # 冷冻冲突排查
            if sid in active_snoozes or any(s.get("skill_id") == sid for s in existing_snoozes):
                snoozes_to_remove.add(sid)

            # 退休记录就地重新激活
            if sid in retired_map:
                to_reactivate.append({
                    "skill_id": sid,
                    "reason": reason_str,
                    "added_at": today_str,
                })
                continue

            # 新增人工排除
            to_add.append({
                "skill_id": sid,
                "reason": reason_str,
                "added_at": today_str,
            })

        # 预先计算应用生效后的预期文件内容与摘要
        if snoozes_to_remove:
            expected_snoozed_list = [s for s in existing_snoozes if s.get("skill_id") not in snoozes_to_remove]
            expected_sn_data = deepcopy(sn_data)
            expected_sn_data["snoozed"] = expected_snoozed_list
            expected_sn_text = json.dumps(expected_sn_data, ensure_ascii=False, indent=2)
            expected_sn_hash = _sha256_hex(expected_sn_text)
        else:
            expected_sn_hash = _sha256_hex(snooze_path.read_text(encoding="utf-8"))

        if to_add or to_reactivate:
            expected_ov_exclusions = deepcopy(existing_exclusions)
            reactivate_map = {item["skill_id"]: item for item in to_reactivate}
            for ex in expected_ov_exclusions:
                ex_sid = ex.get("skill_id")
                if ex_sid in reactivate_map:
                    ex.pop("retired_at", None)
                    ex["reason"] = reactivate_map[ex_sid]["reason"]
                    ex["added_at"] = reactivate_map[ex_sid]["added_at"]
            existing_ex_ids = {e.get("skill_id") for e in expected_ov_exclusions}
            for item in to_add:
                if item["skill_id"] not in existing_ex_ids:
                    expected_ov_exclusions.append({
                        "skill_id": item["skill_id"],
                        "reason": item["reason"],
                        "added_at": item["added_at"],
                    })
                    existing_ex_ids.add(item["skill_id"])
            expected_ov_data = deepcopy(ov_data)
            expected_ov_data["manual_exclusions"] = expected_ov_exclusions
            expected_ov_text = json.dumps(expected_ov_data, ensure_ascii=False, indent=2)
            expected_ov_hash = _sha256_hex(expected_ov_text)
        else:
            expected_ov_hash = _sha256_hex(overrides_path.read_text(encoding="utf-8"))

        plan_doc = {
            "run_id": run_id,
            "review_file": str(review_file),
            "generated_at": now_local().isoformat(),
            "selected_count": len(selected_ids),
            "to_add_count": len(to_add),
            "to_reactivate_count": len(to_reactivate),
            "already_active_count": len(already_active_excluded),
            "snoozes_to_remove_count": len(snoozes_to_remove),
            "conflicts_count": len(conflicts),
            "conflicts": conflicts,
            "to_add": to_add,
            "to_reactivate": to_reactivate,
            "already_active_excluded": already_active_excluded,
            "snoozes_to_remove": sorted(list(snoozes_to_remove)),
            "files_before_digests": {
                "overrides": _sha256_hex(overrides_path.read_text(encoding="utf-8")),
                "snooze": _sha256_hex(snooze_path.read_text(encoding="utf-8")),
            },
            "files_expected_after_digests": {
                "overrides": expected_ov_hash,
                "snooze": expected_sn_hash,
            },
        }
        write_json_atomic(run_dir / "apply-plan.json", plan_doc)

    return plan_doc


def execute_apply(
    root_dir: str | Path,
    run_id: str,
    log: Callable[[str], None] = safe_print,
) -> dict[str, Any]:
    """执行实际的治理名单写入与离线目录同步，具备断点续写与幂等性保障。"""
    root = Path(root_dir).resolve()
    run_dir = _get_run_dir(root, run_id)
    require_tag_run(read_json(run_dir / "run.json"))
    manifest_file = run_dir / "apply-manifest.json"

    # 1. 优先检查是否此前已完成应用（幂等快速返回）
    if manifest_file.exists():
        manifest = read_json(manifest_file)
        if manifest.get("phase") == "completed":
            log(f"批次 #{run_id} 此前已完成应用，幂等返回无需重复写入。")
            return manifest
    else:
        manifest = {
            "run_id": run_id,
            "phase": "prepared",
            "started_at": now_local().isoformat(),
        }
        write_json_atomic(manifest_file, manifest)

    plan_file = run_dir / "apply-plan.json"
    if not plan_file.exists():
        raise FileNotFoundError(f"未找到 apply-plan.json，请先执行预览：{run_id}")

    plan = read_json(plan_file)
    if plan.get("conflicts_count", 0) > 0:
        raise RuntimeError(f"存在 {plan.get('conflicts_count')} 项冲突，禁止执行应用！请核对后重新生成审核选择。")

    run_data = read_json(run_dir / "run.json")
    catalog_path = Path(run_data["config_paths"]["catalog"])
    overrides_path = Path(run_data["config_paths"]["overrides"])
    snooze_path = Path(run_data["config_paths"]["snooze"])

    before_hashes = plan.get("files_before_digests") or {}
    expected_after_hashes = plan.get("files_expected_after_digests") or {}

    with catalog_session(catalog_path.parent):
        curr_ov_hash = _sha256_hex(overrides_path.read_text(encoding="utf-8"))
        curr_sn_hash = _sha256_hex(snooze_path.read_text(encoding="utf-8"))

        # 严格在锁内核对文件摘要 (Issue 4)
        # 核对 snooze.json:
        if manifest.get("phase") == "prepared":
            if curr_sn_hash == expected_after_hashes.get("snooze"):
                # 中断自愈：磁盘已写入预期内容但 manifest 尚未推进
                manifest["phase"] = "snooze_written"
                manifest["snooze_written_at"] = manifest.get("snooze_written_at") or now_local().isoformat()
                write_json_atomic(manifest_file, manifest)
            elif curr_sn_hash != before_hashes.get("snooze"):
                raise RuntimeError("snoozed.json 文件哈希与生成计划时不同，检测到外部并发修改，停止应用！")
        else:
            # 已进入 snooze_written 及后续阶段，必须严格保持预期写入后摘要，任何修改均视为外部篡改
            if curr_sn_hash != expected_after_hashes.get("snooze"):
                raise RuntimeError("snoozed.json 处于写入后阶段但哈希不匹配预期摘要，检测到外部并发修改或异常篡改，停止应用！")

        # 核对 overrides.json:
        if manifest.get("phase") in ("prepared", "snooze_written"):
            if curr_ov_hash == expected_after_hashes.get("overrides"):
                # 中断自愈：磁盘已写入预期内容但 manifest 尚未推进
                manifest["phase"] = "exclusions_written"
                manifest["exclusions_written_at"] = manifest.get("exclusions_written_at") or now_local().isoformat()
                write_json_atomic(manifest_file, manifest)
            elif curr_ov_hash != before_hashes.get("overrides"):
                raise RuntimeError("overrides.json 文件哈希与生成计划时不同，检测到外部并发修改，停止应用！")
        else:
            # 已进入 exclusions_written 及后续阶段，必须严格保持预期写入后摘要，任何修改均视为外部篡改
            if curr_ov_hash != expected_after_hashes.get("overrides"):
                raise RuntimeError("overrides.json 处于写入后阶段但哈希不匹配预期摘要，检测到外部并发修改或异常篡改，停止应用！")
        # 阶段 1: 移除冷冻冲突
        if manifest.get("phase") in ("prepared",):
            snoozes_to_remove = set(plan.get("snoozes_to_remove") or [])
            if snoozes_to_remove:
                sn_data = load_snooze(snooze_path)
                original_snoozed = sn_data.get("snoozed") or []
                filtered_snoozed = [s for s in original_snoozed if s.get("skill_id") not in snoozes_to_remove]
                sn_data["snoozed"] = filtered_snoozed
                write_json_atomic(snooze_path, sn_data)

            manifest["phase"] = "snooze_written"
            manifest["snooze_written_at"] = now_local().isoformat()
            write_json_atomic(manifest_file, manifest)

        # 阶段 2: 写入 overrides 人工排除名单
        if manifest.get("phase") == "snooze_written":
            ov_data = load_overrides(overrides_path)
            existing_exclusions = list(ov_data.get("manual_exclusions") or [])

            # 重新激活退休项
            reactivate_map = {item["skill_id"]: item for item in plan.get("to_reactivate") or []}
            for ex in existing_exclusions:
                sid = ex.get("skill_id")
                if sid in reactivate_map:
                    ex.pop("retired_at", None)
                    ex["reason"] = reactivate_map[sid]["reason"]
                    ex["added_at"] = reactivate_map[sid]["added_at"]

            # 新增项
            to_add_list = plan.get("to_add") or []
            existing_ids = {e.get("skill_id") for e in existing_exclusions}
            for item in to_add_list:
                if item["skill_id"] not in existing_ids:
                    existing_exclusions.append({
                        "skill_id": item["skill_id"],
                        "reason": item["reason"],
                        "added_at": item["added_at"],
                    })
                    existing_ids.add(item["skill_id"])

            ov_data["manual_exclusions"] = existing_exclusions
            write_json_atomic(overrides_path, ov_data)

            manifest["phase"] = "exclusions_written"
            manifest["exclusions_written_at"] = now_local().isoformat()
            write_json_atomic(manifest_file, manifest)

        # 阶段 3: 执行离线目录同步
        if manifest.get("phase") == "exclusions_written":
            sync_res = sync_config_offline(root)
            manifest["phase"] = "catalog_synced"
            manifest["sync_result"] = sync_res
            manifest["completed_at"] = now_local().isoformat()
            write_json_atomic(manifest_file, manifest)

        manifest["files_after_digests"] = {
            "overrides": _sha256_hex(overrides_path.read_text(encoding="utf-8")),
            "snooze": _sha256_hex(snooze_path.read_text(encoding="utf-8")),
        }
        manifest["phase"] = "completed"
        write_json_atomic(manifest_file, manifest)

    return manifest


def filter_and_apply(root_dir: str | Path) -> int:
    """一键把命中标签的条目加入人工排除；整个过程不需交互。"""
    root = Path(root_dir).resolve()
    with catalog_session(root / "data"):
        # 上次若已开始写入，先用原计划完成同步，再筛选最新目录。
        batches = root / "data/local/topic-filter"
        for manifest_path in sorted(batches.glob("*/apply-manifest.json")):
            manifest = read_json(manifest_path)
            if manifest.get("phase") == "completed":
                continue
            previous = read_json(manifest_path.parent / "run.json")
            if previous.get("automatic") and previous.get("match_mode") == MATCH_MODE:
                execute_apply(root, previous["run_id"])

        rules = load_tag_rules(_resolve_config_path(root, "blocked-tags.json", "governance"))
        if not rules.has_evaluation_rules:
            safe_print("标签黑名单为空，无需过滤。")
            return 0

        prepared = prepare_run(root)
        run_id = prepared["run_id"]
        run_dir = Path(prepared["run_dir"])
        run_data = read_json(run_dir / "run.json")
        run_data["automatic"] = True
        write_json_atomic(run_dir / "run.json", run_data)
        progress = run_assessments(root, run_id)
        selected = []
        for path in (run_dir / "results").glob("*.json"):
            result = read_json(path)
            if result.get("result") == "match":
                selected.append(result["skill_id"])
        review_path = run_dir / "review.json"
        write_json_atomic(review_path, {
            "run_id": run_id,
            "rules_fingerprint": run_data["rules_fingerprint"],
            "targets_fingerprint": run_data["targets_fingerprint"],
            "results_fingerprint": compute_results_fingerprint(run_dir / "results"),
            "selected_skill_ids": sorted(selected),
        })
        plan_apply(root, run_id, review_path)
        execute_apply(root, run_id)
        safe_print(f"过滤完成：{progress['matched']} 个技能已加入人工排除名单，页面已同步，刷新前端即可。")
        return progress["matched"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="直接启动即可按标签过滤、加入人工排除并同步页面；全程不调用 AI")
    parser.add_argument("--root", default=str(ROOT), help="项目根目录（默认自动检测）")
    subparsers = parser.add_subparsers(dest="subcommand")

    # prepare
    p_prep = subparsers.add_parser("prepare", help="准备批次快照（离线，无模型调用）")
    p_prep.add_argument("--run-id", default=None, help="批次 ID（省略则自动生成）")

    # run
    p_run = subparsers.add_parser("run", help="按已有标签全量离线筛选，不调用模型")
    p_run.add_argument("--run-id", required=True, help="批次 ID")

    # report
    p_rep = subparsers.add_parser("report", help="生成离线 HTML 审核报告与 review.json")
    p_rep.add_argument("--run-id", required=True, help="批次 ID")

    # apply
    p_app = subparsers.add_parser("apply", help="预览或应用审核通过的屏蔽清单")
    p_app.add_argument("--run-id", required=True, help="批次 ID")
    p_app.add_argument("--review", default=None, help="review.json 路径（省略时自动定位到批次目录下的 review.json）")
    g_app = p_app.add_mutually_exclusive_group()
    g_app.add_argument("--dry-run", action="store_true", help="仅保存预览计划，不修改目录和屏蔽名单（默认）")
    g_app.add_argument("--apply", action="store_true", help="执行实际写入与离线目录同步")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.subcommand is None:
            filter_and_apply(root)
            return 0

        elif args.subcommand == "prepare":
            res = prepare_run(root, args.run_id)
            safe_print(f"✅ 快照准备成功：批次 #{res['run_id']}")
            safe_print(f"   目标技能：{res['total_targets']} 条（推荐 {res['recommended_count']} · 候选 {res['candidate_count']} · 冷冻中 {res['snoozed_count']}）")
            safe_print(f"   标签黑名单：{res['topics_count']} 个标签")
            safe_print(f"   保护排除：已排除 {res['excluded_summary']['manual_excluded']} · 已收藏 {res['excluded_summary']['favorited']} · 已收录 {res['excluded_summary']['owned']}")
            safe_print(f"   快照目录：{res['run_dir']}")
            return 0

        elif args.subcommand == "run":
            prog = run_assessments(root, args.run_id)
            safe_print(f"离线筛选完成：{prog['completed']}/{prog['total']}（命中 {prog['matched']} · 未命中 {prog['no_match']} · 无标签 {prog['unknown']}）")
            return 0

        elif args.subcommand == "report":
            report_path = render_report(root, args.run_id)
            safe_print(f"✅ 审核报告已生成：{report_path}")
            safe_print(f"   默认选择清单已保存：{Path(report_path).parent / 'review.json'}")
            return 0

        elif args.subcommand == "apply":
            require_tag_run(read_json(_get_run_dir(root, args.run_id) / "run.json"))
            is_apply = bool(args.apply)
            manifest_file = _get_run_dir(root, args.run_id) / "apply-manifest.json"
            if is_apply and manifest_file.exists():
                existing_manifest = read_json(manifest_file)
                if existing_manifest.get("phase") == "completed":
                    safe_print(f"🎉 批次 #{args.run_id} 此前已应用完成（幂等返回）。阶段: completed")
                    if existing_manifest.get("sync_result"):
                        sr = existing_manifest["sync_result"]
                        safe_print(f"   已完成离线目录同步：主索引 {sr.get('catalog_path')} · 页面数据 {sr.get('page_path')}")
                    return 0

            review_file = Path(args.review).resolve() if args.review else (_get_run_dir(root, args.run_id) / "review.json")
            plan = plan_apply(root, args.run_id, review_file)
            if not is_apply:
                safe_print(f"📋 变更预览（Dry Run）：批次 #{args.run_id}")
                safe_print(f"   待处理选择：{plan['selected_count']} 条")
                safe_print(f"   拟新增排除：{plan['to_add_count']} 条")
                safe_print(f"   拟就地重新激活：{plan['to_reactivate_count']} 条")
                safe_print(f"   已在活跃排除中：{plan['already_active_count']} 条")
                safe_print(f"   拟移除冷冻冲突：{plan['snoozes_to_remove_count']} 条")
                if plan['conflicts_count'] > 0:
                    safe_print(f"   ❌ 发现 {plan['conflicts_count']} 项冲突（禁止应用）：")
                    for c in plan['conflicts']:
                        safe_print(f"      - {c['skill_id']}: {c['message']}")
                else:
                    safe_print("   ✨ 校验通过，无冲突。如需真正写入，请追加 --apply 参数执行。")
                return 0
            else:
                manifest = execute_apply(root, args.run_id)
                safe_print(f"🎉 变更应用完成！阶段: {manifest['phase']}")
                if manifest.get("sync_result"):
                    sr = manifest["sync_result"]
                    safe_print(f"   已完成离线目录同步：主索引 {sr['catalog_path']} · 页面数据 {sr['page_path']}")
                return 0

    except Exception as exc:
        safe_print(f"❌ 执行失败（{type(exc).__name__}）：{exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
