# Security Policy

## Supported Versions

Security fixes are supported for the current `main` branch and the latest
published Codex plugin version.

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a Vulnerability

Please report suspected vulnerabilities privately through GitHub Security
Advisories:

https://github.com/Abdullah4AI/x-search/security/advisories/new

Do not open a public issue for a vulnerability. Include:

- a description of the issue and impact
- steps to reproduce or a proof of concept
- affected version or commit, if known
- whether credentials, tokens, or local files may be exposed

I will acknowledge reports within 72 hours when possible and follow up with
status updates as the issue is investigated.

## Credential Handling

This plugin must not store credentials in the repository. Runtime credentials
belong only on the user's machine, including:

- `~/.codex-x-search/auth.json`
- `~/.codex-x-search/.env`
- `XAI_API_KEY` in the process environment

If you discover committed secrets, token leakage, unsafe file permissions, or a
path that could expose another user's credentials, report it as a vulnerability.

## Scope

Security reports are especially useful for:

- exposure of xAI OAuth tokens or API keys
- unintended network requests or unsafe redirect handling
- command execution or path traversal through MCP inputs
- unsafe handling of local credential files
- dependency or transport issues that could compromise user data

Reports about expected authentication prompts, invalid credentials, or general
usage questions can be opened as normal GitHub issues.
