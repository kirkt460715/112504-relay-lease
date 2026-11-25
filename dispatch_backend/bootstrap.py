"""按演练台账初始化运行态（幂等）。

把 ``registers.json`` 中的授权、预警与初始断网状态灌入服务；
重复执行不会重复建单，便于断电重启后的联调。
"""
from __future__ import annotations

from typing import Any

from .registry import Registry
from .service import DispatchService


def seed_from_registers(service: DispatchService, registry: Registry, data: dict[str, Any]) -> dict:
    times = data.get("observed_at", {})
    seeded: dict[str, Any] = {"delegations": [], "warnings": [], "towns": []}

    # 初始断网：原乡离线，代理乡在线
    for town in registry.towns:
        online = town != data.get("delegation", {}).get("owner")
        service.set_town_online("county", town, online, at=times.get("initial_outage"))
        seeded["towns"].append({"town": town, "online": online})

    delegation = data.get("delegation")
    if delegation:
        with service.store.tx() as conn:
            existing = service.store.get_delegation(conn, delegation["id"])
        if existing is None:
            valid_from = times.get("authorization", delegation["until"])
            service.create_delegation(
                "county",
                delegation_id=delegation["id"],
                owner=delegation["owner"],
                delegate=delegation["delegate"],
                villages=delegation["villages"],
                # 授权渠道白名单：台账渠道 + 替代路由渠道
                channels=list(registry.channels),
                valid_from=valid_from,
                valid_until=delegation["until"],
                at=times.get("authorization"),
            )
            seeded["delegations"].append(delegation["id"])
        # 村交还属于运行态处置，由值班员通过 /delegations/<id>/return 办理，
        # 不在启动时预载（与撤警请求同理），避免固化派播/交还时序。

    for warning in data.get("warnings", []):
        with service.store.tx() as conn:
            exists = service.store.warning(conn, warning["id"]) is not None
        if not exists:
            service.issue_warning(
                "county", warning["id"], warning["villages"], at=times.get("authorization")
            )
            seeded["warnings"].append(warning["id"])

    # 撤警请求同样属于运行态处置流程，通过 /withdrawals 办理，启动时不预载。
    return seeded
