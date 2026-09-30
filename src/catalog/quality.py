"""普通目录的质量标准与原文校验；不访问网络或调用模型。"""

from copy import deepcopy


from .decide import informational_unknown

BASE_CHECK_NAMES = {
    "scope_match": "范围匹配", "purpose_clarity": "用途与价值",
    "instruction_completeness": "说明与做法", "evidence_traceability": "内容依据",
    "dependency_transparency": "依赖与适用条件", "risk_review": "明显风险",
}

MAX_CITATION_LINES = 60


def enabled(rules):
    return (rules.get("quality_review") or {}).get("enabled") is True


def prompt_instructions():
    return "\n".join([
        "本项目提供技能发现与比较，判断材料是否支持值得进一步了解；不做功能实测或可用性认证。",
        "保留六项基础检查：实际价值并入 purpose_clarity，可操作性并入 instruction_completeness，不再输出三项 quality_checks。",
        "不要按篇幅、Star、作者知名度或术语数量打分；短而具体的技能可以通过。",
        "材料足以理解做法与价值即可；缺完整教程、代码、验收清单不单独阻止推荐。只有宣传或空泛角色设定不能通过。",
        "材料引用的关键文件未提供时不得假设已读取；影响判断核心能力时用 unknown。",
        "六项均给简短判定理由，核心功能的引用集中在 evidence_traceability.citations，其他基础项无需重复引用。",
        '引用格式：{"start_line": 1, "end_line": 2, "quote": "两行完整原文"}。',
        "使用 SKILL.md 编号；quote 不包含编号，逐字复制连续完整行，每条最多60行，最多3条，选择最短充分证据。",
        "evidence_traceability 的 pass 必须有支持核心描述的引用；程序只验证引文存在，介绍与内容是否一致仍需你判断。",
        "领域专项仅在适用时检查，通过项仍提供 citations；不适用的条件不得当作失败。不得因本次放宽取消金融或健康专项要求。",
        "dependency_transparency 增加 blocking 布尔值：非关键信息缺口为 unknown、false；关键条件不清楚为 unknown、true；缺必要依赖为 fail、true。未声明依赖不等于无依赖，不能推断平台兼容性。",
        "risk_review 的 pass 仅指所读材料未见明显风险，无需为风险不存在寻找引文；具体疑点仍须说明。",
        "verification_note 简述材料提供的示例、验收或验证方法；未提供时如实说明。仅作优点或限制展示，不作为额外门槛。",
    ])


def locate_citations(citations, text):
    """核实完整连续行；修正定位漂移，不接受局部相似或截取片段。"""
    if not isinstance(citations, list) or not 1 <= len(citations) <= 3:
        return None
    lines = text.splitlines()
    located = []
    for citation in citations:
        if not isinstance(citation, dict):
            return None
        start, end, quote = (citation.get(k) for k in ("start_line", "end_line", "quote"))
        if type(start) is not int or type(end) is not int:
            return None
        if not 1 <= start <= end:
            return None
        if not isinstance(quote, str) or not quote.strip():
            return None
        quoted = quote.replace("\r\n", "\n").splitlines()
        if not 1 <= len(quoted) <= MAX_CITATION_LINES:
            return None
        matches = [i for i in range(len(lines) - len(quoted) + 1)
                   if lines[i:i + len(quoted)] == quoted]
        if not matches:
            return None
        exact = start - 1 in matches and end - start + 1 == len(quoted)
        nearby = [i for i in matches if abs(i + 1 - start) <= 3 and abs(i + len(quoted) - end) <= 3]
        if exact:
            pos, method = start - 1, "exact"
        elif len(nearby) == 1:
            pos, method = nearby[0], "nearby"
        elif len(matches) == 1:
            pos, method = matches[0], "relocated"
        else:
            return None  # 重复原文且定位不明确，不能随意选一处。
        located.append({"start_line": pos + 1, "end_line": pos + len(quoted),
                        "quote": "\n".join(quoted), "match_method": method,
                        "original_start_line": start, "original_end_line": end})
    return located


def verified_citations(citations, text):
    return locate_citations(citations, text) is not None


def check_quality(evaluation, text, rules):
    """核验核心引用与适用领域证据；文档验证说明仅供展示。"""
    out = deepcopy(evaluation)
    # 历史三项不再参与新规则的判定或展示；完整旧记录仍由历史摘要兼容。
    out.pop("quality_checks", None)
    note = out.get("verification_note")
    if note is not None and not isinstance(note, str):
        raise ValueError("verification_note 必须是文本")
    out["verification_note"] = (note or "").strip() or "验证方法尚未确认。"
    required = set((rules.get("quality_review") or {}).get(
        "citation_required_checks", ["evidence_traceability"]))
    groups = [(out, [c["id"] for c in rules.get("checks", [])], False),
              (out.get("domain_checks") or {}, list((out.get("domain_checks") or {}).keys()), True)]
    invalid, locations = [], {}
    for group, keys, domain in groups:
        for key in keys:
            item = group.get(key)
            if not isinstance(item, dict) or item.get("value") not in ("pass", "fail", "unknown", "not_applicable"):
                if domain:
                    continue
                raise ValueError(f"{key} 检查结构无效")
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                if domain:
                    continue
                raise ValueError(f"{key} 缺少判定理由")
            citations = item.get("citations")
            verified = locate_citations(citations, text)
            if verified is not None:
                locations[key] = verified
            needs_citation = item["value"] == "pass" and (domain or key in required)
            if verified is None and (needs_citation or citations):
                if item["value"] in ("pass", "not_applicable"):
                    item["value"] = "unknown"
                item["evidence"] += "；程序未能核实所引原文与行号。"
                invalid.append(key)
    if invalid and "INSUFFICIENT_EVIDENCE" not in out["reason_codes"]:
        out["reason_codes"].append("INSUFFICIENT_EVIDENCE")
    notices = [key for key in BASE_CHECK_NAMES if informational_unknown(key, out.get(key), rules)]
    blockers = [key for key in BASE_CHECK_NAMES
                if key in out and out[key]["value"] != "pass" and key not in notices]
    out["quality_audit"] = {"version": "3", "invalid_citations": invalid,
                            "verified_citations": locations, "informational_checks": notices,
                            "blocking_checks": blockers, "review_status": "not_required"}
    return out


def quality_summary(evaluation):
    """发布质量理由与评估状态，兼容历史两轮记录。"""
    audit = evaluation.get("quality_audit") or {}
    if not audit:
        return None
    if audit.get("version") == "3":
        informational = set(audit.get("informational_checks", []))
        return {
            "review_status": audit.get("review_status"),
            "checks": {key: {"value": evaluation[key]["value"], "evidence": evaluation[key]["evidence"],
                             "informational": key in informational}
                       for key in BASE_CHECK_NAMES if key in evaluation},
            "verification_note": evaluation.get("verification_note"),
            "blocking_reasons": [f"领域检查：{item.get('evidence', '')}"
                                 for item in (evaluation.get("domain_checks") or {}).values()
                                 if item.get("value") in ("fail", "unknown")],
        }
    review = audit.get("review") or {}
    reasons = []
    for label, source in (("评估" if audit.get("review_status") == "single_pass" else "初评", evaluation), ("复核", review)):
        for key in ("scope_match", "purpose_clarity", "instruction_completeness",
                    "evidence_traceability", "dependency_transparency", "risk_review"):
            item = source.get(key) or {}
            if item.get("value") in ("fail", "unknown"):
                reasons.append(f"{label}：{item.get('evidence', '')}")
        for item in (source.get("domain_checks") or {}).values():
            if item.get("value") in ("fail", "unknown"):
                reasons.append(f"{label}领域检查：{item.get('evidence', '')}")
    return {
        "review_status": audit.get("review_status"),
        "review_note": audit.get("review_note"),
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "checks": {key: {"value": item.get("value"), "evidence": item.get("evidence")}
                   for key, item in (evaluation.get("quality_checks") or {}).items()},
        "review_checks": {key: {"value": item.get("value"), "evidence": item.get("evidence")}
                          for key, item in (review.get("quality_checks") or {}).items()},
    }
