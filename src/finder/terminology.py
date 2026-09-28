"""技术术语表与查询规范化纯函数模块。

实现初始、交互与反思查询的统一规范化，并提供带版本的小型技术术语表与覆盖缺口观察。
纯函数设计，不执行网络请求与磁盘 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
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


def generate_query_identity(query: str) -> str:
    """生成稳定的查询短语唯一标识（由规范化小写内容哈希派生）。"""
    clean = normalize_query(query).casefold()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]


def extract_negative_constraints(text: str) -> list[str]:
    """从需求文本中提取显式负向约束词（例如：不需要 selenium、不用 requests、without headless）。"""
    if not isinstance(text, str):
        return []
    patterns = [
        r"(?:不需要|不用|不要|免去|无需|排斥)\s*([a-zA-Z0-9_\u4e00-\u9fa5]+)",
        r"(?:without|no|not)\s+([a-zA-Z0-9_-]+)",
    ]
    tokens: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            token = normalize_query(m.group(1)).casefold()
            if token and token not in tokens:
                tokens.append(token)
    return tokens


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
        if term_cf in q_cf:
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
        if missing:
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
    negative_tokens: list[str] | None = None,
) -> list[str]:
    """根据缺口生成拟补充查询建议（去除已存在项，过滤负向约束词，受最大建议数约束）。"""
    existing_cf = {normalize_query(q).casefold() for q in existing_queries if q}
    neg_cf = {t.casefold() for t in (negative_tokens or []) if t}
    suggestions: list[str] = []

    for gap in gaps:
        for term in gap.get("missing_terms", []):
            clean_term = normalize_query(term)
            if not clean_term:
                continue
            term_cf = clean_term.casefold()
            if term_cf in existing_cf:
                continue
            # 严格避开用户明确提出的负向约束词
            if any(neg in term_cf for neg in neg_cf):
                continue
            existing_cf.add(term_cf)
            suggestions.append(clean_term)
            if len(suggestions) >= max_suggestions:
                return suggestions

    return suggestions


def apply_terminology_completion(
    topic: str,
    intent: str,
    queries: list[str],
    *,
    max_total_queries: int = 8,
    raw_queries: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """有界术语补全（纯函数）：
    在不突破最大查询上限（max_total_queries）的前提下，将概念缺口建议词合并入最终查询。
    绝不改变 intent、required 或 quality_signal。
    """
    clean_queries = deduplicate_queries(queries)
    available_slots = max(0, max_total_queries - len(clean_queries))
    gaps = detect_terminology_gaps(topic, intent, clean_queries)
    neg_tokens = extract_negative_constraints(topic)
    suggested = generate_suggested_queries(
        gaps, clean_queries, max_suggestions=available_slots, negative_tokens=neg_tokens
    )

    final_queries = list(clean_queries)
    added_queries: list[str] = []
    for s in suggested:
        if len(final_queries) >= max_total_queries:
            break
        s_norm = normalize_query(s)
        if s_norm and s_norm.casefold() not in {q.casefold() for q in final_queries}:
            final_queries.append(s_norm)
            added_queries.append(s_norm)

    # 构造每条查询的稳定身份与来源归因
    attributions: list[dict[str, Any]] = []
    for q in final_queries:
        qid = generate_query_identity(q)
        if q in clean_queries:
            attributions.append({
                "query": q,
                "query_id": qid,
                "source": "model",
            })
        else:
            # 找到来源概念
            matched_concept_id = None
            matched_concept_name = None
            for gap in gaps:
                if any(normalize_query(term).casefold() == q.casefold() for term in gap.get("missing_terms", [])):
                    matched_concept_id = gap.get("concept_id")
                    matched_concept_name = gap.get("concept_name")
                    break
            attributions.append({
                "query": q,
                "query_id": qid,
                "source": "terminology",
                "concept_id": matched_concept_id,
                "concept_name": matched_concept_name,
            })

    metadata = {
        "version": TERMINOLOGY_VERSION,
        "mode": "completion",
        "applied": True,
        "raw_queries": raw_queries if raw_queries is not None else list(clean_queries),
        "original_queries": list(clean_queries),
        "final_queries": list(final_queries),
        "added_queries": added_queries,
        "max_total_queries": max_total_queries,
        "gaps_count": len(gaps),
        "gaps": gaps,
        "query_attributions": attributions,
    }
    return final_queries, metadata


def build_terminology_observation(
    topic: str,
    intent: str,
    queries: list[str],
    *,
    raw_queries: list[str] | None = None,
    max_suggestions: int = 3,
) -> dict[str, Any]:
    """构建术语观察模式事实对象（严格声明 applied=False，不增加真实请求）。"""
    clean_queries = deduplicate_queries(queries)
    gaps = detect_terminology_gaps(topic, intent, clean_queries)
    neg_tokens = extract_negative_constraints(topic)
    suggestions = generate_suggested_queries(
        gaps, clean_queries, max_suggestions=max_suggestions, negative_tokens=neg_tokens
    )

    attributions = [
        {
            "query": q,
            "query_id": generate_query_identity(q),
            "source": "model",
        }
        for q in clean_queries
    ]

    return {
        "version": TERMINOLOGY_VERSION,
        "mode": "observation",
        "applied": False,
        "raw_queries": raw_queries if raw_queries is not None else list(clean_queries),
        "queries": list(clean_queries),
        "original_queries": list(clean_queries),
        "final_queries": list(clean_queries),
        "added_queries": [],
        "query_attributions": attributions,
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
    "generate_query_identity",
    "extract_negative_constraints",
    "TerminologyConcept",
    "TERMINOLOGY_TABLE",
    "detect_terminology_gaps",
    "generate_suggested_queries",
    "apply_terminology_completion",
    "build_terminology_observation",
]
