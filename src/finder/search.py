"""定向查找 GitHub 仓库搜索与技能候选材料抓取。

职责：
1. 单关键词 GitHub 仓库检索与多查询结果轮转交织合并；
2. 展开仓库文件树，根据意图关键词轮转提取 SKILL.md 候选；
3. 跨仓库公平调度候选队列；
4. 按需抓取主技能文件与关联 Markdown 文档材料（限制总字节数与 HTML 拦截）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any

from src.infra.github import (
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_SEARCH_ENDPOINT,
    USER_AGENT,
    apply_github_auth,
    expand_repo_skills,
)
from src.infra.http import fetch_text
from src.shared.identity import content_fingerprint, make_skill_id
from src.shared.models import Candidate
from src.shared.materials import DocumentSnapshot, MaterialBundle, validate_document
from src.infra.github import search_repositories

MAX_REPOS_TO_EXPAND = 20
MAX_SEARCH_REPOS_PER_QUERY = 20
MAX_FILES_PER_REPO = 10
MAX_TOTAL_FILES_TO_FETCH = 80
MAX_PRIMARY_FILE_BYTES = 65536
MAX_TOTAL_MATERIAL_BYTES = 98304
MAX_REFERENCED_FILES = 2


def search_github_repos_for_query(
    query: str,
    *,
    per_page: int = MAX_SEARCH_REPOS_PER_QUERY,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    session=None,
    sleep=time.sleep,
) -> tuple[bool, list[dict[str, str]], str | None]:
    """执行单个关键词的 GitHub 仓库搜索（围绕查询词与 SKILL.md in:readme 检索）。"""
    full_q = f'{query.strip()} "SKILL.md" in:readme'
    ok, items, _, error = search_repositories(full_q, session=session, per_page=per_page,
                                              timeout=timeout, sleep=sleep)
    repos = []
    for item in items:
        owner = (item.get("owner") or {}).get("login", "").lower()
        repo = item.get("name", "").lower()
        if owner and repo:
            repos.append({"owner": owner, "repo": repo,
                          "url": item.get("html_url") or f"https://github.com/{owner}/{repo}",
                          "description": item.get("description") or ""})
    return ok, repos, error


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
        valid_paths = sorted(p for p in paths if p.split("/")[-1] == "SKILL.md")

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

        expansion_logs[-1]["omitted_files"] = max(0, len(valid_paths) - len(selected_paths))
        for p in selected_paths:
            sid = make_skill_id(owner, repo, p)
            if sid not in seen_skills:
                seen_skills.add(sid)
                cand = Candidate(
                    skill_id=sid,
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
    valid, reason = validate_document(candidate.path, primary_text, MAX_PRIMARY_FILE_BYTES)
    if not valid:
        return False, {}, "HTML_CONTENT_REJECTED" if _is_html_content(primary_text) else reason
    candidate.content_fingerprint = content_fingerprint(primary_text)
    ref = "HEAD"
    if "/blob/" in candidate.url and candidate.url.endswith("/" + candidate.path):
        ref = candidate.url.split("/blob/", 1)[1][:-len(candidate.path)-1]
    resolved = ref if re.fullmatch(r"[0-9a-fA-F]{40}", ref) else None
    stamp = datetime.now(timezone.utc).isoformat()
    primary = DocumentSnapshot(candidate.path, primary_text, candidate.content_fingerprint, stamp, raw_url, resolved)
    references, errors = [], {}
    total_bytes = len(primary_text.encode("utf-8"))
    for ref_p in extract_referenced_md_paths(candidate.path, primary_text):
        remaining = MAX_TOTAL_MATERIAL_BYTES - total_bytes
        if remaining <= 0:
            errors[ref_p] = "MATERIAL_LIMIT"
            continue
        url = f"https://raw.githubusercontent.com/{candidate.owner}/{candidate.repo}/{ref}/{ref_p}"
        item = fn(url, max_bytes=remaining, sleep=sleep)
        valid, reason = validate_document(ref_p, item.text or "", remaining)
        if not item.ok or item.truncated or not valid:
            errors[ref_p] = item.reason_code or reason or "FETCH_FAILED"
            continue
        references.append(DocumentSnapshot(ref_p, item.text, content_fingerprint(item.text), stamp, url, resolved))
        total_bytes += len(item.text.encode("utf-8"))
    return True, MaterialBundle(candidate.skill_id, primary, tuple(references), errors), None
