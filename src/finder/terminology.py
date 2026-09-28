"""技术术语表与查询规范化纯函数模块。

实现初始、交互与反思查询的统一规范化，并提供带版本的小型技术术语表与覆盖缺口观察。
纯函数设计，不执行网络请求与磁盘 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from src.shared.versions import TERMINOLOGY_VERSION

# GitHub 搜索操作符正则表达式
SEARCH_OPERATOR_PATTERN = r"\b(?:in|repo|org|user|language|stars|forks|site|is|topic):[^\s]*"


def normalize_query(query: str) -> str:
    """统一的查询空白规范化：折叠连续空白字符并去除首尾空白。"""
    if not isinstance(query, str):
        return ""
    return re.sub(r"\s+", " ", query).strip()


def has_search_operator(query: str) -> bool:
    """检查查询短语中是否包含 GitHub 搜索操作符。"""
    if not isinstance(query, str):
        return False
    return bool(re.search(SEARCH_OPERATOR_PATTERN, query, re.IGNORECASE))


def strip_search_operators(query: str) -> str:
    """剔除查询短语中的 GitHub 搜索操作符并重新规范化空白。"""
    if not isinstance(query, str):
        return ""
    cleaned = re.sub(SEARCH_OPERATOR_PATTERN, "", query, flags=re.IGNORECASE)
    return normalize_query(cleaned)


def deduplicate_queries(
    queries: Iterable[str],
    previous: Iterable[str] | None = None,
) -> list[str]:
    """大小写无关去重，保留首次出现的原始大小写与顺序。"""
    seen: set[str] = set()
    if previous:
        for p in previous:
            clean_p = normalize_query(p)
            if clean_p:
                seen.add(clean_p.casefold())

    result: list[str] = []
    for q in queries:
        clean_q = normalize_query(q)
        if clean_q and clean_q.casefold() not in seen:
            seen.add(clean_q.casefold())
            result.append(clean_q)
    return result


@dataclass(frozen=True)
class TerminologyConcept:
    """术语概念定义。"""

    id: str
    name: str
    aliases: tuple[str, ...]
    boundary: str
    triggers: tuple[str, ...]


# 小型带版本技术术语表 (v1.0.0)
TERMINOLOGY_TABLE: dict[str, TerminologyConcept] = {
    "web_scraping": TerminologyConcept(
        id="web_scraping",
        name="网页爬取",
        aliases=("网页爬取", "网页抓取", "web scraping", "web crawler", "web scraper", "网络爬虫"),
        boundary="不默认增加登录、验证码或特定框架要求",
        triggers=("网页爬取", "网页抓取", "爬虫", "web scraping", "web crawler", "web scraper", "抓取网页"),
    ),
    "data_cleaning": TerminologyConcept(
        id="data_cleaning",
        name="数据清洗",
        aliases=("数据清洗", "data cleaning", "data cleansing", "数据整理", "data sanitization"),
        boundary="仅限结构化与半结构化数据清洗规范化",
        triggers=("数据清洗", "data cleaning", "data cleansing", "清洗数据"),
    ),
    "pdf_extraction": TerminologyConcept(
        id="pdf_extraction",
        name="PDF 提取",
        aliases=("PDF 提取", "pdf extraction", "pdf text extraction", "pdf parser", "提取 pdf"),
        boundary="不把文本提取自动等同于 OCR",
        triggers=("pdf 提取", "pdf extraction", "提取 pdf", "解析 pdf", "pdf parser", "pypdf"),
    ),
    "prompt_optimization": TerminologyConcept(
        id="prompt_optimization",
        name="提示词优化",
        aliases=("提示词优化", "prompt optimization", "prompt refinement", "prompt engineering", "优化提示词"),
        boundary="不把优化自动等同于生成或评测",
        triggers=("提示词优化", "prompt optimization", "prompt refinement", "prompt engineering", "优化 prompt"),
    ),
    "k8s_kubernetes": TerminologyConcept(
        id="k8s_kubernetes",
        name="k8s 运维",
        aliases=("k8s", "kubernetes", "k8s 管理", "kubernetes management", "k8s pod"),
        boundary="保留缩写与完整形式对应，不假定特定云厂商",
        triggers=("k8s", "kubernetes", "k8s pod", "k8s 运维", "pod 诊断"),
    ),
    "workflow_automation": TerminologyConcept(
        id="workflow_automation",
        name="工作流自动化",
        aliases=("工作流自动化", "workflow automation", "automation workflow", "工作流编排"),
        boundary="通用流程自动化编排",
        triggers=("工作流自动化", "workflow automation", "流程自动化", "自动化工作流"),
    ),
    "code_review": TerminologyConcept(
        id="code_review",
        name="代码审查",
        aliases=("代码审查", "code review", "code analysis", "静态代码分析"),
        boundary="不默认绑定特定语言或 linter 工具",
        triggers=("代码审查", "code review", "代码质检", "代码评审"),
    ),
    "api_testing": TerminologyConcept(
        id="api_testing",
        name="API 测试",
        aliases=("API 测试", "api testing", "rest api test", "接口测试"),
        boundary="接口自动化测试与校验",
        triggers=("api 测试", "api testing", "接口测试", "rest api"),
    ),
}


def _term_is_covered(term: str, queries: list[str]) -> bool:
    """检查特定术语是否已被现有搜索短语覆盖。"""
    term_cf = term.casefold()
    for q in queries:
        q_cf = q.casefold()
        if term_cf in q_cf or q_cf in term_cf:
            return True
    return False


def detect_terminology_gaps(
    topic: str,
    intent: str,
    queries: list[str],
) -> list[dict[str, Any]]:
    """检测当前需求与查询中存在的概念覆盖缺口。

    针对明确触发的概念，检查中英文别名、缩写/全称覆盖情况。
    """
    text_to_match = f"{topic} {intent}".strip().casefold()
    clean_queries = [normalize_query(q) for q in queries if q and isinstance(q, str)]

    gaps: list[dict[str, Any]] = []

    for concept_id, concept in TERMINOLOGY_TABLE.items():
        matched_trigger = None
        for trigger in concept.triggers:
            if trigger.casefold() in text_to_match:
                matched_trigger = trigger
                break

        if not matched_trigger:
            continue

        # 检查覆盖情况
        covered: list[str] = []
        missing: list[str] = []

        for alias in concept.aliases:
            if _term_is_covered(alias, clean_queries):
                covered.append(alias)
            else:
                missing.append(alias)

        # 若存在未覆盖的核心别名（例如只有中文没有英文，或有缩写没有全称）
        if missing and covered:
            gaps.append(
                {
                    "concept_id": concept.id,
                    "concept_name": concept.name,
                    "matched_trigger": matched_trigger,
                    "boundary": concept.boundary,
                    "covered_terms": covered,
                    "missing_terms": missing,
                }
            )

    return gaps


def generate_suggested_queries(
    gaps: list[dict[str, Any]],
    existing_queries: list[str],
    max_suggestions: int = 3,
) -> list[str]:
    """根据缺口生成拟补充查询建议（去除已存在项，受最大建议数约束）。"""
    existing_cf = {normalize_query(q).casefold() for q in existing_queries if q}
    suggestions: list[str] = []

    for gap in gaps:
        for term in gap.get("missing_terms", []):
            clean_term = normalize_query(term)
            if clean_term and clean_term.casefold() not in existing_cf:
                existing_cf.add(clean_term.casefold())
                suggestions.append(clean_term)
                if len(suggestions) >= max_suggestions:
                    return suggestions

    return suggestions


def build_terminology_observation(
    topic: str,
    intent: str,
    queries: list[str],
    *,
    raw_queries: list[str] | None = None,
    max_suggestions: int = 3,
) -> dict[str, Any]:
    """构建术语观察模式事实对象（严格声明 applied=False，不增加真实请求）。"""
    gaps = detect_terminology_gaps(topic, intent, queries)
    suggestions = generate_suggested_queries(gaps, queries, max_suggestions=max_suggestions)

    return {
        "version": TERMINOLOGY_VERSION,
        "mode": "observation",
        "applied": False,
        "raw_queries": raw_queries if raw_queries is not None else list(queries),
        "queries": list(queries),
        "gaps_count": len(gaps),
        "gaps": gaps,
        "suggested_queries": suggestions,
    }


__all__ = [
    "TERMINOLOGY_VERSION",
    "SEARCH_OPERATOR_PATTERN",
    "normalize_query",
    "has_search_operator",
    "strip_search_operators",
    "deduplicate_queries",
    "TerminologyConcept",
    "TERMINOLOGY_TABLE",
    "detect_terminology_gaps",
    "generate_suggested_queries",
    "build_terminology_observation",
]
