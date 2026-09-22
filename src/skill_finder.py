"""定向查找技能核心编排器向后兼容门面 (Facade)。

所有业务实现已独立重构为顶层业务包 `src.finder`：
- 配置解析与校验 -> src.finder.config
- 搜索与材料抓取 -> src.finder.search
- 意图规划 -> src.finder.plan
- 评估与排序 -> src.finder.evaluation
- 纯证据核验 -> src.finder.evidence
- 报告与脱敏投影 -> src.finder.report
- 运行生命周期编排 -> src.finder.run

保留本门面以保证已有测试用例的 import 与 mock/patch 目标无缝兼容。
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Any

from src.infra.llm import call_model, resolve_api_key

from src.finder.config import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_EVALUATIONS,
    DEFAULT_MAX_TOKENS,
    _parse_int_val,
    load_finder_model_config,
    load_finder_run_config,
    validate_finder_parameters,
)
from src.finder.evaluation import (
    EVAL_MAX_OUTPUT_TOKENS,
    build_evaluation_prompt,
    parse_skill_evaluation,
    rank_find_results,
    verify_and_adjust_evaluation,
)
from src.finder.evidence import (
    EvidenceVerificationResult,
    verify_evidence_snippet,
    verify_single_evidence,
)
from src.finder.plan import (
    PLAN_MAX_OUTPUT_TOKENS,
    build_plan_prompt,
    parse_query_plan,
)
from src.finder.report import (
    build_public_find_projection,
    render_find_markdown_report,
    sanitize_report_for_public,
    should_update_public_snapshot,
    update_public_snapshot,
    write_local_report,
)
from src.finder.run import (
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
    finalize_run,
    main as _finder_main,
    execute_find_skill as _finder_execute_find_skill,
)
from src.finder.search import (
    MAX_FILES_PER_REPO,
    MAX_PRIMARY_FILE_BYTES,
    MAX_REFERENCED_FILES,
    MAX_REPOS_TO_EXPAND,
    MAX_SEARCH_REPOS_PER_QUERY,
    MAX_TOTAL_FILES_TO_FETCH,
    MAX_TOTAL_MATERIAL_BYTES,
    _interleave_paths,
    _is_html_content,
    _round_robin_merge_repos,
    expand_and_collect_candidates,
    extract_referenced_md_paths,
    fetch_candidate_materials,
    schedule_candidates_fairly,
    search_github_repos_for_query,
)


def execute_find_skill(
    topic: str,
    *,
    limit: int | None = None,
    max_evaluations: int | None = None,
    max_tokens: int | None = None,
    root_dir: str | Path = ".",
    model_cfg: dict[str, Any] | None = None,
    log=print,
    sleep=time.sleep,
    **kwargs,
) -> dict[str, Any]:
    """向后兼容的 execute_find_skill，动态获取本模块可能被 patch 的依赖函数。"""
    this_mod = sys.modules.get(__name__)
    call_model_target = getattr(this_mod, "call_model", call_model)
    fetch_target = getattr(this_mod, "fetch_candidate_materials", fetch_candidate_materials)
    expand_target = getattr(this_mod, "expand_and_collect_candidates", expand_and_collect_candidates)
    search_target = getattr(this_mod, "search_github_repos_for_query", search_github_repos_for_query)

    kwargs.setdefault("call_model_fn", call_model_target)
    kwargs.setdefault("fetch_candidate_materials_fn", fetch_target)
    kwargs.setdefault("expand_and_collect_candidates_fn", expand_target)
    kwargs.setdefault("search_github_repos_fn", search_target)

    return _finder_execute_find_skill(
        topic,
        limit=limit,
        max_evaluations=max_evaluations,
        max_tokens=max_tokens,
        root_dir=root_dir,
        model_cfg=model_cfg,
        log=log,
        sleep=sleep,
        **kwargs,
    )


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    return _finder_main(argv, root=root)


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_EVALUATIONS",
    "DEFAULT_MAX_TOKENS",
    "MAX_REPOS_TO_EXPAND",
    "MAX_SEARCH_REPOS_PER_QUERY",
    "MAX_FILES_PER_REPO",
    "MAX_TOTAL_FILES_TO_FETCH",
    "MAX_PRIMARY_FILE_BYTES",
    "MAX_TOTAL_MATERIAL_BYTES",
    "MAX_REFERENCED_FILES",
    "PLAN_MAX_OUTPUT_TOKENS",
    "EVAL_MAX_OUTPUT_TOKENS",
    "STATUS_TARGET_REACHED",
    "STATUS_COMPLETED",
    "STATUS_TOKEN_LIMIT",
    "STATUS_EVALUATION_LIMIT",
    "STATUS_CANDIDATES_EXHAUSTED",
    "STATUS_MODEL_FAILURES",
    "STATUS_USAGE_UNKNOWN",
    "STATUS_INTERRUPTED",
    "STATUS_ERROR",
    "STATUS_STOPPED",
    "STATUS_INVALID_CONFIG",
    "call_model",
    "resolve_api_key",
    "search_github_repos_for_query",
    "_round_robin_merge_repos",
    "_interleave_paths",
    "expand_and_collect_candidates",
    "schedule_candidates_fairly",
    "extract_referenced_md_paths",
    "_is_html_content",
    "fetch_candidate_materials",
    "_parse_int_val",
    "load_finder_model_config",
    "load_finder_run_config",
    "validate_finder_parameters",
    "finalize_run",
    "execute_find_skill",
    "render_find_markdown_report",
    "build_public_find_projection",
    "sanitize_report_for_public",
    "should_update_public_snapshot",
    "update_public_snapshot",
    "write_local_report",
    "FinderRunState",
    "main",
]
