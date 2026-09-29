"""系统版本契约与兼容规则定义（落实 P1 设计规范）。

统一管理报告 Schema、输出契约、术语表、核验器与规范化算法的版本定义及兼容规则。
"""

from __future__ import annotations

import re
from typing import Any


# 核心契约与算法版本常量
FINDER_REPORT_SCHEMA_VERSION = "1.1.0"
LLM_OUTPUT_CONTRACT_VERSION = "1.0.0"
TERMINOLOGY_VERSION = "1.0.0"
EVIDENCE_VERIFIER_VERSION = "1.1.0"
NORMALIZATION_VERSION = "1.0.0"
STATIC_HEURISTIC_VERSION = "1.0.0"


def parse_version_tuple(version_str: str | None) -> tuple[int, ...]:
    """仅接受完整的 major.minor.patch；不猜测损坏或预发布版本。"""
    if not isinstance(version_str, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version_str):
        return ()
    return tuple(int(part) for part in version_str.split("."))


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

    # Evaluation identity requires equality; semver alone cannot prove equal rules.
    for key, expected, label in (
        ("rules_version", expected_rules_version, "评估规则版本不兼容"),
        ("model_config_version", expected_model_config_version, "模型配置版本不一致"),
        ("output_contract_version", expected_output_contract_version, "输出契约版本不兼容"),
    ):
        if expected is None:
            continue
        actual = record.get(key)
        if not isinstance(actual, str) or not actual:
            return False, f"缺失版本字段: {key}"
        if not isinstance(expected, str) or not expected or actual != expected:
            return False, f"{label}: 记录={actual}, 当前={expected}"

    return True, "兼容"


def get_git_commit_hash(root_dir: Any = None) -> str | None:
    """获取当前代码仓库的短 commit hash；如果不在 git 仓库或执行失败则返回 None。"""
    import subprocess
    try:
        cmd = ["git", "rev-parse", "--short", "HEAD"]
        cwd = str(root_dir) if root_dir is not None else None
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=2)
        if res.returncode == 0:
            val = res.stdout.strip()
            return val or None
    except Exception:
        pass
    return None


def build_config_fingerprint(cfg: dict | None) -> str:
    """生成脱敏的模型与运行配置指纹 (sha256 截断前 16 位)。
    
    严禁包含 api_key、token 等敏感凭据。
    """
    import hashlib
    import json
    if not isinstance(cfg, dict):
        return "sha256:empty"
    safe = {
        "model": cfg.get("model"),
        "endpoint": cfg.get("endpoint"),
        "limits": cfg.get("limits"),
        "request": {
            k: v for k, v in (cfg.get("request") or {}).items()
            if "key" not in k.lower() and "token" not in k.lower() and "auth" not in k.lower() and "secret" not in k.lower()
        },
    }
    raw = json.dumps(safe, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "FINDER_REPORT_SCHEMA_VERSION",
    "LLM_OUTPUT_CONTRACT_VERSION",
    "TERMINOLOGY_VERSION",
    "EVIDENCE_VERIFIER_VERSION",
    "NORMALIZATION_VERSION",
    "STATIC_HEURISTIC_VERSION",
    "parse_version_tuple",
    "is_semver_compatible",
    "check_evaluation_record_compatibility",
    "get_git_commit_hash",
    "build_config_fingerprint",
]
