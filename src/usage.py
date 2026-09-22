"""按接口实际返回的 usage 汇总；推理 Token 属于输出的一部分，不重复相加。

已迁移至 src.shared.usage，本模块保持完全向后兼容重导出。
"""

from __future__ import annotations

from src.shared.usage import UsageTotals, _number

__all__ = ["UsageTotals", "_number"]
