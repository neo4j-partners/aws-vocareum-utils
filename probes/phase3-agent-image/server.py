"""Smallest container an AgentCore Runtime will accept.

AgentCore Runtime requires an HTTP server on port 8080 that answers ``GET
/ping`` for health and ``POST /invocations`` for work. Nothing here is an
agent. The image exists so ``CreateAgentRuntime`` and ``InvokeAgentRuntime``
can be measured against a container that actually starts, instead of against
one that fails to boot and leaves the permission question unanswered.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080


class Handler(BaseHTTPRequestHandler):
    """Answer the two routes AgentCore Runtime contracts for."""

    def _send(self, status: int, payload: dict[str, object]) -> None:
        """Write one JSON response."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Answer the health check."""
        if self.path.rstrip("/") == "/ping":
            self._send(200, {"status": "Healthy"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Echo the payload back so an invocation can be proven end to end."""
        if self.path.rstrip("/") != "/invocations":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            received = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            received = {"raw": raw.decode("utf-8", "replace")}
        self._send(200, {"echo": received, "source": "phase3-permission-probe"})

    def log_message(self, fmt: str, *args: object) -> None:
        """Keep the container log to one line per request."""
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    """Serve until the container is stopped."""
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
