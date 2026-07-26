"""
MCP (Model Context Protocol) Client & Manager for EDUAgent.
Connects to external MCP servers via stdio JSON-RPC.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional


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


class MCPManager:
    """Manages multiple MCP server instances and routes tool calls."""

    def __init__(self):
        self.clients: List[MCPStdioClient] = []
        self._tool_map: dict[str, tuple[MCPStdioClient, str]] = {}
        self._tool_defs: List[dict] = []

    def load_from_json(self, json_path: str | Path) -> None:
        """Load MCP server definitions from mcp_servers.json."""
        path = Path(json_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[mcp] Error reading '{json_path}': {e}")
            return

        # Supports standard Claude Desktop format {"mcpServers": {"name": {"command": "...", "args": [...]}}}
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            for name, cfg in servers.items():
                if isinstance(cfg, dict):
                    cmd = cfg.get("command")
                    args = cfg.get("args", [])
                    if cmd:
                        full_cmd = [cmd] + list(args) if isinstance(args, list) else [cmd]
                        try:
                            self.add_server(full_cmd, server_name=name)
                            print(f"[mcp] Connected MCP server '{name}'")
                        except Exception as e:
                            print(f"[mcp] Failed to connect MCP server '{name}': {e}")

    def add_server(self, command: List[str] | str, server_name: Optional[str] = None) -> None:
        client = MCPStdioClient(command, server_name=server_name)
        client.start()
        self.clients.append(client)

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