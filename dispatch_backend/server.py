"""值班台 HTTP API。

仅监听本机。除 GET 看板外，所有写接口都要求 JSON body 带 ``actor``
（值班乡镇标识），服务端按当前处置权决定是否受理。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import ServiceError
from .service import DispatchService


def _json_default(value: Any) -> Any:  # pragma: no cover - 兜底
    return str(value)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DispatchBackend/1.0"

    # -- 框架 -----------------------------------------------------------

    def send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=_json_default
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise ServiceError("invalid_request", 400, {"error": "invalid_json"}) from None
        return data

    def handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ServiceError):
            self.send_json(exc.http_status, {"error": exc.code, "detail": exc.detail})
        else:
            self.send_json(500, {"error": "internal_error", "detail": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        try:
            service: DispatchService = self.server.service  # type: ignore[attr-defined]
            path = self.path.rstrip("/") or "/"
            if path == "/health":
                self.send_json(200, {"status": "ok"})
            elif path == "/board":
                self.send_json(200, service.board())
            elif path.startswith("/board/villages/"):
                village = path.split("/")[-1]
                self.send_json(200, service.village_board(village))
            elif path.startswith("/warnings/") and path.endswith("/withdrawal"):
                warning_id = path.split("/")[2]
                self.send_json(200, service.withdrawal_plan(warning_id))
            elif path.startswith("/warnings/"):
                warning_id = path.split("/")[2]
                self.send_json(200, service.get_warning(warning_id))
            elif path.startswith("/delegations/"):
                delegation_id = path.split("/")[2]
                self.send_json(200, service.get_delegation(delegation_id))
            elif path == "/delegations":
                self.send_json(200, {"delegations": service.list_delegations()})
            elif path.startswith("/commands/") and path.endswith("/observations"):
                command_id = path.split("/")[2]
                self.send_json(200, {"observations": service.list_observations(command_id)})
            elif path.startswith("/commands/"):
                command_id = path.split("/")[-1]
                self.send_json(200, service.get_command(command_id))
            elif path == "/observations":
                self.send_json(200, {"observations": service.list_observations()})
            elif path == "/audit":
                self.send_json(200, {"events": service.audit_log()})
            else:
                self.send_json(404, {"error": "not_found"})
        except Exception as exc:  # 边界兜底，保证返回结构化错误
            self.handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            service: DispatchService = self.server.service  # type: ignore[attr-defined]
            path = self.path.rstrip("/")
            body = self.read_body()
            actor = body.get("actor")
            at = body.get("at")
            if not isinstance(actor, str):
                raise ServiceError("invalid_request", 400, {"error": "actor_required"})

            route = self._route(service, path, body, actor, at)
            self.send_json(200, route())
        except Exception as exc:
            self.handle_error(exc)

    def _route(
        self, service: DispatchService, path: str, body: dict, actor: str, at: str | None
    ) -> Callable[[], Any]:
        def field(name: str, required: bool = True, default: Any = None) -> Any:
            if name not in body:
                if required:
                    raise ServiceError("invalid_request", 400, {"error": f"missing_{name}"})
                return default
            return body[name]

        if path == "/towns/online":
            return lambda: service.set_town_online(
                actor, field("town"), bool(field("online")), at
            )
        if path == "/delegations":
            return lambda: service.create_delegation(
                actor,
                delegation_id=field("id"),
                owner=field("owner"),
                delegate=field("delegate"),
                villages=field("villages"),
                channels=field("channels"),
                valid_from=field("valid_from"),
                valid_until=field("valid_until"),
                at=at,
            )
        if path.startswith("/delegations/") and path.endswith("/return"):
            delegation_id = path.split("/")[2]
            return lambda: service.return_village(
                actor, delegation_id, field("village"), at
            )
        if path == "/warnings":
            return lambda: service.issue_warning(
                actor, field("id"), field("villages"), at
            )
        if path == "/dispatch":
            return lambda: service.dispatch(
                actor,
                channel_name=field("channel"),
                action=field("action"),
                alert=field("alert", required=False),
                villages=field("villages", required=False),
                command_id=field("id", required=False),
                at=at,
            )
        if path.startswith("/commands/") and path.endswith("/reconcile"):
            command_id = path.split("/")[2]
            return lambda: service.reconcile(actor, command_id, at)
        if path.startswith("/commands/") and path.endswith("/retry"):
            command_id = path.split("/")[2]
            return lambda: service.retry_command(actor, command_id, at)
        if path.startswith("/commands/") and path.endswith("/takeover"):
            command_id = path.split("/")[2]
            return lambda: service.takeover_pending(actor, command_id, at)
        if path == "/observations":
            return lambda: service.record_observation(
                actor,
                remote_id=field("remote_id"),
                kind=field("kind"),
                heard=bool(field("heard")),
                observed_at=field("observed_at"),
                at=at,
            )
        if path == "/observations/fetch":
            return lambda: service.fetch_observation(
                actor,
                operation_id=field("id"),
                remote_id=field("remote_id"),
                kind=field("kind"),
                at=at,
            )
        if path == "/withdrawals":
            return lambda: service.request_withdrawal(
                actor, field("warning"), field("villages"), at
            )
        raise ServiceError("not_found", 404, {"path": path})

    def log_message(self, *args: Any) -> None:
        pass


def build_server(service: DispatchService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = service  # type: ignore[attr-defined]
    return server
