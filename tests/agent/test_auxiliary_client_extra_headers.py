"""Tests for B301: request-scoped extra_headers preservation and auth separation.

Verifies that:
- Request-scoped extra_headers reach the Codex Responses adapter HTTP endpoint
  in sync and async streaming modes.
- Configured fallback carries a fresh copy of safe request-scoped metadata
  without forwarding primary Authorization, Cookies, or provider auth.
- Destination authentication is resolved by the destination client with its
  own credentials.
- Input mappings and client defaults are not mutated.
- Subsequent independent requests have no stale attribution headers.
"""

import asyncio
import http.server
import json
import os
import threading
from unittest.mock import patch

import openai
import pytest

import agent.auxiliary_client as aux
from agent.auxiliary_client import (
    _AsyncCodexCompletionsAdapter,
    _CodexCompletionsAdapter,
)


class _ResponsesSSEHandler(http.server.BaseHTTPRequestHandler):
    received_requests = []

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        self.__class__.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # SSE stream emitting minimal done and completed frames
        self.wfile.write(
            b"event: response.output_item.done\n"
            b'data: {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "responses ok"}]}}\n\n'
        )
        self.wfile.write(
            b"event: response.completed\n"
            b'data: {"type": "response.completed", "response": {"status": "completed", "usage": {"total_tokens": 5}}}\n\n'
        )

    def log_message(self, format, *args):
        pass


@pytest.fixture
def responses_server():
    _ResponsesSSEHandler.received_requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _ResponsesSSEHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/v1"
    try:
        yield {"server": server, "base_url": base_url, "requests": _ResponsesSSEHandler.received_requests}
    finally:
        server.shutdown()
        aux.shutdown_cached_clients()


class TestCodexResponsesExtraHeaders:
    """B301: extra_headers reach the Responses streaming adapter."""

    def test_sync_codex_adapter_forwards_extra_headers(self, responses_server):
        base_url = responses_server["base_url"]
        requests = responses_server["requests"]

        raw_client = openai.OpenAI(base_url=base_url, api_key="synth-codex-key")
        adapter = _CodexCompletionsAdapter(raw_client, "gpt-5-codex")

        input_headers = {"x-initiator": "user", "x-request-id": "req-sync-100"}
        headers_copy = dict(input_headers)

        resp = adapter.create(
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers=input_headers,
        )
        assert resp.choices[0].message.content == "responses ok"

        # Immutability: input mapping was not mutated
        assert input_headers == headers_copy

        assert len(requests) == 1
        req_headers = requests[0]["headers"]
        # Case-insensitive check
        lower_headers = {k.lower(): v for k, v in req_headers.items()}
        assert lower_headers.get("x-initiator") == "user"
        assert lower_headers.get("x-request-id") == "req-sync-100"

    def test_async_codex_adapter_forwards_extra_headers(self, responses_server):
        base_url = responses_server["base_url"]
        requests = responses_server["requests"]

        raw_client = openai.OpenAI(base_url=base_url, api_key="synth-codex-key")
        sync_adapter = _CodexCompletionsAdapter(raw_client, "gpt-5-codex")
        async_adapter = _AsyncCodexCompletionsAdapter(sync_adapter)

        input_headers = {"x-initiator": "user", "x-correlation-id": "corr-async-200"}

        resp = asyncio.run(async_adapter.create(
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers=input_headers,
        ))
        assert resp.choices[0].message.content == "responses ok"

        assert len(requests) == 1
        lower_headers = {k.lower(): v for k, v in requests[0]["headers"].items()}
        assert lower_headers.get("x-initiator") == "user"
        assert lower_headers.get("x-correlation-id") == "corr-async-200"

    def test_subsequent_request_clean_no_stale_attribution(self, responses_server):
        base_url = responses_server["base_url"]
        requests = responses_server["requests"]

        raw_client = openai.OpenAI(base_url=base_url, api_key="synth-codex-key")
        adapter = _CodexCompletionsAdapter(raw_client, "gpt-5-codex")

        # First request WITH extra_headers
        adapter.create(
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "first"}],
            extra_headers={"x-initiator": "user"},
        )

        # Second independent request WITHOUT extra_headers
        adapter.create(
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "second"}],
        )

        assert len(requests) == 2
        second_headers = {k.lower(): v for k, v in requests[1]["headers"].items()}
        assert "x-initiator" not in second_headers


class _FallbackJsonHandler(http.server.BaseHTTPRequestHandler):
    received_requests = []

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        self.__class__.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response_payload = {
            "id": "chatcmpl-fb",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "fallback success"},
                    "finish_reason": "stop",
                }
            ],
        }
        self.wfile.write(json.dumps(response_payload).encode("utf-8"))

    def log_message(self, format, *args):
        pass


@pytest.fixture
def fallback_server():
    _FallbackJsonHandler.received_requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _FallbackJsonHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/v1"
    try:
        yield {"server": server, "base_url": base_url, "requests": _FallbackJsonHandler.received_requests}
    finally:
        server.shutdown()
        aux.shutdown_cached_clients()


class TestFallbackExtraHeadersAndAuthSeparation:
    """B301: Safe extra headers forwarded to fallback; primary auth excluded."""

    def test_sync_fallback_forwards_safe_headers_and_distinct_auth(self, fallback_server):
        fb_url = fallback_server["base_url"]
        fb_requests = fallback_server["requests"]

        cfg = {
            "providers": {
                "fb-provider": {
                    "name": "fb-provider",
                    "base_url": fb_url,
                    "key_env": "FALLBACK_KEY",
                    "api_mode": "chat_completions",
                    "default_model": "fb-model",
                }
            },
            "auxiliary": {
                "test_task": {
                    "fallback_chain": [
                        {"provider": "fb-provider", "model": "fb-model"}
                    ]
                }
            },
        }

        user_headers = {
            "x-initiator": "user",
            "x-request-id": "req-fb-sync-1",
            "Authorization": "Bearer primary-secret-token",
            "Cookie": "session=leaked-cookie",
            "x-api-key": "primary-api-key-secret",
        }
        headers_snapshot = dict(user_headers)

        aux.shutdown_cached_clients()
        with patch.dict(os.environ, {
            "PRIMARY_KEY": "primary-key-123",
            "FALLBACK_KEY": "synth-fallback-key-999",
        }):
            with patch("hermes_cli.config.load_config", return_value=cfg), \
                 patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
                 patch("hermes_cli.runtime_provider.load_config", return_value=cfg), \
                 patch("agent.auxiliary_client._transient_retry_count", return_value=0):
                resp = aux.call_llm(
                    task="test_task",
                    provider="custom",
                    model="pri-model",
                    base_url="http://127.0.0.1:1/v1",  # fails fast
                    api_key="primary-key-123",
                    api_mode="chat_completions",
                    messages=[{"role": "user", "content": "hello"}],
                    extra_headers=user_headers,
                )
                assert resp.choices[0].message.content == "fallback success"

        # User's input mapping was not mutated
        assert user_headers == headers_snapshot

        assert len(fb_requests) == 1
        req_hdrs = {k.lower(): v for k, v in fb_requests[0]["headers"].items()}

        # Safe request metadata forwarded
        assert req_hdrs.get("x-initiator") == "user"
        assert req_hdrs.get("x-request-id") == "req-fb-sync-1"

        # Auth separation: destination receives its own credential, never primary auth/cookie
        assert req_hdrs.get("authorization") == "Bearer synth-fallback-key-999"
        assert "primary-secret-token" not in req_hdrs.get("authorization", "")
        assert "cookie" not in req_hdrs
        assert req_hdrs.get("x-api-key") != "primary-api-key-secret"

    def test_async_fallback_forwards_safe_headers_and_distinct_auth(self, fallback_server):
        fb_url = fallback_server["base_url"]
        fb_requests = fallback_server["requests"]

        cfg = {
            "providers": {
                "fb-provider": {
                    "name": "fb-provider",
                    "base_url": fb_url,
                    "key_env": "FALLBACK_KEY",
                    "api_mode": "chat_completions",
                    "default_model": "fb-model",
                }
            },
            "auxiliary": {
                "test_task": {
                    "fallback_chain": [
                        {"provider": "fb-provider", "model": "fb-model"}
                    ]
                }
            },
        }

        user_headers = {
            "x-initiator": "user",
            "x-request-id": "req-fb-async-2",
            "Authorization": "Bearer primary-secret-token",
            "Cookie": "session=leaked-cookie",
        }

        aux.shutdown_cached_clients()
        with patch.dict(os.environ, {
            "PRIMARY_KEY": "primary-key-123",
            "FALLBACK_KEY": "synth-fallback-key-999",
        }):
            with patch("hermes_cli.config.load_config", return_value=cfg), \
                 patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
                 patch("hermes_cli.runtime_provider.load_config", return_value=cfg), \
                 patch("agent.auxiliary_client._transient_retry_count", return_value=0):
                resp = asyncio.run(aux.async_call_llm(
                    task="test_task",
                    provider="custom",
                    model="pri-model",
                    base_url="http://127.0.0.1:1/v1",  # fails fast
                    api_key="primary-key-123",
                    messages=[{"role": "user", "content": "hello async"}],
                    extra_headers=user_headers,
                ))
                assert resp.choices[0].message.content == "fallback success"

        assert len(fb_requests) == 1
        req_hdrs = {k.lower(): v for k, v in fb_requests[0]["headers"].items()}

        # Safe metadata forwarded
        assert req_hdrs.get("x-initiator") == "user"
        assert req_hdrs.get("x-request-id") == "req-fb-async-2"

        # Auth separation
        assert req_hdrs.get("authorization") == "Bearer synth-fallback-key-999"
        assert "primary-secret-token" not in req_hdrs.get("authorization", "")
        assert "cookie" not in req_hdrs
