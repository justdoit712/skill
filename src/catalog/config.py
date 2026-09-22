"""目录配置加载与运行预检（§7.2 步骤 1）。

加载全量规则、分类、模型、来源、搜索词、人工干预及冷冻配置，
执行严格格式与交叉一致性预检，防止无效配置进入付费调用。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.infra.files import read_json
from src.overrides import get_manual_exclusions, get_manual_picks, load_overrides, validate_overrides
from src.prescreen import load_config
from src.snooze import load_snooze, validate_snooze


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_searches(config_dir: str = "config") -> dict:
    """读取 config/searches.json。"""
    return read_json(Path(config_dir) / "searches.json", default={})


def load_all_config(config_dir: str | Path = "config") -> dict:
    """加载目录流水线运行所需的全部配置字典。"""
    base = Path(config_dir)
    prescreen_cfg = load_config(base)
    model_path = base / "model.local.json"
    model_cfg = _load_json(model_path) if model_path.exists() else _load_json(base / "model.example.json")
    overrides_cfg = load_overrides(base / "overrides.json")
    snooze_cfg = load_snooze(base / "snoozed.json")
    sources_cfg = _load_json(base / "sources.json")

    return {
        "prescreen": prescreen_cfg,
        "taxonomy": prescreen_cfg.taxonomy,
        "rules": prescreen_cfg.rules,
        "searches": load_searches(str(base)),
        "sources": sources_cfg,
        "model": model_cfg,
        "overrides": overrides_cfg,
        "snoozed": snooze_cfg,
        "source_types": {
            s["id"]: s.get("source_type") for s in sources_cfg.get("sources", [])
        },
    }


def precheck(cfg: dict) -> list[str]:
    """预检（§7.2 步骤 1）。返回问题列表；非空时必须中止，不得进入付费调用。"""
    problems: list[str] = []
    if not cfg["prescreen"].domain_names:
        problems.append("taxonomy.json 未加载到任何主分类")
    if not cfg["rules"].get("checks"):
        problems.append("rules.json 未定义检查项")
    if not cfg["searches"].get("per_domain"):
        problems.append("searches.json 未定义任何领域的查询词")
    if not cfg["model"].get("endpoint") or not cfg["model"].get("model"):
        problems.append("模型配置缺 endpoint 或 model")
    if "overrides" in cfg:
        problems.extend(validate_overrides(cfg["overrides"]))
    if "snoozed" in cfg:
        active_picks = set(get_manual_picks((cfg or {}).get("overrides") or {}).keys())
        active_excl = set(get_manual_exclusions((cfg or {}).get("overrides") or {}).keys())
        problems.extend(
            validate_snooze(cfg["snoozed"], active_pick_ids=active_picks, active_exclusion_ids=active_excl)
        )
    return problems


__all__ = [
    "load_all_config",
    "precheck",
    "load_searches",
]
