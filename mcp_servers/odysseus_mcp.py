"""Shared inbound MCP server for Odysseus.

Exposes Odysseus capabilities (todos, email, memory, calendar, documents,
cookbook) as MCP tools that proxy to the scope-gated ``/api/codex/*`` REST
bridge, so external AI agents operate on exactly the same data and with the
same api_token scopes as the existing Codex / Claude Code integrations.

Two transports share this module:

* ``stdio`` — connect with ``ODYSSEUS_API_TOKEN`` (and ``ODYSSEUS_URL``);
  the server's own token is used for every call.
* ``streamable-http`` — the app mounts this server at ``/mcp`` with a
  ``token_verifier``. The verifier authenticates the caller's ``ody_`` Bearer
  token and stashes the raw token in the ``_CURRENT_TOKEN`` contextvar so
  tool calls forward it on loopback requests (same owner + scopes as the
  token that authenticated the MCP session).
"""

from __future__ import annotations

import contextvars
import os
from typing import Annotated, Any

import httpx
from pydantic import Field

from mcp.server.fastmcp import FastMCP

DEFAULT_BASE_URL = os.environ.get("ODYSSEUS_URL") or "http://127.0.0.1:7000"

_CURRENT_TOKEN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "odysseus_mcp_token", default=None
)


class RemoteError(RuntimeError):
    """Raised when a proxied Odysseus API call fails."""


def _email_body(
    to: str, subject: str, body: str,
    cc: str | None, bcc: str | None, body_html: str | None,
    in_reply_to: str | None, references: str | None,
    attachments: list[str] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if body_html:
        payload["body_html"] = body_html
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    if references:
        payload["references"] = references
    if attachments:
        payload["attachments"] = attachments
    return payload


class OdysseusRemote:
    """Thin async client for the ``/api/codex/*`` scope-gated bridge."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0), trust_env=False)
        return self._http

    def resolve_token(self) -> str:
        token = _CURRENT_TOKEN.get() or self.token
        if not token:
            raise RemoteError(
                "No API token available. Set ODYSSEUS_API_TOKEN for stdio, or "
                "connect over streamable-http with an ody_ Bearer token."
            )
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.resolve_token()}"}
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client().request(method, url, headers=headers, params=params, json=body)
        except httpx.HTTPError as exc:
            raise RemoteError(f"Odysseus unreachable at {url}: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                payload = resp.json()
                detail = str(payload.get("detail") or payload.get("error") or payload)
            except Exception:
                detail = resp.text[:300]
            raise RemoteError(f"{method} {path} -> {resp.status_code}: {detail}")
        return resp.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, body=body or None)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)


def _build_tools(mcp: FastMCP, remote: OdysseusRemote) -> None:
    """Register every tool on ``mcp``, proxying calls through ``remote``."""

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_capabilities() -> dict[str, Any]:
        """List Odysseus capability domains and which actions the current API
        token may run (todos, email, memory, calendar, documents, cookbook).
        Check this first to see what is available before acting."""
        return await remote.get("/api/codex/capabilities")

    # ------------------------------------------------------------------ #
    # Todos / notes
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_todos_list(
        archived: Annotated[bool, Field(description="Include archived notes.")] = False,
        label: Annotated[str | None, Field(description="Filter by note label.")] = None,
    ) -> Any:
        """List the user's notes/todos. Requires todos:read."""
        params: dict[str, Any] = {"archived": archived}
        if label:
            params["label"] = label
        return await remote.get("/api/codex/todos", **params)

    @mcp.tool()
    async def odysseus_todos_action(
        action: Annotated[
            str,
            Field(
                description=(
                    "Action to run: add (aliases: create/new/save/remind), update, "
                    "delete (alias: remove), toggle_item (alias: remove_item), or "
                    "list/search/find. Write actions require todos:write."
                )
            ),
        ],
        note_id: Annotated[str | None, Field(description="Note id (id prefix ok) for update/delete/toggle_item")] = None,
        title: Annotated[str | None, Field(description="Note title (for add/update).")] = None,
        content: Annotated[str | None, Field(description="Note body text (for add/update).")] = None,
        label: Annotated[str | None, Field(description="Note label.")] = None,
        pinned: Annotated[bool | None, Field(description="Pin/unpin the note.")] = None,
        items: Annotated[list[str] | None, Field(description="Checklist items for a checklist-type note.")] = None,
        item_index: Annotated[int | None, Field(description="Item index for toggle_item.")] = None,
        due_date: Annotated[str | None, Field(description="Due date (ISO date/time string).")] = None,
        color: Annotated[str | None, Field(description="Note color.")] = None,
        query: Annotated[str | None, Field(description="Search text for search/find actions.")] = None,
        archived: Annotated[bool | None, Field(description="Include archived notes when listing.")] = None,
    ) -> Any:
        """Create, update, delete or toggle notes/todos. POST /api/codex/todos."""
        body: dict[str, Any] = {"action": action}
        for key, value in (
            ("id", note_id if action in ("update", "delete", "toggle_item", "remove", "remove_item") else None),
            ("title", title),
            ("content", content),
            ("label", label),
            ("pinned", pinned),
            ("items", items),
            ("index", item_index if action in ("toggle_item", "remove_item") else None),
            ("due_date", due_date),
            ("color", color),
            ("query", query if action in ("search", "find") else None),
            ("archived", archived if action in ("list", "search", "find") else None),
        ):
            if value is not None:
                body[key] = value
        return await remote.post("/api/codex/todos", body)

    # ------------------------------------------------------------------ #
    # Email
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_emails_list(
        folder: Annotated[str, Field(description="Mailbox folder name.")] = "INBOX",
        limit: Annotated[int, Field(description="Max results (1-50).")] = 10,
        offset: Annotated[int, Field(description="Pagination offset.")] = 0,
        filter: Annotated[str, Field(description="Message filter (all/unseen/etc.).")] = "all",
        from_addr: Annotated[str | None, Field(description="Filter by sender address.")] = None,
        has_attachments: Annotated[int, Field(description="1 to only list messages with attachments.")] = 0,
    ) -> Any:
        """List email messages. Requires email:read."""
        params: dict[str, Any] = {
            "folder": folder,
            "limit": max(1, min(int(limit or 10), 50)),
            "offset": max(0, int(offset or 0)),
            "filter": filter,
            "has_attachments": int(has_attachments or 0),
        }
        if from_addr:
            params["from_addr"] = from_addr
        return await remote.get("/api/codex/emails", **params)

    @mcp.tool()
    async def odysseus_email_read(
        uid: Annotated[str, Field(description="Message uid.")],
        folder: Annotated[str, Field(description="Mailbox folder containing the message.")] = "INBOX",
        mark_seen: Annotated[bool, Field(description="Mark the message as seen.")] = False,
    ) -> Any:
        """Read a single email message body. Requires email:read."""
        return await remote.get(
            f"/api/codex/emails/{uid}", folder=folder, mark_seen=bool(mark_seen)
        )

    def _email_body(
        to: str, subject: str, body: str,
        cc: str | None, bcc: str | None, body_html: str | None,
        in_reply_to: str | None, references: str | None,
        attachments: list[str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "to": to,
            "subject": subject,
            "body": body,
        }
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc
        if body_html:
            payload["body_html"] = body_html
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if references:
            payload["references"] = references
        if attachments:
            payload["attachments"] = attachments
        return payload

    @mcp.tool()
    async def odysseus_email_draft(
        to: Annotated[str, Field(description="Recipient address (comma-separated list allowed).")],
        subject: Annotated[str, Field(description="Email subject line.")],
        body: Annotated[str, Field(description="Plain-text email body (markdown OK).")],
        cc: Annotated[str | None, Field(description="Cc recipients.")] = None,
        bcc: Annotated[str | None, Field(description="Bcc recipients.")] = None,
        body_html: Annotated[str | None, Field(description="Rendered HTML body (optional; server sanitizes).")] = None,
        in_reply_to: Annotated[str | None, Field(description="Source message uid for replies.")] = None,
        references: Annotated[str | None, Field(description="References header for replies.")] = None,
        attachments: Annotated[list[str] | None, Field(description="Uploaded attachment filenames.")] = None,
    ) -> Any:
        """Save a draft email (does NOT send). Requires email:draft."""
        return await remote.post(
            "/api/codex/emails/draft",
            _email_body(
                to, subject, body, cc, bcc, body_html, in_reply_to, references, attachments
            ),
        )

    @mcp.tool()
    async def odysseus_email_send(
        to: Annotated[str, Field(description="Recipient address (comma-separated list allowed).")],
        subject: Annotated[str, Field(description="Email subject line.")],
        body: Annotated[str, Field(description="Plain-text email body (markdown OK).")],
        cc: Annotated[str | None, Field(description="Cc recipients.")] = None,
        bcc: Annotated[str | None, Field(description="Bcc recipients.")] = None,
        body_html: Annotated[str | None, Field(description="Rendered HTML body (optional; server sanitizes).")] = None,
        in_reply_to: Annotated[str | None, Field(description="Source message uid for replies.")] = None,
        references: Annotated[str | None, Field(description="References header for replies.")] = None,
        attachments: Annotated[list[str] | None, Field(description="Uploaded attachment filenames.")] = None,
    ) -> Any:
        """Send an email immediately. Requires email:send."""
        return await remote.post(
            "/api/codex/emails/send",
            _email_body(
                to, subject, body, cc, bcc, body_html, in_reply_to, references, attachments
            ),
        )

    @mcp.tool()
    async def odysseus_email_draft_document(
        to: Annotated[str, Field(description="Recipient address.")],
        subject: Annotated[str, Field(description="Email subject line.")],
        body: Annotated[str, Field(description="Email body; rendered into a Draft document.")],
        cc: Annotated[str | None, Field(description="Cc recipients.")] = None,
        bcc: Annotated[str | None, Field(description="Bcc recipients.")] = None,
        in_reply_to: Annotated[str | None, Field(description="Source message uid for replies.")] = None,
        references: Annotated[str | None, Field(description="References header for replies.")] = None,
        title: Annotated[str | None, Field(description="Document title (defaults to subject).")] = None,
    ) -> Any:
        """Create a Draft document from an email composition (requires email:draft
        AND documents:write). Use for the review-before-send flow."""
        payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc
        if in_reply_to:
            payload["in_reply_to"] = in_reply_to
        if references:
            payload["references"] = references
        if title:
            payload["title"] = title
        return await remote.post("/api/codex/emails/draft-document", payload)

    # ------------------------------------------------------------------ #
    # Memory
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_memory_list() -> Any:
        """List saved memory entries. Requires memory:read."""
        return await remote.get("/api/codex/memory")

    @mcp.tool()
    async def odysseus_memory_add(
        text: Annotated[str, Field(description="Memory text to store.")],
        category: Annotated[str, Field(description="Memory category (fact/preference/etc.).")] = "fact",
        source: Annotated[str, Field(description="Source label.")] = "user",
    ) -> Any:
        """Add a memory entry. Requires memory:write."""
        return await remote.post("/api/codex/memory", {
            "text": text, "category": category, "source": source
        })

    @mcp.tool()
    async def odysseus_memory_delete(
        memory_id: Annotated[str, Field(description="Memory entry id to delete.")],
    ) -> Any:
        """Delete a memory entry by id. Requires memory:write."""
        return await remote.delete(f"/api/codex/memory/{memory_id}")

    # ------------------------------------------------------------------ #
    # Calendar
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_calendar_events_list(
        start: Annotated[str, Field(description="Range start (ISO datetime).")],
        end: Annotated[str, Field(description="Range end (ISO datetime).")],
        calendar: Annotated[str, Field(description="Calendar href filter (empty = all).")] = "",
    ) -> Any:
        """List calendar events in a date range. Requires calendar:read."""
        return await remote.get("/api/codex/calendar/events", start=start, end=end, calendar=calendar)

    @mcp.tool()
    async def odysseus_calendar_event_create(
        summary: Annotated[str, Field(description="Event title.")],
        dtstart: Annotated[str, Field(description="Start time (ISO datetime).")],
        dtend: Annotated[str | None, Field(description="End time (ISO datetime).")] = None,
        all_day: Annotated[bool | None, Field(description="All-day event flag.")] = None,
        description: Annotated[str | None, Field(description="Event description/notes.")] = None,
        location: Annotated[str | None, Field(description="Event location.")] = None,
        calendar_href: Annotated[str | None, Field(description="Target calendar href.")] = None,
        rrule: Annotated[str | None, Field(description="Recurrence rule (RRULE).")] = None,
        color: Annotated[str | None, Field(description="Event color.")] = None,
    ) -> Any:
        """Create a calendar event. Requires calendar:write."""
        body: dict[str, Any] = {"summary": summary, "dtstart": dtstart}
        for key, value in (
            ("dtend", dtend), ("all_day", all_day), ("description", description),
            ("location", location), ("calendar_href", calendar_href),
            ("rrule", rrule), ("color", color),
        ):
            if value is not None:
                body[key] = value
        return await remote.post("/api/codex/calendar/events", body)

    @mcp.tool()
    async def odysseus_calendar_event_delete(
        uid: Annotated[str, Field(description="Event uid to delete.")],
    ) -> Any:
        """Delete a calendar event by uid. Requires calendar:write."""
        return await remote.delete(f"/api/codex/calendar/events/{uid}")

    # ------------------------------------------------------------------ #
    # Documents
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_documents_library(
        search: Annotated[str | None, Field(description="Full-text search query.")] = None,
        language: Annotated[str | None, Field(description="Language filter.")] = None,
        sort: Annotated[str, Field(description="Sort order: recent or other supported sorts.")] = "recent",
        offset: Annotated[int, Field(description="Pagination offset.")] = 0,
        limit: Annotated[int, Field(description="Max results (1-50).")] = 50,
        archived: Annotated[bool, Field(description="Include archived documents.")] = False,
    ) -> Any:
        """List documents from the library. Requires documents:read."""
        params: dict[str, Any] = {
            "sort": sort,
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(int(limit or 50), 50)),
            "archived": bool(archived),
        }
        if search:
            params["search"] = search
        if language:
            params["language"] = language
        return await remote.get("/api/codex/documents", **params)

    @mcp.tool()
    async def odysseus_document_read(
        doc_id: Annotated[str, Field(description="Document id. Accepts an id prefix.")],
    ) -> Any:
        """Read a full document by id. Requires documents:read."""
        return await remote.get(f"/api/codex/documents/{doc_id}")

    @mcp.tool()
    async def odysseus_document_create(
        title: Annotated[str, Field(description="Document title.")] = "Untitled",
        content: Annotated[str, Field(description="Document body content.")] = "",
        language: Annotated[str | None, Field(description="Language hint.")] = None,
        session_id: Annotated[str | None, Field(description="Optional linked session id.")] = None,
    ) -> Any:
        """Create a new document. Requires documents:write."""
        body: dict[str, Any] = {"title": title, "content": content}
        if language:
            body["language"] = language
        if session_id:
            body["session_id"] = session_id
        return await remote.post("/api/codex/documents", body)

    @mcp.tool()
    async def odysseus_document_delete(
        doc_id: Annotated[str, Field(description="Document id to delete.")],
    ) -> Any:
        """Delete a document by id. Requires documents:write."""
        return await remote.delete(f"/api/codex/documents/{doc_id}")

    # ------------------------------------------------------------------ #
    # Cookbook (read surface)
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_cookbook_tasks() -> Any:
        """List cookbook serve tasks (tmux sessions). Requires cookbook:read."""
        return await remote.get("/api/codex/cookbook/tasks")

    @mcp.tool()
    async def odysseus_cookbook_servers() -> Any:
        """List configured cookbook servers (hosts). Requires cookbook:read."""
        return await remote.get("/api/codex/cookbook/servers")

    @mcp.tool()
    async def odysseus_cookbook_output(
        session_id: Annotated[str, Field(description="Task/session id to tail (tmux id issued by the UI).")],
        tail: Annotated[int, Field(description="Number of tail lines (20-4000).")] = 400,
    ) -> Any:
        """Tail output of a running cookbook serve task. Requires cookbook:read.
        session_id must match the tmux-style id the cookbook issues (serve-*, cookbook-*)."""
        return await remote.get(
            f"/api/codex/cookbook/output/{session_id}",
            tail=max(20, min(int(tail or 400), 4000)),
        )

    @mcp.tool()
    async def odysseus_cookbook_presets() -> Any:
        """List saved serve presets (model + host + cmd). Check this before composing
        a serve call — the saved preset usually already has a working command."""
        return await remote.get("/api/codex/cookbook/presets")

    @mcp.tool()
    async def odysseus_cookbook_cached(
        host: Annotated[str | None, Field(description="Configured server name/host to inspect.")] = None,
    ) -> Any:
        """List cached (already-downloaded) models on a server or locally."""
        params: dict[str, Any] = {}
        if host:
            params["host"] = host
        return await remote.get("/api/codex/cookbook/cached", **params)

    # ------------------------------------------------------------------ #
    # Cookbook (launch surface — cookbook:launch scope)
    # ------------------------------------------------------------------ #
    @mcp.tool()
    async def odysseus_cookbook_serve(
        repo_id: Annotated[str, Field(description="HuggingFace model id to serve.")],
        cmd: Annotated[str, Field(description="Launch command (binary must be in the serve allowlist).")],
        remote_host: Annotated[str | None, Field(description="Target host for a remote serve.")] = None,
        ssh_port: Annotated[str | None, Field(description="SSH port for the remote host.")] = None,
        env_prefix: Annotated[str | None, Field(description="Environment prefix for the task.")] = None,
        gpus: Annotated[str | None, Field(description="GPU selection string.")] = None,
        platform: Annotated[str | None, Field(description="Platform: linux/termux/windows.")] = None,
    ) -> Any:
        """Start a cookbook model-serve task. Requires cookbook:launch. The command
        is validated against the same allowlist the UI uses."""
        body: dict[str, Any] = {"repo_id": repo_id, "cmd": cmd}
        for key, value in (
            ("remote_host", remote_host), ("ssh_port", ssh_port),
            ("env_prefix", env_prefix), ("gpus", gpus), ("platform", platform),
        ):
            if value is not None:
                body[key] = value
        return await remote.post("/api/codex/cookbook/serve", body)

    @mcp.tool()
    async def odysseus_cookbook_stop(
        session_id: Annotated[str, Field(description="Task/session id to stop.")],
    ) -> Any:
        """Stop a running cookbook serve task. Requires cookbook:launch."""
        return await remote.post(f"/api/codex/cookbook/stop/{session_id}")

    @mcp.tool()
    async def odysseus_cookbook_serve_preset(
        name: Annotated[str, Field(description="Preset name to launch.")],
    ) -> Any:
        """Launch a saved serve preset by name. Requires cookbook:launch."""
        return await remote.post(f"/api/codex/cookbook/preset/{name}")

    @mcp.tool()
    async def odysseus_cookbook_adopt(
        tmux_session: Annotated[str, Field(description="Existing tmux session name (tmux id issued outside cookbook).")],
        model: Annotated[str, Field(description="Model id the session serves.")],
        host: Annotated[str | None, Field(description="Remote host the session runs on.")] = None,
        port: Annotated[int | None, Field(description="Model API port.")] = 8000,
    ) -> Any:
        """Adopt an externally-launched tmux session into cookbook tracking.
        Requires cookbook:launch. Body parses from tmux_session/model/host/port."""
        body: dict[str, Any] = {"tmux_session": tmux_session, "model": model, "port": int(port or 8000)}
        if host:
            body["host"] = host
        return await remote.post("/api/codex/cookbook/adopt", body)


def build_odysseus_server(
    *,
    base_url: str | None = None,
    token: str | None = None,
    token_verifier: Any | None = None,
    auth: Any | None = None,
    stateless_http: bool = True,
) -> FastMCP:
    """Build a FastMCP server exposing Odysseus tools over /api/codex/*.

    Args:
        base_url: Odysseus base URL for the loopback proxying of tool calls.
        token: Fixed API token for stdio transport (else read per request).
        token_verifier: FastMCP TokenVerifier for HTTP bearer auth.
        auth: FastMCP AuthSettings for HTTP bearer auth.
        stateless_http: Run HTTP transport without server-side sessions.
    """
    if base_url is None:
        base_url = DEFAULT_BASE_URL
    remote = OdysseusRemote(base_url=base_url, token=token)

    mcp = FastMCP(
        "odysseus",
        instructions=(
            "Odysseus personal assistant hub. Tools scope to the API token's "
            "permissions (todos, email, memory, calendar, documents, cookbook). "
            "Call odysseus_capabilities first to confirm what is allowed. "
            "Adopt a careful tone for email and destructive actions."
        ),
        token_verifier=token_verifier,
        auth=auth,
        stateless_http=stateless_http,
        streamable_http_path="/",
        log_level="WARNING",
    )
    _build_tools(mcp, remote)
    return mcp


__all__ = ["OdysseusRemote", "RemoteError", "_CURRENT_TOKEN", "build_odysseus_server"]