"""治理配置路径：只使用 governance 子目录，拒绝旧平铺配置。"""

from pathlib import Path


GOVERNANCE_FILES = (
    "favorites.json",
    "overrides.json",
    "snoozed.json",
    "owned-skills.json",
)


class GovernancePathError(ValueError):
    """治理配置存在旧路径或调用方指定了非标准位置。"""


def resolve_governance_path(config_dir: str | Path, filename: str) -> Path:
    """config_dir 是配置根目录；缺失文件的默认值由加载器决定。"""
    if filename not in GOVERNANCE_FILES:
        raise GovernancePathError(f"不支持的治理配置：{filename}")
    base = Path(config_dir)
    target = base / "governance" / filename
    legacy = base / filename
    if legacy.exists():
        if target.exists():
            raise GovernancePathError(
                f"治理配置路径冲突：{legacy} 与 {target} 同时存在。"
                "请核对内容并移除旧路径文件后重试。"
            )
        raise GovernancePathError(f"发现旧路径配置：{legacy}；请先迁移至 {target} 后重试。")
    return target


def require_governance_file(path: str | Path, filename: str) -> Path:
    """显式文件参数也必须指向标准位置，不能绕过冲突检查。"""
    target = Path(path)
    if target.name != filename or target.parent.name != "governance":
        raise GovernancePathError(
            f"治理配置路径不合法：{target}；请使用配置根目录下的 governance/{filename}。"
        )
    return resolve_governance_path(target.parent.parent, filename)
