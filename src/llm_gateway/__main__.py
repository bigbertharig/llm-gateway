from __future__ import annotations

import logging

from .server import Gateway, make_server
from .settings import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    settings.validate()
    server = make_server(Gateway(settings))
    logging.getLogger("llm_gateway").info(
        "listening on %s:%s (rig=%s, profiles=%s)",
        settings.bind, settings.port, " ".join(settings.rig_command), settings.profiles_path,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
