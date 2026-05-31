# X Search for Codex

Codex plugin that exposes an MCP tool named `x_search`. It mirrors Hermes'
X Search tool by calling xAI's Responses API with the server-side
`{"type": "x_search"}` tool.

## Features

- Search X posts, profiles, and threads from Codex
- `allowed_x_handles` and `excluded_x_handles` filters, max 10 handles
- `from_date` and `to_date` validation with `YYYY-MM-DD`
- `enable_image_understanding` and `enable_video_understanding`
- xAI citations and inline URL citation extraction
- `degraded` result flag when filters are active but no citations are returned
- xAI OAuth reuse from `~/.hermes/auth.json`, preferred when available
- `XAI_API_KEY` fallback using Hermes env precedence: `~/.hermes/.env`, then process environment
- Config reuse from Hermes `load_config().get("x_search")`, with `~/.hermes/config.yaml` parsing as fallback

## Authentication

The plugin resolves credentials in this order:

1. Hermes' own xAI HTTP resolver, when `~/.hermes/hermes-agent` is available
2. Standalone Hermes-compatible xAI OAuth refresh from `~/.hermes/auth.json`
3. `XAI_API_KEY` from `~/.hermes/.env`, then process environment

OAuth access tokens are refreshed when possible. To skip Hermes OAuth and force the API-key path, set:

```bash
X_SEARCH_DISABLE_HERMES_OAUTH=1
```

To skip importing Hermes' resolver and use the standalone fallback implementation, set:

```bash
X_SEARCH_DISABLE_HERMES_RESOLVER=1
```

## Credential Isolation

This repository does not include xAI credentials, Hermes OAuth tokens, API keys,
or local credential files. The MCP server resolves credentials only at runtime
from the current user's machine:

- the current user's `~/.hermes/auth.json`
- the current user's `~/.hermes/.env`
- the current process environment

Sharing this plugin does not share your local `~/.hermes` directory or your
environment variables. Anyone who installs the plugin needs their own Hermes
xAI OAuth login or their own `XAI_API_KEY`.

The repository `.gitignore` excludes common credential files such as `.env`,
`auth.json`, `.hermes/`, and private key formats. Keep those files local and
out of commits.

## Configuration

The x_search model, timeout, and retry settings follow Hermes' `x_search:`
configuration. The defaults are:

The plugin reads this Hermes-compatible block:

```yaml
x_search:
  model: grok-4.20-reasoning
  timeout_seconds: 180
  retries: 2
```

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

## Validation

Run the local test suite with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the xAI Responses payload shape, filter/date validation,
degraded-result signaling, Hermes OAuth refresh persistence, and MCP
`tools/list` / `tools/call` behavior.
