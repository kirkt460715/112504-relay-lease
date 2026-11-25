"""可固定到指定时刻的时钟，用于本地联调/演示（生产用默认系统时钟）。"""
from __future__ import annotations

from datetime import datetime

from .clock import Clock, parse_at


class FixedClock(Clock):
    def __init__(self, value: str):
        self.moment: datetime = parse_at(value)

    def now(self) -> datetime:
        return self.moment

    def set(self, value: str) -> None:
        self.moment = parse_at(value)
