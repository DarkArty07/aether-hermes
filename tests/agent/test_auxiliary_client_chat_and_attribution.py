"""Regression tests for auxiliary surface negotiation and fallback attribution."""

import asyncio
import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import openai
import pytest

import agent.auxiliary_client as aux
from agent.auxiliary_client import (
    CodexAuxiliaryClient,
    _AsyncCodexCompletionsAdapter,
    _CodexCompletionsAdapter,
    _call_fallback_candidate_sync,
    _fallback_destination_from_entry,
)


class _AuxiliaryGatewayHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(body)
        self.__class__.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "payload": payload,
        })

        if self.path.split("?", 1)[0].endswith("/responses"):
            if payload.get("model") == "chat-only-model":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({
                        "detail": (
                            "chat-only-model is served on the OpenCode Chat "
                            "Completions surface; call it via /v1/chat/completions"
                        )
                    }).encode("utf-8")
                )
                return
            if payload.get("model") == "primary-model":
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"synthetic primary rate limit"}}'
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b"event: response.output_item.done\n"
                b'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"responses-only-ok"}]}}\n\n'
                b"event: response.completed\n"
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{"total_tokens":5}}}\n\n'
            )
            return

        if self.path.split("?", 1)[0].endswith("/chat/completions"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({
                    "id": "chatcmpl-aux-test",
                    "object": "chat.completion",
                    "model": payload.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "chat-only-ok",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }).encode("utf-8")
            )
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        del format, args


@pytest.fixture
def auxiliary_server():
    _AuxiliaryGatewayHandler.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _AuxiliaryGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_chat_only_directive_retries_existing_chat_path(auxiliary_server):
    raw_client = openai.OpenAI(
        base_url=auxiliary_server, api_key="synth-destination-key"
    )
    adapter = _CodexCompletionsAdapter(raw_client, "chat-only-model")

    response = adapter.create(
        model="chat-only-model",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response.choices[0].message.content == "chat-only-ok"
    assert [
        request["path"].split("?", 1)[0]
        for request in _AuxiliaryGatewayHandler.requests
    ] == ["/v1/responses", "/v1/chat/completions"]


def test_async_chat_only_directive_uses_same_compatible_path(auxiliary_server):
    raw_client = openai.OpenAI(
        base_url=auxiliary_server, api_key="synth-destination-key"
    )
    sync_adapter = _CodexCompletionsAdapter(raw_client, "chat-only-model")
    async_adapter = _AsyncCodexCompletionsAdapter(sync_adapter)

    response = asyncio.run(async_adapter.create(
        model="chat-only-model",
        messages=[{"role": "user", "content": "hello async"}],
    ))

    assert response.choices[0].message.content == "chat-only-ok"
    assert [
        request["path"].split("?", 1)[0]
        for request in _AuxiliaryGatewayHandler.requests
    ] == ["/v1/responses", "/v1/chat/completions"]


def test_responses_native_model_keeps_responses_path_only(auxiliary_server):
    raw_client = openai.OpenAI(
        base_url=auxiliary_server, api_key="synth-destination-key"
    )
    adapter = _CodexCompletionsAdapter(raw_client, "responses-only-model")

    response = adapter.create(
        model="responses-only-model",
        messages=[{"role": "user", "content": "hello responses"}],
    )

    assert response.choices[0].message.content == "responses-only-ok"
    assert [
        request["path"].split("?", 1)[0]
        for request in _AuxiliaryGatewayHandler.requests
    ] == ["/v1/responses"]


def test_title_fallback_preserves_attribution_without_primary_auth(auxiliary_server):
    raw_client = aux._create_openai_client(
        base_url=auxiliary_server,
        api_key="synth-fallback-destination-key",
        default_query={"api-version": "synth-version"},
    )
    fallback_client = CodexAuxiliaryClient(raw_client, "responses-only-model")
    entry = {
        "provider": "named-router",
        "model": "responses-only-model",
        # The named-provider resolver can rebuild a client from a clean
        # provider URL. The configured entry remains the attribution source.
        "base_url": (
            f"{auxiliary_server}?aether_service=morfeo"
            "&aether_operation=title_generation"
        ),
        "api_mode": "codex_responses",
    }
    destination = _fallback_destination_from_entry(
        entry, fallback_client, "responses-only-model"
    )
    setattr(fallback_client, "_hermes_fallback_destination", destination)

    response = _call_fallback_candidate_sync(
        fallback_client,
        "responses-only-model",
        "fallback_chain[0](named-router)",
        task="title_generation",
        messages=[{"role": "user", "content": "make a title"}],
        temperature=0.3,
        max_tokens=64,
        tools=None,
        effective_timeout=30.0,
        effective_extra_body={},
        reasoning_config=None,
        extra_headers={
            "X-Aether-Trace": "synth-title-trace",
            "Authorization": "Bearer synth-primary-auth",
            "Proxy-Authorization": "synth-primary-proxy-auth",
            "Cookie": "synth-primary-cookie",
            "X-Api-Key": "synth-primary-api-key",
        },
    )

    assert response is not None
    assert response.choices[0].message.content == "responses-only-ok"
    assert len(_AuxiliaryGatewayHandler.requests) == 1
    request = _AuxiliaryGatewayHandler.requests[0]
    assert urlsplit(request["path"]).path == "/v1/responses"
    assert parse_qs(urlsplit(request["path"]).query) == {
        "api-version": ["synth-version"],
        "aether_service": ["morfeo"],
        "aether_operation": ["title_generation"],
    }
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers["authorization"] == "Bearer synth-fallback-destination-key"
    assert headers["x-aether-trace"] == "synth-title-trace"
    assert "proxy-authorization" not in headers
    assert "cookie" not in headers
    assert headers.get("x-api-key") != "synth-primary-api-key"


def test_genuine_title_generation_fallback_uses_entry_attribution(auxiliary_server):
    """Exercise call_llm's primary-rate-limit -> configured fallback path."""
    aux.shutdown_cached_clients()
    config = {
        "providers": {
            "named-router": {
                "name": "named-router",
                "base_url": auxiliary_server,
                "api_key": "synth-fallback-destination-key",
                "api_mode": "codex_responses",
            }
        },
        "auxiliary": {
            "title_generation": {
                "provider": "custom",
                "model": "primary-model",
                "base_url": auxiliary_server,
                "api_key": "synth-primary-key",
                "api_mode": "codex_responses",
                "fallback_chain": [
                    {
                        "provider": "named-router",
                        "model": "responses-only-model",
                        "base_url": (
                            f"{auxiliary_server}?aether_service=morfeo"
                            "&aether_operation=title_generation"
                        ),
                        "api_mode": "codex_responses",
                    }
                ],
            }
        },
    }
    try:
        with (
            patch("hermes_cli.config.load_config", return_value=config),
            patch("hermes_cli.config.load_config_readonly", return_value=config),
            patch("hermes_cli.runtime_provider.load_config", return_value=config),
            patch("agent.auxiliary_client._transient_retry_count", return_value=0),
        ):
            response = aux.call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "make a title"}],
                temperature=0.3,
                max_tokens=64,
                extra_headers={
                    "X-Aether-Trace": "synth-title-trace",
                    "Authorization": "Bearer synth-primary-auth",
                    "Cookie": "synth-primary-cookie",
                },
            )

        assert response.choices[0].message.content == "responses-only-ok"
        assert [
            urlsplit(request["path"]).path
            for request in _AuxiliaryGatewayHandler.requests
        ] == [
            "/v1/responses",
            "/v1/responses",
        ]
        assert parse_qs(
            urlsplit(_AuxiliaryGatewayHandler.requests[1]["path"]).query
        ) == {
            "aether_service": ["morfeo"],
            "aether_operation": ["title_generation"],
        }
        fallback_headers = {
            key.lower(): value
            for key, value in _AuxiliaryGatewayHandler.requests[1]["headers"].items()
        }
        assert (
            fallback_headers["authorization"] == "Bearer synth-fallback-destination-key"
        )
        assert fallback_headers["x-aether-trace"] == "synth-title-trace"
        assert "cookie" not in fallback_headers
        assert "synth-primary-auth" not in str(fallback_headers)
    finally:
        aux.shutdown_cached_clients()
