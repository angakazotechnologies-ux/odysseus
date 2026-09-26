#!/usr/bin/env python3
"""Stdio MCP entrypoint for Odysseus.

External MCP hosts (Claude Code, Codex, opencode, ...) launch this script as a
stdio MCP server. It proxies every tool call to the running Odysseus app via the
scope-gated /api/codex/* bridge, using the ODYSSEUS_API_TOKEN bearer token.

Environment:
    ODYSSEUS_URL        Base URL of the running Odysseus app
                        (default http://127.0.0.1:7000).
    ODYSSEUS_API_TOKEN  An ody_ API token with the scopes you want to expose.

Example (Claude Code):
    claude mcp add odysseus -- \
        python C:\\Users\\user\\odysseus\\scripts\\odysseus-mcp-server.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.odysseus_mcp import DEFAULT_BASE_URL, build_odysseus_server  # noqa: E402


def main() -> None:
    base_url = os.environ.get("ODYSSEUS_URL") or DEFAULT_BASE_URL
    token = os.environ.get("ODYSSEUS_API_TOKEN") or ""
    server = build_odysseus_server(base_url=base_url, token=token or None)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()