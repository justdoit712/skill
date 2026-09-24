"""定向查找配置加载与参数验证模块。

处理模型配置、本地运行配置加载、优先级合并与数值规范化。
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from src.infra.files import read_json
from src.infra.llm import validate_model_config

DEFAULT_LIMIT = 5
DEFAULT_MAX_EVALUATIONS = 20
DEFAULT_MAX_TOKENS = 200000

MAX_REPOS_TO_EXPAND = 20
MAX_SEARCH_REPOS_PER_QUERY = 20
MAX_FILES_PER_REPO = 10
MAX_TOTAL_FILES_TO_FETCH = 80
MAX_PRIMARY_FILE_BYTES = 65536
MAX_TOTAL_MATERIAL_BYTES = 98304
MAX_REFERENCED_FILES = 2

PLAN_MAX_OUTPUT_TOKENS = 4000
EVAL_MAX_OUTPUT_TOKENS = 10000


def _read_json_file(path: Path, default=None) -> Any:
    return read_json(path, default=default)


def load_finder_model_config(config_dir: str | Path = "config") -> dict[str, Any]:
    """读取模型配置，仅加载 model.local.json 或 model.example.json。"""
    base = Path(config_dir)
    def _res(fname: str, sub: str) -> Path:
        sub_p = base / sub / fname
        flat_p = base / fname
        if flat_p.exists() and sub_p.exists():
            try:
                return flat_p if flat_p.stat().st_mtime >= sub_p.stat().st_mtime else sub_p
            except OSError:
                return flat_p
        if flat_p.exists():
            return flat_p
        return sub_p

    local_cfg = _res("model.local.json", "models")
    example_cfg = _res("model.example.json", "models")

    if local_cfg.exists():
        cfg = _read_json_file(local_cfg)
    elif example_cfg.exists():
        cfg = _read_json_file(example_cfg)
    else:
        raise FileNotFoundError(f"未找到模型配置文件：{local_cfg} 或 {example_cfg}")

    if not isinstance(cfg, dict):
        raise ValueError("模型配置文件必须是 JSON 对象")

    problems = validate_model_config(cfg)
    if problems:
        raise ValueError("；".join(problems))

    # 禁用底层库嵌套重试，准确统计单次调用
    cfg_copy = deepcopy(cfg)
    cfg_copy.setdefault("request", {})["max_attempts"] = 1
    return cfg_copy


def load_finder_run_config(config_dir: str | Path = "config") -> dict[str, Any]:
    """读取定向查找配置，优先合并 find-skill.json 与 find-skill.local.json。"""
    base = Path(config_dir)
    res: dict[str, Any] = {}

    def _res(fname: str, sub: str) -> Path:
        p = base / sub / fname
        return p if p.exists() else base / fname

    shared_cfg = _res("find-skill.json", "runners")
    if shared_cfg.exists():
        shared = _read_json_file(shared_cfg, default={})
        if not isinstance(shared, dict):
            raise ValueError("find-skill.json 必须为 JSON 对象")
        if isinstance(shared, dict):
            res.update(shared)

    local_cfg = _res("find-skill.local.json", "runners")
    if local_cfg.exists():
        local_data = _read_json_file(local_cfg, default={})
        if not isinstance(local_data, dict):
            raise ValueError("find-skill.local.json 必须为 JSON 对象")
        if isinstance(local_data, dict):
            res.update(local_data)

    return res


def _parse_int_val(val: Any, default: int = 0, field_name: str = "参数") -> int:
    """支持 int 以及带空格、下划线、千分位逗号的表示（如 '200 000'、'200_000'、'200,000'）。
    遇到非法字符、负数或布尔值时抛出 ValueError，严禁静默吞错。"""
    if val is None:
        return default
    if isinstance(val, bool):
        raise ValueError(f"{field_name} 不能是布尔值: {val}")
    if isinstance(val, int):
        int_val = int(val)
        if int_val < 0:
            raise ValueError(f"{field_name} 不能为负数: {val}")
        return int_val
    if isinstance(val, str):
        clean = val.replace(" ", "").replace("_", "").replace(",", "").strip()
        if not clean:
            return default
        try:
            int_val = int(clean)
        except ValueError as exc:
            raise ValueError(f"{field_name} 包含非法字符无法解析为整数: {val!r}") from exc
        if int_val < 0:
            raise ValueError(f"{field_name} 不能为负数: {val}")
        return int_val
    raise ValueError(f"{field_name} 类型不支持: {type(val).__name__}，必须为正整数或数字字符串")


def validate_finder_parameters(params: dict[str, Any]) -> dict[str, Any]:
    """校验并规整化定向查找参数。"""
    limit = _parse_int_val(params.get("limit"), DEFAULT_LIMIT, "limit")
    max_evals = _parse_int_val(params.get("max_evaluations"), DEFAULT_MAX_EVALUATIONS, "max_evaluations")
    max_tokens = _parse_int_val(params.get("max_tokens"), DEFAULT_MAX_TOKENS, "max_tokens")

    return {
        "limit": limit,
        "max_evaluations": max_evals,
        "max_tokens": max_tokens,
    }


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_EVALUATIONS",
    "DEFAULT_MAX_TOKENS",
    "MAX_REPOS_TO_EXPAND",
    "MAX_SEARCH_REPOS_PER_QUERY",
    "MAX_FILES_PER_REPO",
    "MAX_TOTAL_FILES_TO_FETCH",
    "MAX_PRIMARY_FILE_BYTES",
    "MAX_TOTAL_MATERIAL_BYTES",
    "MAX_REFERENCED_FILES",
    "PLAN_MAX_OUTPUT_TOKENS",
    "EVAL_MAX_OUTPUT_TOKENS",
    "load_finder_model_config",
    "load_finder_run_config",
    "_parse_int_val",
    "validate_finder_parameters",
]
