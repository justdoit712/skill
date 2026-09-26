"""GitHub API 基础设施：认证注入、仓库搜索与技能路径展开。

带统一有界指数退避重试，屏蔽底层网络与 GitHub API 细节。
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

GITHUB_SEARCH_ENDPOINT = "https://api.github.com/search/repositories"
GITHUB_TREE_ENDPOINT = "https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}"
GITHUB_REPO_ENDPOINT = "https://api.github.com/repos/{owner}/{repo}"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_PER_PAGE = 30
RETRYABLE_STATUS = frozenset({403, 408, 429, 500, 502, 503, 504})
USER_AGENT = "skills-navigator/0.1 (+https://github.com/justdoit712/skill)"
SKILL_FILENAME = "SKILL.md"


def _backoff_seconds(attempt: int, cap: float = 8.0) -> float:
    return min(2.0 ** (attempt - 1), cap)


def apply_github_auth(session: requests.Session) -> bool:
    """若环境变量中存在 GITHUB_TOKEN 则注入 Bearer 认证头。"""
    token = (os.environ.get(GITHUB_TOKEN_ENV) or "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
        return True
    return False


def _api_get(
    url: str,
    *,
    session: requests.Session,
    timeout: float,
    max_attempts: int,
    sleep=time.sleep,
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


def list_skill_paths(
    owner: str,
    repo: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep=time.sleep,
    filename: str = SKILL_FILENAME,
) -> tuple[list[str], str | None]:
    """列出指定仓库中所有匹配 filename（默认 SKILL.md）的相对路径。

    返回 (路径列表, 错误信息)。若无匹配文件且无网络错误则返回 ([], None)。
    """
    owns = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    sess.headers.setdefault("Accept", "application/vnd.github+json")
    apply_github_auth(sess)
    try:
        ok, repo_info, error = _api_get(
            GITHUB_REPO_ENDPOINT.format(owner=owner, repo=repo),
            session=sess,
            timeout=timeout,
            max_attempts=max_attempts,
            sleep=sleep,
        )
        if not ok:
            return [], error
        branch = (repo_info or {}).get("default_branch") or "HEAD"

        ok, tree, error = _api_get(
            GITHUB_TREE_ENDPOINT.format(owner=owner, repo=repo, ref=branch) + "?recursive=1",
            session=sess,
            timeout=timeout,
            max_attempts=max_attempts,
            sleep=sleep,
        )
        if not ok:
            return [], error

        is_truncated = bool((tree or {}).get("truncated"))
        paths = [
            item.get("path", "")
            for item in (tree or {}).get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").split("/")[-1] == filename
        ]
        if is_truncated:
            return sorted(paths), "TREE_TRUNCATED: GitHub API 返回的文件树已被截断，展开结果可能不完整"
        return sorted(paths), None
    finally:
        if owns:
            sess.close()


expand_repo_skills = list_skill_paths


def search_repositories(
    query_str: str,
    *,
    session: requests.Session | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    page: int = 1,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep=time.sleep,
) -> tuple[bool, list[dict], int, str | None]:
    """执行 GitHub 仓库搜索。返回 (ok, items, total_count, error)。"""
    owns = session is None
    sess = session if session is not None else requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    sess.headers.setdefault("Accept", "application/vnd.github+json")
    apply_github_auth(sess)

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                response = sess.get(
                    GITHUB_SEARCH_ENDPOINT,
                    params={"q": query_str, "per_page": per_page, "page": page},
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                if attempt < max_attempts:
                    sleep(_backoff_seconds(attempt))
                    continue
                return False, [], 0, f"{type(exc).__name__}: {exc}"

            try:
                status = response.status_code
                if status >= 400:
                    if status in RETRYABLE_STATUS and attempt < max_attempts:
                        response.close()
                        sleep(_backoff_seconds(attempt))
                        continue
                    return False, [], 0, f"HTTP {status}"

                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    return False, [], 0, "INVALID_JSON"
                if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                    return False, [], 0, "INVALID_RESPONSE"
                items = payload.get("items") or []
                total = int(payload.get("total_count") or 0)
                return True, items, total, None
            finally:
                response.close()
    finally:
        if owns:
            sess.close()

    return False, [], 0, "重试次数耗尽"
