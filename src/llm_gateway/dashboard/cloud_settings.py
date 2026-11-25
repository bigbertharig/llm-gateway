"""Private cloud-provider credentials and curated model settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class CloudSettingsError(RuntimeError):
    pass


class CloudSettings:
    """Persist provider secrets outside the repository with strict permissions."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.getenv(
            "CLOUD_SETTINGS_ROOT", str(Path.home() / ".config/llm-gateway/cloud")))
        self.settings_path = self.root / "settings.json"
        self.openrouter_base_url = os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        if not self.openrouter_base_url.startswith("https://"):
            raise CloudSettingsError("OPENROUTER_BASE_URL must use HTTPS")

    def _ensure_root(self) -> None:
        if self.root.exists() and self.root.is_symlink():
            raise CloudSettingsError("Cloud settings directory cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _read_settings(self) -> dict:
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise CloudSettingsError("Cloud settings file is invalid") from exc
        if not isinstance(value, dict):
            raise CloudSettingsError("Cloud settings file must contain an object")
        return value

    def _write_bytes(self, path: Path, value: bytes) -> None:
        self._ensure_root()
        fd, temporary = tempfile.mkstemp(prefix=".cloud-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _write_settings(self, value: dict) -> None:
        self._write_bytes(self.settings_path,
                          (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())

    def _secret_path(self, provider: str) -> Path:
        if provider not in ("openrouter", "claude"):
            raise CloudSettingsError("Unknown cloud provider")
        return self.root / f"{provider}.key"

    def _stored_secret(self, provider: str) -> str | None:
        path = self._secret_path(provider)
        try:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise CloudSettingsError(f"{provider} API key permissions are too broad")
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CloudSettingsError(f"Cannot read {provider} API key") from exc
        if not value or "\n" in value:
            raise CloudSettingsError(f"{provider} API key file is invalid")
        return value

    def set_secret(self, provider: str, value: str) -> None:
        value = value.strip()
        if not value or "\n" in value or len(value) > 4096:
            raise CloudSettingsError("API key must be one nonempty line")
        self._write_bytes(self._secret_path(provider), (value + "\n").encode())

    def clear_secret(self, provider: str) -> None:
        path = self._secret_path(provider)
        if path.is_symlink():
            raise CloudSettingsError("Cloud API key path cannot be a symlink")
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def openrouter_models(self) -> list[str]:
        value = self._read_settings().get("openrouter_models", [])
        if not isinstance(value, list):
            raise CloudSettingsError("openrouter_models must be a list")
        return [item for item in value if isinstance(item, str) and item and "/" in item]

    def set_openrouter_models(self, models: list[str]) -> None:
        clean = []
        for model in models:
            model = model.strip()
            if not model or "/" not in model or len(model) > 240:
                raise CloudSettingsError(f"Invalid OpenRouter model ID: {model!r}")
            if model not in clean:
                clean.append(model)
        settings = self._read_settings()
        settings["openrouter_models"] = clean
        self._write_settings(settings)

    def claude_model(self) -> str | None:
        value = self._read_settings().get("claude_model")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set_claude_model(self, model: str) -> None:
        model = model.strip()
        if not model or len(model) > 240:
            raise CloudSettingsError("Claude model ID is required")
        settings = self._read_settings()
        settings["claude_model"] = model
        self._write_settings(settings)

    def openrouter_configuration(self) -> tuple[str, list[str]]:
        key = self._stored_secret("openrouter") or os.getenv("OPENROUTER_API_KEY", "").strip()
        models = self.openrouter_models()
        if not key:
            raise CloudSettingsError("OpenRouter is not configured; add an API key in Cloud")
        return key, models

    def claude_configuration(self) -> tuple[str, str]:
        stored_key = self._stored_secret("claude")
        stored_model = self.claude_model()
        if stored_key and stored_model:
            return stored_key, stored_model
        path = Path(os.getenv("CLAUDE_API_KEY_FILE", ""))
        model = os.getenv("CLAUDE_API_MODEL", "").strip()
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or not model:
            raise CloudSettingsError("Claude is not configured; add an API key and model in Cloud")
        if path.stat().st_mode & 0o077:
            raise CloudSettingsError("Claude API key permissions are too broad")
        key = path.read_text(encoding="utf-8").strip()
        if not key or "\n" in key:
            raise CloudSettingsError("Claude API key file is invalid")
        return key, model

    def status(self) -> dict:
        try:
            openrouter_key = bool(self._stored_secret("openrouter") or
                                  os.getenv("OPENROUTER_API_KEY", "").strip())
        except CloudSettingsError:
            openrouter_key = False
        try:
            self.claude_configuration()
            claude_key = True
        except CloudSettingsError:
            claude_key = False
        return {
            "openrouter": {"api_key_configured": openrouter_key,
                           "models": self.openrouter_models()},
            "claude": {"api_key_configured": claude_key,
                       "model": self.claude_model() or os.getenv("CLAUDE_API_MODEL", "").strip() or None},
        }
