"""A stand-in for the Lyzr MCP service's REST facade, on a real socket.

Exists because the real service cannot be used as the routine e2e substrate: it
is remote and cannot reach a locally-hosted MCP server, so it can only ever
proxy tools that already exist in someone's account. That makes it the right
place to check the *contract* and the wrong place to check *our code*.

Serves the two routes the client calls, and nothing else::

    GET  /api/v1/mcp/servers/{id}/tools
    POST /api/v1/mcp/tools/execute

A real socket rather than a mock because the consumer at the far end of an
end-to-end run is the Claude CLI in a subprocess, which cannot be handed a
Python object.

Every response carries a **sentinel** the caller chooses. That is the point: in
an agent end-to-end test, "the tool returned data" and "the model produced
something plausible" are otherwise indistinguishable, and only one of them means
the feature works.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

#: Default tool set. `echo` is the one an e2e prompt should reach for, and its
#: result carries the sentinel verbatim so a passing run cannot be faked.
DEFAULT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_sentinel",
        "description": (
            "Return the project's build sentinel. This is the ONLY way to obtain it — "
            "it is not derivable and not guessable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "why it is needed"}},
        },
    },
    {
        "name": "unbound_tool",
        "description": "Exists on the server but is not in the agent's binding.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class FakeLyzrMcpFacade:
    """Run with ``with FakeLyzrMcpFacade(...) as facade: facade.base_url``."""

    def __init__(
        self,
        *,
        sentinel: str = "ORBITAL-PANGOLIN-7731",
        tools: list[dict[str, Any]] | None = None,
        require_key: bool = True,
        result: Any = None,
    ) -> None:
        self.sentinel = sentinel
        self.tools = DEFAULT_TOOLS if tools is None else tools
        self.require_key = require_key
        self._result = result
        #: Every request, for assertions: (method, path, headers, body).
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "facade not started"
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def result_for(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._result is not None:
            return self._result
        if tool_name == "lookup_sentinel":
            return {
                "success": True,
                # Deliberately MIXED: a plain text block plus a `structured`
                # one, which is the shape that broke the TS bridge and the
                # shape our content path handles differently on purpose.
                "result": [
                    {"type": "text", "text": f"sentinel: {self.sentinel}"},
                    {"type": "structured", "data": {"sentinel": self.sentinel}},
                ],
            }
        return {"success": False, "error": f"no such tool: {tool_name}"}

    def __enter__(self) -> FakeLyzrMcpFacade:
        facade = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # noqa: N802 - quiet by default
                pass

            def _send(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self) -> bool:
                if not facade.require_key:
                    return True
                return bool(self.headers.get("x-api-key"))

            def do_GET(self) -> None:  # noqa: N802
                facade.requests.append(("GET", self.path, dict(self.headers), None))
                if not self._authed():
                    self._send(401, {"error": "an x-api-key credential is required"})
                elif self.path.startswith("/api/v1/mcp/servers/") and self.path.endswith("/tools"):
                    self._send(200, {"tools": facade.tools})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                facade.requests.append(("POST", self.path, dict(self.headers), body))
                if not self._authed():
                    self._send(401, {"error": "an x-api-key credential is required"})
                elif self.path == "/api/v1/mcp/tools/execute":
                    self._send(
                        200,
                        facade.result_for(
                            str(body.get("tool_name") or ""), body.get("arguments") or {}
                        ),
                    )
                else:
                    self._send(404, {"error": "not found"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


__all__ = ["DEFAULT_TOOLS", "FakeLyzrMcpFacade"]
