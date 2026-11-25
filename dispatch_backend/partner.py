"""合作方/设备本地协议客户端。

对端契约（见 README 与 partner_stub.py）：
* ``POST /operations``，body 为 ``{"id", "operation", "request"}``；
* 接单成功返回 ``{"id", "reply"}``；
* ``commit_disconnect`` 表示设备已接单但 HTTP 连接中断，需用同一 ID 走
  ``GET /operations/<id>`` 查询；
* 同一 ID 同内容重试返回原回报，换内容返回 409；无匹配样例返回 422。

本模块不实现任何业务规则，只负责协议收发与故障分类。
"""
from __future__ import annotations

import json
from http.client import HTTPConnection, RemoteDisconnected
from typing import Any
from urllib.parse import urlparse

from .errors import ConflictError, ServiceError

DISPATCH = "dispatch"
OBSERVATION = "device-observation"


class PartnerUnavailable(ServiceError):
    """对端在接单前就失败（连接拒绝、超时、非预期 HTTP 状态）。"""

    def __init__(self, detail: dict):
        super().__init__("partner_unavailable", 502, detail)


class CommitDisconnect(ServiceError):
    """设备可能已接单，但首次 HTTP 连接没有响应。"""

    def __init__(self, operation_id: str):
        super().__init__("commit_disconnect", 504, {"id": operation_id})
        self.operation_id = operation_id


class PartnerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 3.0):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("base_url 必须是 http URL")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.prefix = parsed.path.rstrip("/")
        self.timeout = timeout

    def _connection(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=self.timeout)

    def submit(self, operation_id: str, operation: str, request: dict) -> dict:
        """提交一次对端操作，返回 reply；断链抛 :class:`CommitDisconnect`。"""
        body = json.dumps(
            {"id": operation_id, "operation": operation, "request": request},
            ensure_ascii=False,
        ).encode("utf-8")
        conn = self._connection()
        try:
            conn.request(
                "POST",
                self.prefix + "/operations",
                body=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            try:
                response = conn.getresponse()
            except RemoteDisconnected:
                # 桩在 commit_disconnect 时接单后直接断开，不能视为失败。
                raise CommitDisconnect(operation_id) from None
            payload = self._read_json(response)
        except OSError as exc:
            raise PartnerUnavailable({"reason": "connection_error", "detail": str(exc)}) from None
        finally:
            conn.close()

        if response.status == 200:
            return self._validate_reply(payload, operation_id)
        if response.status == 409:
            raise ConflictError("operation_id_conflict", payload)
        if response.status == 422:
            raise ServiceError("unsupported_example", 422, payload)
        raise PartnerUnavailable({"reason": "bad_status", "status": response.status, "body": payload})

    def fetch(self, operation_id: str, operation: str, request: dict) -> dict:
        """以独立操作 ID 提交一次对端查询（如 device-observation 监听回报）。"""
        return self.submit(operation_id, operation, request)

    def query(self, operation_id: str) -> dict[str, Any]:
        """按 ID 查询对端日志；未接单时抛 404 风格的 ServiceError。"""
        conn = self._connection()
        try:
            conn.request("GET", f"{self.prefix}/operations/{operation_id}")
            response = conn.getresponse()
            payload = self._read_json(response)
        except (OSError, RemoteDisconnected) as exc:
            raise PartnerUnavailable({"reason": "connection_error", "detail": str(exc)}) from None
        finally:
            conn.close()
        if response.status == 200:
            return self._validate_reply(payload, operation_id)
        if response.status == 404:
            raise ServiceError("unknown_operation", 404, {"id": operation_id})
        raise PartnerUnavailable({"reason": "bad_status", "status": response.status, "body": payload})

    @staticmethod
    def _read_json(response) -> dict:
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PartnerUnavailable({"reason": "bad_json"}) from None
        if not isinstance(payload, dict):
            raise PartnerUnavailable({"reason": "bad_payload"})
        return payload

    @staticmethod
    def _validate_reply(payload: dict, operation_id: str) -> dict:
        if payload.get("id") != operation_id or not isinstance(payload.get("reply"), dict):
            raise PartnerUnavailable({"reason": "bad_reply", "payload": payload})
        return payload["reply"]
