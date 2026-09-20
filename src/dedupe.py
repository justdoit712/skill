"""稳定 ID、内容指纹与去重。

依据 §4.2：多个来源指向同一技能时去重，保留全部发现依据。
依据 §7.3：评估 ID 包含技能稳定 ID、内容指纹、规则版本与模型配置版本。

稳定 ID 由规范位置推导，不依赖发现顺序或来源，因此同一技能无论从哪条渠道
被找到，都会得到同一个 ID。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .models import Candidate

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_BLOB_KINDS = {"blob", "tree", "raw"}


def parse_github_url(url: str) -> tuple[str, str, str, str]:
    """拆解 GitHub URL。

    返回 (owner, repo, path, kind)：
    - kind 为 repo（仓库根）、blob/tree（仓库内文件或目录）、raw（raw 域名）或 unknown
    - owner 与 repo 统一小写，GitHub 对这两段大小写不敏感
    - 无法识别时返回空串，不抛异常
    """
    if not url:
        return "", "", "", "unknown"

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    segments = [s for s in parsed.path.split("/") if s]

    if host == "raw.githubusercontent.com":
        if len(segments) < 3:
            return "", "", "", "unknown"
        owner, repo = segments[0].lower(), segments[1].lower()
        return owner, repo, "/".join(segments[3:]), "raw"

    if host not in _GITHUB_HOSTS:
        return "", "", "", "unknown"

    if len(segments) < 2:
        return "", "", "", "unknown"

    owner, repo = segments[0].lower(), segments[1].lower()
    repo = re.sub(r"\.git$", "", repo)

    if len(segments) == 2:
        return owner, repo, "", "repo"

    kind = segments[2].lower()
    if kind in _BLOB_KINDS:
        # blob/<ref>/<path> 与 tree/<ref>/<path>，去掉 ref 段
        rest = segments[4:] if len(segments) > 4 else []
        return owner, repo, "/".join(rest), kind

    return owner, repo, "", "unknown"


def make_skill_id(owner: str, repo: str, path: str = "") -> str:
    """技能稳定 ID：仓库级为 owner/repo，路径级为 owner/repo:path。"""
    if not owner or not repo:
        raise ValueError("owner 与 repo 不能为空")
    base = f"{owner.lower()}/{repo.lower()}"
    cleaned = path.strip("/")
    return f"{base}:{cleaned}" if cleaned else base


def content_fingerprint(text: str | None) -> str | None:
    """内容指纹，用于判断技能内容是否真的变化（§7.2 与 §5.2 的待复核规则）。"""
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def candidate_from_repo(
    owner: str,
    repo: str,
    *,
    path: str = "",
    url: str = "",
    repo_url: str = "",
    name: str = "",
    description: str = "",
    source_id: str = "",
    discovery_method: str = "",
    search_term: str = "",
    discovered_at: str = "",
) -> Candidate:
    """由仓库信息构造候选，稳定 ID 与发现依据一并填好。"""
    return Candidate(
        skill_id=make_skill_id(owner, repo, path),
        owner=owner.lower(),
        repo=repo.lower(),
        path=path.strip("/"),
        url=url,
        repo_url=repo_url or (f"https://github.com/{owner}/{repo}" if owner and repo else ""),
        name=name or repo,
        description=description,
        source_ids=[source_id] if source_id else [],
        discovery_methods=[discovery_method] if discovery_method else [],
        search_terms=[search_term] if search_term else [],
        discovered_at=discovered_at,
    )


def _merge_unique(target: list[str], extra: list[str]) -> None:
    for item in extra:
        if item and item not in target:
            target.append(item)


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """按稳定 ID 合并候选，保留全部发现依据。

    合并规则：首次出现的条目为主，补全其空缺字段，并累积来源、发现方式、查询词
    与领域线索。不丢弃任何来源信息。
    """
    merged: dict[str, Candidate] = {}
    order: list[str] = []

    for item in candidates:
        key = item.skill_id
        if key not in merged:
            merged[key] = item
            order.append(key)
            continue

        kept = merged[key]
        kept.description = kept.description or item.description
        kept.path = kept.path or item.path
        kept.url = kept.url or item.url
        kept.repo_url = kept.repo_url or item.repo_url
        kept.content_fingerprint = kept.content_fingerprint or item.content_fingerprint
        if not kept.discovered_at or (item.discovered_at and item.discovered_at < kept.discovered_at):
            kept.discovered_at = item.discovered_at or kept.discovered_at
        _merge_unique(kept.source_ids, item.source_ids)
        _merge_unique(kept.discovery_methods, item.discovery_methods)
        _merge_unique(kept.search_terms, item.search_terms)
        _merge_unique(kept.domain_hints, item.domain_hints)

    return [merged[k] for k in order]
