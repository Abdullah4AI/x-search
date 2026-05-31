# X Search for Codex

Standalone Codex plugin that exposes an MCP tool named `x_search`. It calls
xAI's Responses API with the server-side `{"type": "x_search"}` tool and owns
its own xAI sign-in flow.

## Features

- Search X posts, profiles, and threads from Codex
- `allowed_x_handles` and `excluded_x_handles` filters, max 10 handles
- `from_date` and `to_date` validation with `YYYY-MM-DD`
- `enable_image_understanding` and `enable_video_understanding`
- xAI citations and inline URL citation extraction
- `degraded` result flag when filters are active but no citations are returned
- Built-in browser-based xAI OAuth PKCE sign-in with local token storage
- `XAI_API_KEY` fallback from `~/.codex-x-search/.env` or the process environment
- Local config through `~/.codex-x-search/config.json` or environment variables

## Authentication

The plugin resolves credentials in this order:

1. xAI OAuth token stored by this plugin in `~/.codex-x-search/auth.json`
2. `XAI_API_KEY` from `~/.codex-x-search/.env`
3. `XAI_API_KEY` from the current process environment

When `x_search` is called and no credential exists, the plugin starts its own
xAI sign-in flow automatically:

1. It starts a temporary callback server on `127.0.0.1`.
2. It opens the xAI authorization page in your default browser.
3. After you approve access, it stores the token locally for your user only.
4. It continues the original X Search request.

You can also sign in directly through the MCP tool `x_search_auth`, or from a
terminal:

```bash
python3 scripts/x_search_mcp_server.py auth
```

Tokens are stored outside the plugin repository in `~/.codex-x-search/auth.json`
with file mode `0600` where the platform allows it.

## Credential Isolation

This repository does not include xAI credentials, OAuth tokens, API keys, or
local credential files. The MCP server resolves credentials only at runtime from
the current user's machine:

- the current user's `~/.codex-x-search/auth.json`
- the current user's `~/.codex-x-search/.env`
- the current process environment

Sharing this plugin does not share your local credential directory or your
environment variables. Anyone who installs the plugin needs their own xAI
sign-in or their own `XAI_API_KEY`.

The repository `.gitignore` excludes common credential files such as `.env`,
`auth.json`, `.codex-x-search/`, and private key formats. Keep those files local
and out of commits.

## Configuration

The x_search model, timeout, and retry settings can be set in
`~/.codex-x-search/config.json`:

```json
{
  "x_search": {
    "model": "grok-4.20-reasoning",
    "timeout_seconds": 180,
    "retries": 2
  }
}
```

Environment variables override the config file:

- `X_SEARCH_MODEL`
- `X_SEARCH_TIMEOUT_SECONDS`
- `X_SEARCH_RETRIES`
- `X_SEARCH_HOME`
- `X_SEARCH_AUTO_AUTH`
- `XAI_API_KEY`
- `XAI_BASE_URL`

For safety, `XAI_BASE_URL` must point to an HTTPS `x.ai` host. If you
intentionally use a trusted proxy for API-key traffic, set:

```bash
X_SEARCH_ALLOW_CUSTOM_BASE_URL=1
```

## Tool Parameters

`x_search` accepts:

- `query` string, required
- `allowed_x_handles` string array
- `excluded_x_handles` string array
- `from_date` string, `YYYY-MM-DD`
- `to_date` string, `YYYY-MM-DD`
- `enable_image_understanding` boolean
- `enable_video_understanding` boolean

The result is JSON text with `success`, `answer`, `citations`,
`inline_citations`, `degraded`, `degraded_reason`, `credential_source`, `model`,
and `query`.

Additional MCP tools:

- `x_search_auth`: opens the xAI sign-in flow and stores a local token
- `x_search_status`: reports whether this user has a local credential
- `x_search_logout`: removes the stored OAuth token for this user

## Validation

Run the local test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the xAI Responses payload shape, filter/date validation,
degraded-result signaling, standalone OAuth refresh persistence, browser sign-in
tool wiring, and MCP `tools/list` / `tools/call` behavior.
