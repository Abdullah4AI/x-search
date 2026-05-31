#!/usr/bin/env python3
"""MCP server for xAI-backed X Search.

This mirrors Hermes' x_search behavior without depending on Hermes at runtime:
it reuses Hermes xAI OAuth credentials when available, falls back to XAI_API_KEY,
calls the xAI Responses API with a server-side {"type": "x_search"} tool, and
returns citation/degraded metadata in the same shape.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_X_SEARCH_MODEL = "grok-4.20-reasoning"
DEFAULT_X_SEARCH_TIMEOUT_SECONDS = 180
DEFAULT_X_SEARCH_RETRIES = 2
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
MAX_HANDLES = 10
SERVER_NAME = "x-search"
SERVER_VERSION = "0.1.0"


X_SEARCH_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look up on X.",
        },
        "allowed_x_handles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of X handles to include exclusively (max 10).",
        },
        "excluded_x_handles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of X handles to exclude (max 10).",
        },
        "from_date": {
            "type": "string",
            "description": "Optional start date in YYYY-MM-DD format.",
        },
        "to_date": {
            "type": "string",
            "description": "Optional end date in YYYY-MM-DD format.",
        },
        "enable_image_understanding": {
            "type": "boolean",
            "description": "Whether xAI should analyze images attached to matching X posts.",
            "default": False,
        },
        "enable_video_understanding": {
            "type": "boolean",
            "description": "Whether xAI should analyze videos attached to matching X posts.",
            "default": False,
        },
    },
    "required": ["query"],
}


X_SEARCH_TOOL: Dict[str, Any] = {
    "name": "x_search",
    "description": (
        "Search X (Twitter) posts, profiles, and threads using xAI's built-in "
        "x_search Responses tool. Use this for current discussion, reactions, "
        "or claims on X rather than general web pages. Requires Hermes xAI "
        "OAuth credentials or XAI_API_KEY."
    ),
    "inputSchema": X_SEARCH_INPUT_SCHEMA,
}


class XSearchError(Exception):
    """Expected x_search error surfaced as a structured tool result."""


def _home() -> Path:
    return Path.home()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (_home() / ".hermes")).expanduser()


def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    for path in (Path.cwd() / ".env", _hermes_home() / ".env"):
        value = _read_dotenv(path).get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _strip_inline_comment(value: str) -> str:
    quote: Optional[str] = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        if char == "#" and quote is None:
            return value[:index].strip()
    return value.strip()


def _read_hermes_x_search_config() -> Dict[str, str]:
    path = _hermes_home() / "config.yaml"
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    config: Dict[str, str] = {}
    in_section = False
    section_indent = 0
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if re.match(r"^x_search\s*:\s*(?:#.*)?$", stripped):
            in_section = True
            section_indent = indent
            continue
        if in_section and indent <= section_indent and not raw.startswith(" "):
            break
        if not in_section or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = _strip_inline_comment(value).strip()
        if (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        if key and value:
            config[key] = value
    return config


def _x_search_config() -> Dict[str, Any]:
    hermes_cfg = _read_hermes_x_search_config()

    model = (
        _env_value("X_SEARCH_MODEL")
        or hermes_cfg.get("model")
        or DEFAULT_X_SEARCH_MODEL
    )
    timeout_raw = (
        _env_value("X_SEARCH_TIMEOUT_SECONDS")
        or hermes_cfg.get("timeout_seconds")
        or str(DEFAULT_X_SEARCH_TIMEOUT_SECONDS)
    )
    retries_raw = (
        _env_value("X_SEARCH_RETRIES")
        or hermes_cfg.get("retries")
        or str(DEFAULT_X_SEARCH_RETRIES)
    )

    try:
        timeout_seconds = max(30, int(str(timeout_raw)))
    except Exception:
        timeout_seconds = DEFAULT_X_SEARCH_TIMEOUT_SECONDS

    try:
        retries = max(0, int(str(retries_raw)))
    except Exception:
        retries = DEFAULT_X_SEARCH_RETRIES

    return {
        "model": str(model).strip() or DEFAULT_X_SEARCH_MODEL,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
    }


def _base64url_json(segment: str) -> Dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def _jwt_is_expiring(access_token: str, skew_seconds: int) -> bool:
    if "." not in access_token:
        return False
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return False
        payload = _base64url_json(parts[1])
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= time.time() + max(0, int(skew_seconds))
    except Exception:
        return False


def _load_auth_store() -> Tuple[Path, Dict[str, Any]]:
    path = _hermes_home() / "auth.json"
    if not path.is_file():
        return path, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path, {}
    return path, payload if isinstance(payload, dict) else {}


def _save_auth_store(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, path)


def _xai_endpoint_ok(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "x.ai" or host.endswith(".x.ai"))


def _oauth_discovery(timeout_seconds: int) -> Dict[str, str]:
    request = urllib.request.Request(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
    )
    with urllib.request.urlopen(request, timeout=max(5, timeout_seconds)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise XSearchError("xAI OIDC discovery response was not a JSON object")
    token_endpoint = str(payload.get("token_endpoint") or "").strip()
    authorization_endpoint = str(payload.get("authorization_endpoint") or "").strip()
    if not token_endpoint or not authorization_endpoint:
        raise XSearchError("xAI OIDC discovery response was missing required endpoints")
    if not _xai_endpoint_ok(token_endpoint) or not _xai_endpoint_ok(authorization_endpoint):
        raise XSearchError("xAI OIDC discovery returned a non-xAI endpoint")
    return {
        "token_endpoint": token_endpoint,
        "authorization_endpoint": authorization_endpoint,
    }


def _refresh_oauth_token(
    refresh_token: str,
    token_endpoint: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if not refresh_token:
        raise XSearchError("xAI OAuth state is missing refresh_token")
    if not token_endpoint:
        token_endpoint = _oauth_discovery(timeout_seconds)["token_endpoint"]
    if not _xai_endpoint_ok(token_endpoint):
        raise XSearchError("Refusing to send xAI OAuth refresh_token to a non-xAI endpoint")

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _user_agent(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(5, timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 403:
            raise XSearchError(
                "xAI token refresh failed with HTTP 403. This OAuth account may "
                "not be authorized for xAI API access; set XAI_API_KEY as a fallback."
            )
        raise XSearchError(
            f"xAI token refresh failed with HTTP {exc.code}"
            + (f": {detail[:500]}" if detail else "")
        )

    if not isinstance(payload, dict):
        raise XSearchError("xAI token refresh response was not a JSON object")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XSearchError("xAI token refresh response was missing access_token")
    return payload


def _resolve_oauth_credentials(timeout_seconds: int) -> Optional[Tuple[str, str, str]]:
    if str(_env_value("X_SEARCH_DISABLE_HERMES_OAUTH", "") or "").lower() in {"1", "true", "yes"}:
        return None

    auth_path, auth_store = _load_auth_store()
    providers = auth_store.get("providers") if isinstance(auth_store, dict) else None
    state = providers.get("xai-oauth") if isinstance(providers, dict) else None
    if not isinstance(state, dict):
        return None

    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        return None

    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if access_token and not _jwt_is_expiring(access_token, XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS):
        return access_token, _oauth_base_url(), "xai-oauth"

    if not refresh_token:
        return None

    discovery = state.get("discovery") if isinstance(state.get("discovery"), dict) else {}
    token_endpoint = str(discovery.get("token_endpoint") or "").strip()
    refreshed = _refresh_oauth_token(refresh_token, token_endpoint, timeout_seconds)

    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = str(refreshed.get("access_token") or "").strip()
    updated_tokens["refresh_token"] = str(refreshed.get("refresh_token") or refresh_token).strip()
    if refreshed.get("id_token"):
        updated_tokens["id_token"] = str(refreshed.get("id_token") or "").strip()
    if refreshed.get("expires_in") is not None:
        updated_tokens["expires_in"] = refreshed.get("expires_in")
    if refreshed.get("token_type"):
        updated_tokens["token_type"] = str(refreshed.get("token_type") or "Bearer").strip() or "Bearer"

    state["tokens"] = updated_tokens
    state["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state["auth_mode"] = "oauth_pkce"
    if token_endpoint:
        state["discovery"] = {"token_endpoint": token_endpoint}
    _save_auth_store(auth_path, auth_store)
    return updated_tokens["access_token"], _oauth_base_url(), "xai-oauth"


def _oauth_base_url() -> str:
    override = (
        _env_value("HERMES_XAI_BASE_URL")
        or _env_value("XAI_BASE_URL")
        or DEFAULT_XAI_OAUTH_BASE_URL
    ).rstrip("/")
    if not _xai_endpoint_ok(override):
        raise XSearchError("Refusing to send xAI OAuth bearer to a non-xAI base URL")
    return override


def _resolve_xai_credentials(timeout_seconds: int) -> Tuple[str, str, str]:
    try:
        oauth = _resolve_oauth_credentials(timeout_seconds)
        if oauth:
            return oauth
    except XSearchError:
        # Keep Hermes parity: OAuth is preferred when usable, but an API key can
        # still rescue a tier-denied or stale OAuth setup.
        pass

    api_key = str(_env_value("XAI_API_KEY") or "").strip()
    if not api_key:
        raise XSearchError(
            "No xAI credentials available. Reuse Hermes OAuth with "
            "`hermes auth add xai-oauth`, or set XAI_API_KEY."
        )
    base_url = str(_env_value("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL).strip().rstrip("/")
    return api_key, base_url, "xai"


def _normalize_handles(value: Any, field_name: str) -> List[str]:
    if value is None:
        items: Iterable[Any] = []
    elif isinstance(value, str):
        items = [value]
    elif isinstance(value, Iterable):
        items = value
    else:
        raise XSearchError(f"{field_name} must be an array of strings")

    cleaned: List[str] = []
    for handle in items:
        normalized = str(handle or "").strip().lstrip("@")
        if normalized:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise XSearchError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _parse_iso_date(value: str, field_name: str) -> date:
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise XSearchError(f"{field_name} must be YYYY-MM-DD (got {raw!r})") from exc


def _validate_date_range(from_date: str, to_date: str) -> None:
    parsed_from: Optional[date] = None
    parsed_to: Optional[date] = None
    if from_date.strip():
        parsed_from = _parse_iso_date(from_date, "from_date")
    if to_date.strip():
        parsed_to = _parse_iso_date(to_date, "to_date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise XSearchError(
            f"from_date ({parsed_from.isoformat()}) must be on or before "
            f"to_date ({parsed_to.isoformat()})"
        )
    if parsed_from is not None:
        today_utc = datetime.now(timezone.utc).date()
        if parsed_from > today_utc:
            raise XSearchError(
                f"from_date ({parsed_from.isoformat()}) is in the future; "
                f"X Search only indexes past posts (today UTC is {today_utc.isoformat()})"
            )


def _extract_response_text(payload: Dict[str, Any]) -> str:
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    parts: List[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations", []) or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                citations.append(
                    {
                        "url": annotation.get("url", ""),
                        "title": annotation.get("title", ""),
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


def _user_agent() -> str:
    return f"Codex-X-Search/{SERVER_VERSION}"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or "").strip()
        error = str(payload.get("error") or payload.get("message") or "").strip()
        message = error or str(payload)
        if code and code not in message:
            message = f"{code}: {message}"
        return message
    return body[:500] or str(exc)


def _post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: int,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def x_search_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise XSearchError("query is required for x_search")

    config = _x_search_config()
    timeout_seconds = int(config["timeout_seconds"])

    api_key, base_url, source = _resolve_xai_credentials(timeout_seconds)

    allowed = _normalize_handles(arguments.get("allowed_x_handles"), "allowed_x_handles")
    excluded = _normalize_handles(arguments.get("excluded_x_handles"), "excluded_x_handles")
    if allowed and excluded:
        raise XSearchError("allowed_x_handles and excluded_x_handles cannot be used together")

    from_date = str(arguments.get("from_date") or "").strip()
    to_date = str(arguments.get("to_date") or "").strip()
    _validate_date_range(from_date, to_date)

    tool_def: Dict[str, Any] = {"type": "x_search"}
    if allowed:
        tool_def["allowed_x_handles"] = allowed
    if excluded:
        tool_def["excluded_x_handles"] = excluded
    if from_date:
        tool_def["from_date"] = from_date
    if to_date:
        tool_def["to_date"] = to_date
    if bool(arguments.get("enable_image_understanding", False)):
        tool_def["enable_image_understanding"] = True
    if bool(arguments.get("enable_video_understanding", False)):
        tool_def["enable_video_understanding"] = True

    payload = {
        "model": config["model"],
        "input": [{"role": "user", "content": query}],
        "tools": [tool_def],
        "store": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _user_agent(),
    }

    response_payload: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    for attempt in range(int(config["retries"]) + 1):
        try:
            response_payload = _post_json(
                f"{base_url}/responses",
                headers,
                payload,
                timeout_seconds,
            )
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt >= int(config["retries"]):
                return {
                    "success": False,
                    "provider": "xai",
                    "tool": "x_search",
                    "error": _http_error_message(exc),
                    "error_type": "HTTPError",
                }
            last_error = _http_error_message(exc)
        except TimeoutError as exc:
            if attempt >= int(config["retries"]):
                return {
                    "success": False,
                    "provider": "xai",
                    "tool": "x_search",
                    "error": f"xAI x_search timed out after {timeout_seconds} seconds",
                    "error_type": type(exc).__name__,
                }
            last_error = str(exc)
        except OSError as exc:
            if attempt >= int(config["retries"]):
                return {
                    "success": False,
                    "provider": "xai",
                    "tool": "x_search",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            last_error = str(exc)
        time.sleep(min(5.0, 1.5 * (attempt + 1)))

    if response_payload is None:
        return {
            "success": False,
            "provider": "xai",
            "tool": "x_search",
            "error": last_error or "x_search request did not return a response",
            "error_type": "RuntimeError",
        }

    answer = _extract_response_text(response_payload)
    citations = list(response_payload.get("citations") or [])
    inline_citations = _extract_inline_citations(response_payload)

    active_filters: List[str] = []
    if allowed:
        active_filters.append("allowed_x_handles")
    if excluded:
        active_filters.append("excluded_x_handles")
    if from_date:
        active_filters.append("from_date")
    if to_date:
        active_filters.append("to_date")
    degraded = bool(active_filters) and not citations and not inline_citations

    return {
        "success": True,
        "provider": "xai",
        "credential_source": source,
        "tool": "x_search",
        "model": payload["model"],
        "query": query,
        "answer": answer,
        "citations": citations,
        "inline_citations": inline_citations,
        "degraded": degraded,
        "degraded_reason": (
            f"no citations returned despite filters: {', '.join(active_filters)}"
            if degraded
            else None
        ),
    }


def _tool_error(message: str, error_type: str = "XSearchError") -> Dict[str, Any]:
    return {
        "success": False,
        "provider": "xai",
        "tool": "x_search",
        "error": message,
        "error_type": error_type,
    }


def _read_mcp_message() -> Optional[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        decoded = line.decode("ascii", errors="replace").strip()
        if decoded == "":
            break
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()

    length_raw = headers.get("content-length")
    if not length_raw:
        return None
    body = sys.stdin.buffer.read(int(length_raw))
    return json.loads(body.decode("utf-8"))


def _send_mcp_message(payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _mcp_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _mcp_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _handle_mcp_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        return None

    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or "2024-11-05")
        return _mcp_result(
            request_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return _mcp_result(request_id, {})

    if method == "tools/list":
        return _mcp_result(request_id, {"tools": [X_SEARCH_TOOL]})

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name != "x_search":
            return _mcp_error(request_id, -32602, f"Unknown tool: {name}")
        try:
            result = x_search_tool(arguments)
        except XSearchError as exc:
            result = _tool_error(str(exc))
        except Exception as exc:
            result = _tool_error(str(exc), type(exc).__name__)
        return _mcp_result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": not bool(result.get("success")),
            },
        )

    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return _mcp_result(request_id, {key: []})

    return _mcp_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        message = _read_mcp_message()
        if message is None:
            return 0
        response = _handle_mcp_request(message)
        if response is not None:
            _send_mcp_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
