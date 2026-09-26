"""In-app streamable-HTTP MCP mount for Odysseus.

External MCP hosts (Claude Code, Codex, opencode, ...) connect to the mounted
``/mcp`` endpoint. The Bearer ``ody_`` token that authenticates the MCP session
is verified by FastMCP's token_verifier against the api_tokens table, then the
raw token is stashed in ``_CURRENT_TOKEN`` so every tool call forwards it on its
loopback request to the scope-gated ``/api/codex/*`` bridge — so the MCP tools
run with exactly the owner + scopes granted to that token. The app's own
AuthMiddleware already validates the token at the outermost layer, so this
re-verification is defense in depth.
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.auth.provider import AccessToken

from mcp_servers.odysseus_mcp import DEFAULT_BASE_URL, _CURRENT_TOKEN, build_odysseus_server


def odysseus_mcp_enabled() -> bool:
    """Env toggle to disable the /mcp mount entirely."""
    return os.environ.get("ODYSSEUS_MCP_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def odysseus_mcp_base_url() -> str:
    return (os.environ.get("ODYSSEUS_URL") or DEFAULT_BASE_URL).rstrip("/")


_MCP_LIFESPAN_RUN = None
"""Async context manager for the mounted MCP session manager lifecycle.

The nested FastMCP Starlette app's own lifespan is not run by uvicorn (only the
root app's lifespan runs), so the app drives it from its own lifespan instead.
Set by build_odysseus_mcp_app()."""


def odysseus_mcp_lifespan():
    """Return the MCP session-manager run() context manager, or None if the MCP
    server is not mounted. Callers enter it with ``async with``."""
    if _MCP_LIFESPAN_RUN is None:
        return None
    return _MCP_LIFESPAN_RUN()


class _ApiTokenVerifier:
    """FastMCP token_verifier backed by the api_tokens table (bcrypt)."""

    def __init__(self, auth_manager, resource_url: str) -> None:
        self._auth_manager = auth_manager
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        # bcrypt is CPU-bound: verify off the event loop. The contextvar must be
        # set HERE on return (in the calling task), because a value set inside
        # to_thread dies with the worker thread's context copy.
        result = await asyncio.to_thread(self._verify_sync, token)
        if result is not None:
            _CURRENT_TOKEN.set(token)
        return result

    def _verify_sync(self, token: str) -> AccessToken | None:
        import bcrypt

        if not token or not token.startswith("ody_") or not (12 <= len(token) <= 100):
            return None
        prefix = token[:8]

        from core.auth import normalize_known_username
        from core.database import ApiToken, SessionLocal

        db = SessionLocal()
        try:
            rows = (
                db.query(ApiToken)
                .filter(ApiToken.is_active == True, ApiToken.token_prefix == prefix)
                .all()
            )
        finally:
            db.close()

        for row in rows:
            try:
                if not bcrypt.checkpw(token.encode("utf-8"), row.token_hash.encode("utf-8")):
                    continue
            except Exception:
                continue
            owner = normalize_known_username(self._auth_manager.users, row.owner)
            if not owner:
                return None
            scopes = [s.strip() for s in (row.scopes or "chat").split(",") if s.strip()]
            return AccessToken(
                token=token,
                client_id=row.id,
                scopes=scopes,
                subject=owner,
                resource=self._resource_url,
            )
        return None


def build_odysseus_mcp_app(auth_manager):
    """Return the Starlette ASGI app to mount at /mcp (stateless streamable HTTP).

    Also records the MCP session-manager run() CM so the hosting app's lifespan
    (see odysseus_mcp_lifespan) can drive it — a mounted sub-app never gets its
    own lifespan invoked by uvicorn.
    """
    from mcp.server.fastmcp.server import AuthSettings

    global _MCP_LIFESPAN_RUN
    base_url = odysseus_mcp_base_url()
    server = build_odysseus_server(
        base_url=base_url,
        token_verifier=_ApiTokenVerifier(auth_manager, base_url),
        auth=AuthSettings(
            issuer_url=base_url,
            resource_server_url=base_url,
        ),
        stateless_http=True,
    )
    asgi_app = server.streamable_http_app()
    _MCP_LIFESPAN_RUN = server._session_manager.run
    return asgi_app


__all__ = [
    "build_odysseus_mcp_app",
    "odysseus_mcp_base_url",
    "odysseus_mcp_enabled",
    "odysseus_mcp_lifespan",
]