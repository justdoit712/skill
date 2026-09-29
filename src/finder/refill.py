"""有界多轮检索；所有游标、队列和阶段进度保存在运行事实中。"""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import re
import time

from src.shared.models import Candidate
from src.shared.owned import is_skill_owned
from .plan import build_reflection_prompt, parse_reflection_queries, PLAN_MAX_OUTPUT_TOKENS
from .search import schedule_candidates_fairly, _round_robin_merge_repos

MAX_PAGE_ATTEMPTS = 3  # Includes the initial HTTP request; no nested retries.

EVALUATION_BATCH_SIZE = 12
BATCH_MAX_PER_REPO = 2
EXPAND_INTERLEAVE_REPOS = 5
CONSECUTIVE_ZERO_BATCHES_FOR_REFLECTION = 2


def initialize_search(state):
    report = state.report
    search = report["search"]
    defaults = {"current_round": 0, "rounds_history": [], "query_cursors": {},
                "repository_queue": [], "discovered_repo_urls": [], "candidate_queue": [],
                "discovered_skill_ids": [], "processed_skill_ids": [], "consecutive_failures": 0,
                "query_yield": {}, "skill_discovery_sources": {}}
    for key, value in defaults.items():
        search.setdefault(key, value)
    completed = {e["candidate"]["skill_id"] for e in report["evaluations"]}
    # Legacy reports cannot reconstruct pending candidates; rediscover once, skip recorded attempts.
    completed.update(c["skill_id"] for c in report.get("calls", [])
                     if c.get("skill_id") and c.get("state") != "not_sent")
    search["processed_skill_ids"] = sorted(set(search["processed_skill_ids"]) | completed)
    for query, cursor in search["query_cursors"].items():
        if "page_attempts" not in cursor:
            # Legacy failed searches already used the transport's three attempts.
            failed = any(q.get("query") == query and q.get("page", 1) == cursor["next_page"]
                         and not q.get("ok") for q in search["queries_executed"])
            cursor.update(page_attempts=MAX_PAGE_ATTEMPTS if failed else 0,
                          blocked=failed, last_error="legacy_search_failed" if failed else None)


def reset_failed_searches(state):
    """Explicitly reopen failed pages, preserving history and server cooldowns."""
    initialize_search(state)
    search = state.report["search"]
    queries = []
    for query, cursor in search["query_cursors"].items():
        if cursor.get("page_attempts") and not cursor["exhausted"]:
            queries.append(query)
            cursor.update(page_attempts=0, blocked=False)
    if queries:
        search.setdefault("retry_resets", []).append({"at": time.time(), "queries": queries})
        if search["rounds_history"]:
            current = search["rounds_history"][-1]
            current.update(phase="searching", retry_queries=queries)
        state.save()


def _wait_for_search(state, cursor, sleep, log):
    deadline = max(cursor.get("retry_at", 0), state.report["search"].get("retry_at", 0))
    delay = max(0, deadline - time.time())
    if delay:
        log(f"[检索退避] 等待 {delay:.1f} 秒，可中断后续跑。")
    while delay > 0:
        reason = state.stop_reason()
        if reason:
            return reason
        chunk = min(delay, 60)
        sleep(chunk)
        delay -= chunk
    return state.stop_reason()


def _search_page(state, current, query, cursor, search_fn, sleep, log):
    search = state.report["search"]
    page = cursor["next_page"]
    while cursor.get("page_attempts", 0) < MAX_PAGE_ATTEMPTS and not cursor.get("blocked"):
        reason = _wait_for_search(state, cursor, sleep, log)
        if reason:
            return [], reason
        attempt = cursor.get("page_attempts", 0) + 1
        # Reserve an attempt before sending, so an interrupted request cannot
        # gain unlimited retries through --resume.
        cursor.update(page_attempts=attempt, last_error="request_interrupted",
                      retry_at=time.time() + 2 ** (attempt - 1))
        entry = {"query": query, "page": page, "attempt": attempt, "ok": False,
                 "repos_returned": 0, "error": "request_interrupted"}
        current["searches"].append(entry)
        search["queries_executed"].append(dict(entry, round=current["round"]))
        state.save()
        log(f"[轮次 {current['round']}/{state.report['parameters']['max_rounds']}] "
            f"检索 {query}，第 {page} 页，第 {attempt}/{MAX_PAGE_ATTEMPTS} 次尝试")
        retry_info = {}
        try:
            ok, repos, error = search_fn(query, page=page, sleep=sleep,
                                        max_attempts=1, retry_info=retry_info)
        except BaseException:
            state.report["coverage_incomplete"] = True
            state.save()
            raise
        entry.update(ok=ok, repos_returned=len(repos), error=error)
        search["queries_executed"][-1].update(entry)
        if ok:
            # The caller saves cursor advancement and discovered repos together.
            cursor.update(next_page=page + 1, exhausted=len(repos) < 20 or page >= 50,
                          page_attempts=0, blocked=False, last_error=None, retry_at=0)
            return repos, None
        state.report["coverage_incomplete"] = True
        cooldown = retry_info.get("retry_after", 0)
        if cooldown:
            search["retry_at"] = time.time() + cooldown
        cursor.update(last_error=error, retry_at=time.time() + max(2 ** (attempt - 1), cooldown),
                      blocked=attempt >= MAX_PAGE_ATTEMPTS or retry_info.get("retryable") is False)
        state.save()
    cursor["blocked"] = True
    state.report["coverage_incomplete"] = True
    state.save()
    log(f"[跳过失败页] {query} 第 {page} 页已停止自动重试，继续其他查询。")
    return [], None


def _reflect(state, current, cfg, transport, api_key, sleep, log):
    if current.get("reflection_error"):
        return [], "reflection_failed"
    if "reflection_queries" in current:
        return current["reflection_queries"], None
    # A completed or interrupted reflection request must never be silently paid for twice.
    reason = state.stop_reason()
    if reason:
        return [], reason
    report = state.report
    previous = list(report["search"]["query_cursors"])
    recent = report["evaluations"][current.get("feedback_start", 0):]
    system, user = build_reflection_prompt(report["topic"], report["plan"], previous, recent)
    config = deepcopy(cfg)
    config.setdefault("limits", {})["max_output_tokens"] = PLAN_MAX_OUTPUT_TOKENS
    if current.get("reflection_started"):
        call = report["calls"][current["reflection_call_index"]]
        if not call.get("response"):
            return [], "usage_unknown"
        result = SimpleNamespace(**call["response"])
        unknown = bool(state.usage.unknown_usage_requests)
    else:
        current["reflection_started"] = True
        current["reflection_call_index"] = len(report["calls"])
        result, unknown = state.call(transport, config, system, user, api_key=api_key, sleep=sleep,
                                     stage="reflection")
    if unknown:
        return [], "usage_unknown"
    try:
        if not result.ok or not result.content:
            raise ValueError("反思调用失败")
        queries = parse_reflection_queries(result.content, previous)
    except (ValueError, TypeError) as exc:
        report["errors"].append({"stage": "reflection", "code": "reflection_failed", "message": str(exc)})
        current["reflection_queries"] = []
        current["reflection_error"] = True
        state.save()
        return [], "reflection_failed"
    current["reflection_queries"] = queries
    state.save()
    log("[反思拓词] " + ", ".join(queries))
    return queries, state.stop_reason()


def _search_pages(state, current, queries, search_fn, sleep, log):
    search = state.report["search"]
    seen = {r["key"] for r in search["repository_queue"]}
    for query in queries:
        reason = state.stop_reason()
        if reason:
            return reason
        cursor = search["query_cursors"].setdefault(query, {"next_page": 1, "exhausted": False})
        # Only a successful page is complete; failures retain their page/counter.
        if any(q["query"] == query and q["ok"] and q["page"] == cursor["next_page"] - 1
               for q in current["searches"]):
            continue
        if cursor["exhausted"]:
            continue
        repos, reason = _search_page(state, current, query, cursor, search_fn, sleep, log)
        if reason:
            return reason
        if cursor.get("last_error") is None:
            if len(repos) >= 20:
                state.report["coverage_incomplete"] = True
            qy = search.setdefault("query_yield", {}).setdefault(query, {
                "query": query,
                "total_repos": 0,
                "marginal_repos": 0,
                "total_skills": 0,
                "marginal_skills": 0,
                "note": "边际新增计数受执行顺序影响",
            })
            qy["total_repos"] += len(repos)

            # Persist every discovered repository, including those outside the next expansion batch.
            new = []
            for repo in repos:
                key = f"{repo['owner'].lower()}/{repo['repo'].lower()}"
                if key not in seen:
                    seen.add(key)
                    new.append(dict(repo, url=repo.get("url") or f"https://github.com/{key}",
                                    key=key, state="pending", query=query, round=current["round"],
                                    first_discovered_by=query, discovery_queries=[query]))
                    qy["marginal_repos"] += 1
                else:
                    for existing_r in search["repository_queue"]:
                        if existing_r.get("key") == key:
                            d_queries = existing_r.setdefault("discovery_queries", [existing_r.get("query", "")])
                            if query not in d_queries:
                                d_queries.append(query)
                            break
            search["repository_queue"].extend(new)
            search["discovered_repo_urls"] = [r["url"] for r in search["repository_queue"]]
            search["repos_discovered"] = len(search["repository_queue"])
            current["new_repos"] += len(new)
        state.save()
    relevant = [q for q in current["searches"] if q["query"] in queries]
    blocked = any(search["query_cursors"][q].get("blocked") for q in queries)
    if (relevant or blocked) and not any(q["ok"] for q in relevant):
        return "search_failed"
    return None


def _expand_pending(state, current, expand, owned_ids, sleep, log, max_repos: int = 20):
    search = state.report["search"]
    pending_repos = [r for r in search["repository_queue"] if r["state"] == "pending"]
    if not pending_repos:
        return None

    from .relevance import extract_relevance_terms, rank_pending_repositories, score_candidate_relevance
    terms = extract_relevance_terms(state.report.get("topic", ""), state.report.get("plan"))
    pending = rank_pending_repositories(pending_repos, terms, max_repos=max_repos)
    seen = set(search["discovered_skill_ids"])
    keywords = set(terms.keys()) | set(re.findall(r"[\w]+", state.report.get("topic", "").lower()))
    for repo in pending:
        reason = state.stop_reason()
        if reason:
            return reason
        candidates, expansions = expand([repo], keywords=keywords, max_files_per_repo=None, sleep=sleep, log=log)
        failed = bool(expansions) and all(not e.get("ok", False) for e in expansions)
        repo["state"] = "failed" if failed else "expanded"
        search["expansions"].extend(expansions)
        state.report["coverage_incomplete"] |= any(not e.get("ok", False) or e.get("truncated") for e in expansions)
        repo_first_query = repo.get("first_discovered_by") or repo.get("query", "")
        repo_discovery_queries = list(repo.get("discovery_queries") or ([repo_first_query] if repo_first_query else []))

        for dq in repo_discovery_queries:
            if dq:
                search.setdefault("query_yield", {}).setdefault(dq, {
                    "query": dq,
                    "total_repos": 0,
                    "marginal_repos": 0,
                    "total_skills": 0,
                    "marginal_skills": 0,
                    "note": "边际新增计数受执行顺序影响",
                })["total_skills"] += len(candidates)

        for candidate in candidates:
            if candidate.skill_id in seen:
                existing_src = search.setdefault("skill_discovery_sources", {}).get(candidate.skill_id)
                if existing_src:
                    for dq in repo_discovery_queries:
                        if dq and dq not in existing_src.setdefault("discovery_queries", []):
                            existing_src["discovery_queries"].append(dq)
                continue
            seen.add(candidate.skill_id)
            current["candidates"] += 1

            search.setdefault("skill_discovery_sources", {})[candidate.skill_id] = {
                "skill_id": candidate.skill_id,
                "first_discovered_by": repo_first_query,
                "discovery_queries": repo_discovery_queries,
                "repo_url": repo.get("url") or f"https://github.com/{repo.get('owner')}/{repo.get('repo')}",
            }
            if repo_first_query:
                search.setdefault("query_yield", {}).setdefault(repo_first_query, {
                    "query": repo_first_query,
                    "total_repos": 0,
                    "marginal_repos": 0,
                    "total_skills": 0,
                    "marginal_skills": 0,
                    "note": "边际新增计数受执行顺序影响",
                })["marginal_skills"] += 1

            if is_skill_owned(candidate.skill_id, owned_ids):
                search["skipped_owned_ids"].append(candidate.skill_id)
                search["skipped"].append({"skill_id": candidate.skill_id, "code": "owned"})
            else:
                cand_dict = asdict(candidate)
                rel_info = score_candidate_relevance(candidate, terms)
                cand_dict["relevance_score"] = rel_info["score"]
                cand_dict["matched_terms"] = rel_info["matched_terms"]
                search["candidate_queue"].append(cand_dict)
        search["discovered_skill_ids"] = sorted(seen)
        search["candidates_found"] = len(seen)
        search["skipped_owned"] = len(search["skipped_owned_ids"])
        state.save()
    return None


def run_rounds(state, cfg, api_key, transport, search_fn, expand, fetch, evaluate, owned_ids, sleep, log, enable_active_reflection: bool = False):
    initialize_search(state)
    report, search = state.report, state.report["search"]
    while True:
        reason = state.stop_reason()
        if reason:
            return reason
        if search["current_round"] > report["parameters"]["max_rounds"]:
            return "round_limit"
        history = search["rounds_history"]
        if not history or history[-1]["phase"] == "complete":
            if search["current_round"] >= report["parameters"]["max_rounds"]:
                return "round_limit"
            previous = history[-1] if history else {}
            search["current_round"] += 1
            current = {"round": search["current_round"], "phase": "searching", "strategy": "initial" if not history else "pagination",
                       "searches": [], "new_repos": 0, "candidates": 0, "evaluation_start": len(report["evaluations"]),
                       "feedback_start": previous.get("evaluation_start", 0), "batch_index": 0, "consecutive_zero_batches": 0}
            history.append(current)
            state.save()  # Empty rounds consume a round too.
        else:
            current = history[-1]
        if current["phase"] == "searching":
            # Two consecutive fully evaluated, irrelevant rounds warrant changing queries.
            recent = history[-3:-1]
            reflect_first = len(recent) == 2 and all(r.get("all_none") for r in recent)
            if current["strategy"] == "initial":
                queries = report["plan"]["queries"]
            elif current.get("reflection_queries"):
                queries = current["reflection_queries"]
            else:
                queries = list(search["query_cursors"])
            if current.get("retry_queries"):
                queries = list(dict.fromkeys(current["retry_queries"] + queries))
                reflect_first = False
            if not reflect_first:
                reason = _search_pages(state, current, queries, search_fn, sleep, log)
                if reason:
                    return reason
            if current["strategy"] != "initial" and (current.get("reflection_started") or reflect_first or not current["new_repos"]):
                queries, reason = _reflect(state, current, cfg, transport, api_key, sleep, log)
                if reason:
                    return reason
                if not queries:
                    current["phase"] = "complete"
                    state.save()
                    return "candidates_exhausted"
                current["strategy"] = "llm_reflection"
                reason = _search_pages(state, current, queries, search_fn, sleep, log)
                if reason:
                    return reason
            current["phase"] = "processing"
            current.pop("retry_queries", None)
            state.save()
        # Consume saved candidates/repositories in bounded batches and interleave pending repo expansions
        while True:
            reason = state.stop_reason()
            if reason:
                return reason
            done = set(search["processed_skill_ids"])
            from src.shared.models import Candidate
            valid_keys = set(Candidate.__dataclass_fields__.keys())
            queue = [Candidate(**{k: v for k, v in c.items() if k in valid_keys})
                     for c in search["candidate_queue"] if c["skill_id"] not in done]

            # 1. 若候选队列为空但有待展开仓库，首先展开一批待展开仓库
            if not queue and any(r["state"] == "pending" for r in search["repository_queue"]):
                reason = _expand_pending(state, current, expand, owned_ids, sleep, log, max_repos=20)
                if reason:
                    return reason
                continue

            # 2. 如果候选队列非空，调度一个有界批次进行评估（最多 12 个候选，单仓库最多 2 个）
            if queue:
                from .relevance import extract_relevance_terms
                terms = extract_relevance_terms(state.report.get("topic", ""), state.report.get("plan"))
                batch = schedule_candidates_fairly(
                    queue,
                    max_total=EVALUATION_BATCH_SIZE,
                    max_per_repo=BATCH_MAX_PER_REPO,
                    term_weights=terms,
                )
                eval_start_count = len(report["evaluations"])
                reason = evaluate(state, batch, cfg, api_key, transport, fetch, sleep, log=log)

                # 统计批次产出与滑动窗口指标（无论是否由于达到目标或预算停止，均记录本批事实）
                new_evals = report["evaluations"][eval_start_count:]
                if new_evals:
                    batch_hits = sum(1 for e in new_evals if e.get("evaluation", {}).get("match") in ("strong", "partial"))
                    current["batch_index"] = current.get("batch_index", 0) + 1
                    if batch_hits > 0:
                        current["consecutive_zero_batches"] = 0
                    else:
                        current["consecutive_zero_batches"] = current.get("consecutive_zero_batches", 0) + 1
                    state.save()

                if reason not in ("candidates_exhausted", "material_failed"):
                    return reason

                # 3. 检查主动反思：若启用主动反思且连续 2 批无有效产出，且未达轮次上限
                if (enable_active_reflection and
                    current.get("consecutive_zero_batches", 0) >= CONSECUTIVE_ZERO_BATCHES_FOR_REFLECTION and
                    search["current_round"] < report["parameters"]["max_rounds"] and
                    not current.get("reflection_queries")):
                    log(f"[主动反思] 连续 {current['consecutive_zero_batches']} 批评估无有效推荐，主动触发反思拓词...")
                    queries, reflect_reason = _reflect(state, current, cfg, transport, api_key, sleep, log)
                    if reflect_reason:
                        return reflect_reason
                    if queries:
                        current["consecutive_zero_batches"] = 0
                        current["active_reflections"] = current.get("active_reflections", 0) + 1
                        search_reason = _search_pages(state, current, queries, search_fn, sleep, log)
                        if search_reason:
                            return search_reason

                # 4. 批次间隙穿插展开待展开仓库
                if any(r["state"] == "pending" for r in search["repository_queue"]):
                    reason = _expand_pending(state, current, expand, owned_ids, sleep, log, max_repos=EXPAND_INTERLEAVE_REPOS)
                    if reason:
                        return reason
                continue

            break
        evaluated = report["evaluations"][current["evaluation_start"]:]
        current["evaluated"] = len(evaluated)
        current["all_none"] = bool(evaluated) and all(e["evaluation"].get("match") == "none" for e in evaluated)
        current["phase"] = "complete"
        state.save()
        if search["repository_queue"] and all(r["state"] == "failed" for r in search["repository_queue"]):
            return "expansion_failed"
        if not report["evaluations"] and not report["evaluation_attempts"] and any(
                s.get("code") == "material_failed" for s in search["skipped"]):
            return "material_failed"
        if search["current_round"] >= report["parameters"]["max_rounds"]:
            return "round_limit"
        log(f"[补水触发] 当前短名单未达 {report['parameters']['limit']} 个，继续下一轮检索。")
