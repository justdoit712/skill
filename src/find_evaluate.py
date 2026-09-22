"""定向查找评估与核对层向后兼容门面 (Facade)。

所有实现已重构并下沉至 src/finder/：
- 规划逻辑 -> src.finder.plan
- 证据核验 -> src.finder.evidence
- 评估与排序 -> src.finder.evaluation
"""

from __future__ import annotations

from src.finder.evaluation import (
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
    UNTRUSTED_NOTICE,
    VALID_CRITERION_KINDS,
    VALID_DOC_VALUES,
    VALID_MATCH_VALUES,
    VALID_STATUS_VALUES,
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
    MAX_CRITERIA_COUNT,
    MAX_PLAN_QUERIES,
    _normalize_space,
    _strip_fence,
    build_plan_prompt,
    parse_query_plan,
)

__all__ = [
    "MAX_PLAN_QUERIES",
    "MAX_CRITERIA_COUNT",
    "MATCH_STRONG",
    "MATCH_PARTIAL",
    "MATCH_NONE",
    "VALID_MATCH_VALUES",
    "DOC_CLEAR",
    "DOC_PARTIAL",
    "DOC_INSUFFICIENT",
    "VALID_DOC_VALUES",
    "STATUS_SUPPORTED",
    "STATUS_UNSUPPORTED",
    "STATUS_UNKNOWN",
    "VALID_STATUS_VALUES",
    "KIND_REQUIRED",
    "KIND_QUALITY_SIGNAL",
    "VALID_CRITERION_KINDS",
    "UNTRUSTED_NOTICE",
    "build_plan_prompt",
    "parse_query_plan",
    "build_evaluation_prompt",
    "parse_skill_evaluation",
    "verify_evidence_snippet",
    "verify_single_evidence",
    "EvidenceVerificationResult",
    "verify_and_adjust_evaluation",
    "rank_find_results",
    "_strip_fence",
    "_normalize_space",
]
