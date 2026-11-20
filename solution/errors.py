"""Error type used for every rejected request.

A ``ReclaimError`` always maps to a JSON response ``{"ok": false, "code":
..., "message": ..., "detail": ...}``.  Rejections never modify business
state; when they happen for a well formed envelope the response is persisted
as the request's idempotency receipt as well.
"""

from __future__ import annotations

from typing import Any


class ReclaimError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def response(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }

