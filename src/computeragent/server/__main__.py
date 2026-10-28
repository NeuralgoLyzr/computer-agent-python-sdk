"""``python -m computeragent.server``: the app server on ``0.0.0.0:8080``
(``PORT`` overrides the port)."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
