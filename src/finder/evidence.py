"""代码级客观证据核验模块（纯函数）。

严格防范大模型引文幻觉：
1. 协议对齐：source_path / quote / start_line / end_line
2. 严格排除布尔值行号，排除空或纯空白引文
3. 路径必须存在于已读取材料集合，行号不得越界
4. 引文文本经空白标准化后必须完全属于指定行区间
5. 纯函数设计，绝不修改传入的任何参数
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, NamedTuple

from src.shared.materials import DocumentSnapshot


class EvidenceVerificationResult(NamedTuple):
    """单条引文证据核验结果。"""
    is_valid: bool
    verified_file: str
    start_line: int
    end_line: int
    matched_quote: str
    failure_reason: str = ""


def _extract_material_text(materials: dict[str, Any], path: str) -> str | None:
    """从 materials 字典中提取文本（兼容 dict[str, str] 与 dict[str, DocumentSnapshot]）。"""
    item = materials.get(path)
    if item is None:
        return None
    if isinstance(item, DocumentSnapshot):
        return item.content
    if isinstance(item, str):
        return item
    if hasattr(item, "content"):
        return getattr(item, "content")
    return None


def verify_evidence_snippet(
    source_path: str,
    start_line: Any,
    end_line: Any,
    quote: str,
    materials: dict[str, Any],
) -> tuple[bool, str]:
    """严格核验单条引文证据：
    1. 字段对齐实际输出协议：source_path / quote / start_line / end_line
    2. 严格类型检查：行号必须是 int 且绝不能为 bool（bool 是 int 子类）
    3. 引文文本不能为空或纯空白
    4. 路径必须完全一致（规范化路径，不允许多余前缀或跨文件）
    5. 行号必须在材料有效行范围内（1 <= start_line <= end_line <= total_lines）
    6. 行号区间内必须精确包含引文文本（支持换行与连续空白标准化，但不允许删改文字）
    """
    clean_path = (source_path or "").strip()
    text = _extract_material_text(materials, clean_path)
    if not clean_path or text is None:
        return False, f"引用的文件未在已读取材料中找到: {clean_path}"

    # 严密防范 Python 中 isinstance(True, int) == True 的陷阱
    if isinstance(start_line, bool) or not isinstance(start_line, int):
        return False, "start_line 必须为非布尔整数"
    if isinstance(end_line, bool) or not isinstance(end_line, int):
        return False, "end_line 必须为非布尔整数"

    clean_quote = (quote or "").strip()
    if not clean_quote:
        return False, "quote 引文内容不能为空或纯空白"

    lines = text.splitlines()
    total_lines = len(lines)

    if not (1 <= start_line <= end_line <= total_lines):
        return False, f"行号范围越界: [{start_line}, {end_line}]，文件共 {total_lines} 行"

    # 提取行号区间内的实际文本并规范化
    target_block = " ".join(lines[start_line - 1 : end_line])
    normalized_block = re.sub(r"\s+", " ", target_block)
    normalized_quote = re.sub(r"\s+", " ", clean_quote)

    if normalized_quote not in normalized_block:
        return False, "指定行号区间内未找到完整匹配的引文内容"

    return True, "核验通过"


def verify_single_evidence(
    quote_claim: dict[str, Any],
    materials: dict[str, Any],
) -> EvidenceVerificationResult:
    """结构化单条证据核验（纯函数）。"""
    spath = str(quote_claim.get("source_path") or "").strip()
    sline = quote_claim.get("start_line")
    eline = quote_claim.get("end_line")
    quote = str(quote_claim.get("quote") or "")

    ok, reason = verify_evidence_snippet(spath, sline, eline, quote, materials)
    return EvidenceVerificationResult(
        is_valid=ok,
        verified_file=spath if ok else "",
        start_line=int(sline) if (ok and isinstance(sline, int) and not isinstance(sline, bool)) else 0,
        end_line=int(eline) if (ok and isinstance(eline, int) and not isinstance(eline, bool)) else 0,
        matched_quote=quote if ok else "",
        failure_reason=reason if not ok else "",
    )


__all__ = [
    "EvidenceVerificationResult",
    "verify_evidence_snippet",
    "verify_single_evidence",
]
