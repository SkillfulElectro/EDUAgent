"""
MCP (Model Context Protocol) Client & Manager for EDUAgent.
Thin wrapper around the official `mcp` Python SDK.

Supports stdio (subprocess) and HTTP (Streamable HTTP) transports
configured via mcp_servers.json (Claude Desktop format).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSessionGroup
from mcp.client.session_group import StreamableHttpParameters
from mcp.client.stdio import StdioServerParameters
from mcp.types import (
    CallToolResult,
    Implementation,
)

_logger = logging.getLogger(__name__)


def _extract_text_from_result(result: CallToolResult) -> str:
    """Extract text content from a CallToolResult into a single string."""
    texts: List[str] = []
    for block in result.content:
        if hasattr(block, "text"):
            texts.append(block.text)
    if texts:
        return "\n".join(texts)
    # Fallback: serialize the whole result
    return json.dumps(result.model_dump(), default=str)


def _sanitize_config_key(key: str) -> str:
    """Convert a config key (e.g. 'my-server') to a safe tool-name prefix."""
    return key.strip().lower().replace(" ", "_").replace("-", "_")


class MCPManager:
    """Manages multiple MCP server connections via the official mcp SDK.

    Uses a persistent background event loop to bridge async SDK calls
    into EDUAgent's synchronous codebase.

    Public API (kept compatible with the previous custom implementation):
        load_from_json(json_path)
        add_server(command, server_name)
        add_http_server(url, server_name, ...)
        get_tool_definitions() -> List[dict]
        has_tool(name) -> bool
        call_tool(name, arguments) -> str
        close()
    """

    def __init__(self):
        self._tool_defs: List[dict] = []

        # Used by the component_name_hook to prefix tool names with the
        # config key of the server currently being connected.
        self._current_connect_prefix: Optional[str] = None

        # Persistent event loop in a daemon thread.
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread: threading.Thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="mcp-event-loop"
        )
        self._thread.start()

        # Create the session group on the event loop.
        self._group: ClientSessionGroup = self._run_async(
            self._create_group()
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _create_group(self) -> ClientSessionGroup:
        """Create the ClientSessionGroup with a component-name hook."""
        return ClientSessionGroup(
            component_name_hook=self._component_name_hook,
        )

    def _component_name_hook(
        self, tool_name: str, server_info: Implementation
    ) -> str:
        """Prefix tool names with the config key to avoid collisions.

        Since servers are connected sequentially on the same event loop,
        ``_current_connect_prefix`` tells us which config entry the current
        batch of tools belongs to.
        """
        if self._current_connect_prefix:
            return f"{self._current_connect_prefix}.{tool_name}"
        return tool_name

    def _run_async(self, coro):
        """Run a coroutine on the persistent event loop and return its result."""
        if not self._loop.is_running():
            raise RuntimeError("MCP event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=60)

    def _refresh_tool_defs(self) -> None:
        """Rebuild the cached tool definitions from the session group."""
        self._tool_defs.clear()
        for name, tool in self._group.tools.items():
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            params = schema.get("properties", {})
            self._tool_defs.append({
                "name": name,
                "description": tool.description or "",
                "parameters": params,
            })

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def load_from_json(self, json_path: str | Path) -> None:
        """Load MCP server definitions from a JSON config file.

        Supports two formats:
        1. Stdio (Claude Desktop format):
           {"command": "npx", "args": ["-y", "..."]}
        2. HTTP transport:
           {"transport": "http", "url": "https://...", "headers": {...}}
        """
        path = Path(json_path)
        if not path.exists():
            _logger.debug(f"MCP config '{json_path}' not found, skipping.")
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            _logger.warning(f"Error reading MCP config '{json_path}': {e}")
            return

        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            return

        async def _connect_all():
            for config_name, cfg in servers.items():
                if not isinstance(cfg, dict):
                    continue
                try:
                    transport = cfg.get("transport", "stdio")
                    if transport == "http":
                        params = self._build_http_params(config_name, cfg)
                    else:
                        params = self._build_stdio_params(config_name, cfg)

                    if params is None:
                        continue

                    # Set the prefix so the component_name_hook knows which
                    # server these tools belong to.
                    prefix = _sanitize_config_key(config_name)
                    self._current_connect_prefix = prefix
                    try:
                        session = await self._group.connect_to_server(params)
                        _logger.info(
                            f"[mcp] Connected to '{config_name}' "
                            f"(session: {id(session)})"
                        )
                    finally:
                        self._current_connect_prefix = None
                except Exception as e:
                    _logger.warning(
                        f"[mcp] Failed to connect MCP server '{config_name}': {e}"
                    )

        self._run_async(_connect_all())
        self._refresh_tool_defs()

    def _build_stdio_params(
        self, config_name: str, cfg: dict
    ) -> Optional[StdioServerParameters]:
        """Build StdioServerParameters from a config entry."""
        cmd = cfg.get("command")
        if not cmd:
            _logger.warning(
                f"[mcp] Skipping stdio server '{config_name}': missing 'command'"
            )
            return None
        args = cfg.get("args", [])
        if isinstance(args, list):
            args = [str(a) for a in args]
        else:
            args = [str(args)]
        env = cfg.get("env")  # optional dict[str, str]
        cwd = cfg.get("cwd")  # optional str

        return StdioServerParameters(
            command=cmd,
            args=args,
            env=env,
            cwd=cwd,
        )

    def _build_http_params(
        self, config_name: str, cfg: dict
    ) -> Optional[StreamableHttpParameters]:
        """Build StreamableHttpParameters from a config entry."""
        url = cfg.get("url")
        if not url:
            _logger.warning(
                f"[mcp] Skipping HTTP server '{config_name}': missing 'url'"
            )
            return None
        headers = cfg.get("headers")
        timeout = float(cfg.get("timeout", 30.0))
        sse_read_timeout = float(cfg.get("sse_read_timeout", 300.0))

        return StreamableHttpParameters(
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )

    # ------------------------------------------------------------------
    # Programmatic server addition
    # ------------------------------------------------------------------

    def add_server(
        self, command: List[str] | str, server_name: Optional[str] = None
    ) -> None:
        """Add a stdio-based MCP server programmatically."""
        if isinstance(command, str):
            parts = command.split()
        else:
            parts = list(command)

        if not parts:
            _logger.warning("[mcp] add_server called with empty command")
            return

        config_name = server_name or parts[0]
        prefix = _sanitize_config_key(config_name)

        params = StdioServerParameters(
            command=parts[0],
            args=parts[1:],
        )

        async def _connect_one():
            self._current_connect_prefix = prefix
            try:
                await self._group.connect_to_server(params)
                _logger.info(f"[mcp] Connected to '{config_name}'")
            finally:
                self._current_connect_prefix = None

        self._run_async(_connect_one())
        self._refresh_tool_defs()

    def add_http_server(
        self,
        url: str,
        server_name: Optional[str] = None,
        headers: Optional[dict] = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
    ) -> None:
        """Add an HTTP-based MCP server programmatically."""
        config_name = server_name or url
        prefix = _sanitize_config_key(config_name)

        params = StreamableHttpParameters(
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )

        async def _connect_one():
            self._current_connect_prefix = prefix
            try:
                await self._group.connect_to_server(params)
                _logger.info(
                    f"[mcp] Connected HTTP server '{config_name}' at {url}"
                )
            finally:
                self._current_connect_prefix = None

        self._run_async(_connect_one())
        self._refresh_tool_defs()

    # ------------------------------------------------------------------
    # Tool access
    # ------------------------------------------------------------------

    def get_tool_definitions(self) -> List[dict]:
        """Return tool definitions for system prompt construction.

        Each dict has ``name``, ``description``, and ``parameters`` keys.
        """
        return self._tool_defs

    def has_tool(self, name: str) -> bool:
        """Check whether a tool with the given name is registered."""
        return name in self._group.tools

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call a registered MCP tool and return the result text."""
        if name not in self._group.tools:
            return f"Error: Unknown MCP tool '{name}'"

        async def _call():
            result = await self._group.call_tool(name, arguments)
            return _extract_text_from_result(result)

        try:
            return self._run_async(_call())
        except Exception as e:
            return f"MCP Error: {e}"

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect all servers and shut down the event loop."""
        async def _disconnect_all():
            for session in list(self._group.sessions):
                try:
                    await self._group.disconnect_from_server(session)
                except Exception as e:
                    _logger.debug(f"[mcp] Error disconnecting session: {e}")

        try:
            self._run_async(_disconnect_all())
        except Exception as e:
            _logger.debug(f"[mcp] Error during disconnect: {e}")

        # Stop the event loop
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        _logger.debug("[mcp] Event loop stopped")
