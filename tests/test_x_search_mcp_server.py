import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import x_search_mcp_server as server  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        del size
        return json.dumps(self.payload).encode("utf-8")


class XSearchToolTests(unittest.TestCase):
    def test_builds_xai_responses_payload(self):
        captured = {}

        def fake_post(url, headers, payload, timeout_seconds):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            captured["timeout_seconds"] = timeout_seconds
            return {
                "output_text": "answer",
                "citations": [{"url": "https://x.com/xai/status/1"}],
            }

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
            },
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"model": "grok-test", "timeout_seconds": "31", "retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=fake_post):
            result = server.x_search_tool(
                {
                    "query": "latest from xai",
                    "allowed_x_handles": ["@xai"],
                    "from_date": "2026-05-01",
                    "to_date": "2026-05-31",
                    "enable_image_understanding": True,
                    "enable_video_understanding": True,
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["credential_source"], "xai-api-key")
        self.assertEqual(captured["url"], "https://api.x.ai/v1/responses")
        self.assertEqual(captured["timeout_seconds"], 31)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer key")
        self.assertEqual(
            captured["payload"],
            {
                "model": "grok-test",
                "input": [{"role": "user", "content": "latest from xai"}],
                "tools": [
                    {
                        "type": "x_search",
                        "allowed_x_handles": ["xai"],
                        "from_date": "2026-05-01",
                        "to_date": "2026-05-31",
                        "enable_image_understanding": True,
                        "enable_video_understanding": True,
                    }
                ],
                "store": False,
            },
        )

    def test_marks_degraded_for_filtered_uncited_result(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
            },
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer"}):
            result = server.x_search_tool({"query": "anything", "allowed_x_handles": ["ghost"]})

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertEqual(
            result["degraded_reason"],
            "no citations returned despite filters: allowed_x_handles",
        )

    def test_not_degraded_without_filters_or_with_inline_citation(self):
        inline_payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Real post.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://x.com/xai/status/1",
                                    "title": "xAI",
                                    "start_index": 0,
                                    "end_index": 4,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", side_effect=[{"output_text": "broad", "citations": []}, inline_payload]):
            broad = server.x_search_tool({"query": "anything"})
            filtered = server.x_search_tool({"query": "anything", "allowed_x_handles": ["xai"]})

        self.assertFalse(broad["degraded"])
        self.assertIsNone(broad["degraded_reason"])
        self.assertFalse(filtered["degraded"])
        self.assertIsNone(filtered["degraded_reason"])
        self.assertEqual(filtered["inline_citations"][0]["url"], "https://x.com/xai/status/1")

    def test_allows_future_to_date(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(
            server,
            "_read_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer", "citations": []}):
            result = server.x_search_tool(
                {
                    "query": "anything",
                    "from_date": "2026-05-30",
                    "to_date": "2999-01-01",
                }
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "answer")

    def test_rejects_conflicting_handle_filters(self):
        with mock.patch.object(server, "_resolve_xai_credentials") as resolve_credentials:
            with self.assertRaises(server.XSearchError) as ctx:
                server.x_search_tool(
                    {
                        "query": "anything",
                        "allowed_x_handles": ["xai"],
                        "excluded_x_handles": ["openai"],
                    }
                )
        self.assertIn("cannot be used together", str(ctx.exception))
        resolve_credentials.assert_not_called()

    def test_rejects_future_from_date_before_http_call(self):
        with mock.patch.object(server, "_post_json") as post_json, mock.patch.object(
            server,
            "_resolve_xai_credentials",
        ) as resolve_credentials:
            with self.assertRaises(server.XSearchError):
                server.x_search_tool({"query": "anything", "from_date": "2999-01-01"})
        post_json.assert_not_called()
        resolve_credentials.assert_not_called()

    def test_rejects_string_boolean_before_credentials(self):
        with mock.patch.object(server, "_resolve_xai_credentials") as resolve_credentials:
            with self.assertRaises(server.XSearchError) as ctx:
                server.x_search_tool(
                    {
                        "query": "anything",
                        "enable_video_understanding": "false",
                    }
                )
        self.assertIn("must be a boolean", str(ctx.exception))
        resolve_credentials.assert_not_called()

    def test_rejects_custom_api_key_base_url_by_default(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {
                "X_SEARCH_HOME": home,
                "XAI_API_KEY": "key",
                "XAI_BASE_URL": "https://example.com/v1",
            },
            clear=True,
        ):
            with self.assertRaises(server.XSearchError) as ctx:
                server._resolve_xai_credentials(30)
        self.assertIn("non-xAI base URL", str(ctx.exception))

    def test_search_requires_auth_without_starting_login(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            with self.assertRaises(server.XSearchNoCredentialsError) as ctx:
                server.x_search_tool({"query": "anything"})

        self.assertIn("x_search_auth", str(ctx.exception))
        login.assert_not_called()


class OAuthResolutionTests(unittest.TestCase):
    def test_refresh_discovers_and_persists_missing_token_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            auth_path = auth_home / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "xai",
                        "auth_mode": "oauth_pkce",
                        "tokens": {
                            "access_token": "expired.token.value",
                            "refresh_token": "refresh",
                        },
                        "discovery": {},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"X_SEARCH_HOME": str(auth_home)},
                clear=True,
            ), mock.patch.object(
                server,
                "_oauth_discovery",
                return_value={"token_endpoint": "https://auth.x.ai/oauth2/token"},
            ), mock.patch.object(
                server,
                "_jwt_is_expiring",
                return_value=True,
            ), mock.patch.object(
                server,
                "_refresh_oauth_token",
                return_value={"access_token": "fresh", "refresh_token": "refresh2"},
            ):
                token, base_url, source = server._resolve_oauth_credentials(30)

            self.assertEqual((token, base_url, source), ("fresh", "https://api.x.ai/v1", "xai-oauth"))
            saved = json.loads(auth_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["tokens"]["access_token"], "fresh")
            self.assertEqual(saved["tokens"]["refresh_token"], "refresh2")
            self.assertEqual(
                saved["discovery"]["token_endpoint"],
                "https://auth.x.ai/oauth2/token",
            )

    def test_exchange_code_includes_pkce_challenge_echo(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )

        with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen):
            payload = server._exchange_code_for_tokens(
                token_endpoint="https://auth.x.ai/oauth2/token",
                code="code",
                redirect_uri="http://127.0.0.1:56121/callback",
                code_verifier="verifier",
                code_challenge="challenge",
                timeout_seconds=45,
            )

        self.assertEqual(payload["access_token"], "access")
        self.assertEqual(captured["url"], "https://auth.x.ai/oauth2/token")
        parsed = urllib.parse.parse_qs(captured["body"])
        self.assertEqual(parsed["grant_type"], ["authorization_code"])
        self.assertEqual(parsed["code_verifier"], ["verifier"])
        self.assertEqual(parsed["code_challenge"], ["challenge"])
        self.assertEqual(parsed["code_challenge_method"], ["S256"])

    def test_auth_tool_reports_existing_api_key_without_browser(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ), mock.patch.object(server, "_run_xai_oauth_login") as login:
            result = server.x_search_auth_tool({})

        self.assertTrue(result["success"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["credential_source"], "xai-api-key")
        login.assert_not_called()

    def test_status_uses_codex_x_search_home(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            result = server.x_search_status_tool({})

        self.assertTrue(result["success"])
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["auth_store"], str(Path(home) / "auth.json"))
        self.assertIn("x_search_auth", result["error"])

    def test_status_refreshes_oauth_before_reporting_authenticated(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_home = Path(tmp)
            auth_path = auth_home / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "xai",
                        "auth_mode": "oauth_pkce",
                        "tokens": {
                            "access_token": "expired.token.value",
                            "refresh_token": "refresh",
                        },
                        "discovery": {
                            "token_endpoint": "https://auth.x.ai/oauth2/token",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"X_SEARCH_HOME": str(auth_home)},
                clear=True,
            ), mock.patch.object(
                server,
                "_jwt_is_expiring",
                return_value=True,
            ), mock.patch.object(
                server,
                "_refresh_oauth_token",
                return_value={"access_token": "fresh", "refresh_token": "refresh"},
            ):
                result = server.x_search_status_tool({})

        self.assertTrue(result["authenticated"])
        self.assertEqual(result["credential_source"], "xai-oauth")
        self.assertEqual(result["base_url"], "https://api.x.ai/v1")


class McpProtocolTests(unittest.TestCase):
    def test_mcp_tools_list_exposes_auth_before_search(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertNotIn("x_search", names)
        self.assertIn("x_search_auth", names)
        self.assertIn("x_search_status", names)

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home, "XAI_API_KEY": "key"},
            clear=True,
        ):
            authed_response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
            )
        authed_names = [tool["name"] for tool in authed_response["result"]["tools"]]
        self.assertIn("x_search", authed_names)
        self.assertIn("x_search_auth", authed_names)

        call_response = server._handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "x_search", "arguments": {"query": ""}},
            }
        )
        self.assertTrue(call_response["result"]["isError"])
        body = json.loads(call_response["result"]["content"][0]["text"])
        self.assertEqual(body["error"], "query is required for x_search")

    def test_mcp_search_call_reports_auth_required(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            os.environ,
            {"X_SEARCH_HOME": home},
            clear=True,
        ):
            call_response = server._handle_mcp_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "x_search", "arguments": {"query": "xai"}},
                }
            )
        self.assertTrue(call_response["result"]["isError"])
        body = json.loads(call_response["result"]["content"][0]["text"])
        self.assertTrue(body["auth_required"])
        self.assertEqual(body["auth_tool"], "x_search_auth")

    def test_mcp_content_length_round_trip(self):
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            body = json.dumps(message).encode("utf-8")
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body),
                encoding="utf-8",
            )
            read = server._read_mcp_message()
            self.assertEqual(read, (message, "content-length"))

            out = io.BytesIO()
            sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
            server._send_mcp_message(
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                "content-length",
            )
            sys.stdout.flush()
            self.assertIn(b"Content-Length:", out.getvalue())
        finally:
            sys.stdin = original_stdin
            sys.stdout = original_stdout

    def test_mcp_newline_round_trip(self):
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            sys.stdin = io.TextIOWrapper(
                io.BytesIO(json.dumps(message).encode("utf-8") + b"\n"),
                encoding="utf-8",
            )
            read = server._read_mcp_message()
            self.assertEqual(read, (message, "newline"))

            out = io.BytesIO()
            sys.stdout = io.TextIOWrapper(out, encoding="utf-8")
            server._send_mcp_message({"jsonrpc": "2.0", "id": 1, "result": {}})
            sys.stdout.flush()
            self.assertTrue(out.getvalue().endswith(b"\n"))
            self.assertNotIn(b"Content-Length:", out.getvalue())
        finally:
            sys.stdin = original_stdin
            sys.stdout = original_stdout

    def test_initialize_selects_supported_protocol_version(self):
        response = server._handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1900-01-01"},
            }
        )
        self.assertEqual(
            response["result"]["protocolVersion"],
            server.SUPPORTED_PROTOCOL_VERSIONS[0],
        )

    def test_mcp_rejects_malformed_input(self):
        original_stdin = sys.stdin
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(b"not-json\n"), encoding="utf-8")
            with self.assertRaises(server.McpProtocolError) as ctx:
                server._read_mcp_message()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(ctx.exception.code, -32600)


if __name__ == "__main__":
    unittest.main()
