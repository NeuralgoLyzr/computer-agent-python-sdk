"""The app server for stage images: CAS's harness REST API in front of ComputerAgent.

Run it with ``python -m computeragent.server`` (``0.0.0.0:8080``); install with
the ``server`` extra. See ``docs/USING_THE_SDK.md`` §14.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
