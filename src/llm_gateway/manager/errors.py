from __future__ import annotations

from typing import Any

# `rig lease` / `rig claim` / `rig controller` / `rig plan` error kinds -> HTTP status.
KIND_STATUS = {"invalid": 400, "ownership": 403, "not_found": 404, "conflict": 409, "not_controller": 409,
               "state": 500}


class ManagerError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra
