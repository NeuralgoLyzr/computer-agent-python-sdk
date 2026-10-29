"""The default AgentCore sandbox: the CAS SDK's app server, which old CAS
drives when an agent names no AgentCore runtime of its own.

On ``0.0.0.0:8080`` it serves AgentCore's contract

    GET  /ping         {"status": "Healthy" | "HealthyBusy", "time_of_last_update": <unix s>}
    POST /invocations  relay envelope v1, dispatched to the CAS harness API

(``docs/USING_THE_SDK.md`` §14). Only the engine is baked into ``.env``. Old
CAS passes the rest per session in the ``POST /v1/sessions`` ``envs`` it relays:
``COMPUTER_AGENT_ID``, ``COMPUTER_AGENT_EGRESS_PROXY_SECRET`` and the six
``COMPUTER_AGENT_EGRESS_*_URL`` routes.

Run locally:

    pip install 'computer-agent-py[agentos,server,gitagent,otel]' python-dotenv
    cp .env.example .env
    python app.py
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from computeragent.server import create_app

# The app's .env (copied next to app.py in the image); variables already in the
# environment win over it.
load_dotenv(Path(__file__).with_name(".env"), override=False)

app = create_app()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="warning",
        access_log=False,
    )
