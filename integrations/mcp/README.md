# Odysseus MCP Integration

Exposes Odysseus capabilities over the Model Context Protocol (MCP) to external
agents, using either stdio or streamable-HTTP transport. Both transports serve
the same 27 tools (todos, email, memory, calendar, documents, cookbook /
serve-launch) and are gated by the caller's scoped `ody_` bearer token.

Route: all MCP tool calls proxy over HTTP to the scope-gated `/api/codex/*`
endpoints; raw SSH / Docker / direct imports / DB access are not used.

## Enabling

The HTTP `/mcp` mount is controlled by `ODYSSEUS_MCP_ENABLED`.
Defaults to **on**; set it to `0` / `false` / `no` to disable.

```powershell
# optional: machine-wide
[Environment]::SetEnvironmentVariable("ODYSSEUS_MCP_ENABLED", "1", "User")
```

The server process (`uvicorn app:app --port 7000`) must be running; it is
self-healed by the "Odysseus UI" watchdog (scheduled task + `start-odysseus.ps1`).

## Tokens

Create a token in Odysseus Settings > Integrations. Treat it like a password:
it is set in the HTTP `Authorization` header and matched by bcrypt on every call.

Granted scopes gate tool behavior:

| Scope                    | Grants                                                              |
| ------------------------ | ------------------------------------------------------------------- |
| `todos:read` / `write`   | list / take action on todos                                         |
| `email:read`             | list + read emails                                                  |
| `email:draft`, `email:send` | draft and send emails (send implies draft anyway)                |
| `memory:read` / `write`  | list / add / delete memory entries                                  |
| `calendar:read` / `write`| list events, create / delete events                                 |
| `documents:read` / `write`| library, read / create / delete documents                          |
| `cookbook:read`          | list tasks / servers / output / presets                             |
| `cookbook:launch`        | serve / stop / serve-preset / adopt (launch commands)               |

Note: read scopes include the corresponding write scope (e.g. granting only
`todos:write` also permits reads), matching the API's scope behavior.

## stdio transport

Run the normal server process and launch the entrypoint with the token + URL in
the environment:

```bash
ODYSSEUS_URL=http://127.0.0.1:7000 \
ODYSSEUS_API_TOKEN=ody_generated_token \
python C:\Users\user\odysseus\scripts\odysseus-mcp-server.py
```

### Claude Code

```bash
claude mcp add odysseus \
  --scope user \
  --env ODYSSEUS_URL=http://127.0.0.1:7000 \
  --env ODYSSEUS_API_TOKEN=ody_generated_token \
  -- python C:\Users\user\odysseus\scripts\odysseus-mcp-server.py
```

### opencode

In `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "odysseus": {
      "type": "local",
      "command": ["C:\\Users\\user\\odysseus\\venv\\Scripts\\python.exe", "C:\\Users\\user\\odysseus\\scripts\\odysseus-mcp-server.py"],
      "environment": {
        "ODYSSEUS_URL": "http://127.0.0.1:7000",
        "ODYSSEUS_API_TOKEN": "ody_generated_token"
      },
      "enabled": true
    }
  }
}
```

### Codex

Uses the built-in `mcp_server` / `mcp-bridge` tooling:

```
codex mcp add odysseus -- cmd /c "set ODYSSEUS_URL=http://127.0.0.1:7000 && set ODYSSEUS_API_TOKEN=ody_generated_token && C:\Users\user\odysseus\venv\Scripts\python.exe C:\Users\user\odysseus\scripts\odysseus-mcp-server.py"
```

## streamable-HTTP transport

Mount URL: `http://127.0.0.1:7000/mcp/`

Note the trailing slash. The app mounts a Starlette sub-app at `/mcp`, so a
request to `/mcp` is answered with a `307` redirect to `/mcp/`. Most MCP clients
follow that transparently, but ones that don't will fail — use `/mcp/`.

```bash
Authorization: Bearer ody_generated_token
```

Both `application/json` and `text/event-stream` must be accepted. The server is
stateless; every request is individually authenticated by the outer app auth
middleware and re-verified by the MCP layer.

### opencode (remote)

In `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "odysseus": {
      "type": "remote",
      "url": "http://127.0.0.1:7000/mcp/",
      "headers": { "Authorization": "Bearer ody_generated_token" },
      "enabled": true
    }
  }
}
```

## Verification

```bash
ODYSSEUS_URL=http://127.0.0.1:7000 \
ODYSSEUS_API_TOKEN=ody_generated_token \
python C:\Users\user\odysseus\scripts\odysseus-mcp-server.py
# then in the same session confirm: initialize, 27 tools, odysseus_capabilities
```

Failed or missing tokens are rejected with HTTP 401 (`{"error":"Invalid API token"}`).