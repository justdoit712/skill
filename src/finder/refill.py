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


def initialize_search(state):
    report = state.report
    search = report["search"]
    defaults = {"current_round": 0, "rounds_history": [], "query_cursors": {},
                "repository_queue": [], "discovered_repo_urls": [], "candidate_queue": [],
                "discovered_skill_ids": [], "processed_skill_ids": [], "consecutive_failures": 0}
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
            # Persist every discovered repository, including those outside the next expansion batch.
            new = []
            for repo in repos:
                key = f"{repo['owner'].lower()}/{repo['repo'].lower()}"
                if key not in seen:
                    seen.add(key)
                    new.append(dict(repo, url=repo.get("url") or f"https://github.com/{key}",
                                    key=key, state="pending", query=query, round=current["round"]))
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


def _expand_pending(state, current, expand, owned_ids, sleep, log):
    search = state.report["search"]
    groups = {}
    for repo in search["repository_queue"]:
        if repo["state"] == "pending":
            groups.setdefault(repo.get("query", ""), []).append(repo)
    pending = _round_robin_merge_repos(list(groups.values()), max_repos=20)
    seen = set(search["discovered_skill_ids"])
    keywords = set(re.findall(r"[\w]+", state.report["topic"].lower()))
    for repo in pending:
        reason = state.stop_reason()
        if reason:
            return reason
        candidates, expansions = expand([repo], keywords=keywords, max_files_per_repo=None, sleep=sleep, log=log)
        failed = bool(expansions) and all(not e.get("ok", False) for e in expansions)
        repo["state"] = "failed" if failed else "expanded"
        search["expansions"].extend(expansions)
        state.report["coverage_incomplete"] |= any(not e.get("ok", False) or e.get("truncated") for e in expansions)
        for candidate in candidates:
            if candidate.skill_id in seen:
                continue
            seen.add(candidate.skill_id)
            current["candidates"] += 1
            if is_skill_owned(candidate.skill_id, owned_ids):
                search["skipped_owned_ids"].append(candidate.skill_id)
                search["skipped"].append({"skill_id": candidate.skill_id, "code": "owned"})
            else:
                search["candidate_queue"].append(asdict(candidate))
        search["discovered_skill_ids"] = sorted(seen)
        search["candidates_found"] = len(seen)
        search["skipped_owned"] = len(search["skipped_owned_ids"])
        state.save()
    return None


def run_rounds(state, cfg, api_key, transport, search_fn, expand, fetch, evaluate, owned_ids, sleep, log):
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
                       "feedback_start": previous.get("evaluation_start", 0)}
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
        # Consume all saved candidates/repositories before spending on another search round.
        while True:
            reason = state.stop_reason()
            if reason:
                return reason
            done = set(search["processed_skill_ids"])
            queue = [Candidate(**c) for c in search["candidate_queue"] if c["skill_id"] not in done]
            if queue:
                queue = schedule_candidates_fairly(queue, max_total=None, max_per_repo=None)
                reason = evaluate(state, queue, cfg, api_key, transport, fetch, sleep, log=log)
                if reason not in ("candidates_exhausted", "material_failed"):
                    return reason
                continue
            if any(r["state"] == "pending" for r in search["repository_queue"]):
                reason = _expand_pending(state, current, expand, owned_ids, sleep, log)
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
