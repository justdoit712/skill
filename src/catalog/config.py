"""目录配置加载与运行预检（§7.2 步骤 1）。

加载全量规则、分类、模型、来源、搜索词、人工干预及冷冻配置，
执行严格格式与交叉一致性预检，防止无效配置进入付费调用。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.infra.files import read_json
from src.infra.llm import validate_model_config
from src.infra.owned import load_owned_config
from src.shared.owned import validate_owned_config
from .overrides import get_manual_exclusions, get_manual_picks, load_overrides, validate_overrides
from .prescreen import load_config
from .snooze import load_snooze, validate_snooze


def _resolve_config_file(base: Path, filename: str, subfolder: str | None = None) -> Path:
    flat = base / filename
    sub = (base / subfolder / filename) if subfolder else flat
    if flat.exists() and sub.exists():
        try:
            return flat if flat.stat().st_mtime >= sub.stat().st_mtime else sub
        except OSError:
            return flat
    if flat.exists():
        return flat
    if sub.exists():
        return sub
    return sub


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_searches(config_dir: str | Path = "config") -> dict:
    """读取 searches.json。"""
    base = Path(config_dir)
    target = _resolve_config_file(base, "searches.json", "discovery")
    return read_json(target, default={})


AUTOMATION_FILENAME = "automation.json"


def load_automation(config_dir: str | Path = "config") -> dict:
    """读取 automation.json（自动化开关，可选文件）。

    语义（与 `.github/workflows/sync-skills.yml` 的 gate job 保持一致）：
    - 缺失文件或缺失字段 → `scheduled_sync_enabled` 视为 **false**：无人值守的定时任务
      不会因为文件丢失或改名而开始花钱。
    - 文件存在但内容不是合法 JSON → 直接抛错，不静默降级。
    - 字段存在但类型不是布尔 → 由 precheck 报错，避免 `"true"` 这类字符串被当成真值。
    """
    base = Path(config_dir)
    path = _resolve_config_file(base, AUTOMATION_FILENAME, "runners")
    if not path.exists():
        return {"scheduled_sync_enabled": False, "missing": True}
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{AUTOMATION_FILENAME} 顶层必须是 JSON 对象")
    return payload


def load_all_config(config_dir: str | Path = "config") -> dict:
    """加载目录流水线运行所需的全部配置字典。"""
    base = Path(config_dir)
    prescreen_cfg = load_config(base)
    model_path = _resolve_config_file(base, "model.local.json", "models")
    if model_path.exists():
        model_cfg = _load_json(model_path)
    else:
        ex_path = _resolve_config_file(base, "model.example.json", "models")
        model_cfg = _load_json(ex_path) if ex_path.exists() else {}

    overrides_cfg = load_overrides(_resolve_config_file(base, "overrides.json", "governance"))
    snooze_cfg = load_snooze(_resolve_config_file(base, "snoozed.json", "governance"))
    sources_file = _resolve_config_file(base, "sources.json", "discovery")
    sources_cfg = _load_json(sources_file) if sources_file.exists() else {}
    owned_cfg = load_owned_config(base)

    return {
        "prescreen": prescreen_cfg,
        "taxonomy": prescreen_cfg.taxonomy,
        "rules": prescreen_cfg.rules,
        "searches": load_searches(str(base)),
        "sources": sources_cfg,
        "model": model_cfg,
        "overrides": overrides_cfg,
        "snoozed": snooze_cfg,
        "owned": owned_cfg,
        "automation": load_automation(base),
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
    quality = cfg["rules"].get("quality_review", {})
    if not isinstance(quality, dict) or type(quality.get("enabled", False)) is not bool:
        problems.append("rules.json quality_review.enabled 必须是布尔值")
    if not cfg["searches"].get("per_domain"):
        problems.append("searches.json 未定义任何领域的查询词")
    problems.extend(validate_model_config(cfg["model"]))
    if "overrides" in cfg:
        problems.extend(validate_overrides(cfg["overrides"]))
    if "snoozed" in cfg:
        active_picks = set(get_manual_picks((cfg or {}).get("overrides") or {}).keys())
        active_excl = set(get_manual_exclusions((cfg or {}).get("overrides") or {}).keys())
        problems.extend(
            validate_snooze(cfg["snoozed"], active_pick_ids=active_picks, active_exclusion_ids=active_excl)
        )
    automation = cfg.get("automation") or {}
    if "owned" in cfg:
        try:
            validate_owned_config(cfg["owned"])
        except ValueError as exc:
            problems.append(f"owned-skills.json 校验失败：{exc}")
    if "scheduled_sync_enabled" in automation and not isinstance(
        automation["scheduled_sync_enabled"], bool
    ):
        problems.append(
            f"{AUTOMATION_FILENAME} 的 scheduled_sync_enabled 必须是 true 或 false"
            f"（当前为 {type(automation['scheduled_sync_enabled']).__name__}）"
        )
    return problems


__all__ = [
    "AUTOMATION_FILENAME",
    "load_all_config",
    "load_automation",
    "precheck",
    "load_searches",
]
