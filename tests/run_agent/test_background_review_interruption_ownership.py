"""Event-controlled qualification tests for background review interruption ownership (B294 / #294).

Proves cancellation ownership with instance/request assertions and Event/Barrier synchronization:
1. Agent, client, and thread ownership separation between parent and review.
2. New foreground turn while review is in a blocked HTTP call cancels the review
   without aborting the foreground agent or corrupting its response.
3. Review abort/cancellation does not leak to parent flags or active requests.
4. Intentional /stop propagates from parent to active review child.
5. Review cleanup is identity-qualified (ABA-safe against successor reviews).
6. Pre-admission cancellation prevents outbound HTTP requests.
7. Cache prefix parity and session isolation invariants are maintained.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Ensure repo root is on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from run_agent import AIAgent
from agent.background_review import (
    spawn_background_review_thread,
)


class _EventControlledHttpHandler(http.server.BaseHTTPRequestHandler):
    """Local HTTP handler with deterministic event/barrier synchronization for review qualification."""

    # Class-level state reset per test
    chat_requests_received: List[Dict[str, Any]] = []
    review_request_started: threading.Event = threading.Event()
    allow_review_response: threading.Event = threading.Event()
    review_request_aborted: threading.Event = threading.Event()
    foreground_request_started: threading.Event = threading.Event()

    @classmethod
    def reset_controls(cls) -> None:
        cls.chat_requests_received = []
        cls.review_request_started = threading.Event()
        cls.allow_review_response = threading.Event()
        cls.review_request_aborted = threading.Event()
        cls.foreground_request_started = threading.Event()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8")

        # Non-chat endpoints (e.g. Ollama context length probing /api/show)
        if not self.path.endswith("/chat/completions"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return

        req = json.loads(raw_body)
        self.chat_requests_received.append(req)

        messages = req.get("messages", [])
        is_review = any(
            "memory" in str(m.get("content", "")).lower()
            or "skill" in str(m.get("content", "")).lower()
            for m in messages
        )

        if is_review:
            self.review_request_started.set()
            # Wait for either test signal to allow response or client abort
            if not self.allow_review_response.wait(timeout=2.0):
                # Review timed out or was cancelled by client aborting connection
                self.review_request_aborted.set()
                return

            # If allowed to reply normally:
            self._send_sse_response("Review completed: no skills to save.")
        else:
            self.foreground_request_started.set()
            self._send_sse_response("Foreground turn response: success.")

    def do_GET(self) -> None:  # noqa: N802
        # Models probing
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def _send_sse_response(self, text: str) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.review_request_aborted.set()

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        # Suppress standard HTTP server stderr logging
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded HTTP server so concurrent in-flight requests don't block the listen loop."""
    daemon_threads = True


@pytest.fixture()
def mock_server():
    _EventControlledHttpHandler.reset_controls()
    server = ThreadedHTTPServer(("127.0.0.1", 0), _EventControlledHttpHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="mock-http-server")
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1", _EventControlledHttpHandler
    finally:
        server.shutdown()
        server.server_close()


def _build_test_agent(base_url: str, session_id: str = "sess-ownership-test") -> AIAgent:
    agent = AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider="openai-compat",
        model="test-model",
        max_iterations=2,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
    )
    agent.session_id = session_id
    agent._cached_system_prompt = "CACHED-SYSTEM-PROMPT-TEST-BYTES"
    agent._background_review_agent = None
    agent._background_review_lock = threading.Lock()
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    return agent


def test_distinct_agent_client_and_thread_ownership(mock_server):
    """Assert distinct instance, HTTP client, and thread identity between parent and review."""
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)

    target, _prompt = spawn_background_review_thread(
        parent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )

    review_thread_started = threading.Event()
    observed_thread_ids = []

    def _wrapped_target():
        review_thread_started.set()
        observed_thread_ids.append(threading.get_ident())
        target()

    t = threading.Thread(target=_wrapped_target, daemon=True, name="bg-review-test")
    t.start()

    assert review_thread_started.wait(timeout=2.0)
    assert handler.review_request_started.wait(timeout=3.0)

    # Review agent is now registered on parent
    review_agent = parent._background_review_agent
    assert review_agent is not None
    assert review_agent is not parent, "Review agent must be a distinct AIAgent instance"
    assert getattr(review_agent, "provider", None) == parent.provider

    # Distinct HTTP clients
    parent_client = getattr(parent, "_client", None)
    review_client = getattr(review_agent, "_client", None)
    if parent_client is not None and review_client is not None:
        assert parent_client is not review_client, "Review must own its own client instance"

    # Distinct execution thread
    parent_tid = threading.get_ident()
    review_tid = observed_thread_ids[0]
    assert review_tid != parent_tid, "Review must execute on its own thread"

    # Allow review to finish
    handler.allow_review_response.set()
    t.join(timeout=3.0)


def test_new_foreground_turn_cancels_in_flight_review_without_aborting_foreground(mock_server):
    """Interleaving: New foreground turn while review is blocked in HTTP.

    Asserts:
    - In-flight review is cancelled (aborted).
    - Parent agent's foreground turn executes and completes with intact response.
    - Parent's _interrupt_requested remains False (no review-to-parent abort).
    """
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)

    # Initial foreground turn
    res1 = parent.run_conversation("User turn 1")
    assert res1.get("completed") is True
    assert "Foreground turn response" in res1.get("final_response", "")

    # Spawn background review
    target, _prompt = spawn_background_review_thread(
        parent,
        messages_snapshot=list(parent._session_messages),
        review_memory=True,
    )
    t = threading.Thread(target=target, daemon=True, name="bg-review-in-flight")
    t.start()

    # Wait until review is blocked in HTTP
    assert handler.review_request_started.wait(timeout=3.0)
    review_agent = parent._background_review_agent
    assert review_agent is not None
    assert review_agent is not parent

    # Spy on review_agent.interrupt to record cancellation delivery
    original_interrupt = review_agent.interrupt
    interrupt_calls = []

    def _spy_interrupt(*args: Any, **kwargs: Any) -> None:
        interrupt_calls.append((args, kwargs))
        original_interrupt(*args, **kwargs)

    review_agent.interrupt = _spy_interrupt

    # A new foreground turn arrives while review is blocked
    # conversation_loop.run_conversation executes the cancellation at line 1687
    res2 = parent.run_conversation("User turn 2 (superseding)")

    # Assertions on foreground completion
    assert res2.get("completed") is True
    assert "Foreground turn response: success" in res2.get("final_response", "")
    assert parent._interrupt_requested is False, "Parent foreground must not be marked interrupted"

    # Assertions on review cancellation
    assert len(interrupt_calls) > 0, "Review agent must have received interrupt call"
    assert "superseded" in str(interrupt_calls[0]), "Interrupt reason must cite superseding live turn"

    # Let thread clean up
    handler.allow_review_response.set()
    t.join(timeout=3.0)

    # Both tracking slots on parent cleared
    assert parent._background_review_agent is None
    assert review_agent not in parent._active_children


def test_review_abort_does_not_leak_to_parent_flags_or_active_request(mock_server):
    """Assert cancellation/abort of review agent never triggers parent's request abort or interrupt."""
    base_url, _handler = mock_server
    parent = _build_test_agent(base_url)

    # Setup spy on parent's active request abort
    parent_abort_spy = MagicMock()
    parent._active_request_abort = parent_abort_spy

    # Construct review agent stub as registered child
    review = _build_test_agent(base_url, session_id=parent.session_id)
    parent._background_review_agent = review
    parent._active_children.append(review)

    # Directly interrupt the review as conversation_loop does on superseding
    review.interrupt("superseded by new turn")

    assert review._interrupt_requested is True
    assert parent._interrupt_requested is False, "Parent interrupt_requested must remain False"
    parent_abort_spy.assert_not_called()


def test_intentional_stop_propagates_to_review_agent(mock_server):
    """Interleaving: Intentional /stop propagation.

    When user calls parent.interrupt(), the interrupt must fan out to review in _active_children.
    """
    base_url, _handler = mock_server
    parent = _build_test_agent(base_url)

    review = _build_test_agent(base_url, session_id=parent.session_id)
    parent._background_review_agent = review
    with parent._active_children_lock:
        parent._active_children.append(review)

    # Intentional /stop on parent
    parent.interrupt("user requested /stop", hard_cancel=True)

    assert parent._interrupt_requested is True
    assert review._interrupt_requested is True, "Intentional /stop must propagate to review child"


def test_identity_qualified_cleanup_preserves_successor_review(mock_server):
    """Interleaving: Review completion racing the next review.

    Asserts identity-qualified cleanup does not clear a successor review reference:
    drives a real predecessor review through _run_review_in_thread, installs a
    successor review while the predecessor is in-flight in HTTP, then allows the
    predecessor to finish. Product _unregister_review_agent must keep the successor
    in _background_review_agent and _active_children.
    """
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)

    # Spawn predecessor review via real spawn_background_review_thread / _run_review_in_thread
    target_1, _ = spawn_background_review_thread(
        parent,
        messages_snapshot=[{"role": "user", "content": "turn 1"}],
        review_memory=True,
    )
    t1 = threading.Thread(target=target_1, daemon=True, name="predecessor-review")
    t1.start()

    # Wait until predecessor is in-flight blocked in HTTP
    assert handler.review_request_started.wait(timeout=3.0)
    predecessor_agent = parent._background_review_agent
    assert predecessor_agent is not None

    # Successor review occupies _background_review_agent and _active_children
    successor_agent = _build_test_agent(base_url, session_id=parent.session_id)
    with parent._background_review_lock:
        parent._background_review_agent = successor_agent
    with parent._active_children_lock:
        parent._active_children.append(successor_agent)

    # Now allow predecessor to finish its HTTP call and run product cleanup (_unregister_review_agent)
    handler.allow_review_response.set()
    t1.join(timeout=3.0)

    # Product unregister was executed by _run_review_in_thread.
    # Verify successor is preserved and predecessor was removed from active_children
    assert parent._background_review_agent is successor_agent, "Successor review must not be cleared by predecessor"
    assert successor_agent in parent._active_children, "Successor must remain in active_children"
    assert predecessor_agent not in parent._active_children, "Predecessor must be removed from active_children"


def test_pre_admission_cancellation_prevents_request(mock_server):
    """Interleaving: Cancel before admission prevents outbound request.

    Qualifies cancel-before-admission of the review lifecycle:
    uses an event/barrier-controlled window after the review worker is created
    and before it registers/admits on the parent. Parent cancels in that window.
    Asserts no review chat request is sent to the provider.
    """
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)

    worker_ready_to_register = threading.Event()
    allow_registration = threading.Event()

    # Intercept registration on parent's background review lock to establish
    # a deterministic barrier window after worker creation and before admission.
    real_lock = parent._background_review_lock

    class BarrierLock:
        def __init__(self, lock):
            self._lock = lock
            self._tripped = False

        def __enter__(self):
            if not self._tripped:
                self._tripped = True
                worker_ready_to_register.set()
                allow_registration.wait(timeout=3.0)
            return self._lock.__enter__()

        def __exit__(self, *args):
            return self._lock.__exit__(*args)

        def acquire(self, *args, **kwargs):
            if not self._tripped:
                self._tripped = True
                worker_ready_to_register.set()
                allow_registration.wait(timeout=3.0)
            return self._lock.acquire(*args, **kwargs)

        def release(self):
            return self._lock.release()

    parent._background_review_lock = BarrierLock(real_lock)

    # Spawn background review
    target, _prompt = spawn_background_review_thread(
        parent,
        messages_snapshot=[{"role": "user", "content": "turn 1"}],
        review_memory=True,
    )
    t = threading.Thread(target=target, daemon=True, name="pre-admission-worker")
    t.start()

    # Wait for the review worker to be created and reach registration barrier
    assert worker_ready_to_register.wait(timeout=3.0), "Worker must reach registration barrier"

    # Parent cancel happens in this pre-admission window
    parent.interrupt("user cancel before admission", hard_cancel=True)

    # Release barrier to let worker proceed with admission attempt
    allow_registration.set()
    t.join(timeout=3.0)

    # Assert no review chat request was sent to the mock server
    assert len(handler.chat_requests_received) == 0, (
        "No chat HTTP request should be sent when cancelled before admission"
    )
    assert not handler.review_request_started.is_set(), (
        "Review request should not have started"
    )


def test_pre_admission_superseding_turn_prevents_request(mock_server):
    """Interleaving: New foreground turn during pre-admission window prevents review request.

    When a new live foreground turn begins before the review worker is admitted,
    conversation_loop cancels the pending review token. The worker must be denied
    admission and make zero outbound chat requests.
    """
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)

    worker_ready_to_register = threading.Event()
    allow_registration = threading.Event()

    real_lock = parent._background_review_lock

    class BarrierLock:
        def __init__(self, lock):
            self._lock = lock
            self._tripped = False

        def __enter__(self):
            if not self._tripped:
                self._tripped = True
                worker_ready_to_register.set()
                allow_registration.wait(timeout=3.0)
            return self._lock.__enter__()

        def __exit__(self, *args):
            return self._lock.__exit__(*args)

        def acquire(self, *args, **kwargs):
            if not self._tripped:
                self._tripped = True
                worker_ready_to_register.set()
                allow_registration.wait(timeout=3.0)
            return self._lock.acquire(*args, **kwargs)

        def release(self):
            return self._lock.release()

    parent._background_review_lock = BarrierLock(real_lock)

    target, _prompt = spawn_background_review_thread(
        parent,
        messages_snapshot=[{"role": "user", "content": "turn 1"}],
        review_memory=True,
    )
    t = threading.Thread(target=target, daemon=True, name="pre-admission-superseded")
    t.start()

    assert worker_ready_to_register.wait(timeout=3.0)

    # New live turn arrives: conversation_loop cancels the pending review at line 1687
    res = parent.run_conversation("User turn 2 (superseding before review admission)")
    assert res.get("completed") is True
    assert "Foreground turn response" in res.get("final_response", "")

    allow_registration.set()
    t.join(timeout=3.0)

    # Only the foreground turn's request should have been sent; review must not send any
    review_requests = [
        req for req in handler.chat_requests_received
        if any("memory" in str(m.get("content", "")).lower() or "skill" in str(m.get("content", "")).lower()
               for m in req.get("messages", []))
    ]
    assert len(review_requests) == 0, "No review chat request should be sent when superseded before admission"
    assert not handler.review_request_started.is_set()


def test_cache_parity_and_session_isolation_invariants(mock_server):
    """Preservation of cache prefix parity and session store isolation invariants."""
    base_url, handler = mock_server
    parent = _build_test_agent(base_url)
    parent._cached_system_prompt = "VERBATIM-PARENT-SYSTEM-PROMPT"
    parent.session_id = "parent-sess-uuid"

    target, _prompt = spawn_background_review_thread(
        parent,
        messages_snapshot=[{"role": "user", "content": "turn 1"}],
        review_memory=True,
    )

    t = threading.Thread(target=target, daemon=True)
    t.start()

    # Wait for review to reach HTTP request start via Event (no poll loop)
    assert handler.review_request_started.wait(timeout=3.0)
    review = parent._background_review_agent
    assert review is not None

    seen_cached_prompt = getattr(review, "_cached_system_prompt", None)
    seen_session_id = getattr(review, "session_id", None)
    seen_persist_disabled = getattr(review, "_persist_disabled", None)
    seen_end_session_on_close = getattr(review, "_end_session_on_close", None)

    # Let review finish
    handler.allow_review_response.set()
    t.join(timeout=3.0)

    assert seen_cached_prompt == "VERBATIM-PARENT-SYSTEM-PROMPT", "Cache prefix system prompt must be inherited"
    assert seen_session_id == "parent-sess-uuid", "Session ID must match parent for cache warmth"
    assert seen_persist_disabled is True, "Persistence must be disabled to isolate user session"
    assert seen_end_session_on_close is False, "Session finalization must not occur on review close"
