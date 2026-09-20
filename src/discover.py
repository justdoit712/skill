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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .dedupe import candidate_from_repo
from .fetch import (
    REASON_HTTP_ERROR,
    REASON_NETWORK_ERROR,
    REASON_UPSTREAM_GONE,
    USER_AGENT,
)
from .models import Candidate

GITHUB_SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
DEFAULT_PER_PAGE = 30
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_INTERVAL_SECONDS = 7.0  # 未认证 search 限流 10 次/分钟，留出余量
DEFAULT_EXCLUDE_TERMS = ("-dsh", "-clawhub")
# in:readme 是必需项：仓库搜索默认只匹配 name/description/topics，不含 README，
# 实测不加该限定词时命中为 0。见 config/searches.json 的 file_constraint。
DEFAULT_QUERY_TEMPLATE = '{term} "SKILL.md" in:readme'
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


def discover(
    searches: dict,
    *,
    session: requests.Session | None = None,
    max_queries: int | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    sleep=time.sleep,
    discovered_at: str | None = None,
) -> tuple[list[Candidate], list[SearchOutcome]]:
    """按配置执行查询，返回 (候选, 每次查询的结果)。

    max_queries 用于本地抽样；min_interval_seconds 用于规避限流。
    """
    queries = build_queries(searches)
    if max_queries is not None:
        queries = queries[:max_queries]

    owns_session = session is None
    sess = session if session is not None else requests.Session()

    candidates: list[Candidate] = []
    outcomes: list[SearchOutcome] = []
    try:
        for index, query in enumerate(queries):
            if index and min_interval_seconds > 0:
                sleep(min_interval_seconds)
            outcome = github_search(
                query, session=sess, per_page=per_page, sleep=sleep, discovered_at=discovered_at
            )
            outcomes.append(outcome)
            if outcome.ok:
                candidates.extend(outcome.candidates)
    finally:
        if owns_session:
            sess.close()

    return candidates, outcomes


def load_searches(config_dir: str = "config") -> dict:
    """读取 config/searches.json。"""
    from pathlib import Path

    return json.loads((Path(config_dir) / "searches.json").read_text(encoding="utf-8"))
