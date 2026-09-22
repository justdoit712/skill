"""定向查找技能核心编排器。

依据 docs/产品规范.md §12（目标约束，修复进度见重构实施方案）：
- 需求驱动：输入自然语言需求，自动生成计划并评估；
- 目录规则隔离：不依赖目录的分类、排除词、黑名单与冷冻规则；
- 零目录副作用：不修改 data/catalog.json、周账本或候选池；
- 证据检查：核对引用材料；当前匹配过宽等限制见架构评估，不保证消除模型误判；
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
STATUS_STOPPED = "stopped"
STATUS_INVALID_CONFIG = "invalid_config"


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


def _round_robin_merge_repos(
    query_repo_lists: list[list[dict[str, str]]],
    max_repos: int = MAX_REPOS_TO_EXPAND,
) -> list[dict[str, str]]:
    """多查询结果交织轮转合并，消除第一个查询垄断结果的缺陷。"""
    discovered: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    max_depth = max((len(lst) for lst in query_repo_lists), default=0)

    for depth in range(max_depth):
        for r_list in query_repo_lists:
            if depth < len(r_list):
                r = r_list[depth]
                key = f"{r['owner']}/{r['repo']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    discovered.append(r)
                    if len(discovered) >= max_repos:
                        return discovered
    return discovered


def _interleave_paths(
    related: list[str],
    generic: list[str],
    max_count: int = MAX_FILES_PER_REPO,
) -> list[str]:
    """合集仓库按 2 个相关路径 + 1 个通用路径交替提取，单库上限 max_count。"""
    res: list[str] = []
    r_idx = 0
    g_idx = 0
    while len(res) < max_count and (r_idx < len(related) or g_idx < len(generic)):
        # 尝试取最多 2 个相关
        for _ in range(2):
            if r_idx < len(related) and len(res) < max_count:
                res.append(related[r_idx])
                r_idx += 1
        # 尝试取 1 个通用
        if g_idx < len(generic) and len(res) < max_count:
            res.append(generic[g_idx])
            g_idx += 1
        # 若某一方已耗尽，取另一方填满
        if r_idx >= len(related) and g_idx < len(generic):
            while g_idx < len(generic) and len(res) < max_count:
                res.append(generic[g_idx])
                g_idx += 1
        elif g_idx >= len(generic) and r_idx < len(related):
            while r_idx < len(related) and len(res) < max_count:
                res.append(related[r_idx])
                r_idx += 1
    return res


def expand_and_collect_candidates(
    repos: list[dict[str, str]],
    *,
    keywords: set[str] | None = None,
    max_repos: int = MAX_REPOS_TO_EXPAND,
    sleep=time.sleep,
    log=print,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """展开仓库文件树，收集具体 SKILL.md 候选。
    若存在相关关键词，按 2 个相关路径 + 1 个通用路径交替提取。
    """
    candidates: list[Candidate] = []
    expansion_logs: list[dict[str, Any]] = []
    seen_skills: set[str] = set()
    kw_set = {k.lower() for k in (keywords or set()) if len(k) >= 2}

    for r in repos[:max_repos]:
        owner, repo = r["owner"], r["repo"]
        key = f"{owner}/{repo}"
        paths, err = expand_repo_skills(owner, repo, sleep=sleep)
        truncated = bool(err and err.startswith("TREE_TRUNCATED"))

        # 精确 basename 为 SKILL.md（排除 NOT_SKILL.md、SKILL.md.bak 等）
        valid_paths = [p for p in paths if p.split("/")[-1] == "SKILL.md"]

        expansion_logs.append(
            {
                "repo": key,
                "ok": err is None or truncated,
                "skills_found": len(valid_paths),
                "truncated": truncated,
                "error": err,
            }
        )

        if not valid_paths:
            continue

        if kw_set:
            related = []
            generic = []
            for p in valid_paths:
                p_lower = p.lower()
                if any(k in p_lower for k in kw_set):
                    related.append(p)
                else:
                    generic.append(p)
            selected_paths = _interleave_paths(related, generic, max_count=MAX_FILES_PER_REPO)
        else:
            selected_paths = valid_paths[:MAX_FILES_PER_REPO]

        for p in selected_paths:
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


def _is_html_content(text: str) -> bool:
    """判定文本是否为 HTML 网页（如登录页或错误页），防止伪材料进入评估。"""
    s = text.lstrip().lower()
    return (
        s.startswith("<!doctype html")
        or s.startswith("<html")
        or ("<head>" in s and "<body" in s)
        or ("<title>" in s and "</title>" in s and "<form" in s)
    )


def fetch_candidate_materials(
    candidate: Candidate,
    *,
    fetch_fn=None,
    sleep=time.sleep,
) -> tuple[bool, dict[str, str], str | None]:
    """抓取主 SKILL.md 及可选的关联引用说明文件（合计 <= 96 KiB）。"""
    if candidate.path.split("/")[-1] != "SKILL.md":
        return False, {}, "NOT_A_SKILL_MD"

    fn = fetch_fn or fetch_text
    raw_url = candidate.url.replace("https://github.com/", "https://raw.githubusercontent.com/", 1).replace(
        "/blob/", "/", 1
    )

    fetched = fn(raw_url, max_bytes=MAX_PRIMARY_FILE_BYTES, sleep=sleep)
    if not fetched.ok or not fetched.text or fetched.truncated:
        return False, {}, fetched.reason_code or "FETCH_FAILED"

    primary_text = fetched.text
    if _is_html_content(primary_text):
        return False, {}, "HTML_CONTENT_REJECTED"

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
        if ref_fetched.ok and ref_fetched.text and not ref_fetched.truncated and not _is_html_content(ref_fetched.text):
            materials[ref_p] = ref_fetched.text
            total_bytes += len(ref_fetched.text.encode("utf-8"))

    return True, materials, None


# --------------------------------------------------------------------------
# 查找主控制器（SkillFinder）
# --------------------------------------------------------------------------


def _parse_int_val(val: Any, default: int, field_name: str = "参数") -> int:
    """支持 int 以及带空格、下划线、千分位逗号的表示（如 '200 000'、'200_000'、'200,000'）。
    遇到非法字符、负数或布尔值时抛出 ValueError，严禁静默吞错。"""
    if val is None:
        return default
    if isinstance(val, bool):
        raise ValueError(f"{field_name} 不能是布尔值: {val}")
    if isinstance(val, (int, float)):
        int_val = int(val)
        if int_val < 0:
            raise ValueError(f"{field_name} 不能为负数: {val}")
        return int_val
    if isinstance(val, str):
        clean = val.replace(" ", "").replace("_", "").replace(",", "").strip()
        if not clean:
            return default
        try:
            int_val = int(clean)
        except ValueError as exc:
            raise ValueError(f"{field_name} 包含非法字符无法解析为整数: {val!r}") from exc
        if int_val < 0:
            raise ValueError(f"{field_name} 不能为负数: {val}")
        return int_val
    raise ValueError(f"{field_name} 类型不支持: {type(val).__name__}")


def finalize_run(
    report: dict[str, Any],
    stop_reason: str,
    *,
    status: str | None = None,
    evaluated_items: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    limit: int = DEFAULT_LIMIT,
    run_dir: Path | None = None,
    root_dir: Path | None = None,
    usage: UsageTotals | None = None,
    evaluation_attempts: int | None = None,
    evaluated_count: int | None = None,
    log=print,
) -> dict[str, Any]:
    """中心化收尾函数：
    1. 无论何种停止原因，只要存在已评估候选，重新执行 rank_find_results(..., plan=plan)
    2. 生成规范的 shortlist 与 alternatives
    3. 生成 Markdown 与 JSON 报告并持久化
    4. 规范状态与停止原因
    """
    if status is None:
        if stop_reason in (STATUS_TARGET_REACHED, STATUS_CANDIDATES_EXHAUSTED, STATUS_COMPLETED):
            status = STATUS_COMPLETED
        elif stop_reason in (STATUS_TOKEN_LIMIT, STATUS_EVALUATION_LIMIT, STATUS_USAGE_UNKNOWN, STATUS_MODEL_FAILURES):
            status = STATUS_STOPPED
        elif stop_reason == STATUS_INTERRUPTED:
            status = STATUS_INTERRUPTED
        elif stop_reason in ("plan_failed", "search_failed") or "error" in stop_reason.lower():
            status = STATUS_ERROR
        else:
            status = report.get("status") or STATUS_COMPLETED

    report["status"] = status
    report["stop_reason"] = stop_reason

    items = evaluated_items if evaluated_items is not None else report.get("evaluations", [])
    plan_dict = plan if plan is not None else (report.get("plan") or {"criteria": []})

    shortlist, alternatives = rank_find_results(items, plan=plan_dict, limit=limit)
    report["shortlist"] = shortlist
    report["alternatives"] = alternatives
    report["shortlist_count"] = len(shortlist)
    report["alternatives_count"] = len(alternatives)
    report["evaluated_count"] = evaluated_count if evaluated_count is not None else len(items)
    if evaluation_attempts is not None:
        report["evaluation_attempts"] = evaluation_attempts

    if usage is not None:
        report["usage"] = usage.snapshot()

    report["updated_at"] = now_local().isoformat()

    if run_dir is not None and run_dir.exists():
        _write_json_atomic(run_dir / "report.json", report)
        markdown_text = render_find_markdown_report(report)
        (run_dir / "report.md").write_text(markdown_text, encoding="utf-8")

        if root_dir is not None:
            public_data_dir = Path(root_dir) / "public" / "data"
            if public_data_dir.exists():
                try:
                    _write_json_atomic(public_data_dir / "find-report.json", report)
                except Exception:
                    pass

    return report


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

    # 1. 前置参数防御性校验（不建目录、不静默吞错）
    clean_topic = (topic or "").strip()
    if not clean_topic:
        raise ValueError("必须提供有效非空的查找需求 topic")

    final_limit = (
        _parse_int_val(limit, DEFAULT_LIMIT, "limit")
        if limit is not None
        else _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT, "limit")
    )
    final_max_evaluations = (
        _parse_int_val(max_evaluations, DEFAULT_MAX_EVALUATIONS, "max_evaluations")
        if max_evaluations is not None
        else _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS, "max_evaluations")
    )
    final_max_tokens = (
        _parse_int_val(max_tokens, DEFAULT_MAX_TOKENS, "max_tokens")
        if max_tokens is not None
        else _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")
    )

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

    # 确保模型调用单次无底层嵌套重试
    cfg = deepcopy(cfg)
    cfg.setdefault("request", {})["max_attempts"] = 1

    # 2. 校验全部通过后，才创建运行输出目录
    started_at = now_local()
    run_id = started_at.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    run_dir = root / "data" / "local" / "find-skills" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    usage = UsageTotals()
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "topic": clean_topic,
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
        "evaluation_attempts": 0,
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

        public_data_dir = root / "public" / "data"
        if public_data_dir.exists():
            try:
                _write_json_atomic(public_data_dir / "find-report.json", report)
            except Exception:
                pass

    save_current_report()

    evaluated_items: list[dict[str, Any]] = []
    evaluation_attempts = 0
    plan = None

    try:
        # 1. 需求规划
        log(f"正在分析需求并规划搜索策略：'{clean_topic}'...")
        plan_sys, plan_user = build_plan_prompt(clean_topic)
        cfg_plan = deepcopy(cfg)
        cfg_plan.setdefault("limits", {})["max_output_tokens"] = PLAN_MAX_OUTPUT_TOKENS
        call_plan = call_model(cfg_plan, plan_sys, plan_user, api_key=api_key, sleep=sleep)
        usage.add(call_plan)
        save_current_report()

        # 校验规划未知用量 (F1)
        call_usage = getattr(call_plan, "usage", None)
        if not call_usage or not isinstance(call_usage, dict) or call_usage.get("total_tokens") is None:
            log("规划模型调用缺少有效 usage，触发零容忍熔断停机。")
            return finalize_run(
                report,
                stop_reason=STATUS_USAGE_UNKNOWN,
                status=STATUS_STOPPED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        if not call_plan.ok or not call_plan.content:
            log(f"查询规划模型调用失败：{call_plan.error}")
            return finalize_run(
                report,
                stop_reason="plan_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        try:
            plan = parse_query_plan(call_plan.content)
        except Exception as exc:
            log(f"查询规划解析失败：{exc}")
            return finalize_run(
                report,
                stop_reason="plan_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        report["plan"] = plan
        log(f"规划意图：{plan['intent']}")
        log(f"生成搜索短语（共 {len(plan['queries'])} 条）：{', '.join(plan['queries'])}")
        save_current_report()

        # 2. GitHub 搜索（各词独立检索后轮转交织 F4）
        query_repo_lists: list[list[dict[str, str]]] = []
        all_search_failed = True

        for q in plan["queries"]:
            log(f"检索 GitHub: '{q}'...")
            ok, r_list, err = search_github_repos_for_query(q, sleep=sleep)
            if ok:
                all_search_failed = False
            report["search"]["queries_executed"].append(
                {"query": q, "ok": ok, "repos_returned": len(r_list), "error": err}
            )
            query_repo_lists.append(r_list)

        if all_search_failed and plan["queries"]:
            log("所有搜索短语的 GitHub 检索均失败。")
            return finalize_run(
                report,
                stop_reason="search_failed",
                status=STATUS_ERROR,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        discovered_repos = _round_robin_merge_repos(query_repo_lists, max_repos=MAX_REPOS_TO_EXPAND)
        report["search"]["repos_discovered"] = len(discovered_repos)
        log(f"发现候选仓库：共 {len(discovered_repos)} 个不同仓库（轮转去重后）。")
        save_current_report()

        if not discovered_repos:
            log("未检索到相关仓库。")
            return finalize_run(
                report,
                stop_reason=STATUS_CANDIDATES_EXHAUSTED,
                status=STATUS_COMPLETED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        # 3. 展开仓库获取 SKILL.md
        search_keywords: set[str] = set()
        for text in [clean_topic] + plan.get("queries", []):
            for token in re.findall(r"[\w\u4e00-\u9fa5]+", text.lower()):
                if len(token) >= 2:
                    search_keywords.add(token)

        log("正在扫描各仓库中的真实 SKILL.md 文件...")
        raw_candidates, expansion_logs = expand_and_collect_candidates(
            discovered_repos,
            keywords=search_keywords,
            max_repos=MAX_REPOS_TO_EXPAND,
            sleep=sleep,
            log=log,
        )
        report["search"]["expansions"] = expansion_logs
        report["search"]["candidates_found"] = len(raw_candidates)
        log(f"精确定位技能文件：共 {len(raw_candidates)} 个。")
        save_current_report()

        if not raw_candidates:
            log("各仓库中均未定位到有效的 SKILL.md 技能文件。")
            return finalize_run(
                report,
                stop_reason=STATUS_CANDIDATES_EXHAUSTED,
                status=STATUS_COMPLETED,
                evaluated_items=evaluated_items,
                plan=plan,
                limit=final_limit,
                run_dir=run_dir,
                root_dir=root,
                usage=usage,
                evaluation_attempts=evaluation_attempts,
                evaluated_count=len(evaluated_items),
                log=log,
            )

        # 4. 候选轮转调度与抓取评估
        scheduled_candidates = schedule_candidates_fairly(raw_candidates)
        consecutive_failures = 0

        cfg_eval = deepcopy(cfg)
        cfg_eval.setdefault("limits", {})["max_output_tokens"] = EVAL_MAX_OUTPUT_TOKENS

        for idx, cand in enumerate(scheduled_candidates, start=1):
            if evaluation_attempts >= final_max_evaluations:
                return finalize_run(
                    report,
                    stop_reason=STATUS_EVALUATION_LIMIT,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )
            if usage.total_tokens >= final_max_tokens:
                return finalize_run(
                    report,
                    stop_reason=STATUS_TOKEN_LIMIT,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )
            if consecutive_failures >= 20:
                return finalize_run(
                    report,
                    stop_reason=STATUS_MODEL_FAILURES,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )

            log(f"抓取材料 [{idx}/{len(scheduled_candidates)}]：{cand.skill_id}...")
            ok, materials, fetch_err = fetch_candidate_materials(cand, sleep=sleep)
            if not ok or not materials:
                log(f"材料获取跳过（{fetch_err}）：{cand.skill_id}")
                continue

            # 先占名额后请求 (F1)
            evaluation_attempts += 1
            report["evaluation_attempts"] = evaluation_attempts
            save_current_report()

            cand_info = {
                "name": cand.name,
                "repo_url": cand.repo_url,
                "path": cand.path,
                "description": cand.description,
            }
            eval_sys, eval_user = build_evaluation_prompt(cand_info, materials, plan, clean_topic)

            call_res = call_model(cfg_eval, eval_sys, eval_user, api_key=api_key, sleep=sleep)
            usage.add(call_res)

            # 校验单次评估未知用量 (F1)
            call_res_usage = getattr(call_res, "usage", None)
            if not call_res_usage or not isinstance(call_res_usage, dict) or call_res_usage.get("total_tokens") is None:
                log(f"条目 {cand.skill_id} 评估返回未知用量，触发零容忍熔断停机。")
                return finalize_run(
                    report,
                    stop_reason=STATUS_USAGE_UNKNOWN,
                    status=STATUS_STOPPED,
                    evaluated_items=evaluated_items,
                    plan=plan,
                    limit=final_limit,
                    run_dir=run_dir,
                    root_dir=root,
                    usage=usage,
                    evaluation_attempts=evaluation_attempts,
                    evaluated_count=len(evaluated_items),
                    log=log,
                )

            if not call_res.ok or not call_res.content:
                consecutive_failures += 1
                log(f"评估失败（{call_res.error}）：{cand.skill_id}")
                save_current_report()
                continue

            try:
                raw_eval = parse_skill_evaluation(call_res.content, plan["criteria"])
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
                save_current_report()
            except Exception as exc:
                consecutive_failures += 1
                log(f"评估解析失败（{exc}）：{cand.skill_id}")
                save_current_report()
                continue

        # 5. 循环自然结束
        shortlist, alternatives = rank_find_results(evaluated_items, plan=plan, limit=final_limit)
        stop_reason = STATUS_TARGET_REACHED if len(shortlist) >= final_limit else STATUS_CANDIDATES_EXHAUSTED

        res_report = finalize_run(
            report,
            stop_reason=stop_reason,
            status=STATUS_COMPLETED,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )
        log(f"\n查找完成！优先推荐短名单：{len(res_report['shortlist'])} 项，相关备选：{len(res_report['alternatives'])} 项。")
        log(f"完整报告已生成：{res_report['report_paths']['md']}")
        return res_report

    except KeyboardInterrupt:
        log("\n用户主动中断查找；已完成的结果与 Token 用量已成功保存。")
        return finalize_run(
            report,
            stop_reason=STATUS_INTERRUPTED,
            status=STATUS_INTERRUPTED,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )
    except Exception as exc:
        log(f"\n查找异常中止：{exc}")
        return finalize_run(
            report,
            stop_reason=f"未处理异常：{type(exc).__name__}: {exc}",
            status=STATUS_ERROR,
            evaluated_items=evaluated_items,
            plan=plan,
            limit=final_limit,
            run_dir=run_dir,
            root_dir=root,
            usage=usage,
            evaluation_attempts=evaluation_attempts,
            evaluated_count=len(evaluated_items),
            log=log,
        )


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
    try:
        run_cfg = load_finder_run_config(root_path / "config")
        cfg_limit = _parse_int_val(run_cfg.get("limit"), DEFAULT_LIMIT, "limit")
        cfg_max_eval = _parse_int_val(run_cfg.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS, "max_evaluations")
        cfg_max_tokens = _parse_int_val(run_cfg.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")
        cfg_topic = (run_cfg.get("topic") or "").strip()
    except ValueError as exc:
        print(f"配置参数错误：{exc}", file=sys.stderr)
        return 2

    topic_help = (
        f"想要查找的技能需求（默认取自 config/find-skill.json: '{cfg_topic}'）"
        if cfg_topic
        else "想要查找的技能需求（如：生成高质量 Prompt）"
    )

    parser = argparse.ArgumentParser(description="定向查找特定需求的 AI Agent Skill 并生成短名单对比报告")
    parser.add_argument("topic", nargs="?", help=topic_help)
    parser.add_argument(
        "--limit",
        type=lambda v: _parse_int_val(v, DEFAULT_LIMIT, "limit"),
        default=None,
        help=f"优先查看的短名单数量（默认 {cfg_limit}）",
    )
    parser.add_argument(
        "--max-evaluations",
        type=lambda v: _parse_int_val(v, DEFAULT_MAX_EVALUATIONS, "max_evaluations"),
        default=None,
        help=f"本次最多评估的技能数量（默认 {cfg_max_eval}）",
    )
    parser.add_argument(
        "--max-tokens",
        type=lambda v: _parse_int_val(v, DEFAULT_MAX_TOKENS, "max_tokens"),
        default=None,
        help=f"本次模型调用的 Token 消耗停止阈值（默认 {cfg_max_tokens:,}）",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    topic = (args.topic or cfg_topic).strip()
    if not topic:
        if sys.stdin.isatty():
            try:
                print("你想找什么 Skill？")
                topic = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n操作取消。")
                return 130
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
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"启动错误：{exc}", file=sys.stderr)
        return 1

    status = report.get("status")
    stop_reason = report.get("stop_reason")

    if status == STATUS_INTERRUPTED or stop_reason == STATUS_INTERRUPTED:
        return 130
    if status == STATUS_STOPPED or stop_reason in (
        STATUS_TOKEN_LIMIT,
        STATUS_EVALUATION_LIMIT,
        STATUS_USAGE_UNKNOWN,
        STATUS_MODEL_FAILURES,
    ):
        return 2
    if status == STATUS_ERROR or stop_reason in ("search_failed", "plan_failed"):
        return 1
    if status == STATUS_COMPLETED:
        return 0
    return 0

