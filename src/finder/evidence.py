"""代码级客观证据核验模块（纯函数）。

严格防范大模型引文幻觉与支持合法行号邻近漂移恢复：
1. 协议对齐：source_path / quote / start_line / end_line
2. 严格排除布尔值行号，排除空或纯空白引文
3. 路径必须存在于已读取材料集合，行号不得越界
4. 引文文本经空白标准化后优先在指定行区间精确比对 (exact)
5. 精确核验失败后，在合法行号附近（默认 +-3 行）寻找唯一连续引文进行修复 (nearby_drift)
6. 严格拒绝歧义多处匹配、非法行号、文字删改与跨文件拼接
7. 纯函数设计，绝不修改传入的任何参数
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from src.shared.materials import DocumentSnapshot
from src.shared.versions import EVIDENCE_VERIFIER_VERSION

DEFAULT_MAX_DRIFT_LINES = 3


class EvidenceVerificationResult(NamedTuple):
    """单条引文证据核验结果。"""

    is_valid: bool
    verified_file: str
    start_line: int
    end_line: int
    matched_quote: str
    failure_reason: str = ""
    match_method: str = "none"  # "exact" | "nearby_drift" | "none"
    original_start_line: int = 0
    original_end_line: int = 0
    original_quote: str = ""
    failure_code: str = ""  # "file_not_found" | "invalid_line_type" | "out_of_bounds" | "quote_empty" | "text_mismatch" | "ambiguous_nearby_match" | "nearby_not_found" | "drift_exceeded"
    verifier_version: str = EVIDENCE_VERIFIER_VERSION


def _extract_material_text(materials: dict[str, Any], path: str) -> str | None:
    """从 materials 字典中提取文本（兼容 dict[str, str] 与 dict[str, DocumentSnapshot]）。"""
    item = materials.get(path)
    if item is None:
        return None
    if isinstance(item, DocumentSnapshot):
        return item.text
    if isinstance(item, str):
        return item
    if hasattr(item, "content"):
        return getattr(item, "content")
    return None


def verify_single_evidence(
    quote_claim: dict[str, Any],
    materials: dict[str, Any],
    *,
    allow_drift: bool = True,
    max_drift: int = DEFAULT_MAX_DRIFT_LINES,
) -> EvidenceVerificationResult:
    """结构化单条证据核验（纯函数）。

    两层核验策略：
    第一层：指定区间精确核验 (match_method="exact")；
    第二层：若精确比对失败且启用 allow_drift，在合法行号附近 +-max_drift 行内寻找唯一连续引文 (match_method="nearby_drift")。
    """
    spath = str(quote_claim.get("source_path") or "").strip()
    sline = quote_claim.get("start_line")
    eline = quote_claim.get("end_line")
    quote = str(quote_claim.get("quote") or "")

    orig_sline = sline if (isinstance(sline, int) and not isinstance(sline, bool)) else 0
    orig_eline = eline if (isinstance(eline, int) and not isinstance(eline, bool)) else 0

    text = _extract_material_text(materials, spath) if spath else None
    if not spath or text is None:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason=f"引用的文件未在已读取材料中找到: {spath}",
            match_method="none",
            original_start_line=orig_sline,
            original_end_line=orig_eline,
            original_quote=quote,
            failure_code="file_not_found",
        )

    # 严密防范 Python 中 isinstance(True, int) == True 的陷阱
    if isinstance(sline, bool) or not isinstance(sline, int):
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason="start_line 必须为非布尔整数",
            match_method="none",
            original_start_line=orig_sline,
            original_end_line=orig_eline,
            original_quote=quote,
            failure_code="invalid_line_type",
        )

    if isinstance(eline, bool) or not isinstance(eline, int):
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason="end_line 必须为非布尔整数",
            match_method="none",
            original_start_line=orig_sline,
            original_end_line=orig_eline,
            original_quote=quote,
            failure_code="invalid_line_type",
        )

    clean_quote = quote.strip()
    if not clean_quote:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason="quote 引文内容不能为空或纯空白",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="quote_empty",
        )

    lines = text.splitlines()
    total_lines = len(lines)

    if not (1 <= sline <= eline <= total_lines):
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason=f"行号范围越界: [{sline}, {eline}]，文件共 {total_lines} 行",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="out_of_bounds",
        )

    # 第一层：指定行号区间内的实际文本精确比对
    target_block = " ".join(lines[sline - 1 : eline])
    normalized_block = re.sub(r"\s+", " ", target_block)
    normalized_quote = re.sub(r"\s+", " ", clean_quote)

    if normalized_quote in normalized_block:
        return EvidenceVerificationResult(
            is_valid=True,
            verified_file=spath,
            start_line=sline,
            end_line=eline,
            matched_quote=quote,
            failure_reason="核验通过",
            match_method="exact",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="",
        )

    # 精确匹配未通过，检查是否允许邻近容错漂移修复
    if not allow_drift or max_drift <= 0:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason="指定行号区间内未找到完整匹配的引文内容",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="text_mismatch",
        )

    # 第二层：邻近有限窗口检索 (原起止行前后各不超过 max_drift 行)
    w_start = max(1, sline - max_drift)
    w_end = min(total_lines, eline + max_drift)

    # 在窗口内寻找所有极小匹配区间 (minimal matching spans)
    minimal_matches: list[tuple[int, int]] = []
    for cand_s in range(w_start, w_end + 1):
        for cand_e in range(cand_s, w_end + 1):
            cand_slice = " ".join(lines[cand_s - 1 : cand_e])
            norm_cand = re.sub(r"\s+", " ", cand_slice)
            if normalized_quote in norm_cand:
                # 检查极小性：去除首行或尾行后是否仍包含完整引文
                is_minimal = True
                if cand_s < cand_e:
                    slice_no_head = " ".join(lines[cand_s : cand_e])
                    if normalized_quote in re.sub(r"\s+", " ", slice_no_head):
                        is_minimal = False
                    slice_no_tail = " ".join(lines[cand_s - 1 : cand_e - 1])
                    if normalized_quote in re.sub(r"\s+", " ", slice_no_tail):
                        is_minimal = False
                if is_minimal:
                    minimal_matches.append((cand_s, cand_e))
                # 对于确定的 cand_s，首次包含引文的 cand_e 之后更大 cand_e 必然不是极小的
                break

    if not minimal_matches:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason="指定行号或邻近行号区间内未找到完整匹配的引文内容",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="nearby_not_found",
        )

    if len(minimal_matches) > 1:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason=f"邻近区间存在多处完全相同的引文候选（共 {len(minimal_matches)} 处），存在位置歧义",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="ambiguous_nearby_match",
        )

    actual_s, actual_e = minimal_matches[0]
    drift_s = abs(actual_s - sline)
    drift_e = abs(actual_e - eline)

    if drift_s > max_drift or drift_e > max_drift:
        return EvidenceVerificationResult(
            is_valid=False,
            verified_file="",
            start_line=0,
            end_line=0,
            matched_quote="",
            failure_reason=f"匹配位置 [{actual_s}, {actual_e}] 超出允许的最大漂移范围（最大允许 {max_drift} 行）",
            match_method="none",
            original_start_line=sline,
            original_end_line=eline,
            original_quote=quote,
            failure_code="drift_exceeded",
        )

    # 邻近修复成功：记录实际行号与修复方式
    return EvidenceVerificationResult(
        is_valid=True,
        verified_file=spath,
        start_line=actual_s,
        end_line=actual_e,
        matched_quote=quote,
        failure_reason="邻近位置修复成功",
        match_method="nearby_drift",
        original_start_line=sline,
        original_end_line=eline,
        original_quote=quote,
        failure_code="",
    )


def verify_evidence_snippet(
    source_path: str,
    start_line: Any,
    end_line: Any,
    quote: str,
    materials: dict[str, Any],
    *,
    allow_drift: bool = False,
    max_drift: int = DEFAULT_MAX_DRIFT_LINES,
) -> tuple[bool, str]:
    """严格核验单条引文证据，返回 (是否合法, 原因说明)。"""
    claim = {
        "source_path": source_path,
        "start_line": start_line,
        "end_line": end_line,
        "quote": quote,
    }
    res = verify_single_evidence(
        claim,
        materials,
        allow_drift=allow_drift,
        max_drift=max_drift,
    )
    return res.is_valid, res.failure_reason


__all__ = [
    "DEFAULT_MAX_DRIFT_LINES",
    "EvidenceVerificationResult",
    "verify_evidence_snippet",
    "verify_single_evidence",
]
