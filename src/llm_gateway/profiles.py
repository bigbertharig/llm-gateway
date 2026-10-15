from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path


class ProfileError(RuntimeError):
    pass


class Profiles:
    """remote_profiles from the benchmarking repo's model_roles.json, re-read whenever it changes.

    In the benchmarking repo a remote profile names a model and a registry profile, and the
    repo's scripts/active/model_library.py resolves them to ctx_size, gguf_path, and so on.
    A file without that library beside it is read as already-resolved profiles.
    """

    def __init__(self, path: Path):
        self.path = path
        self._library = path.parent / "scripts" / "active" / "model_library.py"
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._profiles: dict[str, dict] = {}

    def _watched(self) -> list[Path]:
        if self._library.is_file():
            return [self.path, self.path.parent / "model_registry.json"]
        return [self.path]

    def _load(self) -> dict[str, dict]:
        if self._library.is_file():
            spec = importlib.util.spec_from_file_location("rig_model_library", self._library)
            library = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(library)
            return library.remote_profiles(
                library.load_roles(self.path),
                library.load_registry(self.path.parent / "model_registry.json"),
            )
        items = json.loads(self.path.read_text(encoding="utf-8")).get("remote_profiles")
        if not isinstance(items, list):
            return {}
        return {str(p["id"]): p for p in items}

    def all(self) -> dict[str, dict]:
        with self._lock:
            try:
                stamp = tuple(p.stat().st_mtime for p in self._watched())
            except OSError as exc:
                raise ProfileError(f"cannot read {self.path}: {exc}") from exc
            if stamp != self._stamp:
                try:
                    profiles = self._load()
                except (OSError, ValueError, KeyError) as exc:
                    raise ProfileError(f"cannot load profiles from {self.path}: {exc}") from exc
                if not profiles:
                    raise ProfileError(f"no remote_profiles in {self.path}")
                self._profiles = profiles
                self._stamp = stamp
            return dict(self._profiles)
