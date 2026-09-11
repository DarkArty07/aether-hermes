"""TS-382 bounded transcript publication — deterministic coverage (design D1-D7).

Complements the approved research oracle (``specs/006-tools-memory-stability/
fixtures/ts382_transcript_replacement_oracle.py``) with deterministic
barrier/fault-injection coverage: no test here depends on a race outcome for
its assertion. Everything runs against temporary databases and the real
``SessionDB``; the live runtime tree and live state DB are never touched.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hermes_state import SessionDB
from hermes_state_common import (
    COMPACTION_PUBLICATION_BATCH_BYTES,
    COMPACTION_PUBLICATION_BATCH_ROWS,
    COMPACTION_PUBLICATION_MARKER_PREFIX,
    COMPACTION_PUBLICATION_MAX_ROUNDS,
    COMPACTION_STAGING_SESSION_PREFIX,
    COMPACTION_STAGING_SESSION_SOURCE,
    FTS_TOOL_FULL_CONTENT_HIGH_WATER_KEY,
    LEGACY_FTS_SQL,
    LEGACY_FTS_TRIGRAM_SQL,
)
from hermes_state_compaction import (
    PublicationConflictError,
    PublicationSnapshot,
    _PreparedMessage,
    _iter_publication_batches,
    _publication_marker_key,
    invoke_archive_and_compact,
)

SESSION_ID = "ts382-publication-session"
OTHER_SESSION = "ts382-unrelated-session"
MODEL_KEY = "_ts382_patch"
_REARM_KEY = "proactive_prune_rearm_tokens"


# ── helpers ────────────────────────────────────────────────────────────────


def _message(index: int, tag: str, tool: bool = True) -> Dict[str, Any]:
    if tool:
        call_id = f"call-{tag}-{index}"
        return {
            "role": "tool",
            "content": f"{tag} tool result {index:05d} " + ("x" * 200),
            "tool_name": "read_file",
            "tool_call_id": call_id,
            # One call per row keeps tool_call_count exact and predictable.
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
                }
            ],
        }
    return {"role": "user", "content": f"{tag} user {index:05d}"}


def _messages(count: int, tag: str, tool: bool = True) -> List[Dict[str, Any]]:
    return [_message(i, tag, tool) for i in range(count)]


def _staged_replacement() -> List[Dict[str, Any]]:
    """Replacement rows that WOULD surface publicly if staging leaked.

    The tool rows exercise the search/transcript paths; the trailing user row
    and PR-url tool row make the ``list_recent_user_messages`` and
    ``find_pr_url_messages`` assertions non-vacuous (those paths return nothing
    for tool-only content regardless of the exclusion).
    """
    rows = _messages(300, "needle-stage")
    rows.append({"role": "user", "content": "needle-stage user turn"})
    rows.append(
        {
            "role": "tool",
            "content": "needle-stage pr https://github.com/DarkArty07/aether-hermes/pull/999",
            "tool_name": "run_shell",
            "tool_call_id": "call-needle-pr",
        }
    )
    return rows


def _mkdb(tmp_path: Path, *, legacy: bool = False) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session(SESSION_ID, source="tui")
    if legacy:
        _force_legacy_layout(db)
    db.append_messages_batch(SESSION_ID, _messages(6, "old"))
    return db


def _force_legacy_layout(db: SessionDB) -> None:
    """Reproduce the production store's pre-v23 inline-FTS layout (#305 bound)."""
    with db._lock:
        db._drop_fts_triggers(db._conn)
        db._conn.executescript(
            "DROP TABLE IF EXISTS messages_fts;"
            "DROP TABLE IF EXISTS messages_fts_trigram;"
            "DROP VIEW IF EXISTS messages_fts_trigram_src;"
            + LEGACY_FTS_SQL
            + LEGACY_FTS_TRIGRAM_SQL
        )
        db._conn.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, '0') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (FTS_TOOL_FULL_CONTENT_HIGH_WATER_KEY,),
        )
        db._conn.commit()


def _live(db: SessionDB, session_id: str = SESSION_ID) -> List[Dict[str, Any]]:
    return db.get_messages(session_id)


def _counters(db: SessionDB, session_id: str = SESSION_ID) -> Dict[str, int]:
    return dict(
        db._conn.execute(
            "SELECT message_count, tool_call_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    )


def _staging_sessions(db: SessionDB) -> List[str]:
    return [
        row[0]
        for row in db._conn.execute(
            "SELECT id FROM sessions WHERE source = ?", (COMPACTION_STAGING_SESSION_SOURCE,)
        ).fetchall()
    ]


def _marker(db: SessionDB, session_id: str = SESSION_ID) -> Optional[Dict[str, Any]]:
    row = db._conn.execute(
        "SELECT value FROM state_meta WHERE key = ?",
        (_publication_marker_key(session_id),),
    ).fetchone()
    return json.loads(row[0]) if row is not None else None


def _seed_marker(
    db: SessionDB,
    *,
    owner: str,
    expires_at: float,
    stage: str,
    phase: str = "staging",
    session_id: str = SESSION_ID,
    staged_rows: int = 0,
) -> Dict[str, Any]:
    marker = {
        "v": 1,
        "target": session_id,
        "stage": stage,
        "owner": owner,
        "expires_at": expires_at,
        "watermark": 0,
        "source_digest": "x",
        "source_rows": 0,
        "phase": phase,
        "staged_rows": staged_rows,
        "staged_tool_calls": 0,
        "staged_max_id": None,
        "tail_watermark": 0,
        "tail_rows": 0,
        "tail_tool_calls": 0,
    }
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_publication_marker_key(session_id), json.dumps(marker, sort_keys=True)),
        )
    )
    return marker


class _PauseInsideFirstBatch:
    """Pause a publication INSIDE its first staging batch, before it commits.

    The ownership claim (the staging session row) is already committed when the
    pause is reached; this batch's message rows are not — the pause sits after
    the insert call and before the flags UPDATE + COMMIT, inside
    ``_execute_write``'s transaction. Lets a test observe that pre-commit state
    deterministically (a real barrier, not a sleep). To assert against
    COMMITTED staged rows, use ``_PauseAfterWriteCall`` instead.
    """

    def __init__(self, db: SessionDB, calls: int = 1) -> None:
        self.db = db
        self.entered = threading.Event()
        self.resume = threading.Event()
        self._original = db._insert_message_rows
        self._remaining = calls

        def observed(conn, session_id, messages):
            result = self._original(conn, session_id, messages)
            if self._remaining > 0:
                self._remaining -= 1
                self.entered.set()
                assert self.resume.wait(30.0), "test never resumed the publication"
            return result

        db._insert_message_rows = observed

    def release(self) -> None:
        self.db._insert_message_rows = self._original
        self.resume.set()


class _PauseAfterWriteCall:
    """Pause a publication between two committed transactions.

    The pause happens AFTER ``_execute_write`` returns, so the publisher holds
    neither the instance lock nor SQLite's write lock: other connections can
    append freely (which is what an interleaved-tail test needs) and readers
    see only committed state.
    """

    def __init__(self, db: SessionDB, after_calls: int) -> None:
        self.db = db
        self.entered = threading.Event()
        self.resume = threading.Event()
        self._original = db._execute_write
        self._target = after_calls
        self._calls = 0

        def observed(fn, patience_s=None):
            result = self._original(fn, patience_s=patience_s)
            self._calls += 1
            if self._calls >= self._target:
                self.entered.set()
                assert self.resume.wait(60.0), "test never resumed the publication"
            return result

        db._execute_write = observed

    def release(self) -> None:
        self.db._execute_write = self._original
        self.resume.set()


def _publish(db: SessionDB, messages: List[Dict[str, Any]], **kwargs) -> int:
    return db.archive_and_compact(SESSION_ID, messages, **kwargs)


# ── D5: the metadata-only cutover needs no FTS work ────────────────────────


@pytest.mark.parametrize("legacy", [False, True])
def test_update_triggers_never_key_on_metadata_columns(tmp_path, legacy):
    """Verify the design's premise against BOTH candidate store layouts.

    The cutover only rewrites ``session_id``/``active``/``compacted`` on
    existing rows. That is cheap only while no UPDATE trigger's column list
    names those columns; the legacy inline and v23 external-content families
    differ, so both are checked at the store, not in a comment.
    """
    db = _mkdb(tmp_path, legacy=legacy)
    rows = db._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name LIKE 'messages_fts%'"
    ).fetchall()
    assert rows, "no FTS triggers found"
    update_triggers = [(n, s) for n, s in rows if n.endswith("_update")]
    assert update_triggers, "expected update triggers in both layouts"
    for name, sql in update_triggers:
        upper = sql.upper()
        marker = "AFTER UPDATE OF"
        assert marker in upper, f"unrecognized update trigger shape: {name}"
        start = upper.index(marker) + len(marker)
        on_index = upper.index(" ON ", start)
        columns = {column.strip().lower() for column in sql[start:on_index].split(",")}
        assert columns, name
        assert not (columns & {"session_id", "active", "compacted", "id"}), (
            name,
            columns,
        )
    db.close()


# ── D3: concrete batch and fairness policy ─────────────────────────────────


def test_batch_limits_rows_and_bytes():
    """At most BATCH_ROWS rows or BATCH_BYTES of serialized values, first wins."""
    small = [_PreparedMessage(("user", "a" * 100), 0) for _ in range(300)]
    batches = list(_iter_publication_batches(small))
    assert all(len(batch) <= COMPACTION_PUBLICATION_BATCH_ROWS for batch in batches)
    assert sum(len(batch) for batch in batches) == 300
    assert len(batches) == 3

    large = [
        _PreparedMessage(("tool", "x" * (COMPACTION_PUBLICATION_BATCH_BYTES // 2 - 4096)), 0)
    ] * 4
    batches = list(_iter_publication_batches(large))
    assert [len(batch) for batch in batches] == [2, 2]

    # A single oversized row is processed alone — never truncated, never rejected.
    oversized = [_PreparedMessage(("tool", "z" * (COMPACTION_PUBLICATION_BATCH_BYTES * 3)), 0)]
    assert [len(batch) for batch in _iter_publication_batches(oversized)] == [1]


def test_publication_uses_several_bounded_transactions(tmp_path):
    """The publication must not hold the write lock for the whole replacement."""
    db = _mkdb(tmp_path)
    holds: List[float] = []
    original = db._execute_write

    def timed(fn, patience_s=None):
        start = time.monotonic()
        try:
            return original(fn, patience_s=patience_s)
        finally:
            holds.append(time.monotonic() - start)

    db._execute_write = timed
    _publish(db, _messages(600, "new"))
    db._execute_write = original

    assert len(holds) >= 5, "publication did not commit in batches"
    assert max(holds) < 2.0, f"a single transaction held the lock for {max(holds):.2f}s"
    assert _counters(db)["message_count"] == 600
    db.close()


# ── D2: staged rows are invisible on every public surface ──────────────────


@pytest.mark.parametrize("legacy", [False, True])
def test_staged_rows_are_invisible_while_publishing(tmp_path, legacy):
    """No committed staging row reaches a transcript/search read, in any mode.

    The barrier pauses AFTER the claim and the first staging batch have
    COMMITTED, and every surface is read through a FRESH ``SessionDB`` (its own
    pooled read connection). The assertions are therefore made against real
    persisted staging state — they fail if the exclusion is missing — not
    against an uncommitted buffer. The publisher holds neither the instance
    lock nor SQLite's writer lock at the pause point.
    """
    db = _mkdb(tmp_path, legacy=legacy)
    db.append_message(SESSION_ID, role="user", content="old user turn")
    old_live = _live(db)
    barrier = _PauseAfterWriteCall(db, after_calls=2)
    replacement = _staged_replacement()
    errors: List[BaseException] = []

    def publish():
        try:
            _publish(db, replacement)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    reader: Optional[SessionDB] = None
    try:
        assert barrier.entered.wait(30.0), "publication never committed a staging batch"
        reader = SessionDB(tmp_path / "state.db")
        raw_conn = reader._conn
        assert raw_conn is not None
        staged_ids = _staging_sessions(reader)
        assert len(staged_ids) == 1, "no committed staging session"
        staging_id = staged_ids[0]
        committed_rows = raw_conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (staging_id,)
        ).fetchone()[0]
        assert committed_rows > 0, (
            "barrier must sit after a COMMITTED staging batch or these "
            "assertions pass vacuously"
        )
        flags = {
            (row[0], row[1])
            for row in raw_conn.execute(
                "SELECT DISTINCT active, compacted FROM messages WHERE session_id = ?",
                (staging_id,),
            ).fetchall()
        }
        assert flags == {(0, 0)}, f"committed staging rows carry flags {flags}"
        staged_row_id = raw_conn.execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY id LIMIT 1",
            (staging_id,),
        ).fetchone()[0]

        # Transcript surfaces: explicit inactive mode must not expose staging rows.
        assert reader.get_messages(staging_id) == []
        assert reader.get_messages(staging_id, include_inactive=True) == []
        assert reader.get_messages_as_conversation(staging_id, include_inactive=True) == []
        assert reader.get_messages_around(staging_id, staged_row_id)["window"] == []
        assert reader.list_recent_user_messages(staging_id, include_inactive=True) == []

        # Search surfaces: default and explicit inactive mode.
        assert reader.search_messages("needle-stage") == []
        assert reader.search_messages("needle-stage", include_inactive=True) == []
        assert (
            reader.search_messages(
                "needle-stage", role_filter=["tool"], include_inactive=True
            )
            == []
        )

        # Control: the same surfaces DO see the target's own committed rows, so
        # the emptiness above is caused by the exclusion, not by a dead query.
        assert reader.get_messages(SESSION_ID, include_inactive=True) == old_live
        assert reader.list_recent_user_messages(SESSION_ID, include_inactive=True)
        assert reader.search_messages("old", include_inactive=True)
        assert reader.get_messages_around(SESSION_ID, old_live[-1]["id"])["window"]
    finally:
        if reader is not None:
            reader.close()
        barrier.release()
        thread.join(timeout=60.0)

    assert not errors, f"publication failed: {errors[0]!r}"
    assert len(_live(db)) == len(replacement)
    assert not _staging_sessions(db), "staging session survived the cutover"
    assert _marker(db) is None, "publication marker survived the cutover"
    assert db.search_messages("needle-stage"), "published text is not searchable"
    db.close()


def test_staged_text_invisible_when_fts_is_disabled(tmp_path):
    """The LIKE fallback path must not expose committed staging rows either."""
    db = _mkdb(tmp_path)
    barrier = _PauseAfterWriteCall(db, after_calls=2)
    errors: List[BaseException] = []

    def publish():
        try:
            _publish(db, _messages(120, "needle-fts-off"))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    reader: Optional[SessionDB] = None
    try:
        assert barrier.entered.wait(30.0), "publication never committed a staging batch"
        reader = SessionDB(tmp_path / "state.db")
        staged_ids = _staging_sessions(reader)
        assert len(staged_ids) == 1
        staging_id = staged_ids[0]
        raw_conn = reader._conn
        assert raw_conn is not None
        assert (
            raw_conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (staging_id,)
            ).fetchone()[0]
            > 0
        ), "no committed staging rows to test the fallback against"
        reader._fts_stale = True  # force the LIKE fallback paths
        assert reader.search_messages("needle-fts-off") == []
        assert reader.search_messages("needle-fts-off", include_inactive=True) == []
        assert reader.get_messages(staging_id, include_inactive=True) == []
        # Control: the fallback path still answers for the target's own rows.
        assert reader.search_messages("old")
    finally:
        if reader is not None:
            reader.close()
        barrier.release()
        thread.join(timeout=60.0)
    assert not errors, f"publication failed: {errors[0]!r}"
    db.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_staging_session_is_invisible_to_listing_counts_and_lookup(tmp_path, legacy):
    """No listing, count or by-id lookup mode may surface the staging session.

    Same commit-boundary barrier as the transcript test: the staging session
    row and its first batch are committed before every assertion, and the
    reader is a fresh ``SessionDB``. Counts are compared against the exact
    pre-publication values, so a staging session appearing in any of them is a
    hard failure rather than a shifted expectation.
    """
    db = _mkdb(tmp_path, legacy=legacy)
    before = {
        "all": db.session_count(include_archived=True),
        "archived_only": db.session_count(archived_only=True),
        "by_source": db.session_count_by_source(include_archived=True),
        "ge2": db.session_count_ge(2),
    }
    assert before == {
        "all": 1,
        "archived_only": 0,
        "by_source": {"tui": 1},
        "ge2": False,
    }, f"unexpected pre-publication counts: {before}"
    barrier = _PauseAfterWriteCall(db, after_calls=2)
    errors: List[BaseException] = []

    def publish():
        try:
            _publish(db, _staged_replacement())
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    reader: Optional[SessionDB] = None
    try:
        assert barrier.entered.wait(30.0), "publication never committed a staging batch"
        reader = SessionDB(tmp_path / "state.db")
        raw_conn = reader._conn
        assert raw_conn is not None
        staged_ids = _staging_sessions(reader)
        assert len(staged_ids) == 1
        staging_id = staged_ids[0]
        assert (
            raw_conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (staging_id,)
            ).fetchone()[0]
            > 0
        ), "no committed staging rows to test listing/counting against"

        listed = {
            row["id"]
            for row in reader.list_sessions_rich(
                limit=500, include_hidden=True, include_archived=True
            )
        }
        assert SESSION_ID in listed, "target session missing from the listing"
        assert staging_id not in listed, "staging session leaked into the listing"

        searched = {row["id"] for row in reader.search_sessions(limit=500)}
        assert SESSION_ID in searched
        assert staging_id not in searched, "staging session leaked into search_sessions"

        assert reader.session_count(include_archived=True) == before["all"]
        assert reader.session_count(archived_only=True) == before["archived_only"]
        assert (
            reader.session_count_by_source(include_archived=True) == before["by_source"]
        )
        assert reader.session_count_ge(2) is before["ge2"]

        assert reader.get_session(staging_id) is None, "staging session is lookupable"
        assert reader.get_session(SESSION_ID) is not None
        assert reader.export_session(staging_id) is None, "staging session is exportable"
        assert reader.find_pr_url_messages([staging_id]) == [], (
            "staged content is reachable through the PR scan"
        )
        assert reader.message_count(staging_id) == 0, (
            "staged rows are counted for the staging session"
        )
        # Control: the target's own export remains intact.
        export = reader.export_session(SESSION_ID)
        assert export is not None and export["messages"]
    finally:
        if reader is not None:
            reader.close()
        barrier.release()
        thread.join(timeout=60.0)

    assert not errors, f"publication failed: {errors[0]!r}"
    db.close()


def test_uncommitted_first_batch_is_not_observable(tmp_path):
    """The in-flight batch is invisible to a fresh connection before it commits.

    The ownership claim (the staging session row) is committed; this batch's
    message rows are not — the barrier sits inside that batch's transaction.
    A fresh connection must see zero message rows for the staging session.
    """
    db = _mkdb(tmp_path)
    # Open the reader BEFORE the publication starts: SessionDB.__init__ runs
    # schema-init writes, which cannot proceed while the publisher holds the
    # SQLite writer slot inside the batch transaction.
    reader = SessionDB(tmp_path / "state.db")
    barrier = _PauseInsideFirstBatch(db)
    errors: List[BaseException] = []

    def publish():
        try:
            _publish(db, _messages(20, "replacement"))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert barrier.entered.wait(30.0), "publication never entered a batch"
        raw_conn = reader._conn
        assert raw_conn is not None
        staged_ids = _staging_sessions(reader)
        assert len(staged_ids) == 1, "the claim must be committed before the batch"
        visible = raw_conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (staged_ids[0],)
        ).fetchone()[0]
        assert visible == 0, "an uncommitted staging batch is observable"
    finally:
        barrier.release()
        thread.join(timeout=60.0)
        reader.close()
    assert not errors, f"publication failed: {errors[0]!r}"
    db.close()


# ── D4: concurrent appends and stable ordering ─────────────────────────────


def test_interleaved_tail_preserved_in_order_and_exactly_once(tmp_path):
    """A tail row whose id interleaves with staged ids is relocated, once."""
    db = _mkdb(tmp_path)
    appender = SessionDB(tmp_path / "state.db")
    # Pause after the claim + first staged batch, so appends land BETWEEN
    # staged batches and their ids interleave with the remaining staged ids.
    barrier = _PauseAfterWriteCall(db, after_calls=2)
    replacement = _messages(200, "replacement")
    errors: List[BaseException] = []

    def publish():
        try:
            _publish(db, replacement)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    tail_rows: List[Dict[str, Any]] = []
    try:
        assert barrier.entered.wait(30.0)
        # Land concurrent appends WHILE the publisher still has batches to
        # stage, so their ids interleave with the staged ids.
        for index in range(3):
            tail_rows.append(
                {
                    "role": "tool",
                    "content": f"concurrent tail {index}",
                    "tool_name": "read_file",
                    "tool_call_id": f"call-tail-{index}",
                    "reasoning": f"reasoning {index}",
                    "api_content": f"api {index}",
                    "display_metadata": {"index": index},
                }
            )
            appender.append_message(
                SESSION_ID,
                role="tool",
                content=f"concurrent tail {index}",
                tool_name="read_file",
                tool_call_id=f"call-tail-{index}",
                reasoning=f"reasoning {index}",
                api_content=f"api {index}",
                display_metadata={"index": index},
                tool_calls=[
                    {
                        "id": f"call-tail-{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            )
    finally:
        barrier.release()
        thread.join(timeout=120.0)
        appender.close()

    assert not errors, f"publication failed: {errors[0]!r}"
    live = _live(db)
    assert len(live) == len(replacement) + len(tail_rows)
    # Order: replacement -> concurrent tail, each tail row exactly once.
    assert [m["content"] for m in live[len(replacement):]] == [
        row["content"] for row in tail_rows
    ]
    for row in tail_rows:
        assert [m["content"] for m in live].count(row["content"]) == 1
    # Sidecars survive the relocation byte-for-byte.
    relocated = live[len(replacement):]
    assert [m["reasoning"] for m in relocated] == ["reasoning 0", "reasoning 1", "reasoning 2"]
    assert [m["api_content"] for m in relocated] == ["api 0", "api 1", "api 2"]
    assert [m["display_metadata"] for m in relocated] == [
        {"index": 0},
        {"index": 1},
        {"index": 2},
    ]
    # The originals are recoverable but not duplicated into the live view.
    hidden = db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 0 "
        "AND compacted = 0 AND content LIKE 'concurrent tail%'",
        (SESSION_ID,),
    ).fetchone()[0]
    assert hidden == len(tail_rows), "relocated originals were not hidden"
    counters = _counters(db)
    assert counters["message_count"] == len(replacement) + len(tail_rows)
    assert counters["tool_call_count"] == len(replacement) + len(tail_rows)
    db.close()


def test_unrelocated_tail_keeps_its_rows_and_order(tmp_path):
    """Tail rows above the last staged id stay in place (active, same ids)."""
    db = _mkdb(tmp_path)
    appender = SessionDB(tmp_path / "state.db")
    _publish(db, _messages(80, "replacement"))
    appended = appender.append_message(SESSION_ID, role="user", content="after-publish")
    live = _live(db)
    assert len(live) == 80 + 1
    assert live[-1]["content"] == "after-publish"
    row = db._conn.execute(
        "SELECT active, compacted FROM messages WHERE id = ?", (appended,)
    ).fetchone()
    assert (row["active"], row["compacted"]) == (1, 0)
    appender.close()
    db.close()


def test_tail_relocation_counts_parsed_tool_calls(tmp_path):
    """A relocated row's stored multi-call payload keeps its exact count."""
    db = _mkdb(tmp_path)
    appender = SessionDB(tmp_path / "state.db")
    barrier = _PauseAfterWriteCall(db, after_calls=2)

    def publish():
        _publish(db, _messages(120, "replacement"))

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert barrier.entered.wait(30.0)
        appender.append_message(
            SESSION_ID,
            role="assistant",
            content="",
            tool_calls=[
                {"id": "a", "type": "function", "function": {"name": "x"}},
                {"id": "b", "type": "function", "function": {"name": "y"}},
            ],
        )
    finally:
        barrier.release()
        thread.join(timeout=60.0)
        appender.close()

    counters = _counters(db)
    assert counters["message_count"] == 121
    # 120 single-call replacement rows + one relocated two-call row.
    assert counters["tool_call_count"] == 122
    db.close()


def test_bounded_conflict_under_continuous_tail_activity(tmp_path):
    """A permanently moving tail exits as an explicit conflict, losing nothing."""
    db = _mkdb(tmp_path)
    appender = SessionDB(tmp_path / "state.db")
    before = [m["content"] for m in _live(db)]
    original_scan = SessionDB._read_interleaved_tail_ids
    calls = {"n": 0}

    def always_moving(self, target, lower, upper):
        calls["n"] += 1
        return original_scan(self, target, lower, upper)

    # Every validation round sees brand-new interleaved rows: nothing settles.
    def scan_and_append(self, target, lower, upper):
        calls["n"] += 1
        appender.append_message(SESSION_ID, role="user", content=f"churn {calls['n']}")
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND active = 1 "
                "AND id > ? ORDER BY id",
                (SESSION_ID, lower),
            ).fetchall()
        ]

    SessionDB._read_interleaved_tail_ids = scan_and_append
    try:
        with pytest.raises(PublicationConflictError):
            _publish(db, _messages(120, "replacement"))
    finally:
        SessionDB._read_interleaved_tail_ids = original_scan

    assert calls["n"] <= COMPACTION_PUBLICATION_MAX_ROUNDS + 1
    # Every committed append survives; the original transcript is still there.
    remaining = [m["content"] for m in _live(db)]
    for content in before:
        assert content in remaining, "original transcript row was lost"
    appended = db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND content LIKE 'churn %'",
        (SESSION_ID,),
    ).fetchone()[0]
    assert appended >= 1, "committed appends were lost on the conflict path"
    assert _marker(db) is None, "conflict left a publication marker behind"
    assert not _staging_sessions(db), "conflict left a staging session behind"
    appender.close()
    db.close()


# ── D5: one atomic cutover, version-fenced ─────────────────────────────────


def test_external_commit_between_validation_and_begin_is_fenced(tmp_path):
    """An external commit must never slip between validation and publication."""
    db = _mkdb(tmp_path)
    other = SessionDB(tmp_path / "state.db")
    original_version = db._read_data_version
    state = {"calls": 0, "injected": False}

    def version_with_external_commit(conn=None):
        state["calls"] += 1
        if state["calls"] == 2 and not state["injected"]:
            # Between the pre-validation version read and the post-validation
            # one: exactly the window the fence exists for. The publisher holds
            # no transaction here, so this is a real external commit.
            state["injected"] = True
            other.append_message(SESSION_ID, role="user", content="external commit")
        return original_version(conn)

    db._read_data_version = version_with_external_commit
    try:
        published = _publish(db, _messages(200, "replacement"))
    finally:
        db._read_data_version = original_version

    assert state["injected"], "the external commit was never injected"
    assert published >= 200
    contents = [m["content"] for m in _live(db)]
    assert "external commit" in contents, "the external append was lost"
    assert contents.count("external commit") == 1, "the external append was duplicated"
    other.close()
    db.close()


def test_cutover_failure_rolls_back_and_leaves_target_intact(tmp_path):
    """A failing cutover must not publish anything or damage the old view."""
    db = _mkdb(tmp_path)
    old_live = _live(db)
    old_counters = _counters(db)
    db.create_session(OTHER_SESSION, source="tui")
    db.append_messages_batch(OTHER_SESSION, _messages(3, "unrelated"))

    original_merge = SessionDB._merge_model_config_json

    def failing_merge(*args, **kwargs):
        raise RuntimeError("ts382 injected cutover failure")

    SessionDB._merge_model_config_json = failing_merge
    try:
        with pytest.raises(RuntimeError, match="injected cutover failure"):
            _publish(
                db,
                _messages(150, "replacement"),
                model_config_patch={MODEL_KEY: 1},
            )
    finally:
        SessionDB._merge_model_config_json = original_merge

    assert _live(db) == old_live, "visible transcript changed after a failed cutover"
    assert _counters(db) == old_counters, "counters changed after a failed cutover"
    assert not db.search_messages("replacement"), "failed publication text is searchable"
    assert _marker(db) is None, "failed publication left its marker behind"
    assert not _staging_sessions(db), "failed publication left staging rows behind"
    assert [m["content"] for m in _live(db, OTHER_SESSION)] == [
        m["content"] for m in _messages(3, "unrelated")
    ]
    db.close()


def test_preparation_failure_creates_nothing(tmp_path):
    db = _mkdb(tmp_path)
    old_live = _live(db)
    original_prepare = SessionDB._prepare_message_row

    def failing_prepare(self, msg, now_ts):
        if msg.get("content", "").startswith("boom"):
            raise RuntimeError("ts382 injected preparation failure")
        return original_prepare(self, msg, now_ts)

    SessionDB._prepare_message_row = failing_prepare
    try:
        with pytest.raises(RuntimeError, match="injected preparation failure"):
            _publish(db, [_message(0, "fine"), _message(1, "boom")])
    finally:
        SessionDB._prepare_message_row = original_prepare

    assert _live(db) == old_live
    assert _marker(db) is None
    assert not _staging_sessions(db)
    db.close()


def test_cleanup_failure_retains_a_typed_marker_and_the_original_error(tmp_path):
    db = _mkdb(tmp_path)
    old_live = _live(db)
    original_delete = SessionDB._delete_publication_rows
    original_insert = db._insert_message_rows

    def failing_delete(self, stage, batch_rows=COMPACTION_PUBLICATION_BATCH_ROWS):
        raise RuntimeError("ts382 injected cleanup failure")

    def failing_insert(conn, session_id, messages):
        raise RuntimeError("ts382 injected staging failure")

    db._insert_message_rows = failing_insert
    SessionDB._delete_publication_rows = failing_delete
    try:
        with pytest.raises(RuntimeError, match="injected staging failure"):
            _publish(db, _messages(5, "replacement"))
    finally:
        db._insert_message_rows = original_insert
        SessionDB._delete_publication_rows = original_delete

    assert _live(db) == old_live, "a failed publication changed the live transcript"
    marker = _marker(db)
    assert marker is not None, "cleanup failure lost the generation identity"
    assert marker.get("phase") == "cleanup_failed"
    # The identifiable generation is reclaimable once its owner is gone.
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ?",
            (
                json.dumps({**marker, "expires_at": 0.0}, sort_keys=True),
                _publication_marker_key(SESSION_ID),
            ),
        )
    )
    assert _publish(db, _messages(10, "after-reclaim")) == 10
    assert _marker(db) is None
    assert not _staging_sessions(db)
    db.close()


# ── D1/D6: ownership, reclamation, leases ──────────────────────────────────


def test_concurrent_live_publisher_is_excluded(tmp_path):
    db = _mkdb(tmp_path)
    old_live = _live(db)
    stage = f"{COMPACTION_STAGING_SESSION_PREFIX}other-live"
    _seed_marker(
        db,
        owner=f"pid={__import__('os').getpid()}:other:compaction-publication:abc",
        expires_at=time.time() + 300.0,
        stage=stage,
        staged_rows=2,
    )
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO sessions (id, source, started_at, message_count, "
            "tool_call_count, model_config, hidden, archived) "
            "VALUES (?, ?, ?, 0, 0, NULL, 1, 1)",
            (stage, COMPACTION_STAGING_SESSION_SOURCE, time.time()),
        )
    )
    with pytest.raises(PublicationConflictError):
        _publish(db, _messages(10, "replacement"))
    assert _live(db) == old_live
    assert _marker(db) is not None, "a live owner's marker was stolen"
    db.close()


def test_expired_owner_is_reclaimed_lazily_with_bounded_cleanup(tmp_path):
    db = _mkdb(tmp_path)
    stale_stage = f"{COMPACTION_STAGING_SESSION_PREFIX}stale"
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO sessions (id, source, started_at, message_count, "
            "tool_call_count, model_config, hidden, archived) "
            "VALUES (?, ?, ?, 0, 0, NULL, 1, 1)",
            (stale_stage, COMPACTION_STAGING_SESSION_SOURCE, time.time()),
        )
    )
    with db._lock:
        db._conn.execute("BEGIN IMMEDIATE")
        for index in range(5):
            db._conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, active, compacted) "
                "VALUES (?, 'user', ?, ?, 0, 0)",
                (stale_stage, f"stale {index}", time.time()),
            )
        db._conn.commit()
    _seed_marker(
        db,
        owner="pid=999999:crashed:compaction-publication:dead",
        expires_at=time.time() - 1.0,
        stage=stale_stage,
        staged_rows=5,
    )

    assert _publish(db, _messages(20, "replacement")) == 20
    assert db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (stale_stage,)
    ).fetchone()[0] == 0
    assert stale_stage not in _staging_sessions(db)
    assert _marker(db) is None
    assert len(_live(db)) == 20
    db.close()


def test_malformed_marker_is_a_conflict_not_adopted(tmp_path):
    db = _mkdb(tmp_path)
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO state_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_publication_marker_key(SESSION_ID), "{not json"),
        )
    )
    with pytest.raises(PublicationConflictError):
        _publish(db, _messages(10, "replacement"))
    db.close()


def test_known_holder_must_still_own_the_compression_lease(tmp_path):
    db = _mkdb(tmp_path)
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT OR REPLACE INTO compression_locks "
            "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (SESSION_ID, "pid=1:other:compressor", time.time(), time.time() + 300.0),
        )
    )
    with pytest.raises(Exception) as excinfo:
        _publish(db, _messages(20, "replacement"), known_holder="pid=1:mine:compressor")
    assert "another writer" in str(excinfo.value) or "lease" in str(excinfo.value)
    assert _live(db) and len(_live(db)) == 6, "publication proceeded under a foreign lease"
    assert _marker(db) is None
    db.close()


def test_publication_never_steals_or_extends_the_native_lease(tmp_path):
    db = _mkdb(tmp_path)
    holder = "pid=1:mine:compressor"
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT OR REPLACE INTO compression_locks "
            "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (SESSION_ID, holder, 100.0, time.time() + 300.0),
        )
    )
    before = db._conn.execute(
        "SELECT holder, acquired_at, expires_at FROM compression_locks WHERE session_id = ?",
        (SESSION_ID,),
    ).fetchone()
    assert _publish(db, _messages(20, "replacement"), known_holder=holder) == 20
    after = db._conn.execute(
        "SELECT holder, acquired_at, expires_at FROM compression_locks WHERE session_id = ?",
        (SESSION_ID,),
    ).fetchone()
    assert tuple(before) == tuple(after), "publication touched the native lease"
    db.close()


# ── preservation: archive/search/counters/config/rewind ────────────────────


def test_publication_preserves_archive_search_and_config_semantics(tmp_path):
    db = _mkdb(tmp_path)
    db.create_session(OTHER_SESSION, source="tui")
    db.append_messages_batch(OTHER_SESSION, _messages(3, "unrelated", tool=False))
    second_row_id = db._conn.execute(
        "SELECT id FROM messages WHERE session_id = ? ORDER BY id LIMIT 1 OFFSET 1",
        (OTHER_SESSION,),
    ).fetchone()[0]
    db.rewind_to_message(OTHER_SESSION, second_row_id)
    rewind_before = db.get_messages(OTHER_SESSION, include_inactive=True)
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET model_config = ? WHERE id = ?",
            (json.dumps({"keep": "value", MODEL_KEY: "old"}), SESSION_ID),
        )
    )
    replacement = _messages(40, "new-needle")
    assert (
        db.archive_and_compact(
            SESSION_ID, replacement, model_config_patch={MODEL_KEY: None}
        )
        == 40
    )
    config = json.loads(
        db._conn.execute(
            "SELECT model_config FROM sessions WHERE id = ?", (SESSION_ID,)
        ).fetchone()[0]
    )
    assert config == {"keep": "value"}, "None patch value did not remove its key"
    assert db.search_messages("new-needle"), "published rows are not searchable"
    assert db.search_messages("old tool"), "archived prefix lost discoverability"
    archived = db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 0 AND compacted = 1",
        (SESSION_ID,),
    ).fetchone()[0]
    assert archived == 6
    assert db.get_messages(OTHER_SESSION, include_inactive=True) == rewind_before, (
        "unrelated rewind rows were touched"
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 0 AND compacted = 0",
        (OTHER_SESSION,),
    ).fetchone()[0] == 2, "a rewind row was swept by cleanup"
    db.close()


def test_row_ids_are_stamped_only_after_a_successful_publication(tmp_path):
    db = _mkdb(tmp_path)
    messages = _messages(12, "replacement")
    assert all("_row_id" not in message for message in messages)
    _publish(db, messages)
    assert all(isinstance(message.get("_row_id"), int) for message in messages)
    db_ids = {
        row[0]
        for row in db._conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND active = 1",
            (SESSION_ID,),
        ).fetchall()
    }
    assert {message["_row_id"] for message in messages} == db_ids, (
        "stamped row ids do not match the published rows"
    )

    failing = _messages(4, "boom")
    original_prepare = SessionDB._prepare_message_row

    def failing_prepare(self, msg, now_ts):
        raise RuntimeError("ts382 injected preparation failure")

    SessionDB._prepare_message_row = failing_prepare
    try:
        with pytest.raises(RuntimeError):
            _publish(db, failing)
    finally:
        SessionDB._prepare_message_row = original_prepare
    assert all("_row_id" not in message for message in failing)
    db.close()


def test_no_new_tables_columns_or_schema_version(tmp_path):
    db = _mkdb(tmp_path)
    before = db._conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    version_before = db._conn.execute("PRAGMA user_version").fetchone()[0]
    _publish(db, _messages(30, "replacement"))
    after = db._conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after], (
        "the publication changed the schema"
    )
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == version_before
    db.close()


# ── caller propagation contract ────────────────────────────────────────────


def test_micro_compaction_caller_inherits_bound_and_stamps_only_on_success(tmp_path):
    """The unconfigured micro-compaction sync is a publication caller too.

    It must inherit the bounded writer (several short transactions, not one
    unbounded rewrite) and stamp ``_DB_PERSISTED_MARKER`` only after the
    publication actually succeeded.
    """
    from agent.context_compressor import ContextCompressor, _DB_PERSISTED_MARKER

    db = _mkdb(tmp_path)
    compressor = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=1,
        protect_last_n=2,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    compressor._session_db = db
    compressor._session_id = SESSION_ID
    compacted = _messages(300, "micro")

    calls = {"n": 0}
    original = db._execute_write

    def counting(fn, patience_s=None):
        calls["n"] += 1
        return original(fn, patience_s=patience_s)

    db._execute_write = counting
    compressor._sync_micro_compact_to_db(compacted)
    db._execute_write = original
    assert calls["n"] >= 3, "micro-compaction sync did not use the bounded writer"
    assert all(message.get(_DB_PERSISTED_MARKER) for message in compacted), (
        "successful publication did not stamp the persisted markers"
    )
    assert len(_live(db)) == 300

    # Failure path: no marker may be stamped and the live view stays intact.
    failing = _messages(10, "micro-failure")
    original_insert = db._insert_message_rows

    def failing_insert(conn, session_id, messages):
        raise RuntimeError("ts382 injected staging failure")

    db._insert_message_rows = failing_insert
    try:
        compressor._sync_micro_compact_to_db(failing)
    finally:
        db._insert_message_rows = original_insert
    assert not any(message.get(_DB_PERSISTED_MARKER) for message in failing), (
        "a failed publication stamped the persisted markers"
    )
    assert len(_live(db)) == 300
    assert _marker(db) is None
    db.close()


def test_invoke_archive_and_compact_feature_detects_private_context(tmp_path):
    db = _mkdb(tmp_path)
    holder = "pid=1:mine:compressor"
    db._execute_write(
        lambda conn: conn.execute(
            "INSERT OR REPLACE INTO compression_locks "
            "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (SESSION_ID, holder, time.time(), time.time() + 300.0),
        )
    )
    calls: List[Dict[str, Any]] = []
    original = db.archive_and_compact

    def recording(session_id, messages, model_config_patch=None, **kwargs):
        calls.append(kwargs)
        return original(
            session_id, messages, model_config_patch=model_config_patch, **kwargs
        )

    db.archive_and_compact = recording
    invoke_archive_and_compact(
        db,
        SESSION_ID,
        _messages(8, "replacement"),
        model_config_patch={"k": 1},
        known_holder=holder,
    )
    assert calls and calls[0].get("known_holder") == holder

    class DuckStore:
        def __init__(self):
            self.seen: Dict[str, Any] = {}

        def archive_and_compact(self, session_id, messages, model_config_patch=None):
            self.seen = {"model_config_patch": model_config_patch}
            return len(messages)

    duck = DuckStore()
    assert (
        invoke_archive_and_compact(
            duck,
            "s",
            [{"role": "user"}],
            model_config_patch={"k": 1},
            known_holder="pid=1:mine:compressor",
            snapshot=PublicationSnapshot("s", 0, 0, "d", True),
        )
        == 1
    )
    assert duck.seen == {"model_config_patch": {"k": 1}}, (
        "a duck-typed store received arguments it does not accept"
    )
    db.close()
