# X Search for Codex

Codex marketplace for a standalone plugin that exposes an MCP tool named
`x_search`. It calls
xAI's Responses API with the server-side `{"type": "x_search"}` tool and owns
its own xAI sign-in flow.

## Add to Codex

In Codex, open **Add marketplace** and use:

- Source: `git@github.com:Abdullah4AI/x-search.git`
- Git ref: `main`
- Sparse paths: leave empty

The repository root contains `.agents/plugins/marketplace.json`, and the plugin
itself lives at `plugins/x-search`, which is the layout Codex expects for a Git
marketplace.

## Features

- Search X posts, profiles, and threads from Codex
- `allowed_x_handles` and `excluded_x_handles` filters, max 10 handles
- `from_date` and `to_date` validation with `YYYY-MM-DD`
- `enable_image_understanding` and `enable_video_understanding`
- xAI citations and inline URL citation extraction
- `degraded` result flag when filters are active but no citations are returned
- Precision hint for latest-post questions without a known handle
- System-CA-backed HTTPS verification, with `X_SEARCH_CA_BUNDLE` override
- Built-in browser-based xAI OAuth PKCE sign-in with local token storage
- `XAI_API_KEY` fallback from `~/.codex-x-search/.env` or the process environment
- Local config through `~/.codex-x-search/config.json` or environment variables

## Authentication

The plugin resolves credentials in this order:

1. xAI OAuth token stored by this plugin in `~/.codex-x-search/auth.json`
2. `XAI_API_KEY` from `~/.codex-x-search/.env`
3. `XAI_API_KEY` from the current process environment

Before using `x_search`, check authentication with `x_search_status`. If the
user is already authenticated, Codex can use `x_search` immediately. If the
user is not authenticated, call `x_search_auth` without `allow_redirect`; the
tool returns a permission prompt and does not open a browser. Only after the
user allows the redirect should Codex call `x_search_auth` again with
`{"allow_redirect": true}`.

The `x_search_auth` tool:

1. It first verifies whether a valid credential already exists.
2. If no credential exists and `allow_redirect` is not true, it returns:
   `X Search needs to open the xAI authentication page to complete sign-in. Do
   you want to allow this?`
3. After the user allows the redirect, it starts a temporary callback server on
   `127.0.0.1`.
4. It opens the xAI authorization page in your default browser.
5. After you approve access, it stores the token locally for your user only.

You can also sign in from a terminal:

```bash
python3 plugins/x-search/scripts/x_search_mcp_server.py auth
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
    "timeout_seconds": 75,
    "retries": 1
  }
}
```

Environment variables override the config file:

- `X_SEARCH_MODEL`
- `X_SEARCH_TIMEOUT_SECONDS`
- `X_SEARCH_RETRIES`
- `X_SEARCH_HOME`
- `X_SEARCH_CA_BUNDLE`
- `XAI_API_KEY`
- `XAI_BASE_URL`

For safety, `XAI_BASE_URL` must point to an HTTPS `x.ai` host. If you
intentionally use a trusted proxy for API-key traffic, set:

```bash
X_SEARCH_ALLOW_CUSTOM_BASE_URL=1
```

HTTPS requests first try `X_SEARCH_CA_BUNDLE` when set, then common system CA
bundle locations, then `certifi`, and finally Python's default trust store. If
your Python installation still has a broken CA setup, set `X_SEARCH_CA_BUNDLE`
to a valid CA bundle file.

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
`query_strategy`, and `query`.

Additional MCP tools:

- `x_search_auth`: checks existing access, asks permission before opening the
  xAI sign-in page, and stores a local token after authorization
- `x_search_status`: reports whether this user has a local credential
- `x_search_logout`: removes the stored OAuth token for this user

`x_search` is always exposed so Codex can find the right tool immediately. If
credentials are missing, calling it returns an auth-required response that
points Codex to `x_search_auth` without starting browser sign-in.

## Validation

Run the local test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the xAI Responses payload shape, filter/date validation,
degraded-result signaling, latest-post query hinting, cert-aware HTTPS calls,
standalone OAuth refresh persistence, browser sign-in tool wiring, and MCP
`tools/list` / `tools/call` behavior.
