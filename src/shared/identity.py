"""稳定 ID、URL 规范解析与内容指纹。

提供中性身份推导原语，不依赖任何上层业务规则与分类。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .models import CandidateIdentity

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
    """内容指纹，用于判断技能内容是否真的变化。"""
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def dedupe_identities(identities: list[CandidateIdentity]) -> list[CandidateIdentity]:
    """按稳定 ID 合并中性候选身份，首次出现的条目为主。"""
    merged: dict[str, CandidateIdentity] = {}
    order: list[str] = []

    for item in identities:
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

    return [merged[k] for k in order]
