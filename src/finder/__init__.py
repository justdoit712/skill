"""定向查找业务包 (Finder Package)。

提供基于自然语言需求意图驱动的 GitHub Agent Skill 检索、评估、证据客观核验与安全报告投影。
本包物理隔离于主目录逻辑，绝对不导入任何 catalog 模块，零目录副作用。
"""

from __future__ import annotations

from .config import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_EVALUATIONS,
    DEFAULT_MAX_TOKENS,
    _parse_int_val,
    load_finder_model_config,
    load_finder_run_config,
    validate_finder_parameters,
)
from .evaluation import (
    DOC_CLEAR,
    DOC_INSUFFICIENT,
    DOC_PARTIAL,
    KIND_QUALITY_SIGNAL,
    KIND_REQUIRED,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_STRONG,
    STATUS_SUPPORTED,
    STATUS_UNKNOWN,
    STATUS_UNSUPPORTED,
    build_evaluation_prompt,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from .evidence import (
    EvidenceVerificationResult,
    verify_evidence_snippet,
    verify_single_evidence,
)
from .plan import (
    MAX_CRITERIA_COUNT,
    MAX_PLAN_QUERIES,
    PLAN_MAX_OUTPUT_TOKENS,
    build_plan_prompt,
    parse_query_plan,
)
from .report import (
    build_public_find_projection,
    render_find_markdown_report,
    sanitize_report_for_public,
    should_update_public_snapshot,
    update_public_snapshot,
    write_local_report,
)
from .run import (
    STATUS_CANDIDATES_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_EVALUATION_LIMIT,
    STATUS_INTERRUPTED,
    STATUS_INVALID_CONFIG,
    STATUS_MODEL_FAILURES,
    STATUS_STOPPED,
    STATUS_TARGET_REACHED,
    STATUS_TOKEN_LIMIT,
    STATUS_USAGE_UNKNOWN,
    FinderRunState,
    execute_find_skill,
    finalize_run,
    main,
)
from .search import (
    MAX_FILES_PER_REPO,
    MAX_PRIMARY_FILE_BYTES,
    MAX_REFERENCED_FILES,
    MAX_REPOS_TO_EXPAND,
    MAX_SEARCH_REPOS_PER_QUERY,
    MAX_TOTAL_FILES_TO_FETCH,
    MAX_TOTAL_MATERIAL_BYTES,
    _interleave_paths,
    _round_robin_merge_repos,
    expand_and_collect_candidates,
    fetch_candidate_materials,
    schedule_candidates_fairly,
    search_github_repos_for_query,
)

__all__ = [
    # config
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_EVALUATIONS",
    "DEFAULT_MAX_TOKENS",
    "load_finder_model_config",
    "load_finder_run_config",
    "validate_finder_parameters",
    "_parse_int_val",
    # evidence
    "EvidenceVerificationResult",
    "verify_evidence_snippet",
    "verify_single_evidence",
    # plan
    "MAX_CRITERIA_COUNT",
    "MAX_PLAN_QUERIES",
    "PLAN_MAX_OUTPUT_TOKENS",
    "build_plan_prompt",
    "parse_query_plan",
    # evaluation
    "DOC_CLEAR",
    "DOC_INSUFFICIENT",
    "DOC_PARTIAL",
    "KIND_QUALITY_SIGNAL",
    "KIND_REQUIRED",
    "MATCH_NONE",
    "MATCH_PARTIAL",
    "MATCH_STRONG",
    "STATUS_SUPPORTED",
    "STATUS_UNKNOWN",
    "STATUS_UNSUPPORTED",
    "build_evaluation_prompt",
    "parse_skill_evaluation",
    "verify_and_adjust_evaluation",
    "rank_find_results",
    # search
    "MAX_FILES_PER_REPO",
    "MAX_PRIMARY_FILE_BYTES",
    "MAX_REFERENCED_FILES",
    "MAX_REPOS_TO_EXPAND",
    "MAX_SEARCH_REPOS_PER_QUERY",
    "MAX_TOTAL_FILES_TO_FETCH",
    "MAX_TOTAL_MATERIAL_BYTES",
    "_interleave_paths",
    "_round_robin_merge_repos",
    "expand_and_collect_candidates",
    "fetch_candidate_materials",
    "schedule_candidates_fairly",
    "search_github_repos_for_query",
    # report
    "build_public_find_projection",
    "render_find_markdown_report",
    "sanitize_report_for_public",
    "should_update_public_snapshot",
    "update_public_snapshot",
    "write_local_report",
    # run
    "FinderRunState",
    "execute_find_skill",
    "finalize_run",
    "main",
    "STATUS_CANDIDATES_EXHAUSTED",
    "STATUS_COMPLETED",
    "STATUS_ERROR",
    "STATUS_EVALUATION_LIMIT",
    "STATUS_INTERRUPTED",
    "STATUS_INVALID_CONFIG",
    "STATUS_MODEL_FAILURES",
    "STATUS_STOPPED",
    "STATUS_TARGET_REACHED",
    "STATUS_TOKEN_LIMIT",
    "STATUS_USAGE_UNKNOWN",
]
