"""外部系统通信与底层基础设施层。

统一封装外部通信（HTTP、GitHub、LLM）与系统文件读写，
保留既有调用签名与目录重试语义，屏蔽第三方 API 与操作系统差异。

架构红线：
- 绝对禁止反向依赖业务包（catalog、finder）
- 仅依赖标准库、第三方库与 shared 纯数据包
"""

from __future__ import annotations

from .files import read_json, write_json_atomic
from .github import apply_github_auth, list_skill_paths, search_repositories
from .http import FetchResult, fetch_text
from .llm import ModelCallResult, api_key_source, call_model, resolve_api_key

__all__ = [
    "write_json_atomic",
    "read_json",
    "fetch_text",
    "FetchResult",
    "apply_github_auth",
    "list_skill_paths",
    "search_repositories",
    "call_model",
    "resolve_api_key",
    "api_key_source",
    "ModelCallResult",
]
