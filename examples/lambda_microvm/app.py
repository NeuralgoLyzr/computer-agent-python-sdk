"""A Lambda MicroVM app on the CAS SDK: the SDK's app server, which old CAS drives.

On ``0.0.0.0:8080`` it serves

    POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,suspend,resume,terminate}  -> 200
    GET  /v1/health
    POST /v1/sessions, POST /v1/sessions/{id}/messages, GET /v1/sessions/{id}/events, ...

(the CAS harness API; ``docs/USING_THE_SDK.md`` §14). The egress routes and the
engine come from ``.env`` next to this file (baked into the image). The agent
and the sandbox token come per session: old CAS passes ``COMPUTER_AGENT_ID``
and ``COMPUTER_AGENT_EGRESS_PROXY_SECRET`` in ``POST /v1/sessions`` ``envs``.

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
