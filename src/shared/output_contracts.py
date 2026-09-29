"""严格结构化输出契约规范与各阶段 Schema 定义 (Unit 8 / P3.3)。

架构原则：
1. 兼容旧字符串配置（"json_object" / "text"），支持受强校验的完整格式对象（{"type": "json_schema", ...}）。
2. 后端能力声明与本次业务 Schema 分离，不在全局模型配置中固定写死单个业务契约。
3. 普通目录初评、复核，以及 Finder 规划、澄清、反思与候选评估拥有各自独立的契约定义。
4. Schema 与本地解析器共享字段、枚举与版本定义，即便在 Schema 校验后，本地业务层与证据校验依然严格生效（双重防御）。
5. 后端不支持格式参数时明确报错（RESPONSE_FORMAT_UNSUPPORTED），绝不在每个候选上隐式重复重发降级。
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "1.1.0"

STAGE_CATALOG_ASSESSMENT = "catalog_assessment"
STAGE_CATALOG_REVIEW = "catalog_review"
STAGE_FINDER_PLAN = "finder_plan"
STAGE_FINDER_CLARIFY = "finder_clarify"
STAGE_FINDER_REFLECT = "finder_reflect"
STAGE_FINDER_EVALUATION = "finder_evaluation"

# 1. 目录基础与深度评估检查项模式
_CATALOG_CHECK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {
            "type": "string",
            "enum": ["pass", "fail", "unknown", "not_applicable"],
            "description": "判定结果",
        },
        "evidence": {
            "type": "string",
            "description": "判定理由与事实依据",
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["start_line", "end_line", "quote"],
                "additionalProperties": False,
            },
            "description": "逐行核验原文引用",
        },
    },
    "required": ["value", "evidence"],
    "additionalProperties": False,
}

# 目录评估统一输出结构定义（初评与独立复核共用顶层结构）
_CATALOG_EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "scope_match": _CATALOG_CHECK_ITEM_SCHEMA,
        "purpose_clarity": _CATALOG_CHECK_ITEM_SCHEMA,
        "instruction_completeness": _CATALOG_CHECK_ITEM_SCHEMA,
        "evidence_traceability": _CATALOG_CHECK_ITEM_SCHEMA,
        "dependency_transparency": {
            **_CATALOG_CHECK_ITEM_SCHEMA,
            "properties": {**_CATALOG_CHECK_ITEM_SCHEMA["properties"],
                           "blocking": {"type": "boolean", "description": "缺口是否影响判断核心价值或必要运行条件"}},
        },
        "risk_review": _CATALOG_CHECK_ITEM_SCHEMA,
        "verification_note": {"type": "string", "description": "验证方法或文档缺口，仅作提示"},
        "domain_checks": {
            "type": "object",
            "description": "领域专项检查",
            "additionalProperties": True,
        },
        "summary_zh": {"type": "string", "description": "客观中文简述"},
        "skill_type": {
            "type": ["string", "null"],
            "enum": ["tool_script", "guideline", "template", "reference", None],
            "description": "技能实质形态",
        },
        "example_requests": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户示例请求（最多2条）",
        },
        "key_features": {
            "type": "array",
            "items": {"type": "string"},
            "description": "核心亮点（最多3条）",
        },
        "main_category": {"type": "string", "description": "主分类名称或ID"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用途标签",
        },
        "platform_declared": {
            "type": ["string", "null"],
            "description": "声明的运行平台",
        },
        "dependencies_declared": {
            "type": "array",
            "items": {"type": "string"},
            "description": "声明的外部依赖",
        },
        "limitations": {
            "type": ["string", "null"],
            "description": "主要限制",
        },
        "reason_codes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "命中原因码列表",
        },
    },
    "required": [
        "scope_match",
        "purpose_clarity",
        "instruction_completeness",
        "evidence_traceability",
        "dependency_transparency",
        "risk_review",
        "domain_checks",
        "summary_zh",
        "main_category",
        "reason_codes",
    ],
    "additionalProperties": True,
}

# 1. 目录主评估契约 (Catalog Assessment)
CATALOG_ASSESSMENT_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "catalog_assessment",
        "strict": True,
        "schema": _CATALOG_EVALUATION_SCHEMA,
    },
}

# 2. 目录独立复核契约 (Catalog Review)
CATALOG_REVIEW_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "catalog_review",
        "strict": True,
        "schema": _CATALOG_EVALUATION_SCHEMA,
    },
}

# 3. Finder 查询规划契约 (Finder Plan)
FINDER_PLAN_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "finder_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "用户核心诉求概括"},
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "自然语言搜索短语列表",
                },
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "准则标识"},
                            "kind": {"type": "string", "enum": ["required", "quality_signal"]},
                            "description": {"type": "string", "description": "准则描述"},
                        },
                        "required": ["id", "kind", "description"],
                        "additionalProperties": False,
                    },
                    "description": "评估准则列表",
                },
            },
            "required": ["intent", "queries", "criteria"],
            "additionalProperties": False,
        },
    },
}

# 4. Finder 意图澄清契约 (Finder Clarify)
FINDER_CLARIFY_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "finder_clarify",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string", "description": "本次提问聚焦的维度"},
                "question": {"type": "string", "description": "面向用户的定向提问"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "建议的选项列表",
                },
                "summary": {"type": "string", "description": "已明确信息的简要概括"},
            },
            "required": ["focus", "question", "options"],
            "additionalProperties": True,
        },
    },
}

# 5. Finder 轮次反思契约 (Finder Reflect)
FINDER_REFLECT_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "finder_reflect",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "maxItems": 5,
                    "description": "3 到 5 个反思调整后的检索短语",
                },
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
    },
}

# 6. Finder 候选技能匹配评估契约 (Finder Evaluation)
FINDER_EVALUATION_CONTRACT = {
    "type": "json_schema",
    "json_schema": {
        "name": "finder_evaluation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "match": {
                    "type": "string",
                    "enum": ["strong", "partial", "none"],
                    "description": "整体匹配等级",
                },
                "summary_zh": {"type": "string", "description": "客观中文简述"},
                "criteria_results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "status": {"type": "string", "enum": ["supported", "unsupported", "unknown"]},
                            "explanation": {"type": "string"},
                            "evidence": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source_path": {"type": "string"},
                                        "start_line": {"type": "integer"},
                                        "end_line": {"type": "integer"},
                                        "quote": {"type": "string", "maxLength": 1200},
                                    },
                                    "required": ["source_path", "start_line", "end_line", "quote"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["criterion_id", "status", "explanation", "evidence"],
                        "additionalProperties": False,
                    },
                },
                "documentation": {
                    "type": "string",
                    "enum": ["clear", "partial", "insufficient"],
                    "description": "文档清晰度",
                },
                "usage_zh": {"type": "string", "description": "调用方式简述"},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 5,
                    "description": "特殊工具或系统依赖（最多5项，每项<=100字符）",
                },
                "limitations": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 100},
                    "maxItems": 5,
                    "description": "主要限制或未确认项（最多5项，每项<=100字符）",
                },
                "why_consider": {"type": "string", "description": "推荐参考理由"},
            },
            "required": ["match", "summary_zh", "criteria_results", "documentation"],
            "additionalProperties": True,
        },
    },
}

STAGE_CONTRACT_MAP = {
    STAGE_CATALOG_ASSESSMENT: CATALOG_ASSESSMENT_CONTRACT,
    "assessment": CATALOG_ASSESSMENT_CONTRACT,
    STAGE_CATALOG_REVIEW: CATALOG_REVIEW_CONTRACT,
    "review": CATALOG_REVIEW_CONTRACT,
    STAGE_FINDER_PLAN: FINDER_PLAN_CONTRACT,
    "planning": FINDER_PLAN_CONTRACT,
    STAGE_FINDER_CLARIFY: FINDER_CLARIFY_CONTRACT,
    "clarification": FINDER_CLARIFY_CONTRACT,
    STAGE_FINDER_REFLECT: FINDER_REFLECT_CONTRACT,
    "reflection": FINDER_REFLECT_CONTRACT,
    STAGE_FINDER_EVALUATION: FINDER_EVALUATION_CONTRACT,
    "evaluation": FINDER_EVALUATION_CONTRACT,
}


def get_stage_contract(stage: str) -> dict | None:
    """根据业务阶段获取对应的严格输出契约。"""
    return STAGE_CONTRACT_MAP.get(stage)


def resolve_response_format(
    model_cfg: dict,
    stage_or_contract: str | dict | None = None,
) -> dict | None:
    """根据后端配置与当前业务阶段，解析应使用的 response_format 载荷。

    解耦机制：
    1. 若 model_cfg["request"]["response_format"] 显式指定为 "json_schema"，
       或 model_cfg 声明了 capabilities.json_schema = True：
       - 若传入有效契约或业务阶段名，则构造严格 Schema 契约对象；
       - 若未传入契约，则回退为通用的 {"type": "json_object"}。
    2. 若 model_cfg["request"]["response_format"] 为旧版字符串 "json_object"（或默认未声明支持 schema）：
       - 兼容返回 {"type": "json_object"}，不向后端发送复杂的 json_schema，保证与各类开源模型接口 100% 兼容。
    3. 若 model_cfg["request"]["response_format"] 为字典：
       - 直接信任并返回该字典。
    4. 若未配置任何格式或配置为 "text"：
       - 返回 None 或 {"type": "text"}。
    """
    request_cfg = model_cfg.get("request") or {}
    configured_fmt = request_cfg.get("response_format")
    capabilities = model_cfg.get("capabilities") or {}

    supports_schema = (
        configured_fmt == "json_schema"
        or bool(capabilities.get("json_schema"))
    )

    if supports_schema:
        if isinstance(stage_or_contract, dict) and stage_or_contract.get("type") == "json_schema":
            return stage_or_contract
        if isinstance(stage_or_contract, str) and stage_or_contract in STAGE_CONTRACT_MAP:
            return STAGE_CONTRACT_MAP[stage_or_contract]
        return {"type": "json_object"}

    if isinstance(configured_fmt, dict):
        return configured_fmt
    if isinstance(configured_fmt, str):
        return {"type": configured_fmt}

    return None


__all__ = [
    "CONTRACT_VERSION",
    "STAGE_CATALOG_ASSESSMENT",
    "STAGE_CATALOG_REVIEW",
    "STAGE_FINDER_PLAN",
    "STAGE_FINDER_CLARIFY",
    "STAGE_FINDER_REFLECT",
    "STAGE_FINDER_EVALUATION",
    "CATALOG_ASSESSMENT_CONTRACT",
    "CATALOG_REVIEW_CONTRACT",
    "FINDER_PLAN_CONTRACT",
    "FINDER_CLARIFY_CONTRACT",
    "FINDER_REFLECT_CONTRACT",
    "FINDER_EVALUATION_CONTRACT",
    "STAGE_CONTRACT_MAP",
    "get_stage_contract",
    "resolve_response_format",
]
