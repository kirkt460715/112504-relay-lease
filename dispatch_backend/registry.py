"""静态台账：乡镇、村归属、喇叭组/渠道登记、替代路由。

来源是 ``fixtures/registers.json`` 中不随值班操作变化的部分。
预警、授权、撤警属于运行态，由服务 API 创建并存入 SQLite。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .clock import parse_at
from .errors import ValidationError

WHOLE_GROUP = "whole_group"
PER_ALERT = "per_alert"
CHANNEL_MODES = {WHOLE_GROUP, PER_ALERT}


@dataclass(frozen=True)
class Channel:
    name: str
    scope: tuple[str, ...]
    mode: str
    available_from: str | None = None  # None 表示台账渠道，始终可派


@dataclass
class Registry:
    towns: tuple[str, ...]
    village_owners: dict[str, str]
    channels: dict[str, Channel] = field(default_factory=dict)

    @property
    def villages(self) -> tuple[str, ...]:
        return tuple(self.village_owners)

    def channel(self, name: str) -> Channel:
        try:
            return self.channels[name]
        except KeyError:
            raise ValidationError("unknown_channel", {"channel": name}) from None

    def owner_of(self, village: str) -> str:
        try:
            return self.village_owners[village]
        except KeyError:
            raise ValidationError("unknown_village", {"village": village}) from None


def load_registry(path: str | Path) -> Registry:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    towns = tuple(_require_str_list(data, "towns"))
    owners_raw = data.get("village_owners", {})
    if not isinstance(owners_raw, dict) or not owners_raw:
        raise ValidationError("registers_missing_village_owners")
    village_owners: dict[str, str] = {}
    for village, owner in owners_raw.items():
        if owner not in towns:
            raise ValidationError("owner_not_a_town", {"village": village, "owner": owner})
        village_owners[village] = owner

    channels: dict[str, Channel] = {}
    for name, item in data.get("channels", {}).items():
        mode = item.get("mode")
        if mode not in CHANNEL_MODES:
            raise ValidationError("bad_channel_mode", {"channel": name, "mode": mode})
        scope = tuple(_validate_scope(item.get("scope"), village_owners, name))
        channels[name] = Channel(name=name, scope=scope, mode=mode)

    for route in data.get("alternate_routes", []):
        name = route["channel"]
        if name in channels:
            raise ValidationError("duplicate_channel", {"channel": name})
        mode = route.get("mode")
        if mode not in CHANNEL_MODES:
            raise ValidationError("bad_channel_mode", {"channel": name, "mode": mode})
        available_from = route.get("available_from")
        parse_at(available_from)  # 启动期即校验时间格式
        scope = tuple(_validate_scope(
            route.get("scope", route.get("villages")), village_owners, name))
        channels[name] = Channel(
            name=name, scope=scope, mode=mode, available_from=available_from
        )

    return Registry(towns=towns, village_owners=village_owners, channels=channels)


def _require_str_list(data: dict, key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
        raise ValidationError(f"registers_bad_{key}")
    return value


def _validate_scope(scope, village_owners: dict[str, str], channel: str) -> list[str]:
    if not isinstance(scope, list) or not scope:
        raise ValidationError("bad_channel_scope", {"channel": channel})
    for village in scope:
        if village not in village_owners:
            raise ValidationError(
                "scope_village_unknown", {"channel": channel, "village": village}
            )
    return scope
