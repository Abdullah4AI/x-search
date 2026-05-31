#!/usr/bin/env python3
"""MCP server for xAI-backed X Search.

This mirrors Hermes' x_search behavior without depending on Hermes at runtime:
it reuses Hermes xAI OAuth credentials when available, falls back to XAI_API_KEY,
calls the xAI Responses API with a server-side {"type": "x_search"} tool, and
returns citation/degraded metadata in the same shape.
"""

from __future__ import annotations

import base64
import contextlib
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
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_MCP_CONTENT_LENGTH = 5 * 1024 * 1024
SERVER_NAME = "x-search"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2024-11-05")


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
            "maxItems": MAX_HANDLES,
            "description": "Optional list of X handles to include exclusively (max 10).",
        },
        "excluded_x_handles": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_HANDLES,
            "description": "Optional list of X handles to exclude (max 10).",
        },
        "from_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional start date in YYYY-MM-DD format.",
        },
        "to_date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
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
    "additionalProperties": False,
    "not": {"required": ["allowed_x_handles", "excluded_x_handles"]},
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


class McpProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _home() -> Path:
    return Path.home()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (_home() / ".hermes")).expanduser()


def _hermes_agent_path() -> Optional[Path]:
    raw = os.environ.get("HERMES_AGENT_PATH", "").strip()
    candidates = [Path(raw).expanduser()] if raw else []
    candidates.append(_hermes_home() / "hermes-agent")
    for candidate in candidates:
        if (candidate / "tools" / "xai_http.py").is_file():
            return candidate
    return None


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
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = _strip_inline_comment(value.strip())
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
    for path in (_hermes_home() / ".env",):
        value = _read_dotenv(path).get(name)
        if value is not None and str(value).strip():
            return str(value).strip()

    value = os.environ.get(name)
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
    hermes_agent = _hermes_agent_path()
    if hermes_agent and str(hermes_agent) not in sys.path:
        sys.path.insert(0, str(hermes_agent))
    if hermes_agent:
        try:
            from hermes_cli.config import load_config  # type: ignore

            config = load_config().get("x_search", {}) or {}
            if isinstance(config, dict):
                return {str(key): str(value) for key, value in config.items()}
        except Exception:
            pass

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

    model = hermes_cfg.get("model") or DEFAULT_X_SEARCH_MODEL
    timeout_raw = hermes_cfg.get("timeout_seconds") or str(DEFAULT_X_SEARCH_TIMEOUT_SECONDS)
    retries_raw = hermes_cfg.get("retries") or str(DEFAULT_X_SEARCH_RETRIES)

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


@contextlib.contextmanager
def _auth_store_lock():
    lock_path = _hermes_home() / "auth.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl_module = None
        try:
            import fcntl as fcntl_module

            fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_EX)
        except Exception:
            fcntl_module = None
        try:
            yield
        finally:
            if fcntl_module is not None:
                try:
                    fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
                except Exception:
                    pass


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


def _validate_base_url(base_url: str, credential_source: str) -> str:
    stripped = str(base_url or DEFAULT_XAI_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(stripped)
    if parsed.scheme != "https":
        raise XSearchError("Refusing to send xAI bearer to a non-HTTPS base URL")
    if _xai_endpoint_ok(stripped):
        return stripped
    allow_custom = str(_env_value("X_SEARCH_ALLOW_CUSTOM_BASE_URL", "") or "").lower()
    if credential_source == "xai" and allow_custom in {"1", "true", "yes"}:
        return stripped
    raise XSearchError(
        "Refusing to send xAI bearer to a non-xAI base URL. Set "
        "X_SEARCH_ALLOW_CUSTOM_BASE_URL=1 only if you intentionally use a trusted proxy."
    )


def _read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise XSearchError(f"HTTP response exceeded {limit} bytes")
    return data


def _read_json_response(response: Any, limit: int = MAX_RESPONSE_BYTES) -> Dict[str, Any]:
    payload = json.loads(_read_limited(response, limit).decode("utf-8"))
    if not isinstance(payload, dict):
        raise XSearchError("HTTP response was not a JSON object")
    return payload


def _redact(text: str) -> str:
    patterns = [
        r"Bearer\s+[A-Za-z0-9._~+/=-]+",
        r'("?(?:access_token|refresh_token|api_key|authorization)"?\s*[:=]\s*")([^"]+)(")',
        r"((?:access_token|refresh_token|api_key|authorization)\s*[:=]\s*)([^\s,&]+)",
    ]
    redacted = text
    redacted = re.sub(patterns[0], "Bearer [REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(patterns[1], r"\1[REDACTED]\3", redacted, flags=re.IGNORECASE)
    redacted = re.sub(patterns[2], r"\1[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted


def _oauth_discovery(timeout_seconds: int) -> Dict[str, str]:
    request = urllib.request.Request(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
    )
    with urllib.request.urlopen(request, timeout=max(5, timeout_seconds)) as response:
        payload = _read_json_response(response)
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
            payload = _read_json_response(response)
    except urllib.error.HTTPError as exc:
        detail = _redact(_read_limited(exc, MAX_ERROR_BYTES).decode("utf-8", errors="replace").strip())
        if exc.code == 403:
            raise XSearchError(
                "xAI token refresh failed with HTTP 403. This OAuth account may "
                "not be authorized for xAI API access; set XAI_API_KEY as a fallback."
            )
        raise XSearchError(
            f"xAI token refresh failed with HTTP {exc.code}"
            + (f": {detail[:500]}" if detail else "")
        )

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XSearchError("xAI token refresh response was missing access_token")
    return payload


def _resolve_with_hermes_runtime(force_refresh: bool = False) -> Optional[Tuple[str, str, str]]:
    if str(_env_value("X_SEARCH_DISABLE_HERMES_RESOLVER", "") or "").lower() in {"1", "true", "yes"}:
        return None
    hermes_agent = _hermes_agent_path()
    if not hermes_agent:
        return None
    if str(hermes_agent) not in sys.path:
        sys.path.insert(0, str(hermes_agent))
    try:
        from tools.xai_http import resolve_xai_http_credentials  # type: ignore

        creds = resolve_xai_http_credentials(force_refresh=force_refresh)
    except Exception:
        return None

    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        return None
    source = str(creds.get("provider") or "xai")
    base_url = _validate_base_url(
        str(creds.get("base_url") or DEFAULT_XAI_BASE_URL),
        source,
    )
    return api_key, base_url, source


def _resolve_oauth_credentials(
    timeout_seconds: int,
    *,
    force_refresh: bool = False,
) -> Optional[Tuple[str, str, str]]:
    if str(_env_value("X_SEARCH_DISABLE_HERMES_OAUTH", "") or "").lower() in {"1", "true", "yes"}:
        return None

    with _auth_store_lock():
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
        should_refresh = force_refresh or _jwt_is_expiring(
            access_token,
            XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
        )
        if access_token and not should_refresh:
            return access_token, _oauth_base_url(), "xai-oauth"

        if not refresh_token:
            return None

        discovery = state.get("discovery") if isinstance(state.get("discovery"), dict) else {}
        updated_discovery = dict(discovery)
        token_endpoint = str(updated_discovery.get("token_endpoint") or "").strip()
        if not token_endpoint:
            token_endpoint = _oauth_discovery(timeout_seconds)["token_endpoint"]
            updated_discovery["token_endpoint"] = token_endpoint
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
        state["discovery"] = updated_discovery
        _save_auth_store(auth_path, auth_store)
        return updated_tokens["access_token"], _oauth_base_url(), "xai-oauth"


def _oauth_base_url() -> str:
    override = (
        _env_value("HERMES_XAI_BASE_URL")
        or _env_value("XAI_BASE_URL")
        or DEFAULT_XAI_OAUTH_BASE_URL
    ).rstrip("/")
    return _validate_base_url(override, "xai-oauth")


def _resolve_xai_credentials(
    timeout_seconds: int,
    *,
    force_refresh: bool = False,
) -> Tuple[str, str, str]:
    hermes = _resolve_with_hermes_runtime(force_refresh=force_refresh)
    if hermes:
        return hermes

    try:
        oauth = _resolve_oauth_credentials(timeout_seconds, force_refresh=force_refresh)
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
    base_url = _validate_base_url(str(_env_value("XAI_BASE_URL") or DEFAULT_XAI_BASE_URL), "xai")
    return api_key, base_url, "xai"


def check_x_search_requirements() -> bool:
    try:
        timeout_seconds = int(_x_search_config()["timeout_seconds"])
        api_key, _, _ = _resolve_xai_credentials(timeout_seconds)
        return bool(str(api_key or "").strip())
    except Exception:
        return False


def _normalize_handles(value: Any, field_name: str) -> List[str]:
    if value is None:
        items: Iterable[Any] = []
    elif isinstance(value, list):
        items = value
    else:
        raise XSearchError(f"{field_name} must be an array of strings")

    cleaned: List[str] = []
    for handle in items:
        if not isinstance(handle, str):
            raise XSearchError(f"{field_name} must contain only strings")
        normalized = handle.strip().lstrip("@")
        if normalized:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise XSearchError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _bool_arg(arguments: Dict[str, Any], field_name: str) -> bool:
    value = arguments.get(field_name, False)
    if isinstance(value, bool):
        return value
    raise XSearchError(f"{field_name} must be a boolean")


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
    hermes_agent = _hermes_agent_path()
    if hermes_agent and str(hermes_agent) not in sys.path:
        sys.path.insert(0, str(hermes_agent))
    if hermes_agent:
        try:
            from tools.xai_http import hermes_xai_user_agent  # type: ignore

            return str(hermes_xai_user_agent())
        except Exception:
            pass
    return f"Codex-X-Search/{SERVER_VERSION}"


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = _redact(_read_limited(exc, MAX_ERROR_BYTES).decode("utf-8", errors="replace").strip())
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
        return _read_json_response(response)


def x_search_tool(arguments: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = set(X_SEARCH_INPUT_SCHEMA["properties"])
    unknown_keys = sorted(set(arguments) - allowed_keys)
    if unknown_keys:
        raise XSearchError(f"unknown x_search argument(s): {', '.join(unknown_keys)}")

    query = str(arguments.get("query") or "").strip()
    if not query:
        raise XSearchError("query is required for x_search")

    config = _x_search_config()
    timeout_seconds = int(config["timeout_seconds"])

    allowed = _normalize_handles(arguments.get("allowed_x_handles"), "allowed_x_handles")
    excluded = _normalize_handles(arguments.get("excluded_x_handles"), "excluded_x_handles")
    if allowed and excluded:
        raise XSearchError("allowed_x_handles and excluded_x_handles cannot be used together")

    from_date = str(arguments.get("from_date") or "").strip()
    to_date = str(arguments.get("to_date") or "").strip()
    _validate_date_range(from_date, to_date)
    enable_image_understanding = _bool_arg(arguments, "enable_image_understanding")
    enable_video_understanding = _bool_arg(arguments, "enable_video_understanding")

    api_key, base_url, source = _resolve_xai_credentials(timeout_seconds)

    tool_def: Dict[str, Any] = {"type": "x_search"}
    if allowed:
        tool_def["allowed_x_handles"] = allowed
    if excluded:
        tool_def["excluded_x_handles"] = excluded
    if from_date:
        tool_def["from_date"] = from_date
    if to_date:
        tool_def["to_date"] = to_date
    if enable_image_understanding:
        tool_def["enable_image_understanding"] = True
    if enable_video_understanding:
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
    force_refreshed = False
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
            if exc.code == 401 and source == "xai-oauth" and not force_refreshed:
                api_key, base_url, source = _resolve_xai_credentials(
                    timeout_seconds,
                    force_refresh=True,
                )
                headers["Authorization"] = f"Bearer {api_key}"
                force_refreshed = True
                continue
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


def _read_mcp_message() -> Optional[Tuple[Dict[str, Any], str]]:
    first_line = sys.stdin.buffer.readline()
    if first_line == b"":
        return None

    stripped_first = first_line.strip()
    if stripped_first.startswith(b"{"):
        try:
            payload = json.loads(stripped_first.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpProtocolError(-32700, f"Parse error: {exc}") from exc
        if not isinstance(payload, dict):
            raise McpProtocolError(-32600, "Invalid Request")
        return payload, "newline"

    headers: Dict[str, str] = {}
    decoded_first = first_line.decode("ascii", errors="replace").strip()
    if ":" in decoded_first:
        key, value = decoded_first.split(":", 1)
        headers[key.lower()] = value.strip()
    elif decoded_first:
        raise McpProtocolError(-32600, "Invalid Request")
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
        raise McpProtocolError(-32600, "Missing Content-Length")
    try:
        content_length = int(length_raw)
    except ValueError as exc:
        raise McpProtocolError(-32600, "Invalid Content-Length") from exc
    if content_length < 0 or content_length > MAX_MCP_CONTENT_LENGTH:
        raise McpProtocolError(-32600, "Invalid Content-Length")
    body = sys.stdin.buffer.read(content_length)
    if len(body) != content_length:
        raise McpProtocolError(-32700, "Unexpected EOF while reading message body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise McpProtocolError(-32700, f"Parse error: {exc}") from exc
    if not isinstance(payload, dict):
        raise McpProtocolError(-32600, "Invalid Request")
    return payload, "content-length"


def _send_mcp_message(payload: Dict[str, Any], framing: str = "newline") -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "content-length":
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(body)
    else:
        sys.stdout.buffer.write(body + b"\n")
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
    if not isinstance(message, dict):
        return _mcp_error(None, -32600, "Invalid Request")
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        return None

    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or SUPPORTED_PROTOCOL_VERSIONS[0])
        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return _mcp_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return _mcp_result(request_id, {})

    if method == "tools/list":
        tools = [X_SEARCH_TOOL] if check_x_search_requirements() else []
        return _mcp_result(request_id, {"tools": tools})

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
        try:
            read_result = _read_mcp_message()
        except McpProtocolError as exc:
            _send_mcp_message(_mcp_error(None, exc.code, exc.message))
            continue
        except Exception as exc:
            _send_mcp_message(_mcp_error(None, -32603, str(exc)))
            continue
        if read_result is None:
            return 0
        message, framing = read_result
        response = _handle_mcp_request(message)
        if response is not None:
            _send_mcp_message(response, framing)


if __name__ == "__main__":
    raise SystemExit(main())
