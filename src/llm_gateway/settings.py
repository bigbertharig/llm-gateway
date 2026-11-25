from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API_KEYS_FILE = Path("/etc/llm-gateway/api_keys")


def read_api_keys(path: Path) -> tuple[str, ...]:
    """One key per line; blank lines and # comments ignored. A missing file means no keys."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    return tuple(k for k in (line.strip() for line in lines) if k and not k.startswith("#"))


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    """All gateway configuration. Every value comes from the environment with an explicit default."""

    # Host that serves the profile ports: 127.0.0.1 on the rig, or the Pi's end of its rig tunnel.
    upstream_host: str
    # Model ids are published as "<prefix>/<profile id>".
    model_prefix: str
    bind: str
    port: int
    # Upper bound for one `rig start remote <id>` (cold 27B load is ~2m20s).
    start_timeout_seconds: float
    # Upper bound for one upstream inference request.
    request_timeout_seconds: float
    # Bearer keys clients must send. Empty means no auth, which is only allowed on a loopback bind.
    api_keys: tuple[str, ...] = ()
    runtime_enabled: bool = False
    orchestration_url: str = ''
    orchestration_token: str = ''

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_host=os.getenv("UPSTREAM_HOST", "127.0.0.1"),
            model_prefix=os.getenv("MODEL_PREFIX", "gpu-rig"),
            bind=os.getenv("GATEWAY_BIND", "127.0.0.1"),
            port=int(os.getenv("GATEWAY_PORT", "8080")),
            start_timeout_seconds=float(os.getenv("START_TIMEOUT_SECONDS", "900")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "900")),
            api_keys=read_api_keys(Path(os.getenv("GATEWAY_API_KEYS_FILE", str(DEFAULT_API_KEYS_FILE)))),
            runtime_enabled=os.getenv('GATEWAY_MODE', 'cloud' if not os.getenv('ORCHESTRATION_URL') else 'integrated') != 'cloud',
            orchestration_url=os.getenv('ORCHESTRATION_URL', ''),
            orchestration_token=os.getenv('ORCHESTRATION_CONTROL_TOKEN', ''),
        )

    def validate(self) -> None:
        if self.runtime_enabled and (not self.orchestration_url or not self.orchestration_token):
            raise RuntimeError('Local models require ORCHESTRATION_URL and ORCHESTRATION_CONTROL_TOKEN')
        if self.start_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise RuntimeError("timeouts must be positive")
        if not self.api_keys and not is_loopback(self.bind):
            raise RuntimeError(f"GATEWAY_BIND {self.bind} is not loopback: add keys to GATEWAY_API_KEYS_FILE first")
