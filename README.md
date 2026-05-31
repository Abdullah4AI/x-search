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
- `XAI_API_KEY` fallback from process environment, local `.env`, or `~/.hermes/.env`
- Config reuse from `~/.hermes/config.yaml` `x_search:` where available

## Authentication

The plugin resolves credentials in this order:

1. Hermes xAI OAuth credentials from `~/.hermes/auth.json`
2. `XAI_API_KEY`

OAuth access tokens are refreshed when possible. To skip Hermes OAuth and force
the API-key path, set:

```bash
X_SEARCH_DISABLE_HERMES_OAUTH=1
```

## Configuration

Environment variables override the Hermes config file:

```bash
X_SEARCH_MODEL=grok-4.20-reasoning
X_SEARCH_TIMEOUT_SECONDS=180
X_SEARCH_RETRIES=2
XAI_BASE_URL=https://api.x.ai/v1
```

The plugin also reads this Hermes-compatible block:

```yaml
x_search:
  model: grok-4.20-reasoning
  timeout_seconds: 180
  retries: 2
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
