"""受限规范化缓存与复用审计纯函数模块 (Unit 9 / P4)。

架构原则：
1. 首版仅允许经过明确验证的换行编码规范化（CRLF / CR -> LF）。
2. 不自动合并空行、不清理缩进、不删除注释、不改变链接目标、不使用 AST 推断语义等价。
3. 严格两阶段控制：默认运行观察模式（记录潜在命中与拒绝原因），开启后方可安全复用。
4. 证据在新原文中必须严格重新核验通过，否则坚决回退至重新评估。
5. 缓存命中不伪造模型调用或 Token，记录独立复用审计事实。
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.shared.runtime import now_local
from src.shared.versions import LLM_OUTPUT_CONTRACT_VERSION, NORMALIZATION_VERSION


def normalize_material_text(text: str | None) -> str | None:
    """受限换行编码规范化（纯函数）。

    首版严格约束：
    - 仅将 Windows CRLF (`\r\n`) 与旧式 Mac CR (`\r`) 统一转换为标准 LF (`\n`)；
    - 绝对不合并空行、不去除缩进、不删除注释、不重排链接；
    - 增加空行、缩进改动或内容变更会导致规范化指纹不同，安全回退。
    """
    if text is None:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalized_content_fingerprint(text: str | None) -> str | None:
    """计算受限规范化辅助指纹。

    不同换行符（如 Windows 检出的 CRLF 与 Linux 检出的 LF）产出相同指纹；
    任何增减空行、修改空格或正文变动产出不同指纹。
    """
    if text is None:
        return None
    normalized = normalize_material_text(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"sha256:norm:v1:{digest}"


def extract_evidence_quotes(outcome: dict) -> list[dict[str, Any]]:
    """从评估结果中提取全部待核验的证据条目（兼顾目录与 Finder）。"""
    quotes: list[dict[str, Any]] = []
    if not isinstance(outcome, dict):
        return quotes

    evaluation = outcome.get("evaluation") or outcome

    def _extract_from_item(check_id: str, item: Any) -> None:
        if not isinstance(item, dict):
            return
        for c in item.get("citations") or []:
            if isinstance(c, dict) and c.get("quote"):
                q_text = str(c["quote"]).strip("\r\n")
                if q_text.strip():
                    quotes.append({
                        "check_id": check_id,
                        "quote": q_text,
                        "start_line": c.get("start_line"),
                        "end_line": c.get("end_line"),
                    })
        ev = item.get("evidence")
        if isinstance(ev, dict) and ev.get("quote"):
            q_text = str(ev["quote"]).strip("\r\n")
            if q_text.strip():
                quotes.append({
                    "check_id": check_id,
                    "quote": q_text,
                    "start_line": ev.get("start_line"),
                    "end_line": ev.get("end_line"),
                })

    # 1. 结构化 checks 字典
    checks = evaluation.get("checks")
    if isinstance(checks, dict):
        for cid, cdata in checks.items():
            _extract_from_item(cid, cdata)

    # 2. 直接位于 evaluation 上的六维检查
    for dim in ("scope_match", "purpose_clarity", "instruction_completeness",
                "evidence_traceability", "dependency_transparency", "risk_review"):
        _extract_from_item(dim, evaluation.get(dim))

    # 3. 质量门槛检查 (quality_checks)
    quality_checks = evaluation.get("quality_checks")
    if isinstance(quality_checks, dict):
        for qk, qv in quality_checks.items():
            _extract_from_item(f"quality_{qk}", qv)

    # 3.1 领域专项检查 (domain_checks)
    domain_checks = evaluation.get("domain_checks") or outcome.get("domain_checks")
    if isinstance(domain_checks, dict):
        for dk, dv in domain_checks.items():
            _extract_from_item(f"domain_{dk}", dv)

    # 4. 独立复核检查证据（生产复核使用顶层六维检查项、quality_checks 与 domain_checks）
    quality_audit = (
        outcome.get("quality_audit")
        or evaluation.get("quality_audit")
        or (outcome.get("evaluation") or {}).get("quality_audit")
        or {}
    )
    review = quality_audit.get("review")
    if isinstance(review, dict):
        for dim in ("scope_match", "purpose_clarity", "instruction_completeness",
                    "evidence_traceability", "dependency_transparency", "risk_review"):
            _extract_from_item(f"review_{dim}", review.get(dim))
        review_qc = review.get("quality_checks")
        if isinstance(review_qc, dict):
            for qk, qv in review_qc.items():
                _extract_from_item(f"review_quality_{qk}", qv)
        review_dc = review.get("domain_checks")
        if isinstance(review_dc, dict):
            for dk, dv in review_dc.items():
                _extract_from_item(f"review_domain_{dk}", dv)
        for key in ("checks", "review_checks"):
            c_dict = review.get(key)
            if isinstance(c_dict, dict):
                for r_id, r_data in c_dict.items():
                    _extract_from_item(f"review_{r_id}", r_data)

    # 5. Finder 匹配评估准则证据
    criteria_results = evaluation.get("criteria_results") or []
    if isinstance(criteria_results, list):
        for cr in criteria_results:
            if isinstance(cr, dict):
                for ev in cr.get("evidence") or []:
                    if isinstance(ev, dict) and ev.get("quote"):
                        q_text = str(ev["quote"]).strip("\r\n")
                        if q_text.strip():
                            quotes.append({
                                "criterion_id": cr.get("criterion_id"),
                                "quote": q_text,
                                "start_line": ev.get("start_line"),
                                "end_line": ev.get("end_line"),
                            })

    return quotes


def verify_cached_evidence_in_text(outcome: dict, current_text: str) -> tuple[bool, str | None]:
    """在新原文中重新核验历史评估中的所有关键证据。

    任何一条证据在新原文中完全无法唯一定位、超出边界或引文缺失，即判定核验失败，必须安全回退。
    无有效证据引用的评估不得作为可信结果复用。证据行号必须为非布尔整数，非法类型直接拒绝。
    """
    quotes = extract_evidence_quotes(outcome)
    if not quotes:
        # 无证据项的评估不作为高质量受信结果复用
        return False, "缺少有效证据引用，不可复用"

    normalized_current = normalize_material_text(current_text) or ""
    lines = normalized_current.splitlines()
    if not lines:
        return False, "材料正文为空"

    for item in quotes:
        raw_quote = item.get("quote") or ""
        normalized_quote = normalize_material_text(raw_quote) or ""
        if not normalized_quote.strip():
            return False, "证据引文为空"

        raw_quoted_lines = normalized_quote.splitlines()
        while raw_quoted_lines and not raw_quoted_lines[0]:
            raw_quoted_lines.pop(0)
        while raw_quoted_lines and not raw_quoted_lines[-1]:
            raw_quoted_lines.pop()

        if not raw_quoted_lines:
            return False, "证据引文为空"
        quoted_lines = raw_quoted_lines

        matches = [
            i for i in range(len(lines) - len(quoted_lines) + 1)
            if lines[i:i + len(quoted_lines)] == quoted_lines
        ]
        if not matches:
            return False, f"证据引文在新材料中缺失：{raw_quote[:50]}"

        start_line = item.get("start_line")
        end_line = item.get("end_line")

        # 严格非布尔整数类型校验，防止布尔值、字符串或缺失行号绕过校验
        if type(start_line) is not int or type(end_line) is not int:
            return False, f"证据行号类型非法（必须为非布尔整数）: start_line={start_line!r}, end_line={end_line!r}"

        if not (1 <= start_line <= end_line <= len(lines)):
            return False, f"证据行号超出材料行数边界：[{start_line}, {end_line}] (总行数 {len(lines)})"

        exact = (start_line - 1 in matches) and (end_line - start_line + 1 == len(quoted_lines))
        nearby = [
            i for i in matches
            if abs(i + 1 - start_line) <= 3 and abs(i + len(quoted_lines) - end_line) <= 3
        ]
        if not exact and len(nearby) != 1 and len(matches) != 1:
            return False, f"证据引文在材料中存在多处匹配且定位不明确：{raw_quote[:50]}"

    return True, None


def inspect_record_for_normalized_reuse(
    record: dict[str, Any],
    current_text: str,
    *,
    expected_rules_version: str | None = None,
    expected_model_config_version: str | None = None,
    expected_contract_version: str | None = None,
    expected_normalization_version: str | None = None,
) -> tuple[bool, str | None]:
    """核对单条历史评估记录是否具备规范化复用资格。

    严格安全校验：
    1. 记录必须是 completed 终态，未完成、待复核、需恢复或失败记录严禁复用；
    2. 用量未知 (unknown_usage) 或 total_tokens 为 null/非法记录不作为可信结果复用；
    3. rules_version、model_config_version、contract_version 与 normalization_version 必须全部齐全且严格匹配；
    4. outcome 必须具备有效结构与决策，且全面排查 outcome/evaluation/quality_audit 中的待复核状态；
    5. 历史证据在新原文中必须全部经过行号与唯一性核验。
    """
    if not isinstance(record, dict):
        return False, "record_not_dict"

    if record.get("status") != "completed":
        return False, f"status_not_completed:{record.get('status')}"

    # 严禁放行待复核或待初评态（顶层检查）
    if record.get("pending_evaluation") is not None or record.get("pending_review") is not None or record.get("needs_review"):
        return False, "pending_review_or_evaluation"

    if record.get("stage") in ("pending_review", "pending_evaluation"):
        return False, f"status_pending_stage:{record.get('stage')}"

    # 检查是否有未知用量污染 (包括 total_tokens 为 null / 非整数)
    requests_list = record.get("requests")
    if not isinstance(requests_list, list) or not requests_list:
        return False, "contains_unknown_usage"

    for r in requests_list:
        if not isinstance(r, dict):
            return False, "contains_unknown_usage"
        usage = r.get("usage")
        if usage is None or not isinstance(usage, dict):
            return False, "contains_unknown_usage"
        tokens = usage.get("total_tokens")
        if tokens is None or type(tokens) is not int or tokens < 0:
            return False, "contains_unknown_usage"

    # 版本检查（版本必须齐全且严格匹配，缺失字段不可假定兼容）
    if expected_rules_version:
        rec_rules = record.get("rules_version")
        if not rec_rules or rec_rules != expected_rules_version:
            return False, f"rules_version_mismatch:{rec_rules}!={expected_rules_version}"

    if expected_model_config_version:
        rec_model = record.get("model_config_version")
        if not rec_model or rec_model != expected_model_config_version:
            return False, f"model_config_mismatch:{rec_model}!={expected_model_config_version}"

    # 契约版本检查（必须存在且严格一致）
    exp_contract = expected_contract_version or LLM_OUTPUT_CONTRACT_VERSION
    rec_contract = record.get("output_contract_version") or record.get("contract_version")
    if not rec_contract or rec_contract != exp_contract:
        return False, f"contract_version_mismatch:{rec_contract}!={exp_contract}"

    # 规范化版本检查（必须存在且严格一致）
    exp_norm = expected_normalization_version or NORMALIZATION_VERSION
    rec_norm = record.get("normalization_version")
    if not rec_norm or rec_norm != exp_norm:
        return False, f"normalization_version_mismatch:{rec_norm}!={exp_norm}"

    outcome = record.get("outcome")
    if not isinstance(outcome, dict) or not outcome.get("decision"):
        return False, "invalid_outcome_structure"

    # 深度排查待初评与待复核状态（覆盖 record、outcome、evaluation、quality_audit 多层级）
    evaluation = outcome.get("evaluation") if isinstance(outcome.get("evaluation"), dict) else outcome
    quality_audit = (
        outcome.get("quality_audit")
        or evaluation.get("quality_audit")
        or {}
    )
    rev_status = (
        outcome.get("review_status")
        or evaluation.get("review_status")
        or (quality_audit.get("review_status") if isinstance(quality_audit, dict) else None)
    )
    if rev_status in ("pending_review", "needs_review", "pending", "disagreed"):
        return False, f"pending_review_status:{rev_status}"

    if outcome.get("pending_evaluation") is not None or evaluation.get("pending_evaluation") is not None:
        return False, "pending_review_or_evaluation"

    # 重新核验证据
    ev_ok, ev_err = verify_cached_evidence_in_text(outcome, current_text)
    if not ev_ok:
        return False, f"evidence_verification_failed:{ev_err}"

    return True, None


def create_reuse_audit(
    source_record: dict[str, Any],
    current_text: str,
    *,
    exact_match: bool = False,
) -> dict[str, Any]:
    """构建不可篡改的复用审计记录。"""
    return {
        "source_evaluation_id": source_record.get("evaluation_id"),
        "reused_at": now_local().isoformat(),
        "normalization_version": NORMALIZATION_VERSION,
        "exact_fingerprint_match": exact_match,
        "normalized_fingerprint": normalized_content_fingerprint(current_text),
        "status": "reused",
    }


__all__ = [
    "NORMALIZATION_VERSION",
    "normalize_material_text",
    "normalized_content_fingerprint",
    "extract_evidence_quotes",
    "verify_cached_evidence_in_text",
    "inspect_record_for_normalized_reuse",
    "create_reuse_audit",
]
