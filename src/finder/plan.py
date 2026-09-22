"""需求理解与查询规划模块。

将自然语言需求分解为 GitHub 技能搜索计划与评估准则。
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_PLAN_QUERIES = 8
MAX_CRITERIA_COUNT = 6
PLAN_MAX_OUTPUT_TOKENS = 4000

KIND_REQUIRED = "required"
KIND_QUALITY_SIGNAL = "quality_signal"
VALID_CRITERION_KINDS = {KIND_REQUIRED, KIND_QUALITY_SIGNAL}


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def build_plan_prompt(topic: str) -> tuple[str, str]:
    """构造需求理解与查询规划的系统提示词与用户提示词。"""
    system = "\n".join(
        [
            "你是技能检索与需求分析专家。你的任务是将用户的具体需求分解为 GitHub 技能搜索计划与评估维度。",
            "",
            "输出要求：",
            "1. 只输出一个合法的 JSON 对象，严禁包含任何 Markdown 代码块标记（如 ```json）或前后解释文字。",
            "2. queries 数组：生成 3 到 8 条自然语言搜索短语（包含精准中英文关键词、同义表述与直接需求短语）。模型只输出短语本身，不要添加 GitHub 搜索操作符（如 in:readme 或 site:github.com）。",
            "3. criteria 数组：生成 2 到 5 条评估准则。每条包含 id（小写下划线标识符）、kind（只能是 'required' 或 'quality_signal'）、description（明确的能力或质量描述）。",
            "4. 硬性规则：只有用户在需求中明确提出的核心能力才能设为 'required'（至少设 1 条）；其它期望的质量特征（如提供示例、输入澄清、参数优化等）必须设为 'quality_signal'，绝不能擅自升级为硬门槛！",
            "5. 不默认绑定特定平台、语言或私有接口，保持通用适配。",
            "",
            "JSON 输出结构：",
            json.dumps(
                {
                    "intent": "对用户核心意图的一句话归纳",
                    "queries": ["prompt generator", "prompt optimizer", "提示词生成"],
                    "criteria": [
                        {
                            "id": "generate_prompt",
                            "kind": "required",
                            "description": "明确支持根据需求生成提示词",
                        },
                        {
                            "id": "optimize_prompt",
                            "kind": "quality_signal",
                            "description": "说明如何检查、评估或优化已有提示词",
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )

    user = f"用户需求：{topic.strip()}\n\n请直接输出规划 JSON："
    return system, user


def parse_query_plan(content: str) -> dict[str, Any]:
    """解析模型输出的规划结果，并进行严格的强类型校验。"""
    cleaned = _strip_fence(content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"查询规划输出不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("查询规划顶层必须是 JSON 对象")

    intent = str(data.get("intent") or "").strip()
    if not intent:
        raise ValueError("查询规划缺少意图归纳 (intent)")

    # 校验 queries
    raw_queries = data.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("查询规划缺少搜索短语 (queries 数组)")

    queries: list[str] = []
    for q in raw_queries:
        if isinstance(q, str):
            clean_q = _normalize_space(q)
            # 过滤包含明显搜索操作符的短语，保证纯净
            clean_q = re.sub(r"\b(in|repo|org|user|language|stars|forks):[^\s]+", "", clean_q).strip()
            if clean_q and clean_q not in queries:
                queries.append(clean_q)

    if not queries:
        raise ValueError("未能提取出有效的搜索短语")
    queries = queries[:MAX_PLAN_QUERIES]

    # 校验 criteria
    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise ValueError("查询规划缺少评估准则 (criteria 数组)")

    criteria: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    has_required = False

    for c in raw_criteria:
        if not isinstance(c, dict):
            continue
        cid = re.sub(r"[^a-z0-9_]+", "_", str(c.get("id") or "").strip().lower()).strip("_")
        if not cid:
            continue
        if cid in seen_ids:
            continue
        kind = str(c.get("kind") or "").strip().lower()
        if kind not in VALID_CRITERION_KINDS:
            kind = KIND_QUALITY_SIGNAL

        desc = _normalize_space(str(c.get("description") or ""))
        if not desc:
            continue

        if kind == KIND_REQUIRED:
            has_required = True

        seen_ids.add(cid)
        criteria.append({"id": cid, "kind": kind, "description": desc})

    if not criteria:
        raise ValueError("未能提取出有效的评估准则")

    if not has_required:
        raise ValueError("规划结果中缺少必需能力 (kind='required')，模型规划未对齐硬性规则")

    criteria = criteria[:MAX_CRITERIA_COUNT]

    return {
        "intent": intent,
        "queries": queries,
        "criteria": criteria,
    }


__all__ = [
    "MAX_PLAN_QUERIES",
    "MAX_CRITERIA_COUNT",
    "KIND_REQUIRED",
    "KIND_QUALITY_SIGNAL",
    "VALID_CRITERION_KINDS",
    "_strip_fence",
    "_normalize_space",
    "build_plan_prompt",
    "parse_query_plan",
]
