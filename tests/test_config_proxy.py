"""Tests for the per-machine configuration page proxied by the Manager."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# manager.main creates its SQLite database at import time; keep tests out of the runtime data dir.
if not os.environ.get("AB2_DATA_DIR"):
    os.environ["AB2_DATA_DIR"] = tempfile.mkdtemp(prefix="ab2-config-proxy-test-")

from agent.service import AgentService
from manager.main import ManagerState, inject_config_proxy


class ConfigHandler(BaseHTTPRequestHandler):
    """Act as the Agent's local configuration server inside this test process."""

    def _write(self, body: bytes) -> None:
        """Send one UTF-8 JSON response."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Return a representative Agent config payload."""
        self._write(b'{"id":"agent","name":"build-machine"}')

    def do_POST(self) -> None:
        """Echo the submitted config body so the test proves POST forwarding."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._write(b'{"ok":true,"received":' + body + b'}')

    def log_message(self, format: str, *args: object) -> None:
        """Suppress the default per-request stderr noise."""
        pass


class ConfigPageProxyTests(unittest.TestCase):
    """Cover the response rewrite that keeps absolute Agent URLs under the proxy prefix."""

    def test_absolute_fetch_and_popup_urls_are_rewritten(self) -> None:
        """Every absolute request from the Agent page must stay inside its machine prefix."""
        html = inject_config_proxy("<!doctype html><html><head></head><body></body></html>", "10.0.0.8")
        self.assertIn('var p="/agents/10.0.0.8/config"', html)
        self.assertIn("window.fetch", html)
        self.assertIn("window.open", html)
        self.assertLess(html.index("<script>"), html.index("</head>"))


class ManagerProxyTests(unittest.IsolatedAsyncioTestCase):
    """Verify request/response correlation on the Manager's WebSocket proxy."""

    class FakeSocket:
        """Capture commands sent to an Agent socket."""

        def __init__(self) -> None:
            """Start with no recorded commands."""
            self.sent: list[dict[str, object]] = []

        async def send_json(self, payload: dict[str, object]) -> None:
            """Record one outgoing proxy command from the Manager side."""
            self.sent.append(payload)

        async def send(self, payload: str) -> None:
            """Record one outgoing proxy response from the Agent side."""
            self.sent.append(payload)

    class FakeConnection:
        """Stand in for AgentConnection with an in-memory pending request map."""

        def __init__(self) -> None:
            """Create the socket and pending request registry."""
            self.socket = ManagerProxyTests.FakeSocket()
            self.pending: dict[str, asyncio.Future] = {}
            self.info: dict[str, object] = {"capabilities": ["config_proxy"]}

    async def test_proxy_waits_for_matching_agent_response(self) -> None:
        """A response carrying the request id must resolve the browser request."""
        state = ManagerState.__new__(ManagerState)
        connection = self.FakeConnection()
        state.connections = {"10.0.0.8": connection}
        task = asyncio.create_task(state.proxy_web("10.0.0.8", "GET", "/api/config", b"", {}))
        while not connection.socket.sent:
            await asyncio.sleep(0)
        command = connection.socket.sent[0]
        self.assertEqual("proxy_web", command["type"])
        self.assertEqual("/api/config", command["path"])
        connection.pending[command["request_id"]].set_result({"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'})
        self.assertEqual({"status": 200, "headers": {"Content-Type": "application/json"}, "body": '{"ok":true}'}, await task)


class AgentLocalProxyTests(unittest.IsolatedAsyncioTestCase):
    """Verify that the Agent fetches its own configuration server over loopback."""

    async def asyncSetUp(self) -> None:
        """Start a local stand-in for the Agent configuration page."""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.service = AgentService.__new__(AgentService)
        self.service.web_base = f"http://127.0.0.1:{self.server.server_address[1]}"

    async def asyncTearDown(self) -> None:
        """Stop the local test server."""
        self.server.shutdown()
        self.server.server_close()

    async def test_get_and_post_are_forwarded(self) -> None:
        """Both page reads and config saves must reach the Agent's local server."""
        socket = ManagerProxyTests.FakeSocket()
        await self.service._proxy_web_request(socket, {"type": "proxy_web", "request_id": "get-1", "method": "GET", "path": "/api/config"})
        await self.service._proxy_web_request(socket, {"type": "proxy_web", "request_id": "post-1", "method": "POST", "path": "/api/config", "body": '{"name":"x"}', "headers": {"Content-Type": "application/json"}})
        get_result, post_result = (json.loads(payload) for payload in socket.sent)
        self.assertEqual(200, get_result["status"])
        self.assertEqual('{"id":"agent","name":"build-machine"}', get_result["body"])
        self.assertEqual(200, post_result["status"])
        self.assertIn('"received":{"name":"x"}', post_result["body"])


if __name__ == "__main__":
    unittest.main()
