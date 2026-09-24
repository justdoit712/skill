"""普通目录的质量标准与原文校验；不访问网络或调用模型。"""

from copy import deepcopy


QUALITY_CHECKS = {
    "practical_value": "实际价值：指出具体任务、交付物和相对泛泛提示词的增量；宣传语、角色扮演和常识堆砌不足以通过。",
    "actionability": "可执行性：步骤、判断条件或可直接使用的模板/参考内容足够具体；按技能形态判断，不强求纯文本技能附带代码。",
    "verification": "结果验证：提供可检查的输出要求、示例、检查清单、测试或失败处理；简单任务可用清晰验收条件，不强求复杂测试框架。",
}


def enabled(rules):
    return (rules.get("quality_review") or {}).get("enabled") is True


def prompt_instructions():
    return "\n".join([
        "深度质量评估：先识别技能形态和实际任务，再检查可执行步骤、产出及验证方式。",
        "不要按篇幅、Star、作者知名度或术语数量打分；短而具体的技能可以通过。",
        "材料引用的文件未提供时，不得假设已读取或证明其功能；证据不足用 unknown。",
        "quality_checks 必须包含以下三个对象，各含 value（pass/fail/unknown）、evidence（判定理由）、citations：",
        *[f"- {key}: {description}" for key, description in QUALITY_CHECKS.items()],
        "所有基础检查、领域检查和质量检查都增加 citations 数组。",
        '每条引用格式：{"start_line": 1, "end_line": 2, "quote": "两行完整原文"}。',
        "行号来自下方 SKILL.md 的编号；quote 不包含编号，必须逐字复制完整连续行，每条最多 20 行，最多 3 条。",
        "pass 必须有至少一条支持该判定的引用；fail/unknown 可用空数组，并解释缺少什么。",
        "risk_review 的 pass 仅指所提供材料未见明显风险，不代表完整安全审计。",
        "依赖的缺席不证明无依赖；仅有免责声明不证明功能可靠。",
    ])


def verified_citations(citations, text):
    if not isinstance(citations, list) or not 1 <= len(citations) <= 3:
        return False
    lines = text.splitlines()
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        start, end, quote = (citation.get(k) for k in ("start_line", "end_line", "quote"))
        if type(start) is not int or type(end) is not int:
            return False
        if not 1 <= start <= end <= len(lines) or end - start >= 20:
            return False
        if not isinstance(quote, str) or not quote.strip():
            return False
        if quote.replace("\r\n", "\n") != "\n".join(lines[start - 1:end]):
            return False
    return True


def check_quality(evaluation, text, rules):
    """复制评估，降级没有真实引用的通过项，并把质量缺口映射到基础门槛。"""
    out = deepcopy(evaluation)
    quality = out.get("quality_checks")
    if not isinstance(quality, dict) or set(quality) != set(QUALITY_CHECKS):
        raise ValueError("quality_checks 必须完整且精确覆盖三项质量标准")
    groups = [(out, [c["id"] for c in rules.get("checks", [])]),
              (out.get("domain_checks") or {}, list((out.get("domain_checks") or {}).keys())),
              (quality, list(QUALITY_CHECKS))]
    invalid = []
    for group, keys in groups:
        for key in keys:
            item = group.get(key)
            if not isinstance(item, dict) or item.get("value") not in ("pass", "fail", "unknown", "not_applicable"):
                raise ValueError(f"{key} 检查结构无效")
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                raise ValueError(f"{key} 缺少判定理由")
            if key in QUALITY_CHECKS and item["value"] == "not_applicable":
                raise ValueError(f"{key} 不允许跳过质量判断")
            if item["value"] in ("pass", "not_applicable") and not verified_citations(item.get("citations"), text):
                item["value"] = "unknown"
                item["evidence"] += "；程序未能核实所引原文与行号。"
                invalid.append(key)
    blockers = [key for key, item in quality.items() if item["value"] != "pass"]
    if blockers:
        out["instruction_completeness"] = {
            "value": "unknown", "evidence": "质量门槛未通过：" + "；".join(
                f"{key}: {quality[key]['evidence']}" for key in blockers), "citations": [],
        }
        if "QUALITY_BELOW_BAR" not in out["reason_codes"]:
            out["reason_codes"].append("QUALITY_BELOW_BAR")
    if invalid and "INSUFFICIENT_EVIDENCE" not in out["reason_codes"]:
        out["reason_codes"].append("INSUFFICIENT_EVIDENCE")
    out["quality_audit"] = {"version": "1", "invalid_citations": invalid,
                            "blocking_checks": blockers, "review_status": "not_required"}
    return out


def hold_for_review(evaluation, status, explanation):
    out = deepcopy(evaluation)
    out["evidence_traceability"] = {"value": "unknown", "evidence": explanation, "citations": []}
    out["quality_audit"]["review_status"] = status
    out["quality_audit"]["review_note"] = explanation
    if "NEEDS_VERIFICATION" not in out["reason_codes"]:
        out["reason_codes"].append("NEEDS_VERIFICATION")
    return out


def quality_summary(evaluation):
    """仅发布质量理由和复核状态，完整两轮记录留在评估账本。"""
    audit = evaluation.get("quality_audit") or {}
    if not audit:
        return None
    review = audit.get("review") or {}
    reasons = []
    for label, source in (("初评", evaluation), ("复核", review)):
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
