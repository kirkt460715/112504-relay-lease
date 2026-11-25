"""测试支撑：内存假合作方与可控时钟。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from dispatch_backend.clock import parse_at
from dispatch_backend.partner import CommitDisconnect


class ManualClock:
    def __init__(self, start: str = "2028-07-03T12:30:00+08:00"):
        self.moment = parse_at(start)

    def now(self) -> datetime:
        return self.moment

    def set(self, value: str) -> None:
        self.moment = parse_at(value)

    def advance(self, minutes: int = 1) -> None:
        self.moment += timedelta(minutes=minutes)


class FakePartner:
    """按 request 内容脚本化应答；记录全部调用，支持断链与迟到回报。"""

    def __init__(self):
        self.scripts: list[dict] = []
        self.journal: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.disconnect: set[str] = set()

    def script(self, operation: str, request: dict, reply: dict,
               disconnect: bool = False) -> None:
        self.scripts.append({
            "operation": operation,
            "request": json.dumps(request, sort_keys=True, ensure_ascii=False),
            "reply": reply,
            "disconnect": disconnect,
        })

    def _find(self, operation: str, request: dict) -> dict:
        body = json.dumps(request, sort_keys=True, ensure_ascii=False)
        for item in self.scripts:
            if item["operation"] == operation and item["request"] == body:
                return item
        raise AssertionError(f"假合作方没有匹配样例: {operation} {body}")

    def submit(self, operation_id: str, operation: str, request: dict) -> dict:
        self.calls.append({"id": operation_id, "operation": operation, "request": request})
        if operation_id in self.journal:
            return self.journal[operation_id]["reply"]
        item = self._find(operation, request)
        self.journal[operation_id] = {"operation": operation, "request": request,
                                      "reply": item["reply"]}
        if item["disconnect"]:
            raise CommitDisconnect(operation_id)
        return item["reply"]

    def fetch(self, operation_id: str, operation: str, request: dict) -> dict:
        return self.submit(operation_id, operation, request)

    def query(self, operation_id: str) -> dict:
        if operation_id not in self.journal:
            from dispatch_backend.errors import ServiceError

            raise ServiceError("unknown_operation", 404, {"id": operation_id})
        return self.journal[operation_id]["reply"]

    def was_called(self, operation: str, request_subset: dict) -> bool:
        for call in self.calls:
            if call["operation"] != operation:
                continue
            if all(call["request"].get(k) == v for k, v in request_subset.items()):
                return True
        return False


def build_service(tmp_path, partner: FakePartner, clock: ManualClock | None = None):
    from pathlib import Path

    from dispatch_backend.registry import Registry, Channel
    from dispatch_backend.service import DispatchService
    from dispatch_backend.storage import Store

    base = Path(tmp_path)
    registry = Registry(
        towns=("north", "west"),
        village_owners={"ridge": "north", "ford": "north", "bank": "west"},
        channels={
            "speaker-1": Channel("speaker-1", ("ridge", "ford"), "whole_group"),
            "backup-sms": Channel("backup-sms", ("ridge", "ford"), "per_alert"),
            "speaker-3": Channel("speaker-3", ("ridge",), "per_alert",
                                 "2028-07-03T13:20:00+08:00"),
        },
    )
    store = Store(base / f"test-{uuid.uuid4().hex[:8]}.sqlite3")
    service = DispatchService(store, registry, partner, clock or ManualClock())
    return service, store, registry, service.clock
