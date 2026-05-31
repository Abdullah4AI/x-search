import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import x_search_mcp_server as server  # noqa: E402


class XSearchToolTests(unittest.TestCase):
    def test_builds_xai_responses_payload_like_hermes(self):
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

        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": tempfile.mkdtemp(),
                "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                "X_SEARCH_DISABLE_HERMES_OAUTH": "1",
                "XAI_API_KEY": "key",
            },
            clear=False,
        ), mock.patch.object(
            server,
            "_read_hermes_x_search_config",
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
        self.assertEqual(result["credential_source"], "xai")
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
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": tempfile.mkdtemp(),
                "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                "X_SEARCH_DISABLE_HERMES_OAUTH": "1",
                "XAI_API_KEY": "key",
            },
            clear=False,
        ), mock.patch.object(
            server,
            "_read_hermes_x_search_config",
            return_value={"retries": "0"},
        ), mock.patch.object(server, "_post_json", return_value={"output_text": "answer"}):
            result = server.x_search_tool({"query": "anything", "allowed_x_handles": ["ghost"]})

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded"])
        self.assertEqual(
            result["degraded_reason"],
            "no citations returned despite filters: allowed_x_handles",
        )

    def test_rejects_conflicting_handle_filters(self):
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": tempfile.mkdtemp(),
                "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                "X_SEARCH_DISABLE_HERMES_OAUTH": "1",
                "XAI_API_KEY": "key",
            },
            clear=False,
        ), mock.patch.object(server, "_resolve_xai_credentials") as resolve_credentials:
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
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": tempfile.mkdtemp(),
                "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                "X_SEARCH_DISABLE_HERMES_OAUTH": "1",
                "XAI_API_KEY": "key",
            },
            clear=False,
        ), mock.patch.object(server, "_post_json") as post_json, mock.patch.object(
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
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": tempfile.mkdtemp(),
                "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                "X_SEARCH_DISABLE_HERMES_OAUTH": "1",
                "XAI_API_KEY": "key",
                "XAI_BASE_URL": "https://example.com/v1",
            },
            clear=False,
        ):
            with self.assertRaises(server.XSearchError) as ctx:
                server._resolve_xai_credentials(30)
        self.assertIn("non-xAI base URL", str(ctx.exception))


class OAuthResolutionTests(unittest.TestCase):
    def test_prefers_hermes_runtime_resolver(self):
        with mock.patch.object(
            server,
            "_resolve_with_hermes_runtime",
            return_value=("token", "https://api.x.ai/v1", "xai-oauth"),
        ), mock.patch.object(server, "_resolve_oauth_credentials") as standalone_oauth:
            self.assertEqual(
                server._resolve_xai_credentials(30),
                ("token", "https://api.x.ai/v1", "xai-oauth"),
            )
        standalone_oauth.assert_not_called()

    def test_refresh_discovers_and_persists_missing_token_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_home = Path(tmp)
            auth_path = hermes_home / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "xai-oauth": {
                                "tokens": {
                                    "access_token": "expired.token.value",
                                    "refresh_token": "refresh",
                                },
                                "discovery": {},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "X_SEARCH_DISABLE_HERMES_RESOLVER": "1",
                },
                clear=False,
            ), (
                mock.patch.object(
                    server,
                    "_oauth_discovery",
                    return_value={"token_endpoint": "https://auth.x.ai/oauth2/token"},
                )
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
            state = saved["providers"]["xai-oauth"]
            self.assertEqual(state["tokens"]["access_token"], "fresh")
            self.assertEqual(state["tokens"]["refresh_token"], "refresh2")
            self.assertEqual(
                state["discovery"]["token_endpoint"],
                "https://auth.x.ai/oauth2/token",
            )


class McpProtocolTests(unittest.TestCase):
    def test_mcp_tools_list_and_error_call(self):
        with mock.patch.object(server, "check_x_search_requirements", return_value=True):
            list_response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        self.assertEqual(list_response["result"]["tools"][0]["name"], "x_search")

        with mock.patch.object(server, "check_x_search_requirements", return_value=False):
            hidden_response = server._handle_mcp_request(
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
            )
        self.assertEqual(hidden_response["result"]["tools"], [])

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
