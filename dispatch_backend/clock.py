"""时间来源与 ISO-8601 解析。

业务测试需要可注入的时钟；生产运行使用系统时钟（带时区）。
"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """可替换的时钟，默认返回带 UTC 时区的当前时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def parse_at(value: str) -> datetime:
    """解析 ISO-8601 字符串，拒绝朴素时间（无时区会产生歧义）。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO-8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区偏移，例如 2028-07-03T13:05:00+08:00")
    return parsed


def format_at(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
