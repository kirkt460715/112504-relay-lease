"""领域服务：授权、派播、撤警、接管交还、待确认跟踪。

关键不变量（对应演练规则）：

1. 授权只覆盖指定村、指定渠道、指定时段；村可逐个交还，交还后原乡立即恢复处置权。
2. 授权到期不触发任何停机，正在播放的报警保持原状，只是代理不能再下新指令。
3. 撤警按（预警, 村）粒度生效；未撤警村和同组其他有效预警不受影响。
4. 老设备（whole_group）只能整组停；整组停之前，组内仍有效内容必须已在安全
   替代渠道上确认可播（audible），否则拒绝并返回缺口清单。
5. 设备接单（accepted）只代表接单；只有独立 device-observation 的 audible 才代表
   实际成音，silent 才代表实际静默。
6. 已确认撤警的（预警, 村）不会被旧启动回执/迟到 audible 重新挂回值班清单
   （迟到回报保留为 stale 记录，仍可被现任值班员查看）。
7. 已发出但结果未知的指令保留 sent_unknown 状态与可能影响范围，重启不提升、不消失。
8. 授权交还/故障恢复后，原乡可接管未决处置；旧代理的迟到派播请求被拒绝，
   旧设备关于实际发生的回报（observation）始终保留并归现任值班员可见。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import storage
from .clock import Clock, format_at, parse_at
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionError,
    ServiceError,
    ValidationError,
)
from .partner import DISPATCH, OBSERVATION, CommitDisconnect
from .registry import PER_ALERT, WHOLE_GROUP, Channel, Registry

# 指令状态
SEND_FAILED = "send_failed"          # 接单前对端不可达，可重试
SENT_UNKNOWN = "sent_unknown"        # 接单后断链/结果未知，可能已执行
REJECTED = "rejected"                # 对端明确拒单
ACCEPTED_PENDING = "accepted_pending"  # 已接单，等待实际成音/静默观察
ACTIVE = "active"                    # 最新有效观察为 audible
STOPPED = "stopped"                  # 最新有效观察为 silent（整组停同样落 stopped）

START_ACTIONS = {"start"}
STOP_ALERT_ACTIONS = {"stop"}
STOP_GROUP_ACTIONS = {"stop-group"}
ALL_ACTIONS = START_ACTIONS | STOP_ALERT_ACTIONS | STOP_GROUP_ACTIONS


class Partner(Protocol):
    def submit(self, operation_id: str, operation: str, request: dict) -> dict: ...
    def query(self, operation_id: str) -> dict: ...


@dataclass
class ChannelState:
    """一个渠道上某个（预警或整组）会话的当前判断。"""

    channel: str
    alert: str | None  # None 表示 whole_group 整组会话
    villages: tuple[str, ...]
    state: str
    command_id: str
    observed_at: str | None
    uncertain: bool  # True 表示设备可能已动作但未经独立观察证实


class DispatchService:
    def __init__(self, store: storage.Store, registry: Registry, partner: Partner, clock: Clock,
                 admin_actors: tuple[str, ...] = ("county",)):
        self.store = store
        self.registry = registry
        self.partner = partner
        self.clock = clock
        self.admin_actors = set(admin_actors)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _at(self, at: str | None) -> datetime:
        if not at:
            return self.clock.now()
        try:
            return parse_at(at)
        except ValueError as exc:
            raise ValidationError("bad_timestamp", {"value": at, "reason": str(exc)}) from None

    def _require_town(self, actor: str) -> str:
        if actor not in self.registry.towns:
            raise ValidationError("unknown_town", {"actor": actor})
        return actor

    def _require_actor(self, actor: str) -> str:
        """县台管理员可办行政手续；派播类操作仍要求乡值班身份。"""
        if actor not in self.registry.towns and actor not in self.admin_actors:
            raise ValidationError("unknown_actor", {"actor": actor})
        return actor

    def _is_admin(self, actor: str) -> bool:
        return actor in self.admin_actors

    def _delegation_view(self, conn) -> dict[str, Any] | None:
        row = self.store.latest_delegation(conn)
        if row is None:
            return None
        returned = self.store.returned_villages(conn, row["id"])
        return {
            "row": row,
            "villages": [v for v in storage.loads(row["villages_json"]) if v not in returned],
            "returned": sorted(returned),
            "channels": storage.loads(row["channels_json"]),
            "valid_from": parse_at(row["valid_from"]),
            "valid_until": parse_at(row["valid_until"]),
        }

    def duty_town(self, conn, village: str, at: datetime) -> str:
        """返回某村在指定时刻的现任值班乡。"""
        owner = self.registry.owner_of(village)
        delegation = self._delegation_view(conn)
        if delegation is None:
            return owner
        row = delegation["row"]
        if owner != row["owner"]:
            return owner
        if village not in delegation["villages"]:
            return owner  # 已交还或本就不在授权范围
        if not (delegation["valid_from"] <= at < delegation["valid_until"]):
            return owner  # 授权尚未生效或已到期
        return row["delegate"]

    def _assert_duty(self, conn, actor: str, villages, at: datetime) -> None:
        for village in villages:
            duty = self.duty_town(conn, village, at)
            if actor != duty:
                raise PermissionError(
                    "not_current_duty",
                    {"village": village, "actor": actor, "current_duty": duty},
                )

    def _assert_town_online(self, conn, actor: str) -> None:
        if not self.store.town_online(conn, actor):
            raise ConflictError("town_offline", {"town": actor})

    # ------------------------------------------------------------------
    # 乡镇在线状态、授权、交还
    # ------------------------------------------------------------------

    def set_town_online(self, actor: str, town: str, online: bool, at: str | None = None) -> dict:
        self._require_actor(actor)
        self._require_town(town)
        moment = self._at(at)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO towns(name, online) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET online=excluded.online",
                (town, 1 if online else 0),
            )
            self.store.audit(
                conn, format_at(moment), actor,
                "town_online" if online else "town_offline", {"town": town},
            )
        return {"town": town, "online": online, "at": format_at(moment)}

    def create_delegation(
        self,
        actor: str,
        delegation_id: str,
        owner: str,
        delegate: str,
        villages: list[str],
        channels: list[str],
        valid_from: str,
        valid_until: str,
        at: str | None = None,
    ) -> dict:
        self._require_actor(actor)
        if owner not in self.registry.towns or delegate not in self.registry.towns or owner == delegate:
            raise ValidationError("bad_delegation_towns", {"owner": owner, "delegate": delegate})
        if not isinstance(villages, list) or not villages:
            raise ValidationError("delegation_needs_villages")
        if not isinstance(channels, list) or not channels:
            raise ValidationError("delegation_needs_channels")
        for village in villages:
            if self.registry.owner_of(village) != owner:
                raise ValidationError("village_not_owned", {"village": village, "owner": owner})
        for channel in channels:
            self.registry.channel(channel)  # 渠道必须在台账或替代路由中登记
        start = parse_at(valid_from)
        end = parse_at(valid_until)
        if end <= start:
            raise ValidationError("delegation_bad_window")
        moment = self._at(at)
        with self.store.tx() as conn:
            if self.store.get_delegation(conn, delegation_id) is not None:
                raise ConflictError("delegation_exists", {"id": delegation_id})
            conn.execute(
                "INSERT INTO delegations(id, owner, delegate, villages_json, channels_json,"
                " valid_from, valid_until, created_at, actor)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delegation_id, owner, delegate, storage.dumps(villages),
                    storage.dumps(channels), valid_from, valid_until,
                    format_at(moment), actor,
                ),
            )
            self.store.audit(
                conn, format_at(moment), actor, "delegation_created",
                {"id": delegation_id, "owner": owner, "delegate": delegate,
                 "villages": villages, "channels": channels,
                 "valid_from": valid_from, "valid_until": valid_until},
            )
        return self.get_delegation(delegation_id)

    def return_village(
        self, actor: str, delegation_id: str, village: str, at: str | None = None
    ) -> dict:
        """村交还：原乡恢复接入后，已交还村立即由原乡继续操作。"""
        self._require_actor(actor)
        moment = self._at(at)
        with self.store.tx() as conn:
            row = self.store.get_delegation(conn, delegation_id)
            if row is None:
                raise NotFoundError("delegation_not_found", {"id": delegation_id})
            authorized = storage.loads(row["villages_json"])
            if village not in authorized:
                raise ValidationError("village_not_delegated", {"village": village})
            if village in self.store.returned_villages(conn, delegation_id):
                raise ConflictError("village_already_returned", {"village": village})
            if actor not in {row["owner"], row["delegate"]}:
                raise PermissionError("return_requires_party")
            return_id = f"ret-{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO village_returns(id, delegation_id, village, at, actor)"
                " VALUES (?, ?, ?, ?, ?)",
                (return_id, delegation_id, village, format_at(moment), actor),
            )
            self.store.audit(
                conn, format_at(moment), actor, "village_returned",
                {"id": return_id, "delegation_id": delegation_id, "village": village},
            )
        return self.get_delegation(delegation_id)

    def get_delegation(self, delegation_id: str) -> dict:
        with self.store.tx() as conn:
            row = self.store.get_delegation(conn, delegation_id)
            if row is None:
                raise NotFoundError("delegation_not_found", {"id": delegation_id})
            returned = self.store.returned_villages(conn, delegation_id)
            all_villages = storage.loads(row["villages_json"])
            now = self.clock.now()
            view = self._delegation_view(conn)
            active = bool(view and view["row"]["id"] == delegation_id and view["villages"])
            return {
                "id": row["id"],
                "owner": row["owner"],
                "delegate": row["delegate"],
                "villages": all_villages,
                "returned": sorted(returned),
                "active_villages": [v for v in all_villages if v not in returned],
                "channels": storage.loads(row["channels_json"]),
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "in_window": parse_at(row["valid_from"]) <= now < parse_at(row["valid_until"]),
                "has_active_scope": active,
            }

    # ------------------------------------------------------------------
    # 预警登记
    # ------------------------------------------------------------------

    def issue_warning(
        self, actor: str, warning_id: str, villages: list[str], at: str | None = None
    ) -> dict:
        self._require_actor(actor)
        if not isinstance(villages, list) or not villages:
            raise ValidationError("warning_needs_villages")
        for village in villages:
            self.registry.owner_of(village)
        moment = self._at(at)
        with self.store.tx() as conn:
            if self.store.warning(conn, warning_id) is not None:
                raise ConflictError("warning_exists", {"id": warning_id})
            conn.execute(
                "INSERT INTO warnings(id, villages_json, created_at, actor) VALUES (?, ?, ?, ?)",
                (warning_id, storage.dumps(villages), format_at(moment), actor),
            )
            self.store.audit(
                conn, format_at(moment), actor, "warning_issued",
                {"id": warning_id, "villages": villages},
            )
        return self.get_warning(warning_id)

    def list_delegations(self) -> list[dict]:
        with self.store.tx() as conn:
            rows = conn.execute("SELECT id FROM delegations ORDER BY created_at, rowid").fetchall()
        return [self.get_delegation(row["id"]) for row in rows]

    def get_warning(self, warning_id: str) -> dict:
        with self.store.tx() as conn:
            row = self.store.warning(conn, warning_id)
            if row is None:
                raise NotFoundError("warning_not_found", {"id": warning_id})
            return {
                "id": row["id"],
                "villages": storage.loads(row["villages_json"]),
                "created_at": row["created_at"],
                "actor": row["actor"],
                "withdrawal_requested": sorted(self.store.withdrawal_villages(conn, warning_id)),
            }

    # ------------------------------------------------------------------
    # 派播
    # ------------------------------------------------------------------

    def _build_request(self, channel: Channel, action: str, alert: str | None,
                       villages: list[str] | None) -> dict:
        request: dict[str, Any] = {"channel": channel.name, "action": action}
        if action in START_ACTIONS or action in STOP_ALERT_ACTIONS:
            if not alert:
                raise ValidationError("alert_required", {"action": action})
            request["alert"] = alert
        if action in START_ACTIONS and channel.mode == PER_ALERT and villages:
            # 显式给定时按指定村派（替代渠道补播场景）；未给定时由对端按预警全量处理
            request["villages"] = villages
        return request

    def dispatch(
        self,
        actor: str,
        channel_name: str,
        action: str,
        alert: str | None = None,
        villages: list[str] | None = None,
        command_id: str | None = None,
        at: str | None = None,
    ) -> dict:
        self._require_town(actor)
        if action not in ALL_ACTIONS:
            raise ValidationError("unknown_action", {"action": action})
        channel = self.registry.channel(channel_name)
        moment = self._at(at)
        command_id = command_id or f"cmd-{uuid.uuid4().hex[:12]}"

        with self.store.tx() as conn:
            # 幂等：同 ID 直接返回既有指令（对端本身也按 ID 去重）
            existing = self.store.command(conn, command_id)
            if existing is not None:
                return self._command_dict(conn, existing)

            # 1) 预警与目标村
            if action in START_ACTIONS or action in STOP_ALERT_ACTIONS:
                warning_row = self.store.warning(conn, alert) if alert else None
                if warning_row is None:
                    raise NotFoundError("warning_not_found", {"id": alert})
                warning_villages = storage.loads(warning_row["villages_json"])
            else:
                warning_villages = list(self.registry.villages)

            if action in STOP_GROUP_ACTIONS:
                if channel.mode != WHOLE_GROUP:
                    raise ValidationError("stop_group_requires_whole_group", {"channel": channel_name})
                targets = [v for v in channel.scope]
            else:
                requested = list(villages) if villages else list(warning_villages)
                unknown = [v for v in requested if v not in self.registry.village_owners]
                if unknown:
                    raise ValidationError("unknown_village", {"villages": unknown})
                targets = [v for v in requested if v in set(channel.scope)]
                if not targets:
                    raise ValidationError("channel_outside_scope",
                                          {"channel": channel_name, "villages": requested})
                extra = [v for v in requested if v not in channel.scope]
                if extra:
                    raise ValidationError("channel_outside_scope",
                                          {"channel": channel_name, "villages": extra})

            # 2) 处置权。整组停的物理范围无法按村切分：只要组内还有村由该
            #    值班方负责就可受理；安全闸门仍覆盖组内全部仍有效内容。
            self._assert_town_online(conn, actor)
            if action in STOP_GROUP_ACTIONS:
                in_charge = [v for v in targets
                             if self.duty_town(conn, v, moment) == actor]
                if not in_charge:
                    raise PermissionError(
                        "not_current_duty",
                        {"channel": channel_name, "actor": actor, "scope": targets},
                    )
            else:
                self._assert_duty(conn, actor, targets, moment)

            # 3) 授权渠道白名单：代理乡只能使用授权列出的渠道
            delegation = self._delegation_view(conn)
            if delegation and actor == delegation["row"]["delegate"]:
                delegated_villages = set(delegation["villages"])
                if set(targets) & delegated_villages and channel_name not in delegation["channels"]:
                    raise PermissionError(
                        "channel_not_delegated",
                        {"channel": channel_name, "allowed": delegation["channels"]},
                    )

            # 4) 替代渠道可用时间
            if channel.available_from is not None and moment < parse_at(channel.available_from):
                raise ConflictError(
                    "channel_not_yet_available",
                    {"channel": channel_name, "available_from": channel.available_from},
                )

            # 5) 整组停安全闸门：组内仍有效播报必须先在替代渠道确认可播
            if action in STOP_GROUP_ACTIONS:
                self._assert_safe_group_stop(conn, channel, moment)

            request = self._build_request(channel, action, alert, villages)
            conn.execute(
                "INSERT INTO commands(id, channel, action, alert, villages_json, kind,"
                " issued_by, handled_by, issued_at, request_json, state, remote_id, last_error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    command_id, channel_name, action, alert,
                    storage.dumps(targets), channel.mode, actor, actor,
                    format_at(moment), storage.dumps(request), SEND_FAILED, None, None,
                ),
            )
            self.store.command_event(conn, command_id, format_at(moment), "created", actor,
                                     {"request": request, "targets": targets})

        # 6) 协议收发放在事务外（网络 IO 不持锁）
        return self._transmit(command_id, actor, at_override=format_at(moment))

    def _transmit(self, command_id: str, actor: str, at_override: str | None = None) -> dict:
        moment = parse_at(at_override) if at_override else self.clock.now()
        with self.store.tx() as conn:
            row = self.store.command(conn, command_id)
            if row is None:
                raise NotFoundError("command_not_found", {"id": command_id})
            request = storage.loads(row["request_json"])
        try:
            reply = self.partner.submit(command_id, DISPATCH, request)
        except CommitDisconnect:
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE commands SET state=?, last_error=? WHERE id=?",
                    (SENT_UNKNOWN, "commit_disconnect", command_id),
                )
                self.store.command_event(conn, command_id, format_at(moment),
                                         "commit_disconnect", actor, {})
                row = self.store.command(conn, command_id)
                return self._command_dict(conn, row)
        except ServiceError as exc:
            with self.store.tx() as conn:
                conn.execute(
                    "UPDATE commands SET state=?, last_error=? WHERE id=?",
                    (SEND_FAILED if exc.code == "partner_unavailable" else REJECTED,
                     exc.code, command_id),
                )
                self.store.command_event(conn, command_id, format_at(moment),
                                         "send_failed", actor, {"error": exc.code, "detail": exc.detail})
                row = self.store.command(conn, command_id)
                return self._command_dict(conn, row)

        return self._apply_reply(command_id, reply, actor, moment)

    def _apply_reply(self, command_id: str, reply: dict, actor: str, moment: datetime) -> dict:
        result = reply.get("result")
        remote_id = reply.get("remote_id")
        with self.store.tx() as conn:
            if result == "accepted":
                conn.execute(
                    "UPDATE commands SET state=?, remote_id=?, last_error=NULL WHERE id=?",
                    (ACCEPTED_PENDING, remote_id, command_id),
                )
                event, detail = "accepted", {"remote_id": remote_id}
            else:
                conn.execute(
                    "UPDATE commands SET state=?, remote_id=?, last_error=? WHERE id=?",
                    (REJECTED, remote_id, reply.get("reason"), command_id),
                )
                event, detail = "rejected", {"reason": reply.get("reason")}
            self.store.command_event(conn, command_id, format_at(moment), event, actor, detail)
            row = self.store.command(conn, command_id)
            return self._command_dict(conn, row)

    # ------------------------------------------------------------------
    # 独立设备观察（audible / silent）
    # ------------------------------------------------------------------

    def record_observation(
        self,
        actor: str,
        remote_id: str,
        kind: str,
        heard: bool,
        observed_at: str,
        at: str | None = None,
    ) -> dict:
        """登记独立监听结果。可主动向对端查询，也可接受对端推送。"""
        self._require_actor(actor)
        if kind not in {"audible", "silent"}:
            raise ValidationError("bad_observation_kind", {"kind": kind})
        moment_observed = parse_at(observed_at)
        moment = self._at(at)
        with self.store.tx() as conn:
            row = self.store.command_by_remote(conn, remote_id)
            command_id = row["id"] if row else None
            stale = False
            note = None
            if row is None:
                note = "remote_id_unknown"
            else:
                stale, note = self._is_stale_observation(conn, row, kind, heard)
            cur = conn.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM observations"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO observations(id, command_id, remote_id, kind, heard, observed_at,"
                " received_at, stale, note, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cur, command_id, remote_id, kind, 1 if heard else 0,
                 format_at(moment_observed), format_at(moment), 1 if stale else 0, note, actor),
            )
            if row is not None and not stale:
                new_state = ACTIVE if heard else STOPPED
                conn.execute("UPDATE commands SET state=? WHERE id=?", (new_state, command_id))
                self.store.command_event(
                    conn, command_id, format_at(moment),
                    "audible_confirmed" if heard else "silent_confirmed",
                    actor, {"observed_at": format_at(moment_observed), "stale": False},
                )
            elif row is not None:
                self.store.command_event(
                    conn, command_id, format_at(moment), "stale_observation_ignored",
                    actor, {"observed_at": format_at(moment_observed), "note": note},
                )
            self.store.audit(conn, format_at(moment), actor, "observation_recorded",
                             {"remote_id": remote_id, "kind": kind, "heard": heard,
                              "stale": stale, "command_id": command_id, "note": note})
            result = {
                "id": cur, "command_id": command_id, "remote_id": remote_id,
                "kind": kind, "heard": heard, "observed_at": format_at(moment_observed),
                "received_at": format_at(moment), "stale": stale, "note": note,
            }
        # 状态推进后，撤警确认可能随之满足
        if command_id is not None and not stale:
            self._refresh_withdrawal_confirmations(actor, at_override=format_at(moment))
        return result

    def _is_stale_observation(self, conn, command_row, kind: str, heard: bool) -> tuple[bool, str]:
        """判断迟到回报是否已与当前值班结论冲突。"""
        alert = command_row["alert"]
        targets = storage.loads(command_row["villages_json"])
        if heard and kind == "audible" and alert is not None:
            withdrawn = self.store.withdrawal_villages(conn, alert)
            confirmed = self._confirmed_withdrawal_villages(conn, alert)
            already_done = withdrawn & confirmed
            if set(targets) <= already_done:
                return True, "withdrawal_already_confirmed"
        if not heard and kind == "silent":
            # 迟到的静默只在该会话仍标记 active 且预警已撤时才会误导；
            # 正常 stop 流程产生的 silent 不是 stale。
            if command_row["state"] == ACTIVE and alert is not None:
                withdrawn = self.store.withdrawal_villages(conn, alert)
                if set(targets) <= withdrawn:
                    return True, "stop_without_active_withdrawal_flow"
        return False, None

    # ------------------------------------------------------------------
    # 断链查询与重试
    # ------------------------------------------------------------------

    def fetch_observation(
        self, actor: str, operation_id: str, remote_id: str, kind: str,
        at: str | None = None,
    ) -> dict:
        """向对端查询一次独立监听结果并按设备回报规则入账。"""
        self._require_actor(actor)
        if kind not in {"audible", "silent"}:
            raise ValidationError("bad_observation_kind", {"kind": kind})
        payload = self.partner.fetch(
            operation_id, OBSERVATION, {"remote_id": remote_id, "kind": kind})
        # 夹具回报只含 observed_at/heard；remote_id 以请求参数为准
        if "heard" not in payload or "observed_at" not in payload:
            raise ServiceError("unexpected_observation_reply", 502, payload)
        heard = bool(payload.get("heard"))
        observed_at = payload.get("observed_at")
        parse_at(observed_at)
        return self.record_observation(
            actor, remote_id=remote_id, kind=kind, heard=heard,
            observed_at=observed_at, at=at,
        )

    def reconcile(self, actor: str, command_id: str, at: str | None = None) -> dict:
        """用同一 ID 向对端查询接单结果。只有结果未知的指令需要对账。"""
        self._require_actor(actor)
        moment = self._at(at)
        with self.store.tx() as conn:
            row = self.store.command(conn, command_id)
            if row is None:
                raise NotFoundError("command_not_found", {"id": command_id})
            if row["state"] != SENT_UNKNOWN:
                return self._command_dict(conn, row)
        try:
            payload = self.partner.query(command_id)
        except ServiceError as exc:
            if exc.code == "unknown_operation":
                # 对端没有接单记录：指令仍保持未知，不能宣称成功也不能删除
                with self.store.tx() as conn:
                    self.store.command_event(conn, command_id, format_at(moment),
                                             "reconcile_no_record", actor, {})
                    row = self.store.command(conn, command_id)
                    return self._command_dict(conn, row)
            raise
        return self._apply_reply(command_id, payload, actor, moment)

    def retry_command(self, actor: str, command_id: str, at: str | None = None) -> dict:
        """同 ID 同内容重发（对端按 ID 幂等）。仅限发送失败/结果未知。"""
        self._require_town(actor)
        with self.store.tx() as conn:
            row = self.store.command(conn, command_id)
            if row is None:
                raise NotFoundError("command_not_found", {"id": command_id})
            if row["state"] not in {SEND_FAILED, SENT_UNKNOWN}:
                raise ConflictError("command_not_retryable",
                                    {"id": command_id, "state": row["state"]})
            if row["handled_by"] != actor:
                raise PermissionError("command_handled_by_other",
                                      {"handled_by": row["handled_by"]})
        return self._transmit(command_id, actor)

    # ------------------------------------------------------------------
    # 撤警
    # ------------------------------------------------------------------

    def request_withdrawal(
        self,
        actor: str,
        warning_id: str,
        villages: list[str],
        at: str | None = None,
    ) -> dict:
        """登记针对部分村庄的撤警请求，并返回每个村的完成路径与缺口。"""
        self._require_actor(actor)
        if not isinstance(villages, list) or not villages:
            raise ValidationError("withdrawal_needs_villages")
        moment = self._at(at)
        with self.store.tx() as conn:
            row = self.store.warning(conn, warning_id)
            if row is None:
                raise NotFoundError("warning_not_found", {"id": warning_id})
            warning_villages = set(storage.loads(row["villages_json"]))
            unknown = [v for v in villages if v not in self.registry.village_owners]
            if unknown:
                raise ValidationError("unknown_village", {"villages": unknown})
            outside = [v for v in villages if v not in warning_villages]
            if outside:
                raise ValidationError("village_not_under_warning",
                                      {"warning": warning_id, "villages": outside})
            if not self._is_admin(actor):
                self._assert_town_online(conn, actor)
                self._assert_duty(conn, actor, villages, moment)
            request_id = f"wd-{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO withdrawal_requests(id, warning_id, villages_json, requested_at, actor)"
                " VALUES (?, ?, ?, ?, ?)",
                (request_id, warning_id, storage.dumps(villages), format_at(moment), actor),
            )
            self.store.audit(
                conn, format_at(moment), actor, "withdrawal_requested",
                {"id": request_id, "warning": warning_id, "villages": villages},
            )
        return self.withdrawal_plan(warning_id)

    def _confirmed_withdrawal_villages(self, conn, warning_id: str) -> set[str]:
        """已实际停播的（预警, 村）集合。

        依据折叠后的会话：按预警停止使对应会话进入 stopped；整组停会把该渠道
        作用域相交的在播会话一并置为 stopped（另有 alert=None 的整组记录兜底）。
        只有 silent 观察或 stopped 会话算数，待确认/在播都不算。
        """
        confirmed: set[str] = set()
        warning_villages = set()
        row = self.store.warning(conn, warning_id)
        if row is not None:
            warning_villages = set(storage.loads(row["villages_json"]))
        for state in self._channel_states(conn):
            if state.state != STOPPED:
                continue
            if state.alert == warning_id:
                confirmed.update(state.villages)
            elif state.alert is None:  # 整组停记录
                confirmed.update(set(state.villages) & warning_villages)
        return confirmed

    def _live_alert_villages(self, conn, warning_id: str) -> dict[str, list[ChannelState]]:
        """该预警当前仍在播（audible 或结果未知）的村 -> 渠道会话列表。"""
        live: dict[str, list[ChannelState]] = {}
        for state in self._channel_states(conn):
            if state.alert != warning_id:
                continue
            if state.state in {ACTIVE, ACCEPTED_PENDING, SENT_UNKNOWN}:
                for village in state.villages:
                    live.setdefault(village, []).append(state)
        return live

    def _assert_safe_group_stop(self, conn, channel: Channel, moment: datetime) -> None:
        """整组停闸门：组内每个仍有效（预警未在该村撤警）的（预警, 村）都要有可听替代。

        整组停的物理影响是整个喇叭组，无法只停其中一条，因此对组内所有仍有效
        内容一视同仁：无论当前是否已确认在播，没有可听替代就不允许整组停。
        """
        scope = set(channel.scope)
        gaps: list[dict] = []
        for warning_row in self.store.list_warnings(conn):
            warning_id = warning_row["id"]
            warning_villages = set(storage.loads(warning_row["villages_json"]))
            withdrawn = self.store.withdrawal_villages(conn, warning_id)
            still_effective = sorted(scope & warning_villages - withdrawn)
            for village in still_effective:
                cover = self._audible_alternate_cover(conn, warning_id, village, exclude=channel.name)
                if cover is None:
                    gaps.append({"warning": warning_id, "village": village})
        if gaps:
            raise ConflictError("unsafe_group_stop", {
                "channel": channel.name,
                "reason": "effective_alerts_without_confirmed_alternate",
                "uncovered": gaps,
            })

    def _audible_alternate_cover(self, conn, warning_id: str, village: str, exclude: str) -> str | None:
        """返回已在该村确认成音、覆盖该预警的替代渠道名。"""
        for state in self._channel_states(conn):
            if state.alert != warning_id or state.channel == exclude:
                continue
            if village not in state.villages:
                continue
            if state.state == ACTIVE:
                return state.channel
        return None

    def _refresh_withdrawal_confirmations(self, actor: str, at_override: str | None = None) -> dict:
        """观察推进后重算撤警完成情况，落审计但不自动派任何指令。"""
        moment = parse_at(at_override) if at_override else self.clock.now()
        summary: dict[str, dict] = {}
        with self.store.tx() as conn:
            for warning_row in self.store.list_warnings(conn):
                warning_id = warning_row["id"]
                requested = self.store.withdrawal_villages(conn, warning_id)
                if not requested:
                    continue
                confirmed = self._confirmed_withdrawal_villages(conn, warning_id)
                done = sorted(requested & confirmed)
                pending = sorted(requested - confirmed)
                summary[warning_id] = {"confirmed": done, "pending": pending}
                if done:
                    self.store.audit(
                        conn, format_at(moment), actor, "withdrawal_confirmed",
                        {"warning": warning_id, "villages": done},
                    )
        return summary

    def withdrawal_plan(self, warning_id: str) -> dict:
        """值班台视图：撤警完成/未完成村、缺口、仍处于推送范围的村。"""
        with self.store.tx() as conn:
            row = self.store.warning(conn, warning_id)
            if row is None:
                raise NotFoundError("warning_not_found", {"id": warning_id})
            warning_villages = set(storage.loads(row["villages_json"]))
            requested = self.store.withdrawal_villages(conn, warning_id)
            confirmed = self._confirmed_withdrawal_villages(conn, warning_id)
            live = self._live_alert_villages(conn, warning_id)
            items = []
            for village in sorted(warning_villages):
                entry: dict[str, Any] = {
                    "village": village,
                    "withdrawal_requested": village in requested,
                    "withdrawal_confirmed": village in confirmed,
                    "still_in_push_scope": village in live,
                    "live_channels": [
                        self._channel_state_dict(s) for s in live.get(village, [])
                    ],
                }
                if village in requested and village not in confirmed:
                    entry["blockers"] = self._withdrawal_blockers(conn, warning_id, village)
                items.append(entry)
            return {
                "warning": warning_id,
                "requested": sorted(requested),
                "confirmed": sorted(requested & confirmed),
                "pending": sorted(requested - confirmed),
                "unaffected_villages": sorted(warning_villages - requested),
                "villages": items,
            }

    def _withdrawal_blockers(self, conn, warning_id: str, village: str) -> list[dict]:
        blockers: list[dict] = []
        for state in self._channel_states(conn):
            if state.alert != warning_id or village not in state.villages:
                continue
            if state.state in {ACTIVE, ACCEPTED_PENDING, SENT_UNKNOWN}:
                blockers.append({
                    "channel": state.channel,
                    "state": state.state,
                    "uncertain": state.uncertain,
                    "command_id": state.command_id,
                    "mode": self.registry.channel(state.channel).mode,
                })
        return blockers

    # ------------------------------------------------------------------
    # 接管/交还后的未决处置
    # ------------------------------------------------------------------

    def takeover_pending(self, actor: str, command_id: str, at: str | None = None) -> dict:
        """现任值班乡接管同一项未决处置；旧代理的迟到派播仍会被处置权闸门拒绝。"""
        self._require_town(actor)
        moment = self._at(at)
        with self.store.tx() as conn:
            row = self.store.command(conn, command_id)
            if row is None:
                raise NotFoundError("command_not_found", {"id": command_id})
            targets = storage.loads(row["villages_json"])
            self._assert_duty(conn, actor, targets, moment)
            if row["state"] not in {SENT_UNKNOWN, ACCEPTED_PENDING, SEND_FAILED}:
                raise ConflictError("command_not_pending",
                                    {"id": command_id, "state": row["state"]})
            previous = row["handled_by"]
            conn.execute("UPDATE commands SET handled_by=? WHERE id=?", (actor, command_id))
            self.store.command_event(conn, command_id, format_at(moment), "takeover",
                                     actor, {"from": previous})
            self.store.audit(conn, format_at(moment), actor, "pending_takeover",
                             {"command_id": command_id, "from": previous})
            row = self.store.command(conn, command_id)
            return self._command_dict(conn, row)

    # ------------------------------------------------------------------
    # 渠道会话推导与值班看板
    # ------------------------------------------------------------------

    def _command_derived(self, conn, command) -> tuple[str, str | None, bool]:
        latest = self.store.latest_observation(conn, command["id"])
        if latest is not None:
            return (ACTIVE if latest["heard"] else STOPPED, latest["observed_at"], False)
        uncertain = command["state"] in {SENT_UNKNOWN, ACCEPTED_PENDING, SEND_FAILED}
        return command["state"], None, uncertain

    def _channel_states(self, conn) -> list[ChannelState]:
        """把指令流折叠为每个 (渠道, 预警) 的当前会话。

        新启动覆盖同键旧会话；按预警停止终结对应会话；整组停终结该渠道上
        作用域相交的全部在播会话（另产生一条 alert=None 的整组记录）。
        这样迟到的旧启动回报不会把已经整组停掉的内容重新显示为在播。
        """
        sessions: dict[tuple[str, str | None], ChannelState] = {}
        group_events: list[ChannelState] = []
        for command in self.store.list_commands(conn):
            state, observed_at, uncertain = self._command_derived(conn, command)
            villages = tuple(storage.loads(command["villages_json"]))
            channel = command["channel"]
            action = command["action"]
            if action in START_ACTIONS:
                key = (channel, command["alert"])
                sessions[key] = ChannelState(
                    channel=channel, alert=command["alert"], villages=villages,
                    state=state, command_id=command["id"],
                    observed_at=observed_at, uncertain=uncertain,
                )
            elif action in STOP_ALERT_ACTIONS:
                key = (channel, command["alert"])
                if key in sessions:
                    sessions[key] = ChannelState(
                        channel=channel, alert=command["alert"],
                        villages=sessions[key].villages, state=state,
                        command_id=command["id"], observed_at=observed_at,
                        uncertain=uncertain,
                    )
            else:  # stop-group：整组停
                scope = set(villages)
                for key, session in list(sessions.items()):
                    if session.channel == channel and set(session.villages) & scope:
                        sessions[key] = ChannelState(
                            channel=channel, alert=session.alert,
                            villages=session.villages, state=state,
                            command_id=command["id"], observed_at=observed_at,
                            uncertain=uncertain,
                        )
                group_events.append(ChannelState(
                    channel=channel, alert=None, villages=villages, state=state,
                    command_id=command["id"], observed_at=observed_at, uncertain=uncertain,
                ))
        return list(sessions.values()) + group_events

    def village_board(self, village: str) -> dict:
        """按村查看：应播预警、各渠道实际状态、下一步可由谁处置。"""
        if village not in self.registry.village_owners:
            raise NotFoundError("village_not_found", {"village": village})
        now = self.clock.now()
        with self.store.tx() as conn:
            duty = self.duty_town(conn, village, now)
            delegation = self._delegation_view(conn)
            delegation_info = None
            if delegation and self.registry.owner_of(village) == delegation["row"]["owner"]:
                row = delegation["row"]
                delegation_info = {
                    "id": row["id"],
                    "delegate": row["delegate"],
                    "in_window": delegation["valid_from"] <= now < delegation["valid_until"],
                    "returned": village in delegation["returned"],
                }
            warnings_view = []
            for warning_row in self.store.list_warnings(conn):
                warning_id = warning_row["id"]
                if village not in storage.loads(warning_row["villages_json"]):
                    continue
                requested = self.store.withdrawal_villages(conn, warning_id)
                confirmed = self._confirmed_withdrawal_villages(conn, warning_id)
                channels = []
                for cs in self._channel_states(conn):
                    if cs.alert != warning_id or village not in cs.villages:
                        continue
                    channels.append(self._channel_state_dict(cs))
                if village in requested:
                    status = "withdrawal_confirmed" if village in confirmed else "withdrawal_pending"
                else:
                    status = "active_warning"
                warnings_view.append({
                    "warning": warning_id,
                    "status": status,
                    "withdrawal_requested": village in requested,
                    "withdrawal_confirmed": village in confirmed,
                    "channels": channels,
                })
            return {
                "village": village,
                "owner": self.registry.owner_of(village),
                "current_duty": duty,
                "delegation": delegation_info,
                "warnings": warnings_view,
                "next_actor": duty,
            }

    def board(self) -> dict:
        now = self.clock.now()
        with self.store.tx() as conn:
            villages = [self.village_board_locked(conn, v, now) for v in self.registry.villages]
            commands = [self._command_dict(conn, c) for c in self.store.list_commands(conn)]
            return {
                "generated_at": format_at(now),
                "towns": [
                    {"name": t, "online": self.store.town_online(conn, t)} for t in self.registry.towns
                ],
                "villages": villages,
                "commands": commands,
            }

    def village_board_locked(self, conn, village: str, now: datetime) -> dict:
        duty = self.duty_town(conn, village, now)
        warnings_view = []
        for warning_row in self.store.list_warnings(conn):
            warning_id = warning_row["id"]
            if village not in storage.loads(warning_row["villages_json"]):
                continue
            requested = self.store.withdrawal_villages(conn, warning_id)
            confirmed = self._confirmed_withdrawal_villages(conn, warning_id)
            channels = [
                self._channel_state_dict(cs)
                for cs in self._channel_states(conn)
                if cs.alert == warning_id and village in cs.villages
            ]
            status = (
                "withdrawal_confirmed" if village in confirmed
                else "withdrawal_pending" if village in requested
                else "active_warning"
            )
            warnings_view.append({
                "warning": warning_id, "status": status,
                "withdrawal_requested": village in requested,
                "withdrawal_confirmed": village in confirmed,
                "channels": channels,
            })
        return {"village": village, "owner": self.registry.owner_of(village),
                "current_duty": duty, "warnings": warnings_view}

    def get_command(self, command_id: str) -> dict:
        with self.store.tx() as conn:
            row = self.store.command(conn, command_id)
            if row is None:
                raise NotFoundError("command_not_found", {"id": command_id})
            return self._command_dict(conn, row)

    def list_observations(self, command_id: str | None = None) -> list[dict]:
        with self.store.tx() as conn:
            if command_id is not None:
                if self.store.command(conn, command_id) is None:
                    raise NotFoundError("command_not_found", {"id": command_id})
                rows = self.store.observations_for(conn, command_id)
            else:
                rows = conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
            return [dict(row) | {"heard": bool(row["heard"]), "stale": bool(row["stale"])}
                    for row in rows]

    def audit_log(self, limit: int = 100) -> list[dict]:
        with self.store.tx() as conn:
            rows = self.store.audit_tail(conn, limit)
            return [
                {"id": r["id"], "at": r["at"], "actor": r["actor"],
                 "action": r["action"], "detail": storage.loads(r["detail_json"])}
                for r in rows
            ]

    # -- 序列化 ---------------------------------------------------------

    @staticmethod
    def _channel_state_dict(cs: ChannelState) -> dict:
        return {
            "channel": cs.channel, "alert": cs.alert,
            "villages": list(cs.villages), "state": cs.state,
            "command_id": cs.command_id, "observed_at": cs.observed_at,
            "uncertain": cs.uncertain,
        }

    def _command_dict(self, conn, row) -> dict:
        latest = self.store.latest_observation(conn, row["id"])
        return {
            "id": row["id"], "channel": row["channel"], "action": row["action"],
            "alert": row["alert"], "villages": storage.loads(row["villages_json"]),
            "mode": row["kind"], "issued_by": row["issued_by"],
            "handled_by": row["handled_by"], "issued_at": row["issued_at"],
            "request": storage.loads(row["request_json"]), "state": row["state"],
            "remote_id": row["remote_id"], "last_error": row["last_error"],
            "latest_observation": (
                None if latest is None
                else {"kind": latest["kind"], "heard": bool(latest["heard"]),
                      "observed_at": latest["observed_at"], "stale": bool(latest["stale"])}
            ),
        }
