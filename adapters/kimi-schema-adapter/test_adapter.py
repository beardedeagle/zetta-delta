"""Regression proof for the exact native defect and the local transport boundary."""

import asyncio
import contextlib
import copy
import gzip
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientPayloadError, ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

import adapter

NATIVE_VALUE = {
    "anyOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "array", "items": True},
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}


def request_body(name="delta_settings", value=None):
    """Build a fixture containing the reconstructed native schema."""
    return {
        "model": "k3",
        "stream": True,
        "messages": [{"role": "user", "content": "Diagnostic prompt — no private code."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": copy.deepcopy(NATIVE_VALUE if value is None else value)
                        },
                    },
                },
            }
        ],
    }


class SchemaTests(unittest.TestCase):
    def test_native_array_boolean_is_replaced_without_changing_other_request_values(self):
        original = request_body()
        original["messages"].append(
            {
                "role": "tool",
                "tool_call_id": "one",
                "content": json.dumps({"values": [None, True, {}, [], 18446744073709551615]}),
            }
        )
        body = json.dumps(original, ensure_ascii=False).encode()
        corrected, count = adapter.normalize(body)
        expected = copy.deepcopy(original)
        expected["tools"][0]["function"]["parameters"]["properties"]["value"]["anyOf"][1] = {
            "type": "array",
            "items": {},
        }
        self.assertEqual(json.loads(corrected), expected)
        self.assertEqual(count, 1)
        self.assertEqual(
            original["tools"][0]["function"]["parameters"]["properties"]["value"], NATIVE_VALUE
        )

    def test_unrelated_tools_and_nested_settings_values_are_byte_preserved(self):
        fixtures = [request_body("other_tool"), {"messages": [{"content": NATIVE_VALUE}]}]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                body = json.dumps(fixture, indent=3).encode()
                self.assertEqual(adapter.normalize(body), (body, 0))

    def test_schema_correction_preserves_unrelated_raw_json_numbers(self):
        for literal in (b"9007199254740993.0", b"0.1000000000000000000000000001", b"1e400",
                        b"1e-400", b"-0", b"-0.0", b"18446744073709551615"):
            with self.subTest(literal=literal):
                body = (
                    b'{"tools":[{"type":"function","function":{"name":"delta_settings",'
                    b'"parameters":{"properties":{"value":{"anyOf":[{"type":"array","items":true}]}}}}},'
                    b'{"type":"function","function":{"name":"other_tool",'
                    b'"parameters":{"const":' + literal + b'}}}],'
                    b'"messages":[{"content":"9007199254740993.0, 1e400, -0, '
                    b'\\\"const\\\":1e-400, \\\\ and \\u0031"}]}'
                )
                corrected, count = adapter.normalize(body)
                self.assertEqual(count, 1)
                self.assertIn(b'"const":' + literal + b"}", corrected)
                self.assertEqual(json.loads(corrected)["messages"], json.loads(body)["messages"])
                self.assertEqual(adapter.normalize(corrected), (corrected, 0))

    def test_schema_correction_retains_deeply_nested_unrelated_values(self):
        depth = 700
        body = (json.dumps(request_body()).encode()[:-1] + b',"nested":' + b"[" * depth
                + b"9007199254740993.0" + b"]" * depth + b"}")
        corrected, count = adapter.normalize(body)
        self.assertEqual(count, 1)
        self.assertIn(b'"nested":' + b"[" * depth + b"9007199254740993.0" + b"]" * depth, corrected)
        self.assertEqual(adapter.normalize(corrected), (corrected, 0))

    def test_changed_or_already_corrected_native_shapes_are_byte_preserved(self):
        for branch in (
            {"type": "array", "items": {}},
            {"type": "array", "items": False},
            {"type": "array", "items": True, "minItems": 1},
            {"type": "array", "items": 1},
        ):
            fixture = request_body(value={"anyOf": [branch]})
            body = json.dumps(fixture, indent=2).encode()
            self.assertEqual(adapter.normalize(body), (body, 0))

    def test_malformed_json_is_rejected_before_any_rewrite(self):
        for body in (b"{", b'{"tools":NaN}', b'{"model":"a","model":"b"}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                adapter.normalize(body)

    def test_unexpected_parameter_types_do_not_crash_or_trigger_rewrites(self):
        for fixture in (
            None,
            [],
            {"tools": {}},
            {"tools": [None, {"function": False}]},
            request_body(value={"anyOf": None}),
        ):
            body = json.dumps(fixture).encode()
            self.assertEqual(adapter.normalize(body), (body, 0))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.backend = self.default_backend
        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", self.receive)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.target = patch.object(
            adapter, "UPSTREAM", str(self.upstream.make_url("/coding/v1/chat/completions"))
        )
        self.target.start()
        self.models_target = patch.object(
            adapter, "MODELS_UPSTREAM", str(self.upstream.make_url("/coding/v1/models"))
        )
        self.models_target.start()
        self.capability = "private_test_capability_0123456789abcdef"
        self.client = TestClient(
            TestServer(adapter.create_app(self.capability), handler_cancellation=True)
        )
        await self.client.start_server()
        self.path = f"/{self.capability}/coding/v1/chat/completions"
        self.headers = {
            "Authorization": "Bearer synthetic-test-key",
            "Content-Type": "application/json",
        }

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.target.stop()
        self.models_target.stop()

    async def receive(self, request):
        self.received.append((request.path, await request.read(), dict(request.headers)))
        return await self.backend(request)

    async def default_backend(self, request):
        return web.Response(body=b'{"ok":true}', content_type="application/json")

    async def test_model_discovery_forwards_get_auth_and_provider_response(self):
        payload = b'{"object":"list","data":[{"id":"k3","object":"model"}]}'

        async def models(request):
            self.assertEqual(request.method, "GET")
            return web.Response(body=payload, content_type="application/json")

        self.backend = models
        response = await self.client.get(
            f"/{self.capability}/coding/v1/models",
            headers={"Authorization": self.headers["Authorization"],
                     "User-Agent": "KimiCLI/native-fixture"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), payload)
        path, body, headers = self.received[0]
        self.assertEqual(path, "/coding/v1/models")
        self.assertEqual(body, b"")
        self.assertEqual(headers["Authorization"], self.headers["Authorization"])
        self.assertEqual(headers["User-Agent"], "KimiCLI/native-fixture")

    async def test_model_discovery_preserves_access_checks_and_refuses_redirects(self):
        path = f"/{self.capability}/coding/v1/models"
        cases = [
            (path, {}, 401),
            (path, {**self.headers, "Origin": "https://untrusted.example"}, 403),
            (path, {**self.headers, "Host": "untrusted.example"}, 403),
            ("/wrong/coding/v1/models", self.headers, 404),
            (path + "?target=https://untrusted.example", self.headers, 400),
        ]
        for target, headers, status in cases:
            response = await self.client.get(target, headers=headers)
            self.assertEqual(response.status, status)
            await response.read()
        self.assertEqual(self.received, [])

        async def redirect(request):
            return web.Response(status=307, headers={"Location": "/elsewhere"})

        self.backend = redirect
        response = await self.client.get(path, headers=self.headers, allow_redirects=False)
        self.assertEqual(response.status, 307)
        await response.read()
        self.assertEqual(len(self.received), 1)

    async def test_full_request_rewrites_only_schema_and_preserves_native_auth_headers(self):
        original = request_body()
        headers = {
            **self.headers,
            "User-Agent": "KimiCLI/native-fixture",
            "X-Native-Header": "retain",
            "Connection": "X-Hop-Header",
            "X-Hop-Header": "discard",
        }
        response = await self.client.post(self.path, json=original, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'{"ok":true}')
        path, body, outgoing = self.received[0]
        self.assertEqual(path, "/coding/v1/chat/completions")
        expected = copy.deepcopy(original)
        expected["tools"][0]["function"]["parameters"]["properties"]["value"]["anyOf"][1][
            "items"
        ] = {}
        self.assertEqual(json.loads(body), expected)
        self.assertEqual(outgoing["Authorization"], "Bearer synthetic-test-key")
        self.assertEqual(outgoing["User-Agent"], "KimiCLI/native-fixture")
        self.assertEqual(outgoing["X-Native-Header"], "retain")
        self.assertNotIn("X-Hop-Header", outgoing)
        self.assertNotIn(self.capability, path)
        self.assertEqual(int(outgoing["Content-Length"]), len(body))

    async def test_first_sse_event_is_delivered_before_the_upstream_finishes(self):
        release = asyncio.Event()

        async def streamed(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"first":true}\n\n')
            await release.wait()
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        self.backend = streamed
        try:
            response = await asyncio.wait_for(
                self.client.post(self.path, json=request_body(), headers=self.headers), 2
            )
            first = await asyncio.wait_for(response.content.readexactly(22), 2)
            self.assertEqual(first, b'data: {"first":true}\n\n')
            release.set()
            self.assertEqual(await response.read(), b"data: [DONE]\n\n")
        finally:
            release.set()

    async def test_provider_errors_and_compressed_bytes_are_preserved(self):
        encoded = gzip.compress(b'{"error":{"message":"synthetic upstream error"}}')

        async def error(request):
            return web.Response(
                status=429,
                body=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "Retry-After": "17",
                    "X-Request-ID": "fixture-id",
                },
            )

        self.backend = error
        async with (
            ClientSession(auto_decompress=False) as client,
            client.post(
                self.client.make_url(self.path), json=request_body(), headers=self.headers
            ) as response,
        ):
            self.assertEqual(response.status, 429)
            self.assertEqual(await response.read(), encoded)
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(response.headers["Retry-After"], "17")
            self.assertEqual(response.headers["X-Request-ID"], "fixture-id")

    async def test_browser_wrong_host_wrong_route_and_bad_json_never_reach_upstream(self):
        cases = [
            (self.path, {**self.headers, "Origin": "https://untrusted.example"}, b"{}", 403),
            (self.path, {**self.headers, "Host": "untrusted.example"}, b"{}", 403),
            (self.path, {"Content-Type": "application/json"}, b"{}", 401),
            ("/wrong/coding/v1/chat/completions", self.headers, b"{}", 404),
            (self.path + "?target=https://untrusted.example", self.headers, b"{}", 400),
            (self.path, self.headers, b"{", 400),
        ]
        for path, headers, body, status in cases:
            response = await self.client.post(path, data=body, headers=headers)
            self.assertEqual(response.status, status)
            await response.read()
        self.assertEqual(self.received, [])

    async def test_redirect_does_not_forward_credentials_to_a_second_endpoint(self):
        async def redirect(request):
            return web.Response(
                status=307,
                headers={"Location": str(self.upstream.make_url("/elsewhere"))},
                body=b"redirect fixture",
            )

        self.backend = redirect
        response = await self.client.post(
            self.path, json=request_body(), headers=self.headers, allow_redirects=False
        )
        self.assertEqual(response.status, 307)
        self.assertEqual(await response.read(), b"redirect fixture")
        self.assertEqual(len(self.received), 1)

    async def test_logs_exclude_capability_authorization_and_request_content(self):
        output = io.StringIO()
        fixture = request_body()
        fixture["messages"][0]["content"] = "synthetic-private-prompt-marker"

        async def private_response(request):
            return web.Response(body=b"synthetic-private-response-marker",
                                headers={"X-Private-Header": "synthetic-private-header-marker"})

        self.backend = private_response
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = await self.client.post(self.path, json=fixture, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"synthetic-private-response-marker")
        for secret in (self.capability, "synthetic-test-key", "synthetic-private-prompt-marker",
                       "synthetic-private-response-marker", "synthetic-private-header-marker"):
            self.assertNotIn(secret, output.getvalue())

    async def test_caller_cancellation_identifies_pending_response_headers(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        output = io.StringIO()

        async def delayed_headers(request):
            entered.set()
            await release.wait()
            return web.Response(body=b"late response")

        self.backend = delayed_headers
        try:
            with contextlib.redirect_stdout(output):
                pending = asyncio.create_task(self.client.post(self.path, json=request_body(),
                                                              headers=self.headers))
                await asyncio.wait_for(entered.wait(), 2)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                async with asyncio.timeout(2):
                    while not any(json.loads(line)["event"] == "cancelled"
                                  for line in output.getvalue().splitlines()):
                        await asyncio.sleep(0)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[-1]["phase"], "response_headers")
            self.assertNotIn("response_headers", [record["event"] for record in records])
            self.assertNotIn("complete", [record["event"] for record in records])
        finally:
            release.set()

    async def test_closed_diagnostic_sink_does_not_change_the_response(self):
        with patch("builtins.print", side_effect=BrokenPipeError):
            response = await self.client.post(self.path, json=request_body(), headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'{"ok":true}')

    async def test_timing_records_correlate_requests_and_distinguish_reused_connections(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for _ in range(2):
                response = await self.client.post(self.path, json=request_body(), headers=self.headers)
                self.assertEqual(await response.read(), b'{"ok":true}')
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        grouped = {}
        for record in records:
            self.assertEqual(datetime.fromisoformat(record["timestamp"]).utcoffset().total_seconds(), 0)
            self.assertRegex(record["request_id"], r"^[0-9a-f]{32}$")
            self.assertGreaterEqual(record["elapsed_ms"], 0)
            self.assertGreaterEqual(record["phase_ms"], 0)
            grouped.setdefault(record["request_id"], []).append(record)
        self.assertEqual(len(grouped), 2)
        first, second = grouped.values()
        for events in grouped.values():
            names = [record["event"] for record in events]
            for name in ("request_started", "request_headers_sent", "response_headers", "first_chunk", "complete"):
                self.assertIn(name, names)
            self.assertEqual(names[-1], "complete")
            self.assertEqual(events[-1]["status"], 200)
            self.assertEqual([record["elapsed_ms"] for record in events],
                             sorted(record["elapsed_ms"] for record in events))
        self.assertIn("connected", [record["event"] for record in first])
        self.assertIn("connection_reused", [record["event"] for record in second])

    async def test_connection_failure_logs_only_error_type_and_connect_phase(self):
        output = io.StringIO()
        await self.upstream.close()
        with contextlib.redirect_stdout(output):
            response = await self.client.post(self.path, json=request_body(), headers=self.headers)
            self.assertEqual(response.status, 502)
            await response.read()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]["event"], "error")
        self.assertEqual(records[-1]["phase"], "connect")
        self.assertEqual(records[-1]["error_type"], "ClientConnectorError")
        self.assertNotIn("complete", [record["event"] for record in records])
        for secret in (self.capability, "synthetic-test-key", adapter.UPSTREAM,
                       request_body()["messages"][0]["content"]):
            self.assertNotIn(secret, output.getvalue())

    async def test_interrupted_stream_is_an_error_instead_of_a_successful_truncated_response(self):
        async def interrupted(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: partial\n\n")
            request.transport.close()
            return response

        self.backend = interrupted
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            response = await self.client.post(self.path, json=request_body(), headers=self.headers)
            with self.assertRaises(ClientPayloadError):
                await response.read()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[-1]["event"], "error")
        self.assertEqual(records[-1]["phase"], "response_stream")
        self.assertNotIn("complete", [record["event"] for record in records])

    async def test_untrusted_upstream_certificate_is_rejected_without_leaking_request_data(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "certificate.pem"
            key = Path(directory) / "key.pem"
            await asyncio.to_thread(
                subprocess.run,
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                check=True,
                capture_output=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            upstream = web.Application()
            upstream.router.add_post("/coding/v1/chat/completions", self.receive)
            server = TestServer(upstream)
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()
            handshake_errors = []
            loop.set_exception_handler(
                lambda _loop, event: handshake_errors.append(event.get("exception"))
            )
            await server.start_server(ssl=context)
            try:
                with patch.object(
                    adapter, "UPSTREAM", str(server.make_url("/coding/v1/chat/completions"))
                ):
                    response = await self.client.post(
                        self.path, json=request_body(), headers=self.headers
                    )
                    self.assertEqual(response.status, 502)
                    self.assertEqual(
                        await response.json(), {"error": {"message": "Kimi connection failed"}}
                    )
                self.assertEqual(self.received, [])
            finally:
                await server.close()
                loop.set_exception_handler(previous_handler)
            self.assertTrue(
                all(isinstance(error, ConnectionResetError) for error in handshake_errors)
            )


class CapabilityTests(unittest.TestCase):
    def test_created_capability_is_private_and_stable_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            value = adapter.load_capability(path)
            self.assertGreaterEqual(len(value), 32)
            self.assertEqual(adapter.load_capability(path), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())

    def test_shared_or_symlinked_capability_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability"
            target = Path(directory) / "target"
            target.write_text("synthetic_test_capability_0123456789abcdef")
            path.symlink_to(target)
            with self.assertRaises(OSError):
                adapter.load_capability(path)
            path.unlink()
            path.write_text("synthetic_test_capability_0123456789abcdef")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                adapter.load_capability(path)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_binds_only_loopback_writes_private_endpoint_and_cleans_up_on_termination(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                adapter.__file__,
                "--port",
                "0",
                "--state",
                str(state),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 3)
                self.assertIn(b"127.0.0.1:", line)
                endpoint = state / "endpoint.json"
                metadata = json.loads(endpoint.read_text())
                self.assertEqual(endpoint.stat().st_mode & 0o777, 0o600)
                self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                self.assertEqual(metadata["pid"], process.pid)
                self.assertTrue(metadata["base_url"].startswith("http://127.0.0.1:"))
                self.assertNotIn((state / "capability").read_bytes(), line)
                async with (
                    ClientSession() as client,
                    client.post(metadata["base_url"] + "/chat/completions", json={}) as response,
                ):
                    self.assertEqual(response.status, 401)
                process.terminate()
                await asyncio.wait_for(process.wait(), 3)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(endpoint.exists())
                self.assertEqual(await process.stderr.read(), b"")
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    unittest.main()
