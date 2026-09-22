"""运行期时间与时区工具。

统一采用 Asia/Shanghai 时区与 ISO 周标识规范。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # Windows 上若无 tzdata，退回固定偏移
    from zoneinfo import ZoneInfo

    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
    TZ_SOURCE = "zoneinfo:Asia/Shanghai"
except Exception:  # pragma: no cover - 取决于运行环境
    # 上海自 1991 年起不再使用夏令时，固定 +08:00 与真实时区等价
    SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
    TZ_SOURCE = "fixed:+08:00"


def now_local() -> datetime:
    """返回 Asia/Shanghai 时区的当前带时区 datetime。"""
    return datetime.now(SHANGHAI_TZ)


def week_id(moment: datetime | None = None) -> str:
    """ISO 周标识，按 Asia/Shanghai 计算。跨年周由 ISO 规则处理。"""
    local = (moment or now_local()).astimezone(SHANGHAI_TZ)
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def iso_now(moment: datetime | None = None) -> str:
    """格式化当前时间为 ISO 字符串（秒精度）。"""
    return (moment or now_local()).replace(microsecond=0).isoformat()
