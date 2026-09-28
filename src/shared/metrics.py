"""系统统计口径与指标事实模型（落实 P1 设计规范）。

必须严格区分事实：
1. 搜索：实际请求、重试、去重后新增仓库、新增 Skill、有效材料、合格结果；
2. 模型：请求阶段、尝试次数、完成评估数、已知 Token、未知用量、格式失败、截断；
3. 缓存：精确命中、规范化潜在命中、实际复用、拒绝原因；
4. 静态规则：规则版本、命中信号、建议动作、实际跳过及抽样误拦截。

严格保留分子与分母；分母为零标记不可计算；历史缺失字段标记未知，不隐式补零。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RatioMetric:
    """带分子、分母和可解释状态的比例指标。"""
    numerator: Optional[int]
    denominator: Optional[int]
    value: Optional[float]
    status: str  # "calculated" | "undefined_zero_denominator" | "unknown"
    display: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": self.value,
            "status": self.status,
            "display": self.display,
        }


def calc_ratio(numerator: Optional[int], denominator: Optional[int], *, unit: str = "%") -> RatioMetric:
    """安全计算比例指标。保留分子分母，分母为 0 标记不可计算，缺失标记未知。"""
    if numerator is None or denominator is None:
        return RatioMetric(
            numerator=numerator,
            denominator=denominator,
            value=None,
            status="unknown",
            display="未知",
        )
    if denominator == 0:
        return RatioMetric(
            numerator=numerator,
            denominator=0,
            value=None,
            status="undefined_zero_denominator",
            display="不可计算 (分母为0)",
        )
    rate = (numerator / denominator) * 100.0 if unit == "%" else (numerator / denominator)
    disp = f"{numerator}/{denominator} ({rate:.1f}{unit})"
    return RatioMetric(
        numerator=numerator,
        denominator=denominator,
        value=rate,
        status="calculated",
        display=disp,
    )


@dataclass
class SearchMetricFacts:
    """搜索事实统计。"""
    http_requests: int = 0
    retries: int = 0
    repos_discovered_raw: int = 0
    repos_deduped: int = 0
    skills_discovered_raw: int = 0
    skills_deduped: int = 0
    valid_materials: int = 0
    shortlist_count: int = 0
    alternatives_count: int = 0

    def deduplication_ratio(self) -> RatioMetric:
        """去重率：去重后仓库数 / 发现的原始仓库数"""
        return calc_ratio(self.repos_deduped, self.repos_discovered_raw)

    def conversion_ratio(self) -> RatioMetric:
        """转化合格率：入选短名单技能数 / 去重后发现技能数"""
        return calc_ratio(self.shortlist_count, self.skills_deduped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "http_requests": self.http_requests,
            "retries": self.retries,
            "repos_discovered_raw": self.repos_discovered_raw,
            "repos_deduped": self.repos_deduped,
            "skills_discovered_raw": self.skills_discovered_raw,
            "skills_deduped": self.skills_deduped,
            "valid_materials": self.valid_materials,
            "shortlist_count": self.shortlist_count,
            "alternatives_count": self.alternatives_count,
            "conversion_ratio": self.conversion_ratio().to_dict(),
        }


@dataclass
class ModelMetricFacts:
    """模型调用事实统计。"""
    stage_attempts: dict[str, int] = field(default_factory=dict)
    total_attempts: int = 0
    completed_evaluations: int = 0
    known_prompt_tokens: Optional[int] = 0
    known_completion_tokens: Optional[int] = 0
    known_total_tokens: Optional[int] = 0
    unknown_usage_requests: int = 0
    format_failures: int = 0
    length_exceeded_count: int = 0

    def evaluation_success_ratio(self) -> RatioMetric:
        """评估成功率：完成评估数 / 总尝试次数"""
        return calc_ratio(self.completed_evaluations, self.total_attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_attempts": dict(self.stage_attempts),
            "total_attempts": self.total_attempts,
            "completed_evaluations": self.completed_evaluations,
            "known_prompt_tokens": self.known_prompt_tokens,
            "known_completion_tokens": self.known_completion_tokens,
            "known_total_tokens": self.known_total_tokens,
            "unknown_usage_requests": self.unknown_usage_requests,
            "format_failures": self.format_failures,
            "length_exceeded_count": self.length_exceeded_count,
            "success_ratio": self.evaluation_success_ratio().to_dict(),
        }


@dataclass
class CacheMetricFacts:
    """缓存复用事实统计。"""
    exact_hits: int = 0
    normalized_potential_hits: int = 0
    actual_reused: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def effective_reuse_ratio(self, total_candidates: Optional[int]) -> RatioMetric:
        """有效复用率：实际复用数 / 总候选数"""
        return calc_ratio(self.actual_reused, total_candidates)

    def to_dict(self, total_candidates: Optional[int] = None) -> dict[str, Any]:
        return {
            "exact_hits": self.exact_hits,
            "normalized_potential_hits": self.normalized_potential_hits,
            "actual_reused": self.actual_reused,
            "rejection_reasons": dict(self.rejection_reasons),
            "reuse_ratio": self.effective_reuse_ratio(total_candidates).to_dict(),
        }


@dataclass
class PrescreenMetricFacts:
    """静态规则与预筛事实统计。"""
    rules_version: str = "1.0.0"
    signal_hits: dict[str, int] = field(default_factory=dict)
    recommended_actions: dict[str, int] = field(default_factory=dict)
    skipped_count: int = 0
    sample_audit_false_positives: int = 0
    sample_audit_count: Optional[int] = None

    def false_positive_ratio(self) -> RatioMetric:
        """抽样误拦截率：误拦截数 / 抽样核查总跳过数"""
        return calc_ratio(self.sample_audit_false_positives, self.sample_audit_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_version": self.rules_version,
            "signal_hits": dict(self.signal_hits),
            "recommended_actions": dict(self.recommended_actions),
            "skipped_count": self.skipped_count,
            "sample_audit_false_positives": self.sample_audit_false_positives,
            "sample_audit_count": self.sample_audit_count,
            "false_positive_ratio": self.false_positive_ratio().to_dict(),
        }


__all__ = [
    "RatioMetric",
    "calc_ratio",
    "SearchMetricFacts",
    "ModelMetricFacts",
    "CacheMetricFacts",
    "PrescreenMetricFacts",
]


def build_run_metrics(report: dict, *, kind: str) -> dict:
    """Recompute derived metrics from saved facts; absent facts remain unknown.

    This function neither increments counters nor trusts a previous metrics object.
    Only numeric fields and fixed stage names are emitted, including for public reports.
    """
    def number(value):
        return value if type(value) is int and value >= 0 else None

    usage = report.get("usage") or {}
    calls = report.get("calls")
    stages = None
    if isinstance(calls, list):
        stages = {}
        for call in calls:
            if call.get("state") == "not_sent":
                continue
            stage = call.get("stage")
            if stage not in {"planning", "clarification", "reflection", "evaluation", "initial", "review"}:
                stage = "unspecified"
            count = number((call.get("usage") or {}).get("attempts"))
            prior = stages.get(stage, 0)
            stages[stage] = prior + count if prior is not None and count is not None else None
    completed = number(report.get("evaluated_count")) if kind == "finder" else None
    if kind == "catalog" and isinstance(calls, list):
        # Only terminal outcomes carry a decision; an initial response is not a
        # completed evaluation when an independent review is still pending.
        completed = len({c.get("skill_id") for c in calls if c.get("decision") and c.get("skill_id")})
    model = {
        "requests": number(usage.get("requests")), "stage_requests": stages,
        "evaluation_attempts": number(report.get("evaluation_attempts" if kind == "finder" else "evaluations")),
        "completed_evaluations": completed,
        "known_prompt_tokens": number(usage.get("prompt_tokens")),
        "known_completion_tokens": number(usage.get("completion_tokens")),
        "known_total_tokens": number(usage.get("total_tokens")),
        "unknown_usage_requests": number(usage.get("unknown_usage_requests")),
        "format_failures": number(report.get("skipped_output_format")),
        "length_exceeded_count": number(report.get("skipped_length_exceeded")),
    }
    search = report.get("search") or {}
    queries = search.get("queries_executed")
    requests = retries = raw_repos = None
    if isinstance(queries, list):
        if all("attempt" in q and q.get("error") != "request_interrupted" for q in queries):
            requests = len(queries)
        if all(number(q.get("attempt")) is not None for q in queries):
            retries = sum(q["attempt"] > 1 for q in queries)
        if all(number(q.get("repos_returned")) is not None for q in queries):
            raw_repos = sum(q["repos_returned"] for q in queries)
    shortlist = number(report.get("shortlist_count"))
    skills = number(search.get("candidates_found"))
    cache_obs = report.get("cache_observation") or {}
    cache_data = {
        "exact_hits": number(report.get("cached")),
        "normalized_potential_hits": number(cache_obs.get("potential_hits")) if "potential_hits" in cache_obs else None,
        "actual_reused": number(cache_obs.get("actual_reused")) if "actual_reused" in cache_obs else None,
    }
    if "rejection_reasons" in cache_obs:
        cache_data["rejection_reasons"] = dict(cache_obs["rejection_reasons"])

    return {
        "metrics_version": "1.0.0",
        "search": {"http_requests": requests, "retries": retries,
                   "repos_discovered_raw": raw_repos,
                   "repos_deduped": number(search.get("repos_discovered")),
                   "skills_deduped": skills, "shortlist_count": shortlist,
                   "conversion_ratio": calc_ratio(shortlist, skills).to_dict()},
        "model": model,
        "cache": cache_data,
        "prescreen": {
            "rules_version": (report.get("static_heuristics") or {}).get("version") or "1.0.0",
            "signal_hits": (report.get("static_heuristics") or {}).get("signal_counts", {}),
            "recommended_actions": (report.get("static_heuristics") or {}).get("suggested_actions", {}),
            "skipped_count": (number(report.get("prescreen_excluded")) or 0) + (number(report.get("static_skipped")) or 0),
            "sample_audit_count": None,
            "sample_audit_false_positives": None,
            "false_positive_ratio": calc_ratio(None, None).to_dict(),
        },
    }
