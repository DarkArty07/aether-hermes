"""Tests for explicit exhausted-pool 503 classification and configured fallback.

B295 / Issue #295:
Explicit exhausted-pool 503 ("no available Codex accounts") is classified with
should_fallback=True before generic overload, activating the configured fallback
without exhausting same-route retries. Generic 503 overload retains transient
backoff. Only configured candidates are used.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from run_agent import AIAgent
from agent.error_classifier import classify_api_error, FailoverReason


class MockAPIError(Exception):
    """Simulates an OpenAI SDK APIStatusError."""
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


class _RecordingClusterHandler(BaseHTTPRequestHandler):
    """Loopback HTTP server handling primary, secondary, and undeclared endpoints."""

    # Class-level state recorded across requests
    primary_requests: List[Dict[str, Any]] = []
    secondary_requests: List[Dict[str, Any]] = []
    undeclared_requests: List[Dict[str, Any]] = []

    primary_response_mode: str = "exhausted_pool"  # "exhausted_pool" | "generic_overload" | "context_overflow"
    secondary_response_mode: str = "success"        # "success" | "exhausted_pool"

    @classmethod
    def reset_state(cls):
        cls.primary_requests = []
        cls.secondary_requests = []
        cls.undeclared_requests = []
        cls.primary_response_mode = "exhausted_pool"
        cls.secondary_response_mode = "success"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req_data = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            req_data = {}

        record = {
            "path": self.path,
            "headers": dict(self.headers),
            "auth": self.headers.get("Authorization", ""),
            "body": req_data,
        }

        # Route Ollama /api/show or probe calls gracefully
        if "/api/show" in self.path or "/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"details": {"context_length": 128000}}')
            return

        if "/primary/" in self.path:
            type(self).primary_requests.append(record)
            if self.primary_response_mode == "exhausted_pool":
                self._send_exhausted_pool_503()
            elif self.primary_response_mode == "generic_overload":
                self._send_generic_overload_503()
            elif self.primary_response_mode == "context_overflow":
                self._send_context_overflow_503()
            else:
                self._send_success_stream("Primary ok")

        elif "/secondary/" in self.path:
            type(self).secondary_requests.append(record)
            if self.secondary_response_mode == "exhausted_pool":
                self._send_exhausted_pool_503()
            else:
                self._send_success_stream("Secondary success response")

        elif "/undeclared/" in self.path:
            type(self).undeclared_requests.append(record)
            self._send_exhausted_pool_503()

        else:
            self.send_response(404)
            self.end_headers()

    def _send_exhausted_pool_503(self):
        err_body = json.dumps({
            "error": {
                "message": "No available Codex accounts",
                "type": "server_error",
            }
        }).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err_body)))
        self.end_headers()
        self.wfile.write(err_body)

    def _send_generic_overload_503(self):
        err_body = json.dumps({
            "error": {
                "message": "Service Unavailable - Server is temporarily overloaded",
                "type": "server_error",
            }
        }).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err_body)))
        self.end_headers()
        self.wfile.write(err_body)

    def _send_context_overflow_503(self):
        err_body = json.dumps({
            "error": {
                "message": "context_length_exceeded: maximum context length is 128000 tokens",
                "code": "context_length_exceeded",
                "type": "invalid_request_error",
            }
        }).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(err_body)))
        self.end_headers()
        self.wfile.write(err_body)

    def _send_success_stream(self, text: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = [
            {"id": "m1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
            {"id": "m2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def loopback_server():
    """Start an in-process HTTP server on a random loopback port."""
    srv = HTTPServer(("127.0.0.1", 0), _RecordingClusterHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def reset_server_state():
    _RecordingClusterHandler.reset_state()


def _make_test_agent(port: int, fallback: bool = True) -> AIAgent:
    """Construct an AIAgent pointing to loopback primary and optional secondary."""
    fallback_model = [
        {
            "provider": "custom",
            "model": "secondary-model",
            "base_url": f"http://127.0.0.1:{port}/secondary/v1",
            "api_key": "secondary-secret-token",
        }
    ] if fallback else None

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            api_key="primary-secret-token",
            base_url=f"http://127.0.0.1:{port}/primary/v1",
            provider="custom",
            model="primary-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent._persist_session = MagicMock()
        agent._save_trajectory = MagicMock()
        agent._cleanup_task_resources = MagicMock()
        return agent


class TestExhaustedPoolFallbackCaller:
    """Caller-level execution tests verifying failover, auth separation, and bounded retries."""

    def test_exhausted_pool_real_http_failover_to_configured_secondary(self, loopback_server, monkeypatch):
        """Positive case: 503 exhausted-pool activates configured fallback without same-route retry burn."""
        port = loopback_server
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=True)

        with patch("agent.conversation_loop.time.sleep"):
            result = agent.run_conversation("Hello world")

        assert result.get("completed") is True
        assert result.get("final_response") == "Secondary success response"
        assert agent._fallback_activated is True
        assert agent.model == "secondary-model"

        # Verification of destination counts: primary contacted once, secondary once
        assert len(_RecordingClusterHandler.primary_requests) == 1, (
            f"Expected exactly 1 request to primary (no same-route retry exhaustion), "
            f"got {len(_RecordingClusterHandler.primary_requests)}"
        )
        assert len(_RecordingClusterHandler.secondary_requests) == 1
        assert len(_RecordingClusterHandler.undeclared_requests) == 0

        # Authentication separation
        assert _RecordingClusterHandler.primary_requests[0]["auth"] == "Bearer primary-secret-token"
        assert _RecordingClusterHandler.secondary_requests[0]["auth"] == "Bearer secondary-secret-token"

    def test_exhausted_pool_no_fallback_bounded_honest_failure(self, loopback_server, monkeypatch):
        """Negative case 1: When no secondary is configured, 503 exhausted-pool ends bounded immediately."""
        port = loopback_server
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=False)

        with patch("agent.conversation_loop.time.sleep"):
            result = agent.run_conversation("Hello world")

        assert result.get("failed") is True
        assert result.get("completed") is False
        # Must not exhaust retries on the dead pool
        assert len(_RecordingClusterHandler.primary_requests) == 1, (
            f"Expected bounded exit with 1 request, got {len(_RecordingClusterHandler.primary_requests)}"
        )
        assert len(_RecordingClusterHandler.secondary_requests) == 0

    def test_exhausted_pool_all_candidates_fail_bounded(self, loopback_server, monkeypatch):
        """Negative case 2: When both primary and secondary return exhausted-pool, stops cleanly."""
        port = loopback_server
        _RecordingClusterHandler.secondary_response_mode = "exhausted_pool"
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=True)

        with patch("agent.conversation_loop.time.sleep"):
            result = agent.run_conversation("Hello world")

        assert result.get("failed") is True
        assert len(_RecordingClusterHandler.primary_requests) == 1
        assert len(_RecordingClusterHandler.secondary_requests) == 1

    def test_exhausted_pool_does_not_replay_completed_tool_sentinel(self, loopback_server, monkeypatch):
        """Preservation: A completed benign tool execution is not repeated merely to change providers."""
        port = loopback_server
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=True)

        tool_executions = []

        def fake_handler(tc, **kwargs):
            tool_executions.append(tc.get("id"))
            return "tool-output-sentinel"

        # Pre-seed history with an already completed tool sentinel turn
        history = [
            {"role": "user", "content": "run benign sentinel"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_sentinel_123", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_sentinel_123", "content": "tool-output-sentinel"},
        ]

        with (
            patch("run_agent.handle_function_call", side_effect=fake_handler),
            patch("agent.conversation_loop.time.sleep"),
        ):
            result = agent.run_conversation("continue with result", conversation_history=history)

        assert result.get("completed") is True
        assert result.get("final_response") == "Secondary success response"
        # The already completed tool sentinel was never replayed/re-executed
        assert len(tool_executions) == 0, "Completed tool sentinel was replayed during fallback switch!"
        assert len(_RecordingClusterHandler.primary_requests) == 1
        assert len(_RecordingClusterHandler.secondary_requests) == 1

    def test_generic_503_overload_preserves_transient_backoff_and_retries(self, loopback_server, monkeypatch):
        """Preservation: Generic 503 overload continues through transient overload backoff / retries."""
        port = loopback_server
        _RecordingClusterHandler.primary_response_mode = "generic_overload"
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=True)

        sleep_calls = []
        with patch("agent.conversation_loop.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            result = agent.run_conversation("Hello world")

        assert result.get("completed") is True
        # Generic overload uses backoff and retries before falling back (at retry_count >= 2)
        assert len(_RecordingClusterHandler.primary_requests) >= 2, (
            f"Expected generic 503 to retry with backoff, got {len(_RecordingClusterHandler.primary_requests)} requests"
        )
        assert len(sleep_calls) > 0, "Expected backoff sleep calls for generic overload"

    def test_503_context_overflow_preserves_compression_and_not_exhausted_pool(self, loopback_server, monkeypatch):
        """Preservation: 503 context overflow routes to compression, not exhausted-pool fallback."""
        port = loopback_server
        _RecordingClusterHandler.primary_response_mode = "context_overflow"
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        agent = _make_test_agent(port, fallback=True)

        # 1. Assert classifier on the 503 overflow error shape directly
        err = MockAPIError(
            "context_length_exceeded: maximum context length is 128000 tokens",
            status_code=503,
            body={
                "error": {
                    "message": "context_length_exceeded: maximum context length is 128000 tokens",
                    "code": "context_length_exceeded",
                    "type": "invalid_request_error",
                }
            },
        )
        classified = classify_api_error(err)
        assert classified.status_code == 503
        assert classified.reason == FailoverReason.context_overflow
        assert classified.should_compress is True
        assert classified.should_fallback is False
        assert classified.retryable is True

        # 2. Assert caller behavior: routes into _compress_context, does not activate fallback
        compress_called = []

        def fake_compress(messages, system_message, **kwargs):
            compress_called.append(True)
            return messages, system_message

        with (
            patch.object(agent, "_compress_context", side_effect=fake_compress),
            patch("agent.conversation_loop.time.sleep"),
        ):
            result = agent.run_conversation("Hello world")

        # Context overflow handling occurred (compression attempted)
        assert len(compress_called) > 0, "Expected _compress_context to be called for context overflow"
        # Must NOT activate fallback or route to secondary
        assert getattr(agent, "_fallback_activated", False) is False
        assert len(_RecordingClusterHandler.secondary_requests) == 0
        assert len(_RecordingClusterHandler.primary_requests) >= 1
