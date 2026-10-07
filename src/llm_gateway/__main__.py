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
        "rig=%s profiles=%s", " ".join(settings.rig_command), settings.profiles_path
    )
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
