"""批次管理与持久化搜索状态（§4, §5）。

职责：
- 搜索游标、仓库索引、待分配余项与批次状态的集中维护与原子持久化 (data/local/state/discovery.json)
- 按每批最多 N 个新仓库（batch_repo_limit）进行分批发现与待分配余项结转
- 仓库 SKILL.md 路径展开检查点与候选池幂等对账
- 仓库及批次“已处理”状态的正确推导与断点恢复
- 分类范围内的查询扩词规划与用量记账
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import time
from typing import Any, Callable

from src.infra.files import write_json_atomic
from src.infra.github import DEFAULT_PER_PAGE, SKILL_FILENAME
from src.shared.runtime import now_local
from .dedupe import candidate_from_repo
from .discovery import (
    DEFAULT_EXCLUDE_TERMS,
    DEFAULT_QUERY_TEMPLATE,
    Query,
    SearchOutcome,
    build_queries,
    build_query_string,
    candidates_from_sources,
)
from .models import Candidate
from .pool import (
    POOL_TERMINAL_STATUSES,
    STATUS_PENDING,
    CandidatePool,
    PoolItem,
    append_new_candidates,
    classify_pending_candidate,
    save_pool,
)

MAX_PAGE_ATTEMPTS = 3
DISCOVERY_SCHEMA_VERSION = "1.0.0"


class DiscoveryStopped(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _invoke(fn, *args, **kwargs):
    """适配注入函数的签名；函数内部 TypeError 不得导致重复请求。"""
    params = inspect.signature(fn).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **kwargs)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _skill_name_from_path(path: str, fallback: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return fallback


def build_searches_fingerprint(searches: dict) -> str:
    serialized = json.dumps(searches, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


@dataclass
class QueryCursor:
    domain_id: str
    term: str
    q: str
    source: str = "searches"  # "searches" | "expansion" | "sources"
    next_page: int = 1
    exhausted: bool = False
    page_attempts: int = 0
    last_error: str | None = None
    retry_at: float | None = None
    total_count: int | None = None


@dataclass
class DiscoveryState:
    """搜索游标、仓库索引、待分配余项和当前批次持久化状态。"""

    schema_version: str = DISCOVERY_SCHEMA_VERSION
    search_config_fingerprint: str = ""
    query_cursors: dict[str, dict[str, Any]] = field(default_factory=dict)
    repository_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    unassigned_repositories: list[dict[str, Any]] = field(default_factory=list)
    active_batch: dict[str, Any] | None = None
    completed_batches: list[dict[str, Any]] = field(default_factory=list)
    expansion_history: list[dict[str, Any]] = field(default_factory=list)
    pending_expansion: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveryState:
        ver = data.get("schema_version")
        if ver != DISCOVERY_SCHEMA_VERSION:
            raise ValueError(f"不支持的搜索状态 schema_version: {ver}（期望 {DISCOVERY_SCHEMA_VERSION}）")
        return cls(
            schema_version=ver,
            search_config_fingerprint=data.get("search_config_fingerprint", ""),
            query_cursors=dict(data.get("query_cursors") or {}),
            repository_index=dict(data.get("repository_index") or {}),
            unassigned_repositories=list(data.get("unassigned_repositories") or []),
            active_batch=data.get("active_batch"),
            completed_batches=list(data.get("completed_batches") or []),
            expansion_history=list(data.get("expansion_history") or []),
            pending_expansion=data.get("pending_expansion"),
        )


def load_discovery_state(path: Path) -> DiscoveryState | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        content = p.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError(f"搜索状态文件必须为 JSON 对象: {path}")
        return DiscoveryState.from_dict(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"搜索检查点 JSON 文件损坏: {path}: {exc}") from exc


def save_discovery_state(path: Path, state: DiscoveryState) -> None:
    write_json_atomic(Path(path), state.to_dict())


def init_or_migrate_discovery_state(
    state_path: Path,
    pool: CandidatePool | None,
    searches_cfg: dict,
    sources_cfg: dict | None = None,
) -> DiscoveryState:
    """初始化或从已有状态/旧候选池迁移搜索检查点。"""
    current = load_discovery_state(state_path)
    fingerprint = build_searches_fingerprint(searches_cfg)

    if current is not None:
        # 配置发生变化时仅追加新查询，不清空既有游标与仓库索引
        if current.search_config_fingerprint != fingerprint:
            current.search_config_fingerprint = fingerprint
            for query in build_queries(searches_cfg):
                key = f"{query.domain_id}:{query.term}"
                if key not in current.query_cursors:
                    current.query_cursors[key] = {
                        "domain_id": query.domain_id,
                        "term": query.term,
                        "q": query.q,
                        "source": "searches",
                        "next_page": 1,
                        "exhausted": False,
                        "page_attempts": 0,
                        "last_error": None,
                        "retry_at": None,
                        "total_count": None,
                    }
            save_discovery_state(state_path, current)
        return current

    # 全新初始化
    state = DiscoveryState(search_config_fingerprint=fingerprint)

    # 1. 如果有旧候选池，将已知仓库录入 repository_index（标记为历史已知，展开完备性未确认）
    if pool is not None:
        for item in pool.items:
            c = item.candidate
            key = f"{c.owner.lower()}/{c.repo.lower()}"
            if key not in state.repository_index:
                state.repository_index[key] = {
                    "owner": c.owner.lower(),
                    "repo": c.repo.lower(),
                    "url": c.repo_url or f"https://github.com/{c.owner}/{c.repo}",
                    "discovered_at": c.discovered_at or _utc_now(),
                    "search_term": c.search_terms[0] if getattr(c, "search_terms", None) else "",
                    "discovery_method": c.discovery_methods[0] if getattr(c, "discovery_methods", None) else "",
                    "source_id": c.source_ids[0] if getattr(c, "source_ids", None) else "",
                    "domain_id": c.domain_hints[0] if getattr(c, "domain_hints", None) else "",
                    "expanded": False,
                    "expand_error": None,
                    "skill_paths": [c.path] if c.path else [],
                    "has_skills": bool(c.path),
                    "status": "pending",
                    "processed": False,
                    "batch_id": None,
                }
            elif c.path and c.path not in state.repository_index[key]["skill_paths"]:
                state.repository_index[key]["skill_paths"].append(c.path)
                state.repository_index[key]["has_skills"] = True

    # 2. 如果配置了 sources_cfg，添加 sources 游标
    if sources_cfg:
        state.query_cursors["sources:sources.json"] = {
            "domain_id": "sources",
            "term": "sources.json",
            "q": "sources.json",
            "source": "sources",
            "next_page": 1,
            "exhausted": False,
            "page_attempts": 0,
            "last_error": None,
            "retry_at": None,
            "total_count": None,
        }

    # 3. 初始化 searches.json 的查询游标
    for query in build_queries(searches_cfg):
        key = f"{query.domain_id}:{query.term}"
        state.query_cursors[key] = {
            "domain_id": query.domain_id,
            "term": query.term,
            "q": query.q,
            "source": "searches",
            "next_page": 1,
            "exhausted": False,
            "page_attempts": 0,
            "last_error": None,
            "retry_at": None,
            "total_count": None,
        }

    save_discovery_state(state_path, state)
    return state


def acquire_next_repo_batch(
    discovery_state: DiscoveryState,
    state_path: Path,
    batch_repo_limit: int = 1000,
    searches_cfg: dict | None = None,
    sources_cfg: dict | None = None,
    *,
    search_fn: Callable = None,
    github_search_fn: Callable = None,
    per_page: int = DEFAULT_PER_PAGE,
    sleep: Callable = time.sleep,
    log: Callable = print,
    min_interval_seconds: float = 0.0,
    max_attempts: int = MAX_PAGE_ATTEMPTS,
    limit_queries: int | None = None,
    queried_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    """获取下一批新仓库，最多 N 个。

    1. 优先消费 unassigned_repositories
    2. 顺序推进现有来源和查询游标，直到达到 N 或全部当前查询用尽
    3. 超过 N 的返回项结转至 unassigned_repositories 并同事务保存
    4. 不足 N 且当前查询已达边界时，直接封存现有仓库进入评估，不无限扩词
    5. 返回新建的 active_batch 字典；若本轮完全无新仓库则返回 None
    """
    if batch_repo_limit < 1:
        raise ValueError(f"batch_repo_limit 必须为正整数，收到 {batch_repo_limit}")

    search_func = search_fn or github_search_fn
    batch_repos: list[dict[str, Any]] = []
    queried_keys = queried_keys if queried_keys is not None else set()
    limited = False

    # 修复旧版本已归档批次遗留的失败仓库引用。
    for repo in discovery_state.repository_index.values():
        if repo.get("expand_error") and not repo.get("processed") and not discovery_state.active_batch:
            repo["batch_id"] = None

    # 0. 检查并补齐 repository_index 中尚未分配到批次的仓库
    unassigned_keys = {f"{r['owner'].lower()}/{r['repo'].lower()}" for r in discovery_state.unassigned_repositories}
    for k, repo_info in discovery_state.repository_index.items():
        if (not repo_info.get("batch_id") and not repo_info.get("processed")
                and repo_info.get("expand_attempts", 0) < max_attempts and k not in unassigned_keys):
            discovery_state.unassigned_repositories.append(repo_info)
            unassigned_keys.add(k)

    # 1. 优先消费未分配余项
    while discovery_state.unassigned_repositories and len(batch_repos) < batch_repo_limit:
        item = discovery_state.unassigned_repositories.pop(0)
        key = f"{item['owner'].lower()}/{item['repo'].lower()}"
        if key not in [f"{r['owner'].lower()}/{r['repo'].lower()}" for r in batch_repos]:
            batch_repos.append(item)

    if len(batch_repos) >= batch_repo_limit:
        return _create_and_save_batch(discovery_state, state_path, batch_repos, batch_repo_limit)

    # 2. 如果配置了 sources 且 sources 游标未耗尽，先消费 sources 种子
    sources_cursor = discovery_state.query_cursors.get("sources:sources.json")
    if sources_cfg and sources_cursor and not sources_cursor.get("exhausted"):
        seeds = candidates_from_sources(sources_cfg, discovered_at=_utc_now())
        for seed in seeds:
            key = f"{seed.owner.lower()}/{seed.repo.lower()}"
            if key not in discovery_state.repository_index:
                repo_dict = {
                    "owner": seed.owner.lower(),
                    "repo": seed.repo.lower(),
                    "url": seed.repo_url or seed.url,
                    "discovered_at": seed.discovered_at or _utc_now(),
                    "search_term": "sources.json",
                    "discovery_method": "provided_lead",
                    "source_id": seed.source_ids[0] if seed.source_ids else "",
                    "domain_id": "sources",
                    "expanded": False,
                    "expand_error": None,
                    "skill_paths": [seed.path] if seed.path else [],
                    "has_skills": bool(seed.path),
                    "status": "pending",
                    "processed": False,
                    "batch_id": None,
                }
                discovery_state.repository_index[key] = repo_dict
                if len(batch_repos) < batch_repo_limit:
                    batch_repos.append(repo_dict)
                else:
                    discovery_state.unassigned_repositories.append(repo_dict)
            elif seed.path:
                paths = discovery_state.repository_index[key].setdefault("skill_paths", [])
                if seed.path not in paths:
                    paths.append(seed.path)
        sources_cursor["exhausted"] = True
        sources_cursor["next_page"] = 2
        save_discovery_state(state_path, discovery_state)

    if len(batch_repos) >= batch_repo_limit:
        return _create_and_save_batch(discovery_state, state_path, batch_repos, batch_repo_limit)

    # 3. 顺序推进查询游标
    if search_func is not None:
        first_query = True
        for key, cursor in discovery_state.query_cursors.items():
            if cursor.get("source") == "sources":
                continue
            if cursor.get("exhausted"):
                # 旧版本错误地把失败页标成 exhausted；恢复该游标而非跳过。
                if cursor.get("last_error"):
                    cursor["exhausted"] = False
                else:
                    continue
            if limit_queries is not None and key not in queried_keys and len(queried_keys) >= limit_queries:
                limited = True
                continue

            while not cursor.get("exhausted") and len(batch_repos) < batch_repo_limit:
                page = cursor.get("next_page", 1)
                attempts = cursor.get("page_attempts", 0)
                if attempts >= max_attempts:
                    break

                if not first_query and min_interval_seconds > 0:
                    sleep(min_interval_seconds)
                first_query = False

                log(f"[搜索] {cursor['term']} 第 {page} 页，第 {attempts + 1}/{max_attempts} 次尝试")

                q_obj = Query(domain_id=cursor["domain_id"], term=cursor["term"], q=cursor["q"])
                queried_keys.add(key)
                cursor["page_attempts"] = attempts + 1
                cursor["last_error"] = "搜索请求中断，结果未确认"
                save_discovery_state(state_path, discovery_state)
                retry_info = {}
                try:
                    res = _invoke(search_func, q_obj, page=page, per_page=per_page,
                                  sleep=sleep, max_attempts=1, retry_info=retry_info)
                except Exception as exc:
                    res = SearchOutcome(query=q_obj, ok=False, error=str(exc))
                if isinstance(res, SearchOutcome):
                    outcome = res
                elif isinstance(res, tuple) and len(res) == 2:
                    raw_items, total = res
                    candidates = []
                    for item in raw_items:
                        if isinstance(item, Candidate):
                            candidates.append(item)
                        elif isinstance(item, dict):
                            full_name = item.get("full_name") or f"{item.get('owner', '')}/{item.get('repo', '')}"
                            parts = full_name.split("/") if "/" in full_name else ("", "")
                            candidates.append(candidate_from_repo(
                                parts[0], parts[1], url=item.get("html_url") or "",
                                search_term=cursor["term"],
                            ))
                    outcome = SearchOutcome(query=q_obj, ok=True, total_count=total, candidates=candidates)
                elif isinstance(res, tuple) and len(res) == 4:
                    ok, raw_items, total, error = res
                    candidates = []
                    for item in (raw_items or []):
                        if isinstance(item, Candidate):
                            candidates.append(item)
                        elif isinstance(item, dict):
                            full_name = item.get("full_name") or f"{item.get('owner', '')}/{item.get('repo', '')}"
                            parts = full_name.split("/") if "/" in full_name else ("", "")
                            candidates.append(candidate_from_repo(
                                parts[0], parts[1], url=item.get("html_url") or "",
                                search_term=cursor["term"],
                            ))
                    outcome = SearchOutcome(query=q_obj, ok=ok, total_count=total, candidates=candidates, error=error)
                else:
                    outcome = res

                if not outcome.ok:
                    cursor["page_attempts"] = attempts + 1
                    cursor["last_error"] = outcome.error or outcome.reason_code or "search_failed"
                    log(f"[搜索失败] {cursor['term']} 第 {page} 页：{cursor['last_error']}")
                    save_discovery_state(state_path, discovery_state)
                    if cursor["page_attempts"] < max_attempts:
                        sleep(retry_info.get("retry_after") or min(2 ** attempts, 30))
                    continue

                # 成功返回
                cursor["page_attempts"] = 0
                cursor["last_error"] = None
                cursor["total_count"] = outcome.total_count

                new_repos_for_page: list[dict[str, Any]] = []
                for cand in outcome.candidates:
                    repo_key = f"{cand.owner.lower()}/{cand.repo.lower()}"
                    if repo_key not in discovery_state.repository_index:
                        repo_dict = {
                            "owner": cand.owner.lower(),
                            "repo": cand.repo.lower(),
                            "url": cand.repo_url or cand.url,
                            "discovered_at": cand.discovered_at or _utc_now(),
                            "search_term": cand.search_terms[0] if cand.search_terms else cursor["term"],
                            "discovery_method": cand.discovery_methods[0] if cand.discovery_methods else "github_search",
                            "source_id": cand.source_ids[0] if cand.source_ids else "",
                            "domain_id": cursor["domain_id"],
                            "expanded": False,
                            "expand_error": None,
                            "skill_paths": [],
                            "has_skills": False,
                            "status": "pending",
                            "processed": False,
                            "batch_id": None,
                        }
                        discovery_state.repository_index[repo_key] = repo_dict
                        new_repos_for_page.append(repo_dict)

                # 同步保存检查点：将新发现仓库加入 unassigned_repositories 并同事务推进页码
                discovery_state.unassigned_repositories.extend(new_repos_for_page)
                cursor["next_page"] = page + 1
                items_returned = len(outcome.candidates)
                if items_returned < per_page or (outcome.total_count and page * per_page >= outcome.total_count):
                    cursor["exhausted"] = True

                save_discovery_state(state_path, discovery_state)

                # 消费未分配余项填充当前批次
                while discovery_state.unassigned_repositories and len(batch_repos) < batch_repo_limit:
                    item = discovery_state.unassigned_repositories.pop(0)
                    key = f"{item['owner'].lower()}/{item['repo'].lower()}"
                    if key not in [f"{r['owner'].lower()}/{r['repo'].lower()}" for r in batch_repos]:
                        batch_repos.append(item)

            if len(batch_repos) >= batch_repo_limit:
                break

    if not batch_repos:
        if any(c.get("last_error") for c in discovery_state.query_cursors.values()):
            raise DiscoveryStopped("search_failed")
        if limited or (limit_queries is not None and len(queried_keys) >= limit_queries):
            raise DiscoveryStopped("discovery_limit")
        if any(not r.get("processed") and r.get("expand_attempts", 0) >= max_attempts
               for r in discovery_state.repository_index.values()):
            raise DiscoveryStopped("repository_expand_failed")
        return None

    return _create_and_save_batch(discovery_state, state_path, batch_repos, batch_repo_limit)


def _create_and_save_batch(
    discovery_state: DiscoveryState,
    state_path: Path,
    batch_repos: list[dict[str, Any]],
    batch_repo_limit: int,
) -> dict[str, Any]:
    batch_seq = len(discovery_state.completed_batches) + 1
    batch_id = f"batch-{batch_seq}"

    repo_keys = []
    for r in batch_repos:
        k = f"{r['owner']}/{r['repo']}"
        repo_keys.append(k)
        if k in discovery_state.repository_index:
            discovery_state.repository_index[k]["batch_id"] = batch_id

    active_batch = {
        "batch_id": batch_id,
        "batch_seq": batch_seq,
        "repo_limit": batch_repo_limit,
        "stage": "expanding",
        "repositories": repo_keys,
        "skill_ids": [],
        "created_at": now_local().isoformat(),
        "updated_at": now_local().isoformat(),
        "summary": {
            "total_repos": len(repo_keys),
            "expanded_repos": 0,
            "repos_with_skills": 0,
            "total_skills": 0,
            "evaluations_done": 0,
            "evaluations_failed": 0,
        },
    }
    discovery_state.active_batch = active_batch
    save_discovery_state(state_path, discovery_state)
    return active_batch


def expand_batch_skills(
    discovery_state: DiscoveryState,
    state_path: Path,
    pool: CandidatePool,
    pool_path: Path,
    *,
    expand_repo_fn: Callable,
    old_recommended: set[str] | None = None,
    source_types: dict[str, str] | None = None,
    sleep: Callable = time.sleep,
    log: Callable = print,
    max_attempts: int = MAX_PAGE_ATTEMPTS,
    expand_limit: int | None = None,
    expanded_keys: set[str] | None = None,
) -> tuple[int, int]:
    """展开当前批次中尚未展开的仓库，提取真实 SKILL.md 并幂等入池。

    返回 (新增 Skill 数量, 成功展开仓库数量)。
    """
    batch = discovery_state.active_batch
    if not batch:
        return 0, 0

    old_recommended = old_recommended or set()
    source_types = source_types or {}
    new_candidates: list[Candidate] = []
    expanded_repos = 0
    expanded_keys = expanded_keys if expanded_keys is not None else set()
    limited = False

    for repo_key in batch["repositories"]:
        repo_info = discovery_state.repository_index.get(repo_key)
        if not repo_info:
            continue

        if not repo_info.get("expanded"):
            if repo_info.get("expand_attempts", 0) >= max_attempts:
                continue
            if expand_limit is not None and repo_key not in expanded_keys and len(expanded_keys) >= expand_limit:
                limited = True
                continue
            expanded_keys.add(repo_key)
            owner, repo = repo_info["owner"], repo_info["repo"]
            repo_info["expand_attempts"] = repo_info.get("expand_attempts", 0) + 1
            log(f"[展开] {repo_key} 第 {repo_info['expand_attempts']}/{max_attempts} 次尝试")
            repo_info["expand_error"] = "展开请求中断，结果未确认"
            save_discovery_state(state_path, discovery_state)
            try:
                paths, error = _invoke(expand_repo_fn, owner, repo, sleep=sleep, max_attempts=1)
            except Exception as exc:
                paths, error = [], str(exc)
            repo_info["skill_paths"] = list(dict.fromkeys(repo_info.get("skill_paths", []) + (paths or [])))
            if error:
                # 展开网络或权限错误，未确认展开结果，保留待恢复
                repo_info["expanded"] = False
                repo_info["expand_error"] = error
                repo_info["status"] = "expand_failed"
                repo_info["processed"] = False
            else:
                repo_info["expanded"] = True
                repo_info["expand_error"] = error
                repo_info["has_skills"] = bool(repo_info["skill_paths"])
                expanded_repos += 1

                if not repo_info["skill_paths"]:
                    # 明确无 Skill，直接推导为已处理
                    repo_info["processed"] = True
                    repo_info["status"] = "completed"

            save_discovery_state(state_path, discovery_state)

        # 为该库下的所有有效 SKILL.md 生成 Candidate 并入池
        for path in repo_info.get("skill_paths", []):
            cand = candidate_from_repo(
                repo_info["owner"],
                repo_info["repo"],
                path=path,
                url=f"https://github.com/{repo_info['owner']}/{repo_info['repo']}/blob/HEAD/{path}",
                repo_url=repo_info["url"],
                name=_skill_name_from_path(path, repo_info["repo"]),
                description=repo_info.get("description", ""),
                source_id=repo_info.get("source_id", ""),
                discovery_method=repo_info.get("discovery_method", ""),
                search_term=repo_info.get("search_term", ""),
                discovered_at=repo_info.get("discovered_at", ""),
            )
            if repo_info.get("domain_id"):
                cand.domain_hints = [repo_info["domain_id"]]
            if cand.skill_id not in batch["skill_ids"]:
                batch["skill_ids"].append(cand.skill_id)
            new_candidates.append(cand)

    # 幂等追加新候选入池
    added_to_pool = append_new_candidates(pool, new_candidates, old_recommended, source_types)
    save_pool(pool_path, pool)

    # 更新批次阶段至 evaluating
    batch["stage"] = "evaluating"
    batch["expand_limited"] = limited
    batch["expand_max_attempts"] = max_attempts
    batch["updated_at"] = now_local().isoformat()
    batch["summary"]["expanded_repos"] = sum(
        1 for rk in batch["repositories"]
        if discovery_state.repository_index.get(rk, {}).get("expanded")
    )
    batch["summary"]["repos_with_skills"] = sum(
        1 for rk in batch["repositories"]
        if discovery_state.repository_index.get(rk, {}).get("has_skills")
    )
    batch["summary"]["total_skills"] = len(batch["skill_ids"])
    save_discovery_state(state_path, discovery_state)

    return added_to_pool, expanded_repos


def reconcile_batch_and_repositories(
    discovery_state: DiscoveryState,
    state_path: Path,
    pool: CandidatePool,
    active_snoozed: set[str],
    manual_exclusions: set[str] | dict,
    owned_ids: set[str],
) -> bool:
    """对账当前批次及仓库的完成状态。

    “已处理”定义：展开已确认，且该仓库的所有 Skill 均具有终态（done/excluded/fetch_failed等）或政策跳过（snoozed/excluded/owned）。
    若当前批次的所有候选均已对账完成，则将 active_batch 归档至 completed_batches，返回 True；否则返回 False。
    """
    batch = discovery_state.active_batch
    if not batch:
        return False

    pool_status_map = {item.candidate.skill_id: item.status for item in pool.items}

    # 1. 对账仓库状态
    for repo_key in batch["repositories"]:
        repo_info = discovery_state.repository_index.get(repo_key)
        if not repo_info:
            continue
        if not repo_info.get("expanded"):
            repo_info["processed"] = False
            continue

        skill_paths = repo_info.get("skill_paths", [])
        if not skill_paths:
            if not repo_info.get("expand_error"):
                # 确认无 Skill，已完成
                repo_info["processed"] = True
                repo_info["status"] = "completed"
            else:
                repo_info["processed"] = False
                repo_info["status"] = "expand_failed"
            continue

        all_skills_resolved = True
        for path in skill_paths:
            sid = f"{repo_info['owner']}/{repo_info['repo']}:{path}"
            st = pool_status_map.get(sid, STATUS_PENDING)
            if st in POOL_TERMINAL_STATUSES:
                continue
            # 检查政策跳过
            is_act, _ = classify_pending_candidate(sid, active_snoozed, manual_exclusions, owned_ids)
            if not is_act:
                continue
            all_skills_resolved = False
            break

        if all_skills_resolved:
            repo_info["processed"] = True
            repo_info["status"] = "completed"
        else:
            repo_info["processed"] = False

    # 2. 对账当前批次
    all_batch_skills_resolved = True
    for sid in batch["skill_ids"]:
        st = pool_status_map.get(sid, STATUS_PENDING)
        if st in POOL_TERMINAL_STATUSES:
            continue
        is_act, _ = classify_pending_candidate(sid, active_snoozed, manual_exclusions, owned_ids)
        if not is_act:
            continue
        all_batch_skills_resolved = False
        break

    batch["updated_at"] = now_local().isoformat()
    unresolved = [rk for rk in batch["repositories"]
                  if not discovery_state.repository_index.get(rk, {}).get("expanded")]
    if all_batch_skills_resolved and unresolved:
        if batch.get("expand_limited"):
            batch["stage"] = "expanding"
            save_discovery_state(state_path, discovery_state)
            raise DiscoveryStopped("discovery_limit")
        retryable = [rk for rk in unresolved if discovery_state.repository_index[rk].get("expand_attempts", 0)
                     < batch.get("expand_max_attempts", MAX_PAGE_ATTEMPTS)]
        if retryable:
            batch["stage"] = "expanding"
            save_discovery_state(state_path, discovery_state)
            return False
        batch["failed_repositories"] = unresolved
        if len(unresolved) == len(batch["repositories"]):
            batch["stage"] = "expanding"
            save_discovery_state(state_path, discovery_state)
            raise DiscoveryStopped("repository_expand_failed")
        for rk in unresolved:
            discovery_state.repository_index[rk]["batch_id"] = None
    if all_batch_skills_resolved:
        batch["stage"] = "completed"
        discovery_state.completed_batches.append(dict(batch))
        discovery_state.active_batch = None
        save_discovery_state(state_path, discovery_state)
        return True

    save_discovery_state(state_path, discovery_state)
    return False


def build_expansion_prompt(
    taxonomy: dict[str, Any],
    existing_terms: list[str],
) -> tuple[str, str]:
    """构建分类内搜索词扩展的 Prompt。"""
    system = (
        "你是一个 GitHub 技能库（Skill）检索规划专家。根据给定的技能分类体系与已有搜索词，"
        "在现有分类范围内生成更精准、更有针对性的新检索关键词。\n\n"
        "硬性规则：\n"
        "1. 严格在现有分类领域（taxonomy）范围内构思，绝对不得臆造新领域。\n"
        "2. 返回严格的 JSON 对象：{\"queries\": [{\"domain_id\": \"领域ID\", \"term\": \"新检索词\"}]}。\n"
        "3. 检索词应当是具体的工具名称、协议、格式或工程术语，中英文均可，简短精准。\n"
        "4. 绝不包含任何 markdown 代码块外部的说明文字。"
    )

    domains_text = []
    for d in taxonomy.get("main_categories", []):
        domains_text.append(f"- 领域ID: {d['id']}，名称: {d.get('name', '')}，范围: {d.get('scope', '')}")

    user = (
        f"【现有技能分类体系】\n"
        + "\n".join(domains_text)
        + "\n\n【已有历史检索词（请勿重复）】\n"
        + ", ".join(existing_terms[:100])
        + "\n\n请在上述分类领域内，为每个或关键领域各生成 1-3 个此前未覆盖的高质量新检索词，返回纯 JSON。"
    )
    return system, user


def parse_expansion_response(content: str) -> list[dict[str, str]]:
    """解析扩词响应并提取查询列表。"""
    if not content:
        return []
    clean = content.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        data = json.loads(clean)
        queries = data.get("queries") if isinstance(data, dict) else data
        if not isinstance(queries, list):
            return []
        valid = []
        for item in queries:
            if (isinstance(item, dict) and isinstance(item.get("domain_id"), str)
                    and isinstance(item.get("term"), str) and item["domain_id"].strip() and item["term"].strip()):
                valid.append({
                    "domain_id": str(item["domain_id"]).strip(),
                    "term": str(item["term"]).strip(),
                })
        return valid
    except Exception:
        return []
