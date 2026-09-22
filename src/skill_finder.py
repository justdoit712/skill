"""定向查找技能核心编排器。

依据 docs/定向查找Skill实施方案.md：
- 需求驱动：输入自然语言需求，自动生成计划并评估；
- 目录规则隔离：不依赖目录的分类、排除词、黑名单与冷冻规则；
- 零目录副作用：不修改 data/catalog.json、周账本或候选池；
- 结果保真：代码级客观证据核验，杜绝模型幻觉；
- 独立报告输出至 data/local/find-skills/<timestamp-uuid>/。
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .budget import _write_json_atomic, now_local
from .dedupe import candidate_from_repo, content_fingerprint, make_skill_id
from .discover import (
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_SEARCH_ENDPOINT,
    RETRYABLE_STATUS,
    USER_AGENT,
    apply_github_auth,
    expand_repo_skills,
)
from .evaluate import call_model, resolve_api_key
from .fetch import fetch_text
from .find_evaluate import (
    KIND_QUALITY_SIGNAL,
    KIND_REQUIRED,
    MATCH_STRONG,
    STATUS_SUPPORTED,
    build_evaluation_prompt,
    build_plan_prompt,
    parse_query_plan,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from .models import Candidate
from .usage import UsageTotals

DEFAULT_LIMIT = 5
DEFAULT_MAX_EVALUATIONS = 20
DEFAULT_MAX_TOKENS = 200000

MAX_REPOS_TO_EXPAND = 20
MAX_SEARCH_REPOS_PER_QUERY = 20
MAX_FILES_PER_REPO = 10
MAX_TOTAL_FILES_TO_FETCH = 80
MAX_PRIMARY_FILE_BYTES = 65536
MAX_TOTAL_MATERIAL_BYTES = 98304
MAX_REFERENCED_FILES = 2

PLAN_MAX_OUTPUT_TOKENS = 4000
EVAL_MAX_OUTPUT_TOKENS = 10000

STATUS_TARGET_REACHED = "target_reached"
STATUS_COMPLETED = "completed"
STATUS_TOKEN_LIMIT = "token_limit"
STATUS_EVALUATION_LIMIT = "evaluation_limit"
STATUS_CANDIDATES_EXHAUSTED = "candidates_exhausted"
STATUS_MODEL_FAILURES = "model_failures"
STATUS_USAGE_UNKNOWN = "usage_unknown"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"


def _read_json_file(path: Path, default=None) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def load_finder_model_config(config_dir: str | Path = "config") -> dict[str, Any]:
    """读取模型配置，仅加载 model.local.json 或 model.example.json。"""
    base = Path(config_dir)
    local_cfg = base / "model.local.json"
    example_cfg = base / "model.example.json"

    if local_cfg.exists():
        cfg = _read_json_file(local_cfg)
    elif example_cfg.exists():
        cfg = _read_json_file(example_cfg)
    else:
        raise FileNotFoundError(f"未找到模型配置文件：{local_cfg} 或 {example_cfg}")

    if not isinstance(cfg, dict):
        raise ValueError("模型配置文件必须是 JSON 对象")

    endpoint = (cfg.get("endpoint") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not endpoint or not model:
        raise ValueError("模型配置缺少有效的 endpoint 或 model")

    # 禁用底层库嵌套重试，准确统计单次调用
    cfg_copy = deepcopy(cfg)
    cfg_copy.setdefault("request", {})["max_attempts"] = 1
    return cfg_copy


def load_finder_run_config(config_dir: str | Path = "config") -> dict[str, Any]:
    """读取定向查找配置，优先合并 find-skill.json 与 find-skill.local.json。"""
    base = Path(config_dir)
    res: dict[str, Any] = {}
    base_cfg = base / "find-skill.json"
    local_cfg = base / "find-skill.local.json"
    if base_cfg.exists():
        try:
            data = _read_json_file(base_cfg)
            if isinstance(data, dict):
                res.update(data)
        except Exception:
            pass
    if local_cfg.exists():
        try:
            data = _read_json_file(local_cfg)
            if isinstance(data, dict):
                res.update(data)
        except Exception:
            pass
    return res


def search_github_repos_for_query(
    query: str,
    *,
    per_page: int = MAX_SEARCH_REPOS_PER_QUERY,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session=None,
    sleep=time.sleep,
) -> tuple[bool, list[dict[str, str]], str | None]:
    """执行单个关键词的 GitHub 仓库搜索（不包含目录默认排除词）。"""
    import requests

    owns_session = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    sess.headers.setdefault("Accept", "application/vnd.github+json")
    apply_github_auth(sess)

    # 围绕搜索词和 "SKILL.md" in:readme 构造
    full_q = f'{query.strip()} "SKILL.md" in:readme'
    repos: list[dict[str, str]] = []

    try:
        response = sess.get(
            GITHUB_SEARCH_ENDPOINT,
            params={"q": full_q, "per_page": per_page},
            timeout=timeout,
        )
        if response.status_code >= 400:
            return False, [], f"HTTP {response.status_code}"
        payload = response.json()
        items = payload.get("items") or []
        for it in items:
            owner = ((it.get("owner") or {}).get("login") or "").lower()
            repo = (it.get("name") or "").lower()
            html_url = it.get("html_url") or f"https://github.com/{owner}/{repo}"
            desc = it.get("description") or ""
            if owner and repo:
                repos.append({"owner": owner, "repo": repo, "url": html_url, "description": desc})
        return True, repos, None
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"
    finally:
        if owns_session:
            sess.close()


def expand_and_collect_candidates(
    repos: list[dict[str, str]],
    *,
    max_repos: int = MAX_REPOS_TO_EXPAND,
    sleep=time.sleep,
    log=print,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """展开仓库文件树，收集具体 SKILL.md 候选。"""
    candidates: list[Candidate] = []
    expansion_logs: list[dict[str, Any]] = []
    seen_skills: set[str] = set()

    for r in repos[:max_repos]:
        owner, repo = r["owner"], r["repo"]
        key = f"{owner}/{repo}"
        paths, err = expand_repo_skills(owner, repo, sleep=sleep)
        truncated = bool(err and err.startswith("TREE_TRUNCATED"))

        expansion_logs.append(
            {
                "repo": key,
                "ok": err is None or truncated,
                "skills_found": len(paths),
                "truncated": truncated,
                "error": err,
            }
        )

        for p in paths:
            sid = make_skill_id(owner, repo, p)
            if sid not in seen_skills:
                seen_skills.add(sid)
                cand = candidate_from_repo(
                    owner=owner,
                    repo=repo,
                    path=p,
                    url=f"https://github.com/{owner}/{repo}/blob/HEAD/{p}",
                    repo_url=r["url"],
                    name=p.rsplit("/", 2)[-2] if "/" in p else repo,
                    description=r.get("description", ""),
                    discovered_at=datetime.now(timezone.utc).isoformat(),
                )
                candidates.append(cand)

    return candidates, expansion_logs


def schedule_candidates_fairly(candidates: list[Candidate]) -> list[Candidate]:
    """跨仓库公平轮转排序，避免首个大型合集垄断读取配额。"""
    by_repo: dict[str, list[Candidate]] = {}
    for c in candidates:
        r_key = f"{c.owner}/{c.repo}"
        by_repo.setdefault(r_key, []).append(c)

    ordered: list[Candidate] = []
    repo_keys = list(by_repo.keys())
    max_depth = max((len(lst) for lst in by_repo.values()), default=0)

    for depth in range(max_depth):
        for r_key in repo_keys:
            c_list = by_repo[r_key]
            if depth < len(c_list) and depth < MAX_FILES_PER_REPO:
                ordered.append(c_list[depth])
                if len(ordered) >= MAX_TOTAL_FILES_TO_FETCH:
                    return ordered
    return ordered


def extract_referenced_md_paths(base_path: str, markdown_text: str) -> list[str]:
    """从 SKILL.md 中提取相对引用的同仓库 Markdown 文件路径（最多 2 个）。"""
    base_dir = base_path.rsplit("/", 1)[0] if "/" in base_path else ""
    # 匹配 Markdown 相对链接，排除外链或锚点
    link_pattern = re.compile(r"\[.*?\]\((?!https?://|mailto:|#|/)([^)\s]+?\.md)\)")
    found: list[str] = []

    for rel in link_pattern.findall(markdown_text):
        rel = rel.split("?")[0].split("#")[0].strip()
        if ".." in rel:
            continue
        full_rel = f"{base_dir}/{rel}".strip("/") if base_dir else rel.strip("/")
        if full_rel != base_path and full_rel not in found:
            found.append(full_rel)
            if len(found) >= MAX_REFERENCED_FILES:
                break
    return found


def fetch_candidate_materials(
    candidate: Candidate,
    *,
    fetch_fn=None,
    sleep=time.sleep,
) -> tuple[bool, dict[str, str], str | None]:
    """抓取主 SKILL.md 及可选的关联引用说明文件（合计 <= 96 KiB）。"""
    fn = fetch_fn or fetch_text
    raw_url = candidate.url.replace("https://github.com/", "https://raw.githubusercontent.com/", 1).replace(
        "/blob/", "/", 1
    )

    fetched = fn(raw_url, max_bytes=MAX_PRIMARY_FILE_BYTES, sleep=sleep)
    if not fetched.ok or not fetched.text or fetched.truncated:
        return False, {}, fetched.reason_code or "FETCH_FAILED"

    primary_text = fetched.text
    candidate.content_fingerprint = content_fingerprint(primary_text)
    materials: dict[str, str] = {candidate.path: primary_text}
    total_bytes = len(primary_text.encode("utf-8"))

    # 尝试按需抓取最多 2 个同仓库引用的 Markdown 说明
    ref_paths = extract_referenced_md_paths(candidate.path, primary_text)
    for ref_p in ref_paths:
        remaining_budget = MAX_TOTAL_MATERIAL_BYTES - total_bytes
        if remaining_budget <= 2048:
            break
        ref_raw_url = f"https://raw.githubusercontent.com/{candidate.owner}/{candidate.repo}/HEAD/{ref_p}"
        ref_fetched = fn(ref_raw_url, max_bytes=remaining_budget, sleep=sleep)
        if ref_fetched.ok and ref_fetched.text and not ref_fetched.truncated:
            materials[ref_p] = ref_fetched.text
            total_bytes += len(ref_fetched.text.encode("utf-8"))

    return True, materials, None


# --------------------------------------------------------------------------
# 查找主控制器（SkillFinder）
# --------------------------------------------------------------------------


def _parse_int_val(val: Any, default: int) -> int:
    """支持 int 以及带空格、下划线、千分位逗号的表示（如 '200 000'、'200_000'、'200,000'）。"""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        clean = val.replace(" ", "").replace("_", "").replace(",", "").strip()
        try:
            return int(clean)
        except ValueError:
            pass
    return default


def execute_find_skill(
    topic: str,
    *,
    limit: int | None = None,
    max_evaluations: int | None = None,
    max_tokens: int | None = None,
    root_dir: str | Path = ".",
    model_cfg: dict[str, Any] | None = None,
    log=print,
    sleep=time.sleep,
) -> dict[str, Any]:
    """执行定向查找全流程并生成报告。"""
    root = Path(root_dir).resolve()
    run_cfg = load_finder_run_config(root / "config")
    final_limit = limit if limit is not None else _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT)
    final_max_evaluations = max_evaluations if max_evaluations is not None else _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS)
    final_max_tokens = max_tokens if max_tokens is not None else _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS)

    started_at = now_local()
    run_id = started_at.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    run_dir = root / "data" / "local" / "find-skills" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if final_limit < 1:
        raise ValueError("limit 必须是正整数")
    if final_max_evaluations < 1:
        raise ValueError("max_evaluations 必须是正整数")
    if final_max_tokens < 1000:
        raise ValueError("max_tokens 必须 >= 1000")
    if final_limit > final_max_evaluations:
        raise ValueError("limit 不能大于 max_evaluations")

    cfg = model_cfg if model_cfg is not None else load_finder_model_config(root / "config")
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise ValueError("缺少模型 API Key，请设置环境变量 LLM_API_KEY 或配置 model.local.json")

    usage = UsageTotals()
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "topic": topic.strip(),
        "status": "running",
        "parameters": {
            "limit": final_limit,
            "max_evaluations": final_max_evaluations,
            "max_tokens": final_max_tokens,
        },
        "model": cfg.get("model"),
        "plan": None,
        "search": {
            "queries_executed": [],
            "repos_discovered": 0,
            "candidates_found": 0,
            "expansions": [],
        },
        "evaluated_count": 0,
        "shortlist_count": 0,
        "alternatives_count": 0,
        "stop_reason": None,
        "shortlist": [],
        "alternatives": [],
        "evaluations": [],
        "usage": None,
        "report_paths": {
            "json": str(run_dir / "report.json"),
            "md": str(run_dir / "report.md"),
        },
    }

    def save_current_report():
        report["usage"] = usage.snapshot()
        report["updated_at"] = now_local().isoformat()
        _write_json_atomic(run_dir / "report.json", report)
        markdown_text = render_find_markdown_report(report)
        (run_dir / "report.md").write_text(markdown_text, encoding="utf-8")

        # 同步写入 public/data/find-report.json，供前端页面直接可视化展示
        public_data_dir = root / "public" / "data"
        if public_data_dir.exists():
            try:
                _write_json_atomic(public_data_dir / "find-report.json", report)
            except Exception:
                pass

    save_current_report()

    try:
        # 1. 需求规划
        log(f"正在分析需求并规划搜索策略：'{topic}'...")
        plan_sys, plan_user = build_plan_prompt(topic)
        cfg_plan = deepcopy(cfg)
        cfg_plan.setdefault("limits", {})["max_output_tokens"] = PLAN_MAX_OUTPUT_TOKENS
        call_plan = call_model(cfg_plan, plan_sys, plan_user, api_key=api_key, sleep=sleep)
        usage.add(call_plan)
        save_current_report()

        if not call_plan.ok or not call_plan.content:
            report["status"] = STATUS_ERROR
            report["stop_reason"] = f"查询规划模型调用失败：{call_plan.error}"
            save_current_report()
            return report

        plan = parse_query_plan(call_plan.content)
        report["plan"] = plan
        log(f"规划意图：{plan['intent']}")
        log(f"生成搜索短语（共 {len(plan['queries'])} 条）：{', '.join(plan['queries'])}")
        save_current_report()

        # 2. GitHub 搜索
        discovered_repos: list[dict[str, str]] = []
        seen_repo_keys: set[str] = set()

        for q in plan["queries"]:
            log(f"检索 GitHub: '{q}'...")
            ok, r_list, err = search_github_repos_for_query(q, sleep=sleep)
            report["search"]["queries_executed"].append(
                {"query": q, "ok": ok, "repos_returned": len(r_list), "error": err}
            )
            for r in r_list:
                key = f"{r['owner']}/{r['repo']}"
                if key not in seen_repo_keys:
                    seen_repo_keys.add(key)
                    discovered_repos.append(r)
            if len(discovered_repos) >= MAX_REPOS_TO_EXPAND:
                break

        report["search"]["repos_discovered"] = len(discovered_repos)
        log(f"发现候选仓库：共 {len(discovered_repos)} 个不同仓库。")
        save_current_report()

        if not discovered_repos:
            report["status"] = STATUS_COMPLETED
            report["stop_reason"] = STATUS_CANDIDATES_EXHAUSTED
            save_current_report()
            log("未检索到相关仓库。")
            return report

        # 3. 展开仓库获取 SKILL.md
        log("正在扫描各仓库中的真实 SKILL.md 文件...")
        raw_candidates, expansion_logs = expand_and_collect_candidates(
            discovered_repos, max_repos=MAX_REPOS_TO_EXPAND, sleep=sleep, log=log
        )
        report["search"]["expansions"] = expansion_logs
        report["search"]["candidates_found"] = len(raw_candidates)
        log(f"精确定位技能文件：共 {len(raw_candidates)} 个。")
        save_current_report()

        if not raw_candidates:
            report["status"] = STATUS_COMPLETED
            report["stop_reason"] = STATUS_CANDIDATES_EXHAUSTED
            save_current_report()
            log("各仓库中均未定位到有效的 SKILL.md 技能文件。")
            return report

        # 4. 候选轮转排序与抓取评估
        scheduled_candidates = schedule_candidates_fairly(raw_candidates)
        evaluated_items: list[dict[str, Any]] = []
        consecutive_failures = 0

        cfg_eval = deepcopy(cfg)
        cfg_eval.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS

        for idx, cand in enumerate(scheduled_candidates, start=1):
            if len(evaluated_items) >= final_max_evaluations:
                report["stop_reason"] = STATUS_EVALUATION_LIMIT
                break
            if usage.total_tokens >= final_max_tokens:
                report["stop_reason"] = STATUS_TOKEN_LIMIT
                break
            if consecutive_failures >= 20:
                report["stop_reason"] = STATUS_MODEL_FAILURES
                break

            log(f"抓取材料 [{idx}/{len(scheduled_candidates)}]：{cand.skill_id}...")
            ok, materials, fetch_err = fetch_candidate_materials(cand, sleep=sleep)
            if not ok or not materials:
                log(f"材料获取跳过（{fetch_err}）：{cand.skill_id}")
                continue

            # 构造评估 Prompt
            cand_info = {
                "name": cand.name,
                "repo_url": cand.repo_url,
                "path": cand.path,
                "description": cand.description,
            }
            eval_sys, eval_user = build_evaluation_prompt(cand_info, materials, plan, topic)

            call_res = call_model(cfg_eval, eval_sys, eval_user, api_key=api_key, sleep=sleep)
            usage.add(call_res)

            if not call_res.ok or not call_res.content:
                consecutive_failures += 1
                log(f"评估失败（{call_res.error}）：{cand.skill_id}")
                save_current_report()
                continue

            try:
                raw_eval = parse_skill_evaluation(call_res.content, plan["criteria"])
                # 客观证据核验与结论重算
                verified_eval = verify_and_adjust_evaluation(raw_eval, materials, plan["criteria"])
                consecutive_failures = 0

                item_record = {
                    "candidate": {
                        "skill_id": cand.skill_id,
                        "name": cand.name,
                        "repo_url": cand.repo_url,
                        "url": cand.url,
                        "author": cand.owner,
                        "path": cand.path,
                        "content_fingerprint": cand.content_fingerprint,
                    },
                    "evaluation": verified_eval,
                }
                evaluated_items.append(item_record)
                report["evaluations"].append(item_record)
                report["evaluated_count"] = len(evaluated_items)
                log(
                    f"完成评估 [{len(evaluated_items)}/{final_max_evaluations}]：{cand.name} "
                    f"-> 匹配度: {verified_eval['match']} | 说明质量: {verified_eval['documentation']} "
                    f"（累计消耗: {usage.total_tokens:,} Token）"
                )
            except Exception as exc:
                consecutive_failures += 1
                log(f"评估解析失败（{exc}）：{cand.skill_id}")

            save_current_report()

        # 5. 排序与短名单归纳
        shortlist, alternatives = rank_find_results(evaluated_items, plan, limit=final_limit)
        report["shortlist"] = shortlist
        report["alternatives"] = alternatives
        report["shortlist_count"] = len(shortlist)
        report["alternatives_count"] = len(alternatives)

        if not report["stop_reason"]:
            if len(shortlist) >= final_limit:
                report["stop_reason"] = STATUS_TARGET_REACHED
            else:
                report["stop_reason"] = STATUS_COMPLETED

        report["status"] = STATUS_COMPLETED
        save_current_report()

        log(f"\n查找完成！优先推荐短名单：{len(shortlist)} 项，相关备选：{len(alternatives)} 项。")
        log(f"完整报告已生成：{report['report_paths']['md']}")
        return report

    except KeyboardInterrupt:
        report["status"] = STATUS_COMPLETED
        report["stop_reason"] = STATUS_INTERRUPTED
        save_current_report()
        log("\n用户主动中断查找；已完成的结果与 Token 用量已成功保存。")
        return report
    except Exception as exc:
        report["status"] = STATUS_ERROR
        report["stop_reason"] = f"未处理异常：{type(exc).__name__}: {exc}"
        save_current_report()
        log(f"\n查找异常中止：{exc}")
        return report


# --------------------------------------------------------------------------
# Markdown 报告渲染
# --------------------------------------------------------------------------


def render_find_markdown_report(report: dict[str, Any]) -> str:
    """将查找结果格式化为高可读性的 Markdown 报告。"""
    topic = report.get("topic", "")
    params = report.get("parameters", {})
    usage = report.get("usage") or {}
    plan = report.get("plan") or {}
    search = report.get("search") or {}

    lines = [
        f"# 定向查找 Skill 报告",
        "",
        f"- **需求意图**：{topic}",
        f"- **分析归纳**：{plan.get('intent', '（未完成）')}",
        f"- **运行编号**：`{report.get('run_id')}`（{report.get('started_at', '')}）",
        f"- **运行状态**：{report.get('stop_reason') or report.get('status')}",
        f"- **检查范围**：检索查询 {len(search.get('queries_executed', []))} 条，发现仓库 {search.get('repos_discovered', 0)} 个，展开技能文件 {search.get('candidates_found', 0)} 个，实际评估 {report.get('evaluated_count', 0)} 个",
        f"- **Token 用量**：输入 {usage.get('prompt_tokens', 0):,}，输出 {usage.get('completion_tokens', 0):,}，总计 {usage.get('total_tokens', 0):,} Token（本次停止阈值 {params.get('max_tokens', 0):,}）",
        "",
        "---",
        "",
        f"## 优先查看（短名单 {len(report.get('shortlist', []))} 个）",
        "",
    ]

    shortlist = report.get("shortlist") or []
    if not shortlist:
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
                    q_str = f"（引文：\"{quotes[0]}\"）" if quotes else ""
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


# --------------------------------------------------------------------------
# CLI 入口
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    root_path = Path(root or Path(__file__).resolve().parents[1]).resolve()
    run_cfg = load_finder_run_config(root_path / "config")
    cfg_limit = _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT)
    cfg_max_eval = _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS)
    cfg_max_tokens = _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS)
    cfg_topic = (run_cfg.get("topic") or "").strip()

    topic_help = f"想要查找的技能需求（默认取自 config/find-skill.json: '{cfg_topic}'）" if cfg_topic else "想要查找的技能需求（如：生成高质量 Prompt）"

    parser = argparse.ArgumentParser(description="定向查找特定需求的 AI Agent Skill 并生成短名单对比报告")
    parser.add_argument("topic", nargs="?", help=topic_help)
    parser.add_argument("--limit", type=lambda v: _parse_int_val(v, DEFAULT_LIMIT), default=None, help=f"优先查看的短名单数量（默认 {cfg_limit}，取自 config/find-skill.json）")
    parser.add_argument("--max-evaluations", type=lambda v: _parse_int_val(v, DEFAULT_MAX_EVALUATIONS), default=None, help=f"本次最多评估的技能数量（默认 {cfg_max_eval}，取自 config/find-skill.json）")
    parser.add_argument("--max-tokens", type=lambda v: _parse_int_val(v, DEFAULT_MAX_TOKENS), default=None, help=f"本次模型调用的 Token 消耗停止阈值（默认 {cfg_max_tokens:,}，取自 config/find-skill.json）")
    args = parser.parse_args(argv)

    topic = (args.topic or cfg_topic).strip()
    if not topic:
        if sys.stdin.isatty():
            try:
                print("你想找什么 Skill？")
                topic = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n操作取消。")
                return 1
        if not topic:
            parser.error("必须提供需求 topic 参数（或在 config/find-skill.json 中配置 topic，或在交互终端中输入）")

    try:
        report = execute_find_skill(
            topic,
            limit=args.limit,
            max_evaluations=args.max_evaluations,
            max_tokens=args.max_tokens,
            root_dir=root_path,
        )
        return 0 if report.get("status") == STATUS_COMPLETED else 1
    except Exception as exc:
        print(f"启动错误：{exc}", file=sys.stderr)
        return 1

