---
name: x-search
description: Search X/Twitter posts, profiles, and threads from Codex using the plugin's x_search MCP tool backed by xAI/Grok. Use when the user asks for current X discussion, reactions, posts from specific handles, X citations, or X media-aware search.
---

# X Search

Use the `x_search` MCP tool when the user specifically wants X/Twitter posts,
threads, reactions, claims, or handle-focused research. Use normal web search
for general web pages.

## Workflow

1. Call `x_search` with a concise query.
2. If authentication is missing, let `x_search` start the built-in xAI browser
   sign-in flow, or call `x_search_auth` directly when the user asks to sign in.
3. Add `allowed_x_handles` when the user names up to 10 accounts to search
   exclusively. Do not use it together with `excluded_x_handles`.
4. Add `from_date` and `to_date` only as strict `YYYY-MM-DD` dates.
5. Set `enable_image_understanding` or `enable_video_understanding` when the
   user asks about media attached to matching posts.
6. Cite the returned X URLs. Prefer `inline_citations` when present, otherwise
   use `citations`.
7. If `degraded` is `true`, say the result was not citation-backed and either
   broaden the query/date/handle filters or ask whether to retry.

## Safety

This plugin is read-only. It does not post, like, reply, DM, follow, or mutate
X state.
