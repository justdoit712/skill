"""中性候选身份数据模型。

无任何上层业务属性（如 domain_hints、source_ids、discovery_methods 等）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CandidateIdentity:
    """中性候选身份，不含目录业务属性。"""

    skill_id: str
    owner: str
    repo: str
    path: str = ""
    url: str = ""
    repo_url: str = ""
    name: str = ""
    description: str = ""
    discovered_at: str = ""
    content_fingerprint: str | None = None
