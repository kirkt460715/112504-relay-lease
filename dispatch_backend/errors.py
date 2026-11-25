"""领域错误。

HTTP 层统一把 :class:`ServiceError` 映射为结构化 JSON 响应，
错误码同时用于值班台界面展示和自动化测试断言。
"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, http_status: int = 409, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.detail = detail or {}


class ValidationError(ServiceError):
    def __init__(self, code: str = "invalid_request", detail: dict | None = None):
        super().__init__(code, 400, detail)


class NotFoundError(ServiceError):
    def __init__(self, code: str = "not_found", detail: dict | None = None):
        super().__init__(code, 404, detail)


class ConflictError(ServiceError):
    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code, 409, detail)


class PermissionError(ServiceError):  # noqa: A001 - 领域命名优先于内建遮蔽
    def __init__(self, code: str = "forbidden", detail: dict | None = None):
        super().__init__(code, 403, detail)
