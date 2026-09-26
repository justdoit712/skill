"""定向查找报告生成、脱敏与公共快照投影。

职责：
1. 渲染本地详细 Markdown 报告（`render_find_markdown_report`）；
2. 白名单安全脱敏投影（`build_public_find_projection` / `sanitize_report_for_public`）：
   - 彻底剔除本地绝对路径（C:\\, D:\\, /Users/ 等）与内部诊断路径；
   - 结构兼容前端双模消费；
3. 公共快照发布条件矩阵判断（`should_update_public_snapshot`）：
   - 正常完成有推荐 -> 更新；
   - 正常完成 0 推荐（包括全部已收录） -> 覆盖写空（防止旧主题残留误导）；
   - 中断/熔断但有部分条目 -> 更新带状态的部分结果；
   - 早期完全失败 0 成功 -> 坚决保留旧快照，不写空破坏已有展示；
4. 原子写入本地及公共报告（`write_local_report`, `update_public_snapshot`）。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, quote

from src.infra.files import write_json_atomic, write_text_atomic, file_lock, read_json
from src.shared.runtime import now_local

STATUS_SUPPORTED = "supported"


PUBLIC_REASONS = {"completed", "target_reached", "candidates_exhausted", "all_candidates_owned", "token_limit", "evaluation_limit",
                  "usage_unknown", "model_failures", "interrupted", "search_failed", "expansion_failed",
                  "material_failed", "plan_failed", "execution_error", "artifact_failed", "round_limit", "reflection_failed"}


def _escape_markdown(value):
    if isinstance(value, str):
        return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", value).replace("\r", " ").replace("\n", " ")
    if isinstance(value, list):
        return [_escape_markdown(v) for v in value]
    if isinstance(value, dict):
        return {k: _markdown_url(v) if k in {"url", "repo_url"} else _escape_markdown(v)
                for k, v in value.items()}
    return value


def _markdown_url(value):
    if not isinstance(value, str):
        return "#"
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "#"
    except ValueError:
        return "#"
    return quote(value, safe=":/?=&%#@+;,_~.-")


def _count(value):
    return f"{value:,}" if type(value) is int and value >= 0 else "未知"


def render_find_markdown_report(report: dict[str, Any]) -> str:
    """将查找结果格式化为高可读性的 Markdown 报告。"""
    raw_stop_reason = str(report.get("stop_reason") or "")
    raw_coverage_incomplete = bool(report.get("coverage_incomplete"))
    report = _escape_markdown(report)
    topic = report.get("topic", "")
    params = report.get("parameters", {})
    usage = report.get("usage") or {}
    plan = report.get("plan") or {}
    search = report.get("search") or {}

    skipped_str = f"，已收录跳过 {search['skipped_owned']} 个" if search.get("skipped_owned") else ""
    lines = [
        "# 定向查找 Skill 报告",
        "",
        f"- **需求意图**：{topic}",
        f"- **分析归纳**：{plan.get('intent', '（未完成）')}",
        f"- **运行编号**：`{report.get('run_id')}`（{report.get('started_at', '')}）",
        f"- **运行状态**：{report.get('stop_reason') or report.get('status')}",
        f"- **检索轮次**：{search.get('current_round', 1)} / {params.get('max_rounds', 1)}（包含首轮）",
        f"- **检查范围**：检索查询 {len(search.get('queries_executed', []))} 条，发现仓库 {search.get('repos_discovered', 0)} 个，展开技能文件 {search.get('candidates_found', 0)} 个{skipped_str}，实际评估 {report.get('evaluated_count', 0)} 个",
        f"- **Token 用量**：输入 {_count(usage.get('prompt_tokens'))}，输出 {_count(usage.get('completion_tokens'))}，总计 {_count(usage.get('total_tokens'))} Token（本次停止阈值 {_count(params.get('max_tokens'))}）",
    ]
    if raw_coverage_incomplete:
        lines.append("- **检索覆盖**：本次检索覆盖不完整，部分来源读取失败或超出读取范围。")
    if search.get("candidates_found") and search.get("skipped_owned") == search.get("candidates_found"):
        lines.append("- 本次发现的候选已全部收录。")
    for round_info in search.get("rounds_history", []):
        lines.append(f"- 第 {round_info.get('round')} 轮：{round_info.get('strategy')}，新增仓库 {round_info.get('new_repos', 0)} 个，"
                     f"新增候选 {round_info.get('candidates', 0)} 个，已评估 {round_info.get('evaluated', 0)} 个。")
    lines.extend([
        "",
        "---",
        "",
        f"## 优先查看（短名单 {len(report.get('shortlist', []))} 个）",
        "",
    ])

    shortlist = report.get("shortlist") or []
    if not shortlist:
        if raw_stop_reason == "all_candidates_owned":
            lines.append("本次发现的候选已全部收录。\n")
        else:
            lines.append("本次未找到完全符合所有必需条件且说明完整的强匹配技能。请参考下方的相关备选与差距说明。\n")
    else:
        for idx, item in enumerate(shortlist, start=1):
            cand = item.get("candidate", {})
            ev = item.get("evaluation", {})
            name = cand.get("name") or cand.get("skill_id")
            url = cand.get("url")

            lines.append(f"### {idx}. [{name}]({url})")
            lines.append(f"- **上游仓库**：[{cand.get('author')}/{cand.get('name')}]({cand.get('repo_url')}) （路径：`{cand.get('path')}`）")
            lines.append(f"- **能做什么**：{ev.get('summary_zh') or '（无简述）'}")
            lines.append(f"- **优先查看理由**：{ev.get('why_consider') or '客观材料表明与需求高度契合'}")

            # 匹配证据
            ev_list = []
            for cr in ev.get("criteria_results", []):
                if cr.get("status") == STATUS_SUPPORTED:
                    cid = cr.get("criterion_id")
                    quotes = [e.get("quote") for e in cr.get("evidence", []) if e.get("quote")]
                    q_str = f'（引文："{quotes[0]}"）' if quotes else ""
                    ev_list.append(f"  - `{cid}`：{cr.get('explanation') or '支持'} {q_str}")
            if ev_list:
                lines.append("- **已核验的能力证据**：")
                lines.extend(ev_list)

            # 使用方式与依赖
            if ev.get("usage_zh"):
                lines.append(f"- **使用指引**：{ev.get('usage_zh')}")
            if ev.get("dependencies"):
                lines.append(f"- **声明依赖**：{', '.join(ev.get('dependencies'))}")
            if ev.get("limitations"):
                lines.append(f"- **主要限制或未确认项**：{', '.join(ev.get('limitations'))}")
            lines.append("")

    lines.extend(["---", "", f"## 相关备选及差距（{len(report.get('alternatives', []))} 个）", ""])

    alternatives = report.get("alternatives") or []
    if not alternatives:
        lines.append("无备选技能。\n")
    else:
        for idx, item in enumerate(alternatives, start=1):
            cand = item.get("candidate", {})
            ev = item.get("evaluation", {})
            name = cand.get("name") or cand.get("skill_id")
            url = cand.get("url")

            unsupported_reasons = []
            for cr in ev.get("criteria_results", []):
                if cr.get("status") != STATUS_SUPPORTED:
                    unsupported_reasons.append(f"`{cr.get('criterion_id')}` 状态为 {cr.get('status')}")

            gap_text = f"主要差距：{', '.join(unsupported_reasons)}" if unsupported_reasons else "说明质量或观察项有待完善"
            lines.append(f"{idx}. **[{name}]({url})** — *{ev.get('summary_zh') or '相关技能'}*")
            lines.append(f"   - 匹配度：`{ev.get('match')}` | 说明完整度：`{ev.get('documentation')}`")
            lines.append(f"   - {gap_text}")
            lines.append("")

    return "\n".join(lines) + "\n"


def _sanitize_card(item: dict[str, Any]) -> dict[str, Any]:
    """对单张技能卡片脱敏：仅保留安全字段，双模兼顾嵌套与扁平结构。"""
    cand = item.get("candidate") or item
    ev = item.get("evaluation") or item

    c_info = {
        "skill_id": str(cand.get("skill_id") or ""),
        "name": str(cand.get("name") or ""),
        "repo_url": str(cand.get("repo_url") or ""),
        "url": str(cand.get("url") or ""),
        "author": str(cand.get("author") or ""),
        "path": str(cand.get("path") or ""),
    }

    raw_cr = ev.get("criteria_results") or []
    sanitized_cr = []
    matched_criteria = []
    verified_evidence = []

    for cr in raw_cr:
        if not isinstance(cr, dict):
            continue
        cid = str(cr.get("criterion_id") or "")
        c_status = str(cr.get("status") or "unknown")
        if c_status == STATUS_SUPPORTED:
            matched_criteria.append(cid)

        ev_items = []
        for e in cr.get("evidence") or []:
            if isinstance(e, dict):
                ev_obj = {
                    "source_path": str(e.get("source_path") or ""),
                    "start_line": e.get("start_line"),
                    "end_line": e.get("end_line"),
                    "quote": str(e.get("quote") or ""),
                }
                ev_items.append(ev_obj)
                if c_status == STATUS_SUPPORTED and ev_obj["quote"]:
                    verified_evidence.append(ev_obj)

        sanitized_cr.append(
            {
                "criterion_id": cid,
                "status": c_status,
                "explanation": str(cr.get("explanation") or ""),
                "evidence": ev_items,
            }
        )

    e_info = {
        "match": str(ev.get("match") or "none"),
        "documentation": str(ev.get("documentation") or "insufficient"),
        "summary_zh": str(ev.get("summary_zh") or ""),
        "why_consider": str(ev.get("why_consider") or ""),
        "usage_zh": str(ev.get("usage_zh") or ""),
        "dependencies": [str(d) for d in (ev.get("dependencies") or [])],
        "limitations": [str(l) for l in (ev.get("limitations") or [])],
        "criteria_results": sanitized_cr,
    }

    return {
        "candidate": c_info,
        "evaluation": e_info,
        # 扁平快捷字段
        "skill_id": c_info["skill_id"],
        "name": c_info["name"],
        "url": c_info["url"],
        "repo_url": c_info["repo_url"],
        "match_level": e_info["match"],
        "summary": e_info["summary_zh"],
        "matched_criteria": matched_criteria,
        "verified_evidence": verified_evidence,
    }


def sanitize_report_for_public(report: dict[str, Any]) -> dict[str, Any]:
    """白名单安全脱敏投影转换器：严禁泄露本机绝对路径、文件树全量元数据或凭据。"""
    params = report.get("parameters") or {}
    usage = report.get("usage") or {}
    plan = report.get("plan") or {}
    search = report.get("search") or {}

    sanitized_queries = []
    for qe in search.get("queries_executed") or []:
        if isinstance(qe, dict):
            sanitized_queries.append(
                {
                    "query": str(qe.get("query") or ""),
                    "ok": bool(qe.get("ok")),
                    "repos_returned": int(qe.get("repos_returned") or 0),
                    "error": "search_failed" if qe.get("error") else None,
                }
            )

    sanitized_shortlist = [_sanitize_card(item) for item in (report.get("shortlist") or [])]
    sanitized_alternatives = [_sanitize_card(item) for item in (report.get("alternatives") or [])]

    projection = {
        "schema_version": "1.0.0",
        "run_id": str(report.get("run_id") or ""),
        "started_at": str(report.get("started_at") or ""),
        "updated_at": str(report.get("updated_at") or now_local().isoformat()),
        "topic": str(report.get("topic") or ""),
        "status": str(report.get("status") or ""),
        "stop_reason": report.get("stop_reason") if report.get("stop_reason") in PUBLIC_REASONS else "execution_error" if report.get("stop_reason") else "",
        "coverage_incomplete": bool(report.get("coverage_incomplete")),
        "errors": [{"stage": e.get("stage"), "code": e.get("code")} for e in report.get("errors", [])],
        "parameters": {
            "limit": params.get("limit", 5),
            "max_evaluations": params.get("max_evaluations", 20),
            "max_tokens": params.get("max_tokens", 200000),
            "max_rounds": params.get("max_rounds", 1),
        },
        "model": str(report.get("model") or ""),
        "plan": {
            "intent": str(plan.get("intent") or ""),
            "criteria": plan.get("criteria") or [],
        },
        "search": {
            "queries_executed": sanitized_queries,
            "repos_discovered": int(search.get("repos_discovered") or 0),
            "candidates_found": int(search.get("candidates_found") or 0),
            "skipped_owned": int(search.get("skipped_owned") or 0),
            "current_round": int(search.get("current_round") or 0),
            "rounds_history": [{"round": r.get("round"), "strategy": r.get("strategy"),
                                "repos": r.get("new_repos", 0), "candidates": r.get("candidates", 0),
                                "evaluated": r.get("evaluated", 0)} for r in search.get("rounds_history", [])],
        },
        "evaluation_attempts": report.get("evaluation_attempts"),
        "evaluated_count": report.get("evaluated_count"),
        "shortlist_count": len(sanitized_shortlist),
        "alternatives_count": len(sanitized_alternatives),
        "shortlist": sanitized_shortlist,
        "alternatives": sanitized_alternatives,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "unknown_usage_requests": usage.get("unknown_usage_requests"),
            "requests": usage.get("requests"),
        },
    }

    return projection


def build_public_find_projection(report: dict[str, Any]) -> dict[str, Any]:
    """与 sanitize_report_for_public 等价的别名接口。"""
    return sanitize_report_for_public(report)


def should_update_public_snapshot(report: dict[str, Any]) -> bool:
    """根据规范 4.2 公共快照更新条件矩阵判定是否应该写出 public/data/find-report.json：

    1. 正常完成且有推荐结果 (evaluated > 0 或 shortlist/alternatives > 0): True
    2. 正常完成但 0 匹配 (status in (completed, target_reached) 或 candidates_exhausted, all_candidates_owned): True (写入空结果快照)
    3. 异常中断/熔断，但已有部分有效评估 (evaluated_count > 0 或 shortlist/alternatives > 0): True
    4. 完全失败/启动错误，0 成功 (0 evaluated, status is error / plan_failed / search_failed 等): False (保留上次有效快照)
    """
    status = str(report.get("status") or "").lower()
    stop_reason = str(report.get("stop_reason") or "").lower()
    evaluated_count = int(report.get("evaluated_count") or 0)
    shortlist_len = len(report.get("shortlist") or [])
    alternatives_len = len(report.get("alternatives") or [])
    has_any_items = (shortlist_len + alternatives_len > 0) or (evaluated_count > 0)

    # 4. 完全失败/启动错误，无任何有效条目（0 成功） -> 绝不破坏已有展示，坚决保留旧快照
    if not has_any_items:
        if status == "error" or "failed" in stop_reason or "error" in stop_reason or stop_reason in ("plan_failed", "search_failed"):
            return False

    # 1 & 2. 正常完成（包含有推荐或 0 匹配自然结束）
    if status == "completed" or stop_reason in ("target_reached", "candidates_exhausted", "all_candidates_owned", "completed", "round_limit"):
        return True

    # 3. 异常中断/熔断，但已有部分有效条目
    if has_any_items and (
        status in ("stopped", "interrupted")
        or stop_reason in ("token_limit", "evaluation_limit", "usage_unknown", "model_failures", "interrupted")
    ):
        return True

    return has_any_items and status == "error"


def write_local_report(report: dict[str, Any], run_dir: Path) -> None:
    """原子写入本地事实报告 JSON 与 Markdown。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(run_dir / "report.json", report)
    md_text = render_find_markdown_report(report)
    write_text_atomic(run_dir / "report.md", md_text)


def update_public_snapshot(report: dict[str, Any], public_data_dir: Path) -> bool:
    """按条件矩阵安全更新公共展示快照。返回是否执行了写更新。"""
    if not should_update_public_snapshot(report):
        return False

    public_data_dir.mkdir(parents=True, exist_ok=True)
    projection = sanitize_report_for_public(report)
    target_file = public_data_dir / "find-report.json"
    with file_lock(public_data_dir / ".find-report.lock"):
        write_json_atomic(target_file, projection)
    return True


def rebuild_find_report(run_dir: Path, public_data_dir: Path | None = None) -> dict:
    """Rebuild derived reports from saved facts; never calls a model."""
    report = read_json(Path(run_dir) / "report.json")
    if report.get("schema_version") not in (None, "1.0.0"):
        raise ValueError("不支持的报告 schema_version")
    write_local_report(report, Path(run_dir))
    if public_data_dir is not None:
        update_public_snapshot(report, public_data_dir)
    return report
