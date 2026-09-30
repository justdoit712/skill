"""仅在新技能首次成功评估时，按主要用途判断配置的排除主题。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_FILTER_RULES_PATH = "config/governance/filter-rules.json"
ALLOWED_TOP_KEYS = frozenset(
    {"filter_rules_version", "blocked_topics", "source", "note", "description", "_comment"}
)
ALLOWED_TOPIC_KEYS = frozenset({"name", "description", "note", "_comment"})
TOPIC_RESULTS = frozenset({"match", "no_match", "unknown"})


def validate_filter_rules(data: Any) -> list[str]:
    """名称必填、范围说明可选；旧的关键词/标签配置必须明确迁移。"""
    if not isinstance(data, dict):
        return ["filter-rules 根结构必须是 JSON 对象"]
    errors: list[str] = []
    if "discovery" in data or "evaluation" in data:
        errors.append("旧 discovery/evaluation 规则已停用，请迁移到 blocked_topics 主题列表")
    unknown = set(data) - ALLOWED_TOP_KEYS - {"discovery", "evaluation"}
    if unknown:
        errors.append(f"filter-rules 包含未识别的顶层键：{', '.join(sorted(unknown))}")
    if "filter_rules_version" in data and (
        not isinstance(data["filter_rules_version"], str) or not data["filter_rules_version"].strip()
    ):
        errors.append("filter_rules_version 必须是非空字符串")
    topics = data.get("blocked_topics", [])
    if not isinstance(topics, list):
        return [*errors, "blocked_topics 必须是数组"]
    seen: set[str] = set()
    for index, topic in enumerate(topics):
        prefix = f"blocked_topics[{index}]"
        if not isinstance(topic, dict):
            errors.append(f"{prefix} 必须是包含 name 的对象")
            continue
        extra = set(topic) - ALLOWED_TOPIC_KEYS
        if extra:
            errors.append(f"{prefix} 包含未识别的字段：{', '.join(sorted(extra))}")
        name = topic.get("name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            errors.append(f"{prefix}.name 必须是去掉首尾空白后的非空字符串")
        elif name.casefold() in seen:
            errors.append(f"{prefix}.name 重复：{name}")
        else:
            seen.add(name.casefold())
        if "description" in topic and not isinstance(topic["description"], str):
            errors.append(f"{prefix}.description 必须是字符串，可省略")
    return errors


class FilterRules:
    """可信配置的主题快照；主题标识由名称派生，无需用户填写。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = {} if data is None else data
        errors = validate_filter_rules(data)
        if errors:
            raise ValueError("主题屏蔽配置非法：" + "；".join(errors))
        self.blocked_topics: list[dict[str, str]] = [
            {
                "topic_id": "topic-" + hashlib.sha256(topic["name"].encode("utf-8")).hexdigest()[:12],
                "name": topic["name"],
                "description": topic.get("description", "").strip(),
            }
            for topic in data.get("blocked_topics", [])
        ]
        canonical = json.dumps(self.blocked_topics, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def has_evaluation_rules(self) -> bool:
        return bool(self.blocked_topics)


def eligible_for_topic_filter(
    filter_rules: FilterRules | None,
    previous_entry: dict | None = None,
    *,
    previously_evaluated: bool = False,
) -> bool:
    """已有收藏、推荐、候选及成功评估历史均不追溯检查。"""
    previous = previous_entry or {}
    return bool(
        filter_rules and filter_rules.has_evaluation_rules
        and not previously_evaluated and not previous.get("manual_pick")
        and previous.get("status") not in ("recommended", "candidate")
        and not previous.get("evaluated_at") and not previous.get("last_evaluation_id")
    )


def normalize_topic_assessments(raw: Any, filter_rules: FilterRules) -> list[dict[str, str]]:
    """每个配置主题只接受一份合法判定；缺失、重复或非法字段降为 unknown。"""
    grouped: dict[str, list[dict]] = {topic["topic_id"]: [] for topic in filter_rules.blocked_topics}
    if isinstance(raw, list):
        for assessment in raw:
            if not isinstance(assessment, dict):
                continue
            topic_id = assessment.get("topic_id")
            if isinstance(topic_id, str) and topic_id in grouped:
                grouped[topic_id].append(assessment)
    normalized = []
    for topic in filter_rules.blocked_topics:
        topic_id = topic["topic_id"]
        values = grouped[topic_id]
        result, evidence = "unknown", ""
        if len(values) == 1:
            value = values[0]
            raw_result, raw_evidence = value.get("result"), value.get("evidence")
            if (isinstance(raw_result, str) and raw_result in TOPIC_RESULTS
                    and isinstance(raw_evidence, str)):
                evidence = raw_evidence.strip()
                if raw_result != "match" or evidence:
                    result = raw_result
        normalized.append({"topic_id": topic_id, "result": result, "evidence": evidence})
    return normalized


def build_topic_filter_audit(filter_rules: FilterRules, raw_assessments: Any) -> dict:
    """程序生成审计快照，不能使用模型自己声明的配置或指纹。"""
    return {
        "rules_fingerprint": filter_rules.fingerprint,
        "blocked_topics": [dict(topic) for topic in filter_rules.blocked_topics],
        "topic_assessments": normalize_topic_assessments(raw_assessments, filter_rules),
    }


def _rules_from_audit(audit: Any) -> FilterRules | None:
    """校验已持久化的规则快照；续跑时不把旧响应重新解释为当前规则。"""
    if not isinstance(audit, dict) or not isinstance(audit.get("blocked_topics"), list):
        return None
    try:
        topics = audit["blocked_topics"]
        if any(not isinstance(topic, dict) for topic in topics):
            return None
        rules = FilterRules({"blocked_topics": [
            {"name": topic.get("name"), "description": topic.get("description", "")}
            for topic in topics
        ]})
        if topics != rules.blocked_topics or audit.get("rules_fingerprint") != rules.fingerprint:
            return None
        return rules
    except (ValueError, TypeError):
        return None


def successful_evaluation_skill_ids(*directories: Path) -> set[str]:
    """运行开始时读取成功评估身份，避免把目录尚未恢复的旧结果当作首次评估。"""
    skill_ids = set()
    for directory in set(directories):
        for path in directory.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("status") == "completed"
                    and isinstance((record.get("outcome") or {}).get("evaluation"), dict)
                    and record.get("skill_id")):
                skill_ids.add(record["skill_id"])
    return skill_ids


def filter_new_evaluation(
    decision: dict,
    evaluation: dict,
    filter_rules: FilterRules | None,
    previous_entry: dict | None = None,
    *,
    previously_evaluated: bool = False,
) -> dict:
    """只依据同次 AI 评估的主要用途判定排除，标签不参与判断。"""
    previous = previous_entry or {}
    if (previously_evaluated or previous.get("manual_pick")
            or previous.get("status") in ("recommended", "candidate")
            or previous.get("evaluated_at") or previous.get("last_evaluation_id")):
        return decision
    has_saved_audit = "topic_filter_audit" in evaluation
    saved_audit = evaluation.get("topic_filter_audit")
    effective_rules = _rules_from_audit(saved_audit) if has_saved_audit else filter_rules
    if not eligible_for_topic_filter(
        effective_rules, previous_entry, previously_evaluated=previously_evaluated
    ):
        return decision
    raw = saved_audit.get("topic_assessments") if has_saved_audit else evaluation.get("topic_assessments")
    audit = build_topic_filter_audit(effective_rules, raw)
    matched_ids = {
        item["topic_id"] for item in audit["topic_assessments"] if item["result"] == "match"
    }
    matched_names = [
        topic["name"] for topic in effective_rules.blocked_topics if topic["topic_id"] in matched_ids
    ]
    result = {**decision, "topic_filter_audit": audit}
    if matched_names:
        result.update({
            "original_decision": dict(decision),
            "decision": "excluded",
            "reason_codes": list(dict.fromkeys([*(decision.get("reason_codes") or []), "TOPIC_FILTERED"])),
            "topic_filtered": True,
            "blocked_topics": matched_names,
        })
    return result


def load_filter_rules(path: str | Path = DEFAULT_FILTER_RULES_PATH) -> FilterRules:
    """加载主题配置；文件不存在时返回空规则，非法配置明确报错。"""
    file_path = Path(path)
    if not file_path.exists():
        if "governance" not in file_path.parts:
            alternate = file_path.parent / "governance" / file_path.name
        else:
            alternate = file_path.parent.parent / file_path.name
        if alternate.exists():
            file_path = alternate
    if not file_path.exists():
        return FilterRules()
    try:
        content = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"读取主题屏蔽规则失败（{file_path}）：{exc}") from exc
    errors = validate_filter_rules(content)
    if errors:
        raise ValueError(f"主题屏蔽规则配置非法（{file_path}）：" + "；".join(errors))
    return FilterRules(content)
