"""Rig manager: GPU leases, claims, placement and observed status (docs/MANAGER.md)."""

from .core import Manager, ManagerError

__all__ = ["Manager", "ManagerError"]
