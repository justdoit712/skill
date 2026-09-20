"""决策：把六维评估输出映射为 recommended / candidate / excluded（§5.2）。

本模块是纯函数，不调用模型，因此可以用固定评估输出直接测试——§5.2 明确要求
"测试决策逻辑使用固定评估输出；真实模型另做有限校准"。

判据全部来自 config/rules.json，不在代码里另写一份阈值。
"""

from __future__ import annotations

DECISION_RECOMMENDED = "recommended"
DECISION_CANDIDATE = "candidate"
DECISION_EXCLUDED = "excluded"
DECISION_PROCESSING_FAILURE = "processing_failure"

# 领域专项检查里视为"不阻塞"的取值：§5.2 规定没有回测功能时记为不适用
NON_BLOCKING_DOMAIN_VALUES = frozenset(
    {"pass", "n/a", "na", "not_applicable", "不适用", "not-applicable"}
)


def _value_of(entry) -> str:
    """检查项取值。支持 {"value": ..., "evidence": ...} 与裸字符串两种写法。"""
    if isinstance(entry, dict):
        return str(entry.get("value", "")).strip().lower()
    return str(entry).strip().lower()


def _evidence_of(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("evidence") or entry.get("source") or "").strip()
    return ""


def decide(evaluation: dict, rules: dict) -> dict:
    """给出决策。

    返回 {"decision", "reason_codes", "blocking_checks", "values", "notes"}。
    """
    out: dict = {
        "decision": DECISION_CANDIDATE,
        "reason_codes": [str(c) for c in (evaluation.get("reason_codes") or [])],
        "blocking_checks": [],
        "values": {},
        "notes": [],
    }

    schema = rules.get("output_schema", {})
    required = list(schema.get("required_fields", []))
    missing = [field for field in required if field not in evaluation]
    if missing:
        out["decision"] = DECISION_PROCESSING_FAILURE
        out["notes"].append("输出结构无效，缺字段：" + "、".join(missing))
        return out

    codes = out["reason_codes"]
    reason_codes = rules.get("reason_codes", {})
    processing = set(reason_codes.get("processing_failure", {}))
    exclusion = set(reason_codes.get("exclusion", {}))

    if processing.intersection(codes):
        out["decision"] = DECISION_PROCESSING_FAILURE
        out["notes"].append("处理失败不当作质量判定（§5.2）")
        return out

    hard = [c for c in codes if c in exclusion]
    if hard:
        out["decision"] = DECISION_EXCLUDED
        out["notes"].append("命中硬性拒绝项：" + "、".join(hard))
        return out

    checks = [c["id"] for c in rules.get("checks", [])]
    values = {cid: _value_of(evaluation.get(cid)) for cid in checks}
    out["values"] = values

    not_pass = [cid for cid, value in values.items() if value != "pass"]
    if not_pass:
        out["decision"] = DECISION_CANDIDATE
        out["blocking_checks"] = not_pass
        out["notes"].append("存在未通过的检查项，留在候选区并注明原因")
        return out

    no_evidence = [cid for cid in checks if not _evidence_of(evaluation.get(cid))]
    if no_evidence:
        out["decision"] = DECISION_CANDIDATE
        out["blocking_checks"] = no_evidence
        out["notes"].append("缺少可定位证据；不得用模型自报置信度替代证据")
        return out

    domain_checks = evaluation.get("domain_checks") or {}
    failing_domains = [
        key for key, entry in domain_checks.items() if _value_of(entry) not in NON_BLOCKING_DOMAIN_VALUES
    ]
    if failing_domains:
        out["decision"] = DECISION_CANDIDATE
        out["blocking_checks"] = failing_domains
        out["notes"].append("领域专项检查未全部通过")
        return out

    out["decision"] = DECISION_RECOMMENDED
    return out
