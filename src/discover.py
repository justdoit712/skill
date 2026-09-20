"""发现层：按配置的来源与查询词找出候选。

依据 §4.1 的三层来源：第一层官方仓库与第二层社区清单由 config/sources.json 给出
入口，第三层由 config/searches.json 的查询词主动发现——本模块负责第三层，
并负责把三层的结果统一成 Candidate。

限流是硬约束：未认证时 search 10 次/分钟、core 60 次/小时；带 token 后 30 次/分钟、
5000 次/小时。因此查询之间默认加最小间隔，并允许限制查询总数。本地只适合抽样，
全量必须在 GitHub Actions 中带 token 运行。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .dedupe import candidate_from_repo, parse_github_url
from .fetch import (
    REASON_HTTP_ERROR,
    REASON_NETWORK_ERROR,
    REASON_UPSTREAM_GONE,
    USER_AGENT,
)
from .models import Candidate

GITHUB_SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
GITHUB_TREE_ENDPOINT = "https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}"
GITHUB_REPO_ENDPOINT = "https://api.github.com/repos/{owner}/{repo}"
DEFAULT_PER_PAGE = 30
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_INTERVAL_SECONDS = 7.0  # 未认证 search 限流 10 次/分钟，留出余量
DEFAULT_EXCLUDE_TERMS = ("-dsh", "-clawhub")
# in:readme 是必需项：仓库搜索默认只匹配 name/description/topics，不含 README，
# 实测不加该限定词时命中为 0。见 config/searches.json 的 file_constraint。
DEFAULT_QUERY_TEMPLATE = '{term} "SKILL.md" in:readme'
SKILL_FILENAME = "SKILL.md"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"


def apply_github_auth(session: requests.Session) -> bool:
    """有 GITHUB_TOKEN 就带上。

    未认证时搜索 10 次/分钟、core 60 次/小时；展开一个仓库要 2 次 core 调用，
    因此未认证时每小时只能展开约 30 个仓库。认证后为 30 次/分钟与 5000 次/小时。
    """
    token = (os.environ.get(GITHUB_TOKEN_ENV) or "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        return True
    return False
RETRYABLE_STATUS = frozenset({403, 408, 429, 500, 502, 503, 504})


@dataclass
class Query:
    """一条查询：领域 + 词条 + 最终查询串。"""

    domain_id: str
    term: str
    q: str


@dataclass
class SearchOutcome:
    """一次搜索的结果或失败原因。"""

    query: Query
    ok: bool
    candidates: list[Candidate] = field(default_factory=list)
    total_count: int = 0
    reason_code: str | None = None
    error: str | None = None


def build_query_string(term: str, template: str, exclude_terms: tuple[str, ...] = DEFAULT_EXCLUDE_TERMS) -> str:
    """按 searches.json 的 query_template 组装查询串，并追加全局排除词。"""
    base = template.replace("{term}", term)
    extras = " ".join(t for t in exclude_terms if t)
    return f"{base} {extras}".strip()


def build_queries(searches: dict) -> list[Query]:
    """由 config/searches.json 生成全部查询。"""
    template = (searches.get("file_constraint") or {}).get("query_template", DEFAULT_QUERY_TEMPLATE)
    exclude_terms = tuple(
        (searches.get("global_exclusions") or {}).get("query_terms", DEFAULT_EXCLUDE_TERMS)
    )

    queries: list[Query] = []
    for domain_id, spec in (searches.get("per_domain") or {}).items():
        for term in list(spec.get("zh", [])) + list(spec.get("en", [])):
            if not term:
                continue
            queries.append(
                Query(domain_id=domain_id, term=term, q=build_query_string(term, template, exclude_terms))
            )
    return queries


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _backoff_seconds(attempt: int, cap: float = 8.0) -> float:
    return min(2.0 ** (attempt - 1), cap)


def github_search(
    query: Query,
    *,
    session: requests.Session | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep=time.sleep,
    discovered_at: str | None = None,
) -> SearchOutcome:
    """执行一次 GitHub 仓库搜索，把结果转成候选。"""
    owns_session = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    sess.headers.setdefault("Accept", "application/vnd.github+json")
    apply_github_auth(sess)

    stamped = discovered_at or _utc_now()
    outcome = SearchOutcome(query=query, ok=False)

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                response = sess.get(
                    GITHUB_SEARCH_ENDPOINT,
                    params={"q": query.q, "per_page": per_page},
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                if attempt < max_attempts:
                    sleep(_backoff_seconds(attempt))
                    continue
                outcome.reason_code = REASON_NETWORK_ERROR
                return outcome

            try:
                status = response.status_code
                if status == 404:
                    outcome.reason_code = REASON_UPSTREAM_GONE
                    outcome.error = f"HTTP {status}"
                    return outcome
                if status >= 400:
                    outcome.error = f"HTTP {status}"
                    if status in RETRYABLE_STATUS and attempt < max_attempts:
                        response.close()
                        sleep(_backoff_seconds(attempt))
                        continue
                    outcome.reason_code = REASON_HTTP_ERROR
                    return outcome

                payload = response.json()
            finally:
                response.close()

            items = payload.get("items") or []
            outcome.total_count = int(payload.get("total_count") or 0)
            for item in items:
                owner = ((item.get("owner") or {}).get("login") or "").lower()
                repo = (item.get("name") or "").lower()
                if not owner or not repo:
                    continue
                outcome.candidates.append(
                    candidate_from_repo(
                        owner,
                        repo,
                        url=item.get("html_url") or "",
                        repo_url=item.get("html_url") or "",
                        name=item.get("name") or repo,
                        description=item.get("description") or "",
                        discovery_method="github_search",
                        search_term=query.term,
                        discovered_at=stamped,
                    )
                )
            outcome.ok = True
            return outcome
    finally:
        if owns_session:
            sess.close()

    outcome.reason_code = REASON_NETWORK_ERROR
    outcome.error = "重试次数耗尽"
    return outcome


def _skill_name_from_path(path: str, fallback: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return fallback


def _expand_candidates(
    candidates: list[Candidate],
    *,
    session: requests.Session,
    limit: int | None,
    sleep,
    outcomes: list[SearchOutcome],
    discovered_at: str,
    progress=None,
) -> list[Candidate]:
    """把仓库级候选细化到具体 SKILL.md（§4.2）。

    - 已有路径的候选原样保留
    - 仓库里没有任何 SKILL.md 的，**不计为独立技能**，从候选中剔除并记入结果
    - 展开失败（限流、网络）时保留仓库级候选，不静默丢弃
    """
    out: list[Candidate] = []
    cache: dict[str, tuple[list[str], str | None]] = {}
    expanded = 0

    for candidate in candidates:
        if candidate.path:
            out.append(candidate)
            continue

        key = f"{candidate.owner}/{candidate.repo}"
        limited = False
        if key not in cache:
            if limit is not None and expanded >= limit:
                # 达到展开上限是刻意边界，不是采集失败：候选保留在仓库级，下次继续展开
                cache[key] = ([], None)
                limited = True
            else:
                if progress:
                    progress(f"展开仓库 {expanded + 1}：{key}")
                paths, error = expand_repo_skills(candidate.owner, candidate.repo, session=session, sleep=sleep)
                cache[key] = (paths, error)
                expanded += 1

        paths, error = cache[key]
        probe = Query(domain_id="expand", term=key, q=key)

        if limited:
            outcomes.append(
                SearchOutcome(
                    query=probe, ok=True, total_count=0,
                    error="达到本轮展开上限，仍按仓库级候选保留，下次继续展开",
                )
            )
            out.append(candidate)
            continue

        if error:
            outcomes.append(SearchOutcome(query=probe, ok=False, reason_code=REASON_HTTP_ERROR, error=error))
            out.append(candidate)
            continue

        if not paths:
            outcomes.append(
                SearchOutcome(
                    query=probe, ok=True, total_count=0,
                    error="仓库内无 SKILL.md，不计为独立技能",
                )
            )
            continue

        for path in paths:
            out.append(
                candidate_from_repo(
                    candidate.owner,
                    candidate.repo,
                    path=path,
                    url=f"https://github.com/{candidate.owner}/{candidate.repo}/blob/HEAD/{path}",
                    repo_url=candidate.repo_url,
                    name=_skill_name_from_path(path, candidate.name),
                    description=candidate.description,
                    source_id=candidate.source_ids[0] if candidate.source_ids else "",
                    discovery_method=candidate.discovery_methods[0] if candidate.discovery_methods else "",
                    search_term=candidate.search_terms[0] if candidate.search_terms else "",
                    discovered_at=candidate.discovered_at or discovered_at,
                )
            )

    return out


def discover(
    searches: dict,
    *,
    sources: dict | None = None,
    expand: bool = False,
    expand_limit: int | None = None,
    session: requests.Session | None = None,
    max_queries: int | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    sleep=time.sleep,
    discovered_at: str | None = None,
    progress=None,
) -> tuple[list[Candidate], list[SearchOutcome]]:
    """执行来源种子与搜索查询，并按需展开到具体技能。

    返回 (候选, 每次查询的结果)。expand 会为仓库级候选查询其 SKILL.md 路径，
    使评估对象是具体技能而不是合集首页（§4.2）。
    """
    stamped = discovered_at or _utc_now()
    owns_session = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    apply_github_auth(sess)

    candidates: list[Candidate] = []
    outcomes: list[SearchOutcome] = []
    try:
        # 1. 来源种子：官方仓库与社区线索（config/sources.json）
        if sources:
            seeds = candidates_from_sources(sources, discovered_at=stamped)
            outcomes.append(
                SearchOutcome(
                    query=Query(domain_id="sources", term="sources.json", q="sources.json"),
                    ok=True,
                    candidates=list(seeds),
                    total_count=len(seeds),
                )
            )
            candidates.extend(seeds)

        # 2. 主动搜索（config/searches.json）
        queries = build_queries(searches)
        if max_queries is not None:
            queries = queries[:max_queries]
        for index, query in enumerate(queries):
            if progress:
                progress(f"搜索 {index + 1}/{len(queries)}：{query.term}")
            if index and min_interval_seconds > 0:
                sleep(min_interval_seconds)
            outcome = github_search(
                query, session=sess, per_page=per_page, sleep=sleep, discovered_at=stamped
            )
            outcomes.append(outcome)
            if outcome.ok:
                candidates.extend(outcome.candidates)

        # 3. 展开到具体技能
        if expand:
            candidates = _expand_candidates(
                candidates, session=sess, limit=expand_limit, sleep=sleep,
                outcomes=outcomes, discovered_at=stamped, progress=progress,
            )
    finally:
        if owns_session:
            sess.close()

    return candidates, outcomes


def load_searches(config_dir: str = "config") -> dict:
    """读取 config/searches.json。"""
    from pathlib import Path

    return json.loads((Path(config_dir) / "searches.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 展开到具体技能（§4.2：合集仓库尽量展开到具体技能，不能把仓库数冒充技能数）
# --------------------------------------------------------------------------


def _api_get(
    url: str,
    *,
    session: requests.Session,
    timeout: float,
    max_attempts: int,
    sleep,
) -> tuple[bool, dict | None, str | None]:
    """带重试的 GitHub API GET，返回 (ok, payload, error)。"""
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            if attempt < max_attempts:
                sleep(_backoff_seconds(attempt))
                continue
            return False, None, f"{type(exc).__name__}: {exc}"
        try:
            if response.status_code >= 400:
                if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                    response.close()
                    sleep(_backoff_seconds(attempt))
                    continue
                return False, None, f"HTTP {response.status_code}"
            return True, response.json(), None
        finally:
            response.close()
    return False, None, "重试次数耗尽"


def expand_repo_skills(
    owner: str,
    repo: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep=time.sleep,
) -> tuple[list[str], str | None]:
    """列出一个仓库里所有 SKILL.md 的路径。

    返回 (路径列表, 错误)。空列表且错误为 None 表示仓库里确实没有 SKILL.md——
    这种仓库按 §4.2 不能计为一个独立技能。
    """
    owns = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    sess.headers.setdefault("Accept", "application/vnd.github+json")
    apply_github_auth(sess)
    try:
        ok, repo_info, error = _api_get(
            GITHUB_REPO_ENDPOINT.format(owner=owner, repo=repo),
            session=sess, timeout=timeout, max_attempts=max_attempts, sleep=sleep,
        )
        if not ok:
            return [], error
        branch = (repo_info or {}).get("default_branch") or "HEAD"

        ok, tree, error = _api_get(
            GITHUB_TREE_ENDPOINT.format(owner=owner, repo=repo, ref=branch) + "?recursive=1",
            session=sess, timeout=timeout, max_attempts=max_attempts, sleep=sleep,
        )
        if not ok:
            return [], error

        paths = [
            item.get("path", "")
            for item in (tree or {}).get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").endswith(SKILL_FILENAME)
        ]
        return sorted(paths), None
    finally:
        if owns:
            sess.close()


def candidates_from_sources(sources_cfg: dict, *, discovered_at: str | None = None) -> list[Candidate]:
    """由 config/sources.json 生成种子候选：官方仓库、社区清单与已定位的单技能。"""
    stamped = discovered_at or _utc_now()
    out: list[Candidate] = []

    for source in sources_cfg.get("sources", []):
        if (source.get("exclusion") or {}).get("excluded"):
            continue
        url = source.get("url")
        if not url:
            continue
        owner, repo, path, _ = parse_github_url(url)
        if not owner or not repo:
            continue

        common = {
            "source_id": source["id"],
            "discovery_method": source.get("discovery_method") or "provided_lead",
            "discovered_at": stamped,
        }
        known_paths = source.get("skill_paths") or []
        if known_paths:
            for known in known_paths:
                out.append(
                    candidate_from_repo(
                        owner, repo, path=known, url=url,
                        name=known.rsplit("/", 1)[-1] or repo, **common,
                    )
                )
        else:
            out.append(candidate_from_repo(owner, repo, url=url, path=path, **common))

    return out
