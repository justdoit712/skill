"""系统版本契约与兼容规则定义（落实 P1 设计规范）。

统一管理报告 Schema、输出契约、术语表、核验器与规范化算法的版本定义及兼容规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# 核心契约与算法版本常量
FINDER_REPORT_SCHEMA_VERSION = "1.1.0"
LLM_OUTPUT_CONTRACT_VERSION = "1.0.0"
TERMINOLOGY_VERSION = "1.0.0"
EVIDENCE_VERIFIER_VERSION = "1.1.0"
NORMALIZATION_VERSION = "1.0.0"


def parse_version_tuple(version_str: str | None) -> tuple[int, ...]:
    """将语义化版本字符串解析为整型元组，无法解析的部分静默忽略。"""
    if not version_str or not isinstance(version_str, str):
        return ()
    parts = []
    for part in version_str.strip().split("."):
        clean = "".join(ch for ch in part if ch.isdigit())
        if clean:
            parts.append(int(clean))
        else:
            break
    return tuple(parts)


def is_semver_compatible(actual_version: str | None, expected_version: str | None) -> bool:
    """检查实际版本与预期版本是否向后兼容。

    规则：
    - 主版本号 (Major) 必须完全一致；
    - 实际版本 (Actual) 的次版本与补丁号必须 >= 预期版本 (Expected)。
    - 若任一版本无法解析，返回 False。
    """
    actual = parse_version_tuple(actual_version)
    expected = parse_version_tuple(expected_version)
    if not actual or not expected:
        return False
    # 主版本必须一致
    if actual[0] != expected[0]:
        return False
    # 实际版本不得低于要求的最低版本
    return actual >= expected


def check_evaluation_record_compatibility(
    record: dict[str, Any],
    *,
    expected_rules_version: str | None = None,
    expected_model_config_version: str | None = None,
    expected_output_contract_version: str | None = None,
) -> tuple[bool, str]:
    """检查已持久化的评估记录与当前期望的规则/模型契约是否兼容。

    不重写旧 evaluation_id，也不因只读新增字段强制废弃旧记录。
    缺失字段明确返回缺失原因，不盲目假设兼容。
    """
    if not isinstance(record, dict):
        return False, "记录格式非法，非字典结构"

    rec_rules = record.get("rules_version")
    if expected_rules_version and rec_rules != expected_rules_version:
        # 若主版本不一致则直接拒绝
        if not is_semver_compatible(str(rec_rules), str(expected_rules_version)):
            return False, f"评估规则版本不兼容: 记录={rec_rules}, 当前={expected_rules_version}"

    rec_model_v = record.get("model_config_version")
    if expected_model_config_version and rec_model_v and rec_model_v != expected_model_config_version:
        return False, f"模型配置版本不一致: 记录={rec_model_v}, 当前={expected_model_config_version}"

    rec_contract = record.get("output_contract_version")
    if expected_output_contract_version and rec_contract:
        if not is_semver_compatible(str(rec_contract), str(expected_output_contract_version)):
            return False, f"输出契约版本不兼容: 记录={rec_contract}, 当前={expected_output_contract_version}"

    return True, "兼容"


__all__ = [
    "FINDER_REPORT_SCHEMA_VERSION",
    "LLM_OUTPUT_CONTRACT_VERSION",
    "TERMINOLOGY_VERSION",
    "EVIDENCE_VERIFIER_VERSION",
    "NORMALIZATION_VERSION",
    "parse_version_tuple",
    "is_semver_compatible",
    "check_evaluation_record_compatibility",
]
