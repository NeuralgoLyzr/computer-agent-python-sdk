"""The default Lambda MicroVM sandbox: the CAS SDK's app server, which old CAS
drives when an agent names no MicroVM image of its own.

On ``0.0.0.0:8080`` it serves

    POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,suspend,resume,terminate}  -> 200
    GET  /v1/health
    POST /v1/sessions, POST /v1/sessions/{id}/messages, GET /v1/sessions/{id}/events, ...

(the CAS harness API; ``docs/USING_THE_SDK.md`` §14). Nothing is baked into
``.env``. Old CAS passes the settings per session in ``POST /v1/sessions``
``envs``: ``COMPUTER_AGENT_ID``, ``COMPUTER_AGENT_EGRESS_PROXY_SECRET``,
``COMPUTER_AGENT_ENGINE`` (default ``claude-agent-sdk``) and the six
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
