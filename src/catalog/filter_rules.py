"""统一内容黑名单：主题校验、本地预筛匹配与首次评估判定。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_FILTER_RULES_PATH = "config/governance/overrides.json"
ALLOWED_TOP_KEYS = frozenset(
    {
        "filter_rules_version",
        "overrides_version",
        "manual_exclusions",
        "blocked_topics",
        "source",
        "note",
        "description",
        "_comment",
    }
)
ALLOWED_TOPIC_KEYS = frozenset(
    {"name", "description", "note", "_comment", "keywords", "local_match"}
)
ALLOWED_LOCAL_MATCH_KEYS = frozenset(
    {"names", "purpose_phrases", "preserve_phrases", "note", "_comment"}
)
TOPIC_RESULTS = frozenset({"match", "no_match", "unknown"})


def validate_filter_rules(data: Any) -> list[str]:
    """名称必填、范围说明及关键词/本地匹配条件可选；旧的关键词/标签配置必须明确迁移。"""
    if not isinstance(data, dict):
        return ["主题屏蔽/黑名单配置根结构必须是 JSON 对象"]
    errors: list[str] = []
    if "discovery" in data or "evaluation" in data:
        errors.append("旧 discovery/evaluation 规则已停用，请迁移到 blocked_topics 主题列表")
    unknown = set(data) - ALLOWED_TOP_KEYS - {"discovery", "evaluation"}
    if unknown:
        errors.append(f"主题屏蔽配置包含未识别的顶层键：{', '.join(sorted(unknown))}")
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

        # 校验 keywords
        if "keywords" in topic:
            kws = topic["keywords"]
            if not isinstance(kws, list):
                errors.append(f"{prefix}.keywords 必须是字符串数组")
            else:
                kw_seen: set[str] = set()
                for k_idx, kw in enumerate(kws):
                    if not isinstance(kw, str) or not kw.strip() or kw != kw.strip():
                        errors.append(f"{prefix}.keywords[{k_idx}] 必须是去掉首尾空白后的非空字符串")
                    elif kw.casefold() in kw_seen:
                        errors.append(f"{prefix}.keywords[{k_idx}] 重复：{kw}")
                    else:
                        kw_seen.add(kw.casefold())

        # 校验 local_match
        if "local_match" in topic:
            lm = topic["local_match"]
            if not isinstance(lm, dict):
                errors.append(f"{prefix}.local_match 必须是对象")
            else:
                extra_lm = set(lm) - ALLOWED_LOCAL_MATCH_KEYS
                if extra_lm:
                    errors.append(f"{prefix}.local_match 包含未识别的字段：{', '.join(sorted(extra_lm))}")
                for list_field in ("names", "purpose_phrases", "preserve_phrases"):
                    if list_field in lm:
                        items = lm[list_field]
                        if not isinstance(items, list):
                            errors.append(f"{prefix}.local_match.{list_field} 必须是字符串数组")
                        else:
                            item_seen: set[str] = set()
                            for i_idx, item in enumerate(items):
                                if not isinstance(item, str) or not item.strip() or item != item.strip():
                                    errors.append(f"{prefix}.local_match.{list_field}[{i_idx}] 必须是去掉首尾空白后的非空字符串")
                                elif item.casefold() in item_seen:
                                    errors.append(f"{prefix}.local_match.{list_field}[{i_idx}] 重复：{item}")
                                else:
                                    item_seen.add(item.casefold())
    return errors


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FilterRules:
    """可信配置的主题快照；主题标识由名称派生，无需用户填写。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = {} if data is None else data
        errors = validate_filter_rules(data)
        if errors:
            raise ValueError("主题屏蔽配置非法：" + "；".join(errors))
        self.blocked_topics: list[dict[str, Any]] = []
        for topic in data.get("blocked_topics", []):
            t_obj: dict[str, Any] = {
                "topic_id": "topic-" + hashlib.sha256(topic["name"].encode("utf-8")).hexdigest()[:12],
                "name": topic["name"],
                "description": topic.get("description", "").strip(),
            }
            if "keywords" in topic and isinstance(topic["keywords"], list):
                clean_kws = [k.strip() for k in topic["keywords"] if isinstance(k, str) and k.strip()]
                if clean_kws:
                    t_obj["keywords"] = clean_kws
            if "local_match" in topic and isinstance(topic["local_match"], dict):
                lm = topic["local_match"]
                clean_lm = {}
                for k in ("names", "purpose_phrases", "preserve_phrases"):
                    if k in lm and isinstance(lm[k], list):
                        items = [item.strip() for item in lm[k] if isinstance(item, str) and item.strip()]
                        if items:
                            clean_lm[k] = items
                if clean_lm:
                    t_obj["local_match"] = clean_lm
            self.blocked_topics.append(t_obj)

        canonical = _canonical_json(self.blocked_topics)
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


def extract_skill_name_from_path(raw_name_or_path: str) -> str:
    """从名称或路径中提取技能自身目录名（排除仓库名或任意祖先目录，排除尾随的 SKILL.md）。"""
    if not raw_name_or_path:
        return ""
    p = str(raw_name_or_path).strip().replace("\\", "/")
    if ":" in p:
        p = p.split(":", 1)[1]
    p = p.strip("/")
    parts = [part for part in p.split("/") if part]
    if not parts:
        return ""
    if parts[-1].lower() == "skill.md":
        return parts[-2] if len(parts) >= 2 else ""
    return parts[-1]


def extract_frontmatter_description(text: str) -> tuple[str, str]:
    """从 Markdown 正文的 YAML frontmatter 中提取 (name, description)。
    若无 frontmatter 或字段缺失，返回 ("", "")。
    """
    if not text or not text.strip().startswith("---"):
        return "", ""
    clean = text.strip()
    parts = clean.split("---", 2)
    if len(parts) < 3:
        return "", ""
    fm = parts[1]
    name = ""
    description = ""
    m_name = re.search(r"(?m)^name\s*:\s*(?:['\"](.*?)['\"]|(.*))$", fm)
    if m_name:
        name = (m_name.group(1) or m_name.group(2) or "").strip()

    m_desc = re.search(
        r"(?ms)^description\s*:\s*(?:['\"](.*?)['\"]|([>|]?\s*\n\s+.*?(?=\n\S|\Z))|(.*))$", fm
    )
    if m_desc:
        raw_desc = m_desc.group(1) or m_desc.group(2) or m_desc.group(3) or ""
        raw_desc = re.sub(r"^[>|]\s*", "", raw_desc.strip())
        description = " ".join(line.strip() for line in raw_desc.splitlines() if line.strip())

    return name, description


def _build_phrase_pattern(phrase: str) -> str:
    phrase_clean = phrase.strip()
    if not phrase_clean:
        return ""
    pattern = r"(?i)"
    if phrase_clean.isascii():
        if re.match(r"^\w", phrase_clean, re.ASCII):
            pattern += r"\b"
        pattern += re.escape(phrase_clean)
        if re.search(r"\w$", phrase_clean, re.ASCII):
            pattern += r"\b"
    else:
        pattern += re.escape(phrase_clean)
    return pattern


def _phrase_in_text(phrase: str, text: str) -> bool:
    """检查短语是否在文本中。英文短语按单词边界匹配，中文短语按子串匹配。"""
    if not phrase or not text:
        return False
    pattern = _build_phrase_pattern(phrase)
    if not pattern:
        return False
    return bool(re.search(pattern, text))


def _has_negation_or_secondary_context(phrase: str, text: str) -> bool:
    """检查短语在文本中的所有匹配是否均处于否定或次要用途上下文中。
    如果短语存在至少一处肯定的主要用途陈述，则返回 False。
    如果所有匹配均被否定或次要修饰，则返回 True（不作为主要排除依据）。
    """
    if not phrase or not text:
        return False
    pattern = _build_phrase_pattern(phrase)
    if not pattern:
        return False

    matches = list(re.finditer(pattern, text))
    if not matches:
        return False

    neg_words = r"(?:不|非|并非|不再|不支持|不包含|不提供|无需|免于|未|不能|禁止|不用于|not|no|never|neither|nor|without|cannot|unsupported|doesn't|does not|won't)"
    sec_words = r"(?:仅顺带|顺带|仅辅助|仅作为辅助|作为辅助|辅助|次要|仅用于导出|仅用于生成|仅用于临时|仅作为补充|副产物|secondary|ancillary|auxiliary|incidental|incidentally|minor|only for export|just for export|only as export|side effect)"

    for m in matches:
        start, end = m.start(), m.end()
        pre_window = text[max(0, start - 30):start]
        post_window = text[end:min(len(text), end + 30)]

        is_negated = bool(
            re.search(rf"{neg_words}\s*$", pre_window, re.IGNORECASE)
            or re.search(rf"{neg_words}", pre_window[-15:], re.IGNORECASE)
            # 后置谓语也可能否定用途；只看同一分句，避免把下一句的限制
            # （例如“文档处理。不支持图片编辑”）误当成对当前用途的否定。
            or re.search(
                r"^\s*(?:(?:功能|能力|用途|操作|服务)\s*)?"
                r"(?:(?:并)?不(?:是|在|属|支持|提供|包含|适用|作为|用于)|并非|非主要|无法|不能|未实现)"
                r"|^\s*(?:(?:is|are|was|were)\s+(?:not\b|unsupported\b|unavailable\b)"
                r"|(?:isn't|aren't|wasn't|weren't)\b)",
                post_window,
                re.IGNORECASE,
            )
        )
        is_secondary = bool(
            re.search(rf"{sec_words}\s*$", pre_window, re.IGNORECASE)
            or re.search(rf"^{sec_words}", post_window.strip(), re.IGNORECASE)
            or re.search(rf"{sec_words}", pre_window[-15:], re.IGNORECASE)
            or re.search(rf"{sec_words}", post_window[:15], re.IGNORECASE)
        )

        if not is_negated and not is_secondary:
            return False

    return True


def match_local_filter_rules(
    filter_rules: FilterRules | None,
    name: str,
    summary: str = "",
    *,
    materials: dict[str, Any] | None = None,
    stage: str = "local",
) -> dict[str, Any]:
    """纯本地判定契约。

    必须同时满足：
    1. 技能完整名称符合 local_match.names（仅提取自身目录名）；
    2. 有可信的自身用途摘要，且主要用途陈述命中 purpose_phrases（排除否定与次要用途）；
    3. 可用上下文及 materials（含正文）中没有任何 preserve_phrases。

    返回结果：
    - match: 明确命中排除条件，包含证据及指纹；
    - unknown: 线索冲突、材料不全或命中专业保留；
    - no_match: 本地无命中。
    """
    if not filter_rules or not filter_rules.has_evaluation_rules:
        return {"result": "no_match", "matcher_version": "local_match_v1", "stage": stage}

    clean_name = extract_skill_name_from_path(name).strip().casefold()
    summary_clean = str(summary or "").strip()

    all_context_texts = [summary_clean]
    if materials and isinstance(materials, dict):
        for k in (
            "key_features",
            "example_requests",
            "limitations",
            "tags",
            "description",
            "summary_zh",
            "text",
            "raw_text",
            "content",
        ):
            v = materials.get(k)
            if isinstance(v, str) and v.strip():
                all_context_texts.append(v.strip())
            elif isinstance(v, (list, tuple)):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        all_context_texts.append(item.strip())
    combined_context = " \n ".join(all_context_texts)

    has_unknown = False
    unknown_reason = ""

    for topic in filter_rules.blocked_topics:
        lm = topic.get("local_match")
        if not lm or not isinstance(lm, dict):
            continue
        names = [n.strip().casefold() for n in lm.get("names", []) if isinstance(n, str) and n.strip()]
        purpose_phrases = [p.strip() for p in lm.get("purpose_phrases", []) if isinstance(p, str) and p.strip()]
        preserve_phrases = [pr.strip() for pr in lm.get("preserve_phrases", []) if isinstance(pr, str) and pr.strip()]

        if not names or not purpose_phrases:
            continue

        # 条件 1：完整名称匹配
        if clean_name not in names:
            continue

        # 条件 3：专业用途保留短语拦截（含正文全文）
        if any(_phrase_in_text(phrase, combined_context) for phrase in preserve_phrases):
            has_unknown = True
            unknown_reason = f"命中专业用途保留短语（规则：{topic['name']}）"
            continue

        # 条件 2：必须有可信自身用途摘要
        if not summary_clean:
            has_unknown = True
            unknown_reason = f"用途摘要缺失，无法确认主要用途（规则：{topic['name']}，名称：{clean_name}）"
            continue

        matched_phrase = None
        has_negated_or_secondary = False
        for phrase in purpose_phrases:
            if _phrase_in_text(phrase, summary_clean):
                if _has_negation_or_secondary_context(phrase, summary_clean):
                    has_negated_or_secondary = True
                    continue
                matched_phrase = phrase
                break

        if not matched_phrase:
            has_unknown = True
            if has_negated_or_secondary:
                unknown_reason = f"用途陈述包含否定或次要修饰，无法确定为主要排除用途（规则：{topic['name']}，名称：{clean_name}）"
            else:
                unknown_reason = f"用途摘要未命中明确基础用途短语（规则：{topic['name']}，名称：{clean_name}）"
            continue

        # 条件 1、2、3 均满足 -> MATCH
        return {
            "result": "match",
            "matched_topic_id": topic["topic_id"],
            "matched_topic_name": topic["name"],
            "matched_name": clean_name,
            "matched_purpose": summary_clean[:200],
            "matched_phrase": matched_phrase,
            "rules_fingerprint": filter_rules.fingerprint,
            "matcher_version": "local_match_v1",
            "stage": stage,
        }

    if has_unknown:
        return {
            "result": "unknown",
            "reason": unknown_reason,
            "matcher_version": "local_match_v1",
            "stage": stage,
        }

    return {"result": "no_match", "matcher_version": "local_match_v1", "stage": stage}


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
    """校验已持久化的规则快照；兼容历史 v2 审计及 v3 审计，续跑时不把旧响应重新解释为当前规则。"""
    if not isinstance(audit, dict) or not isinstance(audit.get("blocked_topics"), list):
        return None
    try:
        topics = audit["blocked_topics"]
        if any(not isinstance(topic, dict) for topic in topics):
            return None
        reconstructed = []
        for topic in topics:
            t = {
                "name": topic.get("name"),
                "description": topic.get("description", ""),
            }
            if "keywords" in topic and isinstance(topic["keywords"], list) and topic["keywords"]:
                t["keywords"] = topic["keywords"]
            if "local_match" in topic and isinstance(topic["local_match"], dict) and topic["local_match"]:
                t["local_match"] = topic["local_match"]
            reconstructed.append(t)
        rules = FilterRules({"blocked_topics": reconstructed})
        legacy_v2_canonical = _canonical_json([
            {"topic_id": t["topic_id"], "name": t["name"], "description": t["description"]}
            for t in rules.blocked_topics
        ])
        legacy_v2_fp = hashlib.sha256(legacy_v2_canonical.encode("utf-8")).hexdigest()

        audit_fp = audit.get("rules_fingerprint")
        if audit_fp != rules.fingerprint and audit_fp != legacy_v2_fp:
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
        # 若传入旧路径 filter-rules.json，兼容回退至同一层级的 overrides.json
        if file_path.name == "filter-rules.json":
            override_candidate = file_path.parent / "overrides.json"
            if override_candidate.exists():
                file_path = override_candidate
            else:
                override_alt = (
                    file_path.parent / "governance" / "overrides.json"
                    if "governance" not in file_path.parts
                    else file_path.parent.parent / "overrides.json"
                )
                if override_alt.exists():
                    file_path = override_alt

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
