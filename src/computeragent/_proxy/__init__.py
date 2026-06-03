"""Proxied entry points — same signatures as upstream, plus telemetry tap."""

from __future__ import annotations

from .client import ClaudeSDKClient
from .query import query

__all__ = ["ClaudeSDKClient", "query"]
