from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .settings import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    settings.validate()
    logging.getLogger("llm_gateway").info(
        "rig=%s profiles=%s bind=%s:%s auth=%s", " ".join(settings.rig_command), settings.profiles_path,
        settings.bind, settings.port, f"{len(settings.api_keys)} key(s)" if settings.api_keys else "off",
    )
    # Browser SSE status streams are intentionally long-lived. Bound graceful
    # shutdown so a deployment cancels them and reaches application cleanup
    # instead of waiting until systemd hard-kills the whole control group.
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, log_level="info",
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
