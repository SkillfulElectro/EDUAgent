"""
MCP (Model Context Protocol) Client & Manager for EDUAgent.
Connects to external MCP servers via stdio subprocess or HTTP transport.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterator, List, Optional, Union


class MCPStdioClient:
    """Client for an individual MCP server running as a stdio subprocess."""

    def __init__(self, command: List[str] | str, server_name: Optional[str] = None):
        if isinstance(command, str):
            self.command = command.split()
        else:
            self.command = command
        self.server_name = server_name or (self.command[0] if self.command else "mcp")
        self.process: Optional[subprocess.Popen] = None
        self._req_id = 1

    def start(self) -> dict:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "EDUAgent", "version": "1.0.0"},
            },
        }
        res = self._send(init_req)
        self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def _next_id(self) -> int:
        rid = self._req_id
        self._req_id += 1
        return rid

    def _send(self, req: dict) -> dict:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError(f"MCP server '{self.server_name}' is not running.")
        line = json.dumps(req)
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        while True:
            out = self.process.stdout.readline()
            if not out:
                raise RuntimeError(f"MCP server '{self.server_name}' process terminated.")
            try:
                msg = json.loads(out)
                if msg.get("id") == req["id"]:
                    return msg
            except json.JSONDecodeError:
                continue

    def _notify(self, notif: dict) -> None:
        if self.process and self.process.stdin:
            line = json.dumps(notif)
            self.process.stdin.write(line + "\n")
            self.process.stdin.flush()

    def list_tools(self) -> List[dict]:
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }
        res = self._send(req)
        return res.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        res = self._send(req)
        if "error" in res:
            return f"MCP Tool Error: {res['error']}"
        result = res.get("result", {})
        content = result.get("content", [])
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if texts:
            return "\n".join(texts)
        return json.dumps(result)

    def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                pass


class MCPHttpClient:
    """Client for an MCP server accessible over HTTP (Streamable HTTP transport).

    Supports both plain JSON responses and Server-Sent Events (SSE) streaming
    as defined by the MCP Streamable HTTP transport spec:
      - POST {url}/message  — send JSON-RPC request, receive JSON or SSE response
      - GET  {url}/sse       — optional SSE stream for server→client notifications
    """

    def __init__(
        self,
        url: str,
        server_name: Optional[str] = None,
        headers: Optional[dict] = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
    ):
        self.url = url.rstrip("/")
        self.server_name = server_name or url
        self.headers = headers or {}
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self._req_id = 1
        self._initialized = False

    # ------------------------------------------------------------------
    # SSE parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sse(text: str) -> List[dict]:
        """Parse an SSE (text/event-stream) body into a list of JSON messages.

        Handles standard SSE fields: event, data, id, retry. Comments (lines
        starting with ':') are ignored.  Multiple 'data' lines for the same
        event are joined with newlines.
        """
        messages: List[dict] = []
        current_event: Optional[str] = None
        data_lines: List[str] = []

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r\n")

            # Dispatch previous event on empty line (event boundary)
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    # A JSON message can be the entire data, or data may be a
                    # JSON-RPC envelope. Try to parse it as JSON.
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        msg = {"_sse_data": payload}
                    if current_event:
                        msg["_sse_event"] = current_event
                    messages.append(msg)
                current_event = None
                data_lines = []
                continue

            # Comment line — skip
            if line.startswith(":"):
                continue

            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                val = line[5:]
                if val.startswith(" "):
                    val = val[1:]
                data_lines.append(val)
            # id: and retry: fields are silently ignored

        # Flush any remaining event at EOF
        if data_lines:
            payload = "\n".join(data_lines)
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                msg = {"_sse_data": payload}
            if current_event:
                msg["_sse_event"] = current_event
            messages.append(msg)

        return messages

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request_headers(self, accept_sse: bool = True) -> dict:
        """Build request headers including Accept negotiation."""
        h = {**self.headers, "Content-Type": "application/json"}
        if accept_sse:
            h["Accept"] = "text/event-stream, application/json"
        return h

    def _read_response(self, response) -> dict:
        """Read a response body that may be JSON or SSE.

        - If Content-Type is text/event-stream, parse as SSE and return the
          first JSON-RPC message whose `id` matches the request.
        - Otherwise parse as plain JSON.
        """
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            messages = self._parse_sse(response.text)
            for msg in messages:
                # Return the first JSON-RPC looking message
                if "jsonrpc" in msg:
                    return msg
            # If no JSON-RPC envelope found, return last parsed message
            return messages[-1] if messages else {}
        else:
            return response.json()

    def _read_sse_stream(self, response) -> List[dict]:
        """Read a full SSE stream and return all JSON-RPC messages."""
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(response.text)
        else:
            # Plain JSON fallback — wrap as single-element list
            return [response.json()]

    def _http_request(self, method: str, path: str, json_body: Optional[dict] = None) -> "httpx.Response":
        """Low-level HTTP request with uniform error handling."""
        import httpx

        url = f"{self.url}{path}"
        try:
            if method.upper() == "POST":
                response = httpx.post(
                    url,
                    json=json_body,
                    headers=self._request_headers(),
                    timeout=self.timeout,
                )
            elif method.upper() == "GET":
                response = httpx.get(
                    url,
                    headers=self._request_headers(),
                    timeout=self.sse_read_timeout,
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP {e.response.status_code} from MCP server "
                f"'{self.server_name}': {e.response.text[:500]}"
            )
        except httpx.RequestError as e:
            raise RuntimeError(
                f"HTTP request failed for MCP server '{self.server_name}': {e}"
            )

    # ------------------------------------------------------------------
    # JSON-RPC primitives
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        rid = self._req_id
        self._req_id += 1
        return rid

    def _send(self, req: dict) -> dict:
        """Send a JSON-RPC request over HTTP and return the response.

        Automatically handles SSE streaming responses — if the server returns
        text/event-stream, the stream is parsed and the first matching JSON-RPC
        response is returned.
        """
        response = self._http_request("POST", "/message", json_body=req)
        return self._read_response(response)

    def _send_stream(self, req: dict) -> List[dict]:
        """Send a JSON-RPC request and collect *all* responses from the SSE stream.

        Useful for streaming tool calls where the server may emit multiple
        progress events before the final result.
        """
        response = self._http_request("POST", "/message", json_body=req)
        return self._read_sse_stream(response)

    def _notify(self, notif: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        import httpx

        try:
            httpx.post(
                f"{self.url}/message",
                json=notif,
                headers=self._request_headers(),
                timeout=self.timeout,
            )
        except httpx.RequestError:
            pass  # Notifications are fire-and-forget

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> dict:
        """Initialize the MCP session over HTTP."""
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "EDUAgent", "version": "1.0.0"},
            },
        }
        data = self._send(init_req)

        # Send initialized notification
        self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return data

    def open_sse_stream(self) -> "Iterator[dict]":
        """Open a long-lived SSE stream (GET /sse) for server→client push.

        Yields parsed JSON messages as they arrive.  This is intended to be
        used in a background thread/task to receive server-initiated
        notifications (e.g. resource updates).

        Example:
            for event in client.open_sse_stream():
                print(f"Server push: {event}")
        """
        import httpx

        url = f"{self.url}/sse"
        try:
            with httpx.stream(
                "GET",
                url,
                headers=self._request_headers(),
                timeout=self.sse_read_timeout,
            ) as response:
                response.raise_for_status()
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    # Process complete events (separated by blank lines)
                    while "\n\n" in buffer:
                        event_block, buffer = buffer.split("\n\n", 1)
                        messages = self._parse_sse(event_block + "\n\n")
                        yield from messages
                # Flush remainder
                if buffer.strip():
                    messages = self._parse_sse(buffer)
                    yield from messages
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP {e.response.status_code} from SSE stream "
                f"'{self.server_name}': {e.response.text[:500]}"
            )
        except httpx.RequestError as e:
            raise RuntimeError(
                f"SSE stream failed for MCP server '{self.server_name}': {e}"
            )

    def list_tools(self) -> List[dict]:
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }
        res = self._send(req)
        return res.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call an MCP tool. Supports SSE streaming responses."""
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        messages = self._send_stream(req)

        # Collect all text content from all messages in the stream
        all_texts: List[str] = []
        for msg in messages:
            if "error" in msg:
                all_texts.append(f"MCP Tool Error: {msg['error']}")
                continue
            result = msg.get("result", {})
            content = result.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    all_texts.append(item.get("text", ""))

        if all_texts:
            return "\n".join(all_texts)
        # Fallback: return raw JSON of last message
        last = messages[-1] if messages else {}
        result = last.get("result", {})
        return json.dumps(result)

    def call_tool_stream(self, name: str, arguments: dict) -> "Iterator[str]":
        """Call a tool and yield text chunks as they arrive via SSE.

        Yields individual text blocks from SSE events as the server streams
        them.  This provides real-time output for long-running tool calls.
        """
        import httpx

        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

        url = f"{self.url}/message"
        try:
            with httpx.stream(
                "POST",
                url,
                json=req,
                headers=self._request_headers(),
                timeout=self.sse_read_timeout,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    buffer = ""
                    for chunk in response.iter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            event_block, buffer = buffer.split("\n\n", 1)
                            messages = self._parse_sse(event_block + "\n\n")
                            for msg in messages:
                                if "error" in msg:
                                    yield f"MCP Tool Error: {msg['error']}"
                                    return
                                result = msg.get("result", {})
                                content = result.get("content", [])
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        yield item.get("text", "")
                    # Flush remaining
                    if buffer.strip():
                        messages = self._parse_sse(buffer)
                        for msg in messages:
                            if "error" in msg:
                                yield f"MCP Tool Error: {msg['error']}"
                                return
                            result = msg.get("result", {})
                            content = result.get("content", [])
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    yield item.get("text", "")
                else:
                    # Plain JSON — parse and yield once
                    data = response.json()
                    if "error" in data:
                        yield f"MCP Tool Error: {data['error']}"
                        return
                    result = data.get("result", {})
                    content = result.get("content", [])
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            yield item.get("text", "")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP {e.response.status_code} from MCP server "
                f"'{self.server_name}': {e.response.text[:500]}"
            )
        except httpx.RequestError as e:
            raise RuntimeError(
                f"HTTP stream request failed for MCP server '{self.server_name}': {e}"
            )

    def stop(self) -> None:
        """No persistent connection to close for HTTP transport."""
        pass


MCPClient = Union[MCPStdioClient, MCPHttpClient]


class MCPManager:
    """Manages multiple MCP server instances (stdio + HTTP) and routes tool calls."""

    def __init__(self):
        self.clients: List[MCPClient] = []
        self._tool_map: dict[str, tuple[MCPClient, str]] = {}
        self._tool_defs: List[dict] = []

    def load_from_json(self, json_path: str | Path) -> None:
        """Load MCP server definitions from mcp_servers.json.

        Supports two formats:
        1. Stdio (Claude Desktop format):
           {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        2. HTTP transport:
           {"transport": "http", "url": "https://example.com/mcp", "headers": {...}}
        """
        path = Path(json_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[mcp] Error reading '{json_path}': {e}")
            return

        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            for name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue

                transport = cfg.get("transport", "stdio")
                try:
                    if transport == "http":
                        url = cfg.get("url")
                        if not url:
                            print(f"[mcp] Skipping HTTP MCP server '{name}': missing 'url'")
                            continue
                        headers = cfg.get("headers", {})
                        timeout = cfg.get("timeout", 30.0)
                        sse_read_timeout = cfg.get("sse_read_timeout", 300.0)
                        self.add_http_server(
                            url,
                            server_name=name,
                            headers=headers,
                            timeout=timeout,
                            sse_read_timeout=sse_read_timeout,
                        )
                        print(f"[mcp] Connected HTTP MCP server '{name}' at {url}")
                    else:
                        # Stdio transport
                        cmd = cfg.get("command")
                        args = cfg.get("args", [])
                        if cmd:
                            full_cmd = [cmd] + list(args) if isinstance(args, list) else [cmd]
                            self.add_server(full_cmd, server_name=name)
                            print(f"[mcp] Connected MCP server '{name}'")
                except Exception as e:
                    print(f"[mcp] Failed to connect MCP server '{name}': {e}")

    def _register_tools(self, client: MCPClient) -> None:
        """Register tools from a connected client into the tool map."""
        mcp_tools = client.list_tools()
        for tool in mcp_tools:
            name = tool.get("name")
            desc = tool.get("description", "")
            schema = tool.get("inputSchema", {})
            params = schema.get("properties", {})

            self._tool_map[name] = (client, name)
            self._tool_defs.append({
                "name": name,
                "description": f"[MCP Server: {client.server_name}] {desc}",
                "parameters": params,
            })

    def add_server(self, command: List[str] | str, server_name: Optional[str] = None) -> None:
        """Add a stdio-based MCP server."""
        client = MCPStdioClient(command, server_name=server_name)
        client.start()
        self.clients.append(client)
        self._register_tools(client)

    def add_http_server(
        self,
        url: str,
        server_name: Optional[str] = None,
        headers: Optional[dict] = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
    ) -> None:
        """Add an HTTP-based MCP server."""
        client = MCPHttpClient(
            url,
            server_name=server_name,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )
        client.start()
        self.clients.append(client)
        self._register_tools(client)

    def get_tool_definitions(self) -> List[dict]:
        return self._tool_defs

    def has_tool(self, name: str) -> bool:
        return name in self._tool_map

    def call_tool(self, name: str, arguments: dict) -> str:
        if name not in self._tool_map:
            return f"Error: Unknown MCP tool '{name}'"
        client, orig_name = self._tool_map[name]
        return client.call_tool(orig_name, arguments)

    def close(self) -> None:
        for client in self.clients:
            client.stop()