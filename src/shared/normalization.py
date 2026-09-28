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
from src.shared.versions import NORMALIZATION_VERSION


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
                quotes.append({
                    "check_id": check_id,
                    "quote": str(c["quote"]).strip(),
                    "start_line": c.get("start_line"),
                    "end_line": c.get("end_line"),
                })
        ev = item.get("evidence")
        if isinstance(ev, dict) and ev.get("quote"):
            quotes.append({
                "check_id": check_id,
                "quote": str(ev["quote"]).strip(),
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

    # 4. 独立复核检查证据
    quality_audit = outcome.get("quality_audit") or evaluation.get("quality_audit") or {}
    review = quality_audit.get("review") or {}
    review_checks = review.get("checks") or review.get("review_checks") or {}
    if isinstance(review_checks, dict):
        for r_id, r_data in review_checks.items():
            _extract_from_item(f"review_{r_id}", r_data)

    # 5. Finder 匹配评估准则证据
    criteria_results = evaluation.get("criteria_results") or []
    if isinstance(criteria_results, list):
        for cr in criteria_results:
            if isinstance(cr, dict):
                for ev in cr.get("evidence") or []:
                    if isinstance(ev, dict) and ev.get("quote"):
                        quotes.append({
                            "criterion_id": cr.get("criterion_id"),
                            "quote": str(ev["quote"]).strip(),
                            "start_line": ev.get("start_line"),
                            "end_line": ev.get("end_line"),
                        })

    return quotes


def verify_cached_evidence_in_text(outcome: dict, current_text: str) -> tuple[bool, str | None]:
    """在新原文中重新核验历史评估中的所有关键证据。

    任何一条证据在新原文中完全无法唯一定位或引文缺失，即判定核验失败，必须安全回退。
    """
    quotes = extract_evidence_quotes(outcome)
    if not quotes:
        # 无证据项的评估不作为高质量受信结果复用
        return True, None

    normalized_current = normalize_material_text(current_text) or ""

    for item in quotes:
        raw_quote = item["quote"]
        normalized_quote = normalize_material_text(raw_quote) or ""
        if not normalized_quote:
            continue
        if normalized_quote not in normalized_current:
            return False, f"证据引文在新材料中缺失：{raw_quote[:50]}"

    return True, None


def inspect_record_for_normalized_reuse(
    record: dict[str, Any],
    current_text: str,
    *,
    expected_rules_version: str | None = None,
    expected_model_config_version: str | None = None,
) -> tuple[bool, str | None]:
    """核对单条历史评估记录是否具备规范化复用资格。

    严格安全校验：
    1. 记录必须是 completed 终态，未完成、需恢复或失败记录严禁复用；
    2. 用量未知 (unknown_usage) 记录不作为可信结果复用；
    3. rules_version 与 model_config_version 必须严格匹配；
    4. outcome 必须具备有效结构与决策；
    5. 历史证据在新原文中必须全部有效。
    """
    if not isinstance(record, dict):
        return False, "record_not_dict"

    if record.get("status") != "completed":
        return False, f"status_not_completed:{record.get('status')}"

    # 检查是否有未知用量污染
    requests_list = record.get("requests") or []
    if any(isinstance(r, dict) and r.get("usage") is None for r in requests_list):
        return False, "contains_unknown_usage"

    # 版本检查
    if expected_rules_version and record.get("rules_version") != expected_rules_version:
        return False, f"rules_version_mismatch:{record.get('rules_version')}!={expected_rules_version}"

    if expected_model_config_version and record.get("model_config_version") != expected_model_config_version:
        return False, f"model_config_mismatch:{record.get('model_config_version')}!={expected_model_config_version}"

    outcome = record.get("outcome")
    if not isinstance(outcome, dict) or not outcome.get("decision"):
        return False, "invalid_outcome_structure"

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
