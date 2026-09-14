"""Tests for Gateway collaboration delivery consumer (CE-HF-DELIVER, #334).

Covers CE-03 (origin reach), CE-06 (exact origin, no passive human ping),
CE-07 (no duplicate supervisor), plan D4-D5:
- Independent processing of collaboration records vs legacy terminal-notification cursor
- Exact origin platform/chat/thread matching
- Internal collaboration wake without passive adapter.send() ping
- Prompt formatting with labeled native peer-evidence block and NO_REPLY directive
- Busy session non-concurrency (deferred wake, rewound claim, delivered when idle)
- Cross-board origin isolation
- Disconnected adapter rewinding to pending
- Coexistence of ordinary terminal events and collaboration
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb


def assert_isolated_db_path(db_path: Path, expected_root: Path) -> None:
    try:
        resolved_path = db_path.resolve()
        resolved_root = expected_root.resolve()
        assert resolved_path.is_relative_to(resolved_root), (
            f"Isolation check failed: db_path {resolved_path} is not under expected root {resolved_root}"
        )
    except (ValueError, AttributeError):
        assert str(db_path.resolve()).startswith(str(expected_root.resolve())), (
            f"Isolation check failed: db_path {db_path} is not under expected root {expected_root}"
        )


@pytest.fixture(autouse=True)
def scrub_kanban_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_environment,
):
    """Scrub ambient kanban env vars and isolate kanban home under tmp_path."""
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    kanban_home = tmp_path / "kanban"
    kanban_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    db_path = kb.kanban_db_path("default")
    assert_isolated_db_path(db_path, tmp_path)


def _isolated_connect(board: str = "default"):
    """Open only after asserting the resolved board DB stays under this fixture."""
    home = Path(os.environ["HERMES_KANBAN_HOME"]).expanduser()
    db_path = kb.kanban_db_path(board)
    assert_isolated_db_path(db_path, home.parent)
    return kb.connect(board=board)


class FakeTelegramAdapter:
    def __init__(self):
        self.sends: list[dict[str, Any]] = []
        self.wakes: list[dict[str, Any]] = []
        self.supports_async_delivery = True

    async def send(
        self, chat_id: str, message: str, *, metadata: Optional[dict] = None
    ) -> SimpleNamespace:
        self.sends.append({
            "chat_id": chat_id,
            "message": message,
            "metadata": metadata,
        })
        return SimpleNamespace(success=True)

    async def handle_message(self, event: Any) -> None:
        self.wakes.append({"event": event})


class FakeApiServerAdapter(FakeTelegramAdapter):
    """Controlled stateless adapter used to exercise the API-server wake path."""

    supports_async_delivery = False

    def __init__(self):
        super().__init__()
        self.supports_async_delivery = False


class FakeGatewayRunner(GatewayKanbanWatchersMixin):
    def __init__(
        self,
        adapter: Any,
        *,
        is_busy: bool = False,
        platform: Platform = Platform.TELEGRAM,
    ):
        self._running = True
        self.adapter = adapter
        self._platform = platform
        self.adapters = {platform: adapter}
        self._profile_adapters = {}
        self._kanban_notifier_profile = "default"
        self._busy = is_busy
        self._ticks = 0

    def _active_profile_name(self) -> str:
        return "default"

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return True

    def _authorization_adapter(
        self, platform: Platform, profile: Optional[str] = None
    ) -> Optional[Any]:
        if platform == self._platform:
            return self.adapter
        return None

    def _is_session_running(self, session_key: str) -> bool:
        return self._busy


def _create_opted_in_root(
    *,
    board: str = "default",
    platform: str = "telegram",
    chat_id: str = "chat-100",
    thread_id: str | None = None,
    assignee: str = "morfeo",
    session_id: str | None = None,
    origin_route: dict[str, Any] | None = None,
) -> str:
    conn = _isolated_connect(board)
    try:
        raw_session_id = session_id or f"session-db-raw-{chat_id}"
        root_id = kb.create_task(
            conn, title="gateway root", assignee=assignee, session_id=raw_session_id
        )
        resolved_route = origin_route or {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "notifier_profile": "default",
            "origin_session_id": raw_session_id,
        }
        kb.opt_in_collaboration(
            conn,
            root_id,
            mode="advisory",
            session_id=raw_session_id,
            origin_route=resolved_route,
        )
        kb.add_notify_sub(
            conn,
            task_id=root_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type="group",
        )
        return root_id
    finally:
        conn.close()


def _create_child_task(
    root_id: str, *, board: str = "default", assignee: str = "implementer"
) -> str:
    conn = _isolated_connect(board)
    try:
        child_id = kb.create_task(
            conn, title="child implementation", assignee=assignee, parents=[root_id]
        )
        kb.claim_task(conn, root_id)
        assert kb.complete_task(conn, root_id)
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = "
            "(SELECT COALESCE(MAX(id), 0) FROM task_events) WHERE task_id = ?",
            (root_id,),
        )
        conn.execute("DELETE FROM kanban_collaboration")
        assert kb.claim_task(conn, child_id)
        return child_id
    finally:
        conn.close()


async def _run_single_tick(
    runner: FakeGatewayRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_sleep(_seconds):
        runner._ticks += 1
        # Startup sleep is ticks==1; first loop tick is ticks >= 2
        if runner._ticks >= 2:
            runner._running = False
        return None

    monkeypatch.setattr("gateway.kanban_watchers.asyncio.sleep", fake_sleep)
    await runner._kanban_notifier_watcher()


@pytest.mark.asyncio
async def test_gateway_collaboration_delivery_wakes_origin_without_passive_ping(
    monkeypatch,
):
    """Collaboration delivery invokes deliver_wake with peer block + NO_REPLY and skips adapter.send."""
    root_id = _create_opted_in_root(thread_id="thread-7")
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="peer review needed",
            recipient="origin",
        )
        assert res["ok"] is True
        sub_before = kb.list_notify_subs(conn, task_id=root_id)[0]
        pre_cursor = sub_before["last_event_id"]
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({
            "adapter": adapt,
            "text": text,
            "session_id": session_id,
            "source": source,
        })

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)

    await _run_single_tick(runner, monkeypatch)

    # 1. adapter.send() was NOT called (no passive human ping)
    assert len(adapter.sends) == 0, f"Expected 0 passive sends, got: {adapter.sends}"

    # 2. deliver_wake was called exactly once
    assert len(wakes_delivered) == 1
    wake_item = wakes_delivered[0]
    assert "[PEER COLLABORATION EVIDENCE - Role: worker-1]" in wake_item["text"]
    assert "peer review needed" in wake_item["text"]
    assert "NO_REPLY" in wake_item["text"]
    assert wake_item["source"].chat_id == "chat-100"
    assert wake_item["source"].thread_id == "thread-7"

    # 3. Notify cursor is untouched
    conn = _isolated_connect()
    try:
        sub_after = kb.list_notify_subs(conn, task_id=root_id)[0]
        assert sub_after["last_event_id"] == pre_cursor
        # 4. Collaboration row delivery_state is queued
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_api_server_collaboration_uses_exact_session_without_ping(
    monkeypatch,
):
    """Stateless API origins use the raw session id and never call passive send."""
    root_id = _create_opted_in_root(platform="api_server", chat_id="api-session-9")
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="API origin consultation",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeApiServerAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False, platform=Platform.API_SERVER)
    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({
            "adapter": adapt,
            "text": text,
            "session_id": session_id,
            "source": source,
        })

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)
    await _run_single_tick(runner, monkeypatch)

    assert adapter.sends == []
    assert len(wakes_delivered) == 1
    assert wakes_delivered[0]["session_id"] == "api-session-9"
    assert wakes_delivered[0]["source"] is None
    assert "API origin consultation" in wakes_delivered[0]["text"]


@pytest.mark.asyncio
async def test_gateway_busy_session_defers_wake_and_delivers_when_idle(monkeypatch):
    """When target session is busy, wake is deferred and rewound to pending; delivers once idle."""
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        kb.block_task(conn, child_id, reason="blocked on API key")
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=True)

    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({"text": text})

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)

    # First tick while busy
    await _run_single_tick(runner, monkeypatch)

    assert len(wakes_delivered) == 0, "Should not wake while session is busy"

    # Claim must have been rewound to pending
    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "pending"
    finally:
        conn.close()

    # Second tick once session is idle
    runner._busy = False
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)

    assert len(wakes_delivered) == 1
    assert "blocked on API key" in wakes_delivered[0]["text"]

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert rows[0]["delivery_state"] == "queued"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_separate_boards_never_cross_deliver(monkeypatch):
    """Separate boards with identical chat IDs never cross-deliver collaboration messages."""
    board_a = "board-a"
    board_b = "board-b"
    conn_a = _isolated_connect(board_a)
    conn_a.close()
    conn_b = _isolated_connect(board_b)
    conn_b.close()

    # Both boards have root tasks subscribed to the same chat
    root_a = _create_opted_in_root(board=board_a, chat_id="shared-chat")
    root_b = _create_opted_in_root(board=board_b, chat_id="shared-chat")

    child_a = _create_child_task(root_a, board=board_a, assignee="worker-a")
    child_b = _create_child_task(root_b, board=board_b, assignee="worker-b")

    # Enqueue notice ONLY on Board A
    conn = _isolated_connect(board_a)
    try:
        kb.request_review(
            conn,
            child_a,
            summary="notice on board A only",
            reviewer="reviewer",
            force=True,
        )
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({"text": text, "source": source})

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)

    await _run_single_tick(runner, monkeypatch)

    assert len(wakes_delivered) == 1
    assert "notice on board A only" in wakes_delivered[0]["text"]
    assert root_b not in wakes_delivered[0]["text"]


@pytest.mark.asyncio
async def test_gateway_disconnected_adapter_rewinds_claim(monkeypatch):
    """When adapter is disconnected / None, claimed collaboration message is rewound to pending."""
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        kb.request_review(
            conn, child_id, summary="needs adapter", reviewer="reviewer", force=True
        )
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)
    # Simulate disconnected adapter
    runner._authorization_adapter = lambda platform, profile=None: None

    await _run_single_tick(runner, monkeypatch)

    # Message must be rewound to pending
    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "pending"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_missing_root_route_is_not_rerouted_to_child(
    monkeypatch,
):
    """Removing the root route leaves collaboration unavailable, not redirected."""
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="origin route is required",
            recipient="origin",
        )
        assert res["ok"] is True
        assert kb.remove_notify_sub(
            conn,
            task_id=root_id,
            platform="telegram",
            chat_id="chat-100",
            thread_id="",
        )
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)
    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({"text": text})

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)
    await _run_single_tick(runner, monkeypatch)

    assert wakes_delivered == []
    assert adapter.sends == []
    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "pending"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_terminal_and_collaboration_coexist(monkeypatch):
    """Terminal event sends passive message and advances cursor; collaboration wakes without passive ping."""
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        # Subscribe to child_id for terminal event
        kb.add_notify_sub(
            conn, task_id=child_id, platform="telegram", chat_id="chat-100"
        )
        # Enqueue collaboration notice on origin (root)
        kb.request_review(
            conn,
            child_id,
            summary="collaboration notice",
            reviewer="reviewer",
            force=True,
        )
        # Complete child_id (terminal event)
        kb.complete_task(conn, child_id, summary="child completed")
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    wakes_delivered: list[dict[str, Any]] = []

    async def fake_deliver_wake(adapt, *, text, session_id="", source=None):
        wakes_delivered.append({"text": text})

    monkeypatch.setattr("gateway.wake.deliver_wake", fake_deliver_wake)

    await _run_single_tick(runner, monkeypatch)

    # 1. adapter.send() was called for terminal event
    assert len(adapter.sends) >= 1
    assert any("child completed" in s["message"] for s in adapter.sends)

    # 2. deliver_wake was called for collaboration
    assert len(wakes_delivered) == 1
    assert "collaboration notice" in wakes_delivered[0]["text"]


def _expire_collaboration_leases(board: str = "default") -> None:
    """Age every collaboration lease so the next claim exercises CORE's reclaim."""
    conn = _isolated_connect(board)
    try:
        conn.execute(
            "UPDATE kanban_collaboration "
            "SET lease_expires = CAST(strftime('%s', 'now') AS INTEGER) - 10"
        )
    finally:
        conn.close()


def _create_root_with_extra_sub(
    *,
    platform: str,
    chat_id: str,
    extra_platform: str,
    extra_chat_id: str,
    request_body: str,
    thread_id: str | None = None,
    extra_thread_id: str | None = None,
    session_id: str | None = None,
    origin_route: dict[str, Any] | None = None,
) -> str:
    """Root with two notify routes (origin + another platform) plus one request."""
    conn = _isolated_connect()
    try:
        raw_session_id = session_id or f"session-db-raw-{chat_id}"
        root_id = kb.create_task(
            conn, title="multi-route root", assignee="morfeo", session_id=raw_session_id
        )
        resolved_route = origin_route or {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "notifier_profile": "default",
            "origin_session_id": raw_session_id,
        }
        kb.opt_in_collaboration(
            conn,
            root_id,
            mode="advisory",
            session_id=raw_session_id,
            origin_route=resolved_route,
        )
        kb.add_notify_sub(
            conn,
            task_id=root_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type="group",
        )
        kb.add_notify_sub(
            conn,
            task_id=root_id,
            platform=extra_platform,
            chat_id=extra_chat_id,
            thread_id=extra_thread_id,
        )
        child_id = kb.create_task(
            conn, title="child implementation", assignee="worker-1", parents=[root_id]
        )
        kb.claim_task(conn, root_id)
        assert kb.complete_task(conn, root_id)
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = "
            "(SELECT COALESCE(MAX(id), 0) FROM task_events)"
        )
        conn.execute("DELETE FROM kanban_collaboration")
        assert kb.claim_task(conn, child_id)
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body=request_body,
            recipient="origin",
        )
        assert res["ok"] is True
        return root_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_live_origin_not_rewoken_after_lease_reclaim(monkeypatch):
    """A reclaimed expired lease must not wake a still-live origin twice (D3).

    Uses the real ``gateway.wake.deliver_wake`` path against the controlled
    fake adapter (no monkeypatched wake), so the recorded wake count is the
    one the consumer actually produced.
    """
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="still-live origin must be woken once",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    await _run_single_tick(runner, monkeypatch)

    assert len(adapter.wakes) == 1
    wake_event = adapter.wakes[0]["event"]
    assert wake_event.internal is True
    assert "still-live origin must be woken once" in wake_event.text
    assert adapter.sends == []

    # CORE reclaims an expired, still-unacknowledged lease (process-loss
    # redelivery path). The same live consumer must not enqueue a second
    # wake for the record that is still visible/queued.
    _expire_collaboration_leases()
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)

    assert len(adapter.wakes) == 1, "still-live origin was re-woken after lease reclaim"
    assert adapter.sends == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
        # The lease was aged to the past and must not have been refreshed
        assert rows[0].get("lease_expires") is None or rows[0]["lease_expires"] <= int(
            time.time()
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_process_loss_redelivers_unconsumed_record(monkeypatch):
    """A fresh consumer (process loss) still redelivers the unconsumed record."""
    root_id = _create_opted_in_root()
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="redelivery after process loss",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 1
    assert adapter.sends == []

    # The wake record is process-scoped, so CORE's lease reclaim still does its
    # job: a restarted gateway (fresh consumer instance) redelivers the record
    # that was never consumed.
    _expire_collaboration_leases()
    restarted = FakeGatewayRunner(adapter, is_busy=False)
    await _run_single_tick(restarted, monkeypatch)

    assert len(adapter.wakes) == 2
    assert "redelivery after process loss" in adapter.wakes[1]["event"].text
    assert adapter.wakes[1]["event"].internal is True
    assert adapter.sends == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_two_telegram_chats_on_one_root_stay_unavailable(monkeypatch):
    """An extra telegram chat is not a second origin; only the commissioned chat wakes.

    Pre-D5 uniqueness-among-reachable-platforms treated two telegram chats as
    ambiguous. Extra notify subscribers do not expand the recipient.
    """
    root_id = _create_opted_in_root(chat_id="chat-100")
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=root_id,
            platform="telegram",
            chat_id="chat-101",
            chat_type="group",
        )
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="two telegram routes",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)
    await _run_single_tick(runner, monkeypatch)

    assert len(adapter.wakes) == 1
    assert adapter.wakes[0]["event"].internal is True
    assert adapter.wakes[0]["event"].source.chat_id == "chat-100"
    assert adapter.sends == []
    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_same_surface_foreign_chat_ignored(monkeypatch):
    """An extra telegram sub on the same root for a foreign chat is ignored; only commissioned chat wakes."""
    root_id = _create_opted_in_root(chat_id="chat-100")
    child_id = _create_child_task(root_id, assignee="worker-1")

    conn = _isolated_connect()
    try:
        kb.add_notify_sub(
            conn,
            task_id=root_id,
            platform="telegram",
            chat_id="chat-foreign-999",
            chat_type="group",
        )
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="foreign chat ignored test",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    await _run_single_tick(runner, monkeypatch)

    assert len(adapter.wakes) == 1
    assert adapter.wakes[0]["event"].source.chat_id == "chat-100"
    assert "foreign chat ignored test" in adapter.wakes[0]["event"].text
    assert adapter.sends == []


@pytest.mark.asyncio
async def test_origin_route_missing_stays_unavailable(monkeypatch):
    """A root with collaboration opted in but missing origin_route stays pending/unavailable."""
    conn = _isolated_connect()
    try:
        root_id = kb.create_task(conn, title="legacy opted root", assignee="morfeo")
        kb.opt_in_collaboration(
            conn, root_id, mode="advisory", session_id="legacy-sess"
        )
        kb.add_notify_sub(
            conn, task_id=root_id, platform="telegram", chat_id="chat-100"
        )
        child_id = kb.create_task(
            conn, title="child", assignee="worker-1", parents=[root_id]
        )
        kb.claim_task(conn, root_id)
        assert kb.complete_task(conn, root_id)
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = "
            "(SELECT COALESCE(MAX(id), 0) FROM task_events)"
        )
        conn.execute("DELETE FROM kanban_collaboration")
        assert kb.claim_task(conn, child_id)
        res = kb.create_collaboration_request(
            conn,
            task_id=child_id,
            author="worker-1",
            body="missing route test",
            recipient="origin",
        )
        assert res["ok"] is True
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 0

    from tui_gateway import server as tui_server

    session = {
        "session_key": "chat-100",
        "history_lock": threading.Lock(),
        "running": False,
        "_finalized": False,
    }
    assert tui_server._collect_kanban_collaboration(session) == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "pending"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_tui_sub_does_not_make_telegram_origin_ambiguous(monkeypatch):
    """A TUI session sub on the same root must not block the gateway's route.

    Uniqueness is scoped to the routes this consumer would use: the gateway
    has no ``tui`` adapter, so the TUI sub is not a candidate origin for it.
    """
    root_id = _create_root_with_extra_sub(
        platform="telegram",
        chat_id="chat-100",
        extra_platform="tui",
        extra_chat_id="tui-session-11",
        request_body="gateway route survives a tui watcher",
    )

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    await _run_single_tick(runner, monkeypatch)

    assert len(adapter.wakes) == 1
    wake_event = adapter.wakes[0]["event"]
    assert wake_event.internal is True
    assert wake_event.source.chat_id == "chat-100"
    assert "gateway route survives a tui watcher" in wake_event.text
    assert adapter.sends == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exclusivity_commissioned_telegram_origin_gateway_first(monkeypatch):
    """Commissioned Telegram origin: gateway delivers 1 wake; extra TUI subscriber receives 0."""
    from tui_gateway import server as tui_server

    monkeypatch.setattr(tui_server, "_KANBAN_COLLAB_ENQUEUED", {})

    session_key = "tui-session-extra-1"
    root_id = _create_root_with_extra_sub(
        platform="telegram",
        chat_id="chat-100",
        extra_platform="tui",
        extra_chat_id=session_key,
        request_body="telegram commissioning origin exclusivity test",
        session_id="session-db-raw-tg-1",
        origin_route={
            "platform": "telegram",
            "chat_id": "chat-100",
            "thread_id": None,
            "notifier_profile": "default",
            "origin_session_id": "session-db-raw-tg-1",
        },
    )

    conn = _isolated_connect()
    try:
        pre_cursors = {
            sub["chat_id"]: sub["last_event_id"]
            for sub in kb.list_notify_subs(conn, task_id=root_id)
        }
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    # 1. Gateway ticks first: exactly 1 internal telegram wake, adapter.send == [], row queued with lease
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 1
    wake = adapter.wakes[0]["event"]
    assert wake.internal is True
    assert wake.source.chat_id == "chat-100"
    assert "telegram commissioning origin exclusivity test" in wake.text
    assert adapter.sends == []

    session = {
        "session_key": session_key,
        "history_lock": threading.Lock(),
        "running": False,
        "_finalized": False,
    }

    # 2. TUI collects for extra subscriber session_key: 0 items (not commissioned origin)
    assert tui_server._collect_kanban_collaboration(session) == []

    # 3. Expire lease
    _expire_collaboration_leases()

    # 4. Gateway ticks again with the same live runner:
    # D3 hold: still 1 wake; gateway does NOT re-claim and does NOT refresh lease
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 1
    assert adapter.sends == []

    # 5. TUI collects again after lease expiry: still 0 items (never eligible)
    assert tui_server._collect_kanban_collaboration(session) == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
        # Verify ordinary notification cursor was untouched
        for sub in kb.list_notify_subs(conn, task_id=root_id):
            assert sub.get("last_event_id") == pre_cursors[sub["chat_id"]]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exclusivity_commissioned_telegram_origin_tui_first(monkeypatch):
    """Commissioned Telegram origin, TUI ticks first: TUI collects 0; gateway delivers 1."""
    from tui_gateway import server as tui_server

    monkeypatch.setattr(tui_server, "_KANBAN_COLLAB_ENQUEUED", {})

    session_key = "tui-session-extra-2"
    root_id = _create_root_with_extra_sub(
        platform="telegram",
        chat_id="chat-100",
        extra_platform="tui",
        extra_chat_id=session_key,
        request_body="telegram origin tui first test",
        session_id="session-db-raw-tg-2",
        origin_route={
            "platform": "telegram",
            "chat_id": "chat-100",
            "thread_id": None,
            "notifier_profile": "default",
            "origin_session_id": "session-db-raw-tg-2",
        },
    )

    conn = _isolated_connect()
    try:
        pre_cursors = {
            sub["chat_id"]: sub["last_event_id"]
            for sub in kb.list_notify_subs(conn, task_id=root_id)
        }
    finally:
        conn.close()

    session = {
        "session_key": session_key,
        "history_lock": threading.Lock(),
        "running": False,
        "_finalized": False,
    }

    # 1. TUI collects first: 0 items (origin is Telegram, TUI is extra subscriber)
    assert tui_server._collect_kanban_collaboration(session) == []

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    # 2. Gateway ticks: exactly 1 internal telegram wake, adapter.send == []
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 1
    wake = adapter.wakes[0]["event"]
    assert wake.internal is True
    assert wake.source.chat_id == "chat-100"
    assert "telegram origin tui first test" in wake.text
    assert adapter.sends == []

    # 3. Expire lease
    _expire_collaboration_leases()

    # 4. TUI collects again: still 0 items
    assert tui_server._collect_kanban_collaboration(session) == []

    # 5. Gateway ticks again: 0 wakes (D3 hold, still 1 total wake)
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 1
    assert adapter.sends == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
        for sub in kb.list_notify_subs(conn, task_id=root_id):
            assert sub.get("last_event_id") == pre_cursors[sub["chat_id"]]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exclusivity_commissioned_tui_origin_tui_first(monkeypatch):
    """Commissioned TUI origin, TUI collects first: TUI collects 1; extra Telegram subscriber receives 0."""
    from tui_gateway import server as tui_server

    monkeypatch.setattr(tui_server, "_KANBAN_COLLAB_ENQUEUED", {})

    session_key = "tui-session-origin-1"
    root_id = _create_root_with_extra_sub(
        platform="tui",
        chat_id=session_key,
        extra_platform="telegram",
        extra_chat_id="chat-extra-200",
        request_body="tui origin exclusivity test",
        session_id="session-db-raw-tui-1",
        origin_route={
            "platform": "tui",
            "chat_id": session_key,
            "thread_id": None,
            "notifier_profile": "default",
            "origin_session_id": "session-db-raw-tui-1",
        },
    )

    conn = _isolated_connect()
    try:
        pre_cursors = {
            sub["chat_id"]: sub["last_event_id"]
            for sub in kb.list_notify_subs(conn, task_id=root_id)
        }
    finally:
        conn.close()

    session = {
        "session_key": session_key,
        "history_lock": threading.Lock(),
        "running": False,
        "_finalized": False,
    }

    # 1. TUI collects first: 1 item, queued with lease
    tui_items = tui_server._collect_kanban_collaboration(session)
    assert len(tui_items) == 1
    collab_dict, text = tui_items[0]
    assert collab_dict["recipient_kind"] == "origin"
    assert collab_dict["delivery_state"] == "queued"
    assert "tui origin exclusivity test" in text

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    # 2. Gateway ticks while lease is active: 0 wakes (origin is TUI, gateway ignores)
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 0
    assert adapter.sends == []

    # 3. Expire lease
    _expire_collaboration_leases()

    # 4. TUI collects again with the same live process:
    # D3 hold: already enqueued, returns 0 items
    assert tui_server._collect_kanban_collaboration(session) == []

    # 5. Gateway ticks after lease expiry: still 0 wakes (never eligible)
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 0
    assert adapter.sends == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
        for sub in kb.list_notify_subs(conn, task_id=root_id):
            assert sub.get("last_event_id") == pre_cursors[sub["chat_id"]]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exclusivity_commissioned_tui_origin_gateway_first(monkeypatch):
    """Commissioned TUI origin, Gateway ticks first: Gateway wakes 0; TUI collects 1."""
    from tui_gateway import server as tui_server

    monkeypatch.setattr(tui_server, "_KANBAN_COLLAB_ENQUEUED", {})

    session_key = "tui-session-origin-2"
    root_id = _create_root_with_extra_sub(
        platform="tui",
        chat_id=session_key,
        extra_platform="telegram",
        extra_chat_id="chat-extra-201",
        request_body="tui origin gateway first test",
        session_id="session-db-raw-tui-2",
        origin_route={
            "platform": "tui",
            "chat_id": session_key,
            "thread_id": None,
            "notifier_profile": "default",
            "origin_session_id": "session-db-raw-tui-2",
        },
    )

    conn = _isolated_connect()
    try:
        pre_cursors = {
            sub["chat_id"]: sub["last_event_id"]
            for sub in kb.list_notify_subs(conn, task_id=root_id)
        }
    finally:
        conn.close()

    adapter = FakeTelegramAdapter()
    runner = FakeGatewayRunner(adapter, is_busy=False)

    # 1. Gateway ticks first: 0 wakes (platform is TUI, gateway does not touch)
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 0
    assert adapter.sends == []

    session = {
        "session_key": session_key,
        "history_lock": threading.Lock(),
        "running": False,
        "_finalized": False,
    }

    # 2. TUI collects for matching session_key: 1 item, queued
    tui_items = tui_server._collect_kanban_collaboration(session)
    assert len(tui_items) == 1
    collab_dict, text = tui_items[0]
    assert collab_dict["recipient_kind"] == "origin"
    assert collab_dict["delivery_state"] == "queued"
    assert "tui origin gateway first test" in text

    # 3. Expire lease
    _expire_collaboration_leases()

    # 4. Gateway ticks again after lease expiry: still 0 wakes
    runner._running = True
    runner._ticks = 0
    await _run_single_tick(runner, monkeypatch)
    assert len(adapter.wakes) == 0
    assert adapter.sends == []

    # 5. TUI collects again: 0 items (D3 hold)
    assert tui_server._collect_kanban_collaboration(session) == []

    conn = _isolated_connect()
    try:
        rows = kb.list_collaboration_for_task(conn, root_id)
        assert len(rows) == 1
        assert rows[0]["delivery_state"] == "queued"
        assert rows[0]["acknowledged_at"] is None
        for sub in kb.list_notify_subs(conn, task_id=root_id):
            assert sub.get("last_event_id") == pre_cursors[sub["chat_id"]]
    finally:
        conn.close()
