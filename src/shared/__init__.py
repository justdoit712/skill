"""共享纯数据结构、中性身份模型与通用运行时工具。

架构红线：
- 本包不依赖任何外部业务包（catalog、finder）
- 本包不依赖基础设施层（infra）
- 不加载业务配置，不读取模型凭据
"""

from __future__ import annotations

from .identity import content_fingerprint, dedupe_identities, make_skill_id, parse_github_url
from .materials import DocumentSnapshot, MaterialBundle, validate_document
from .models import CandidateIdentity
from .runtime import SHANGHAI_TZ, TZ_SOURCE, iso_now, now_local, week_id
from .schema import VALID_SKILL_TYPES, normalize_skill_type, normalize_string_list
from .usage import UsageTotals

__all__ = [
    "CandidateIdentity",
    "DocumentSnapshot",
    "MaterialBundle",
    "validate_document",
    "parse_github_url",
    "make_skill_id",
    "content_fingerprint",
    "dedupe_identities",
    "VALID_SKILL_TYPES",
    "normalize_skill_type",
    "normalize_string_list",
    "UsageTotals",
    "SHANGHAI_TZ",
    "TZ_SOURCE",
    "now_local",
    "week_id",
    "iso_now",
]
