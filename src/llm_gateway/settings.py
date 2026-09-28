from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.json"


@dataclass(frozen=True)
class Settings:
    gateway_api_key: str
    openrouter_api_key: str
    openrouter_base_url: str
    orchestrator_url: str
    orchestrator_api_key: str
    model_catalog_path: Path
    request_timeout_seconds: float = 600.0
    local_session_ttl_seconds: int = 900
    local_session_renew_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gateway_api_key=os.getenv("GATEWAY_API_KEY", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            orchestrator_url=os.getenv(
                "ORCHESTRATOR_URL", "http://127.0.0.1:8790"
            ).rstrip("/"),
            orchestrator_api_key=os.getenv("ORCHESTRATOR_API_KEY", "").strip(),
            model_catalog_path=Path(
                os.getenv("MODEL_CATALOG_PATH", str(DEFAULT_CATALOG_PATH))
            ),
            request_timeout_seconds=float(
                os.getenv("REQUEST_TIMEOUT_SECONDS", "600")
            ),
            local_session_ttl_seconds=int(
                os.getenv("LOCAL_SESSION_TTL_SECONDS", "900")
            ),
            local_session_renew_seconds=int(
                os.getenv("LOCAL_SESSION_RENEW_SECONDS", "60")
            ),
        )

    def validate(self) -> None:
        if not self.gateway_api_key:
            raise RuntimeError("GATEWAY_API_KEY is required")
        if self.request_timeout_seconds <= 0:
            raise RuntimeError("REQUEST_TIMEOUT_SECONDS must be positive")
        if self.local_session_ttl_seconds <= 0:
            raise RuntimeError("LOCAL_SESSION_TTL_SECONDS must be positive")
        if self.local_session_renew_seconds <= 0:
            raise RuntimeError("LOCAL_SESSION_RENEW_SECONDS must be positive")
