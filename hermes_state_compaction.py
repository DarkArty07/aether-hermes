"""Bounded, atomic transcript publication for ``SessionDB`` (TS-382).

Implements the recorded design at
``specs/006-tools-memory-stability/TS-382-design.md`` decisions D1-D7: an
``archive_and_compact`` publication stages its replacement rows in a hidden
internal session, commits them in bounded batches, relocates concurrent
interleaved appends, and publishes with one metadata-only cutover transaction.
The store's single write lock is therefore never held for the whole rewritten
transcript (which is what starved concurrent transcript appends and lease
refreshes — see ``evidence/TS-382-research.md`` and ``evidence/TS-382.md``).

Mixin contract: this is a plain mixin class consumed by
``hermes_state.SessionDB`` (like ``hermes_state_search``). It defines no
``__init__`` and no per-instance state of its own; methods use the host's
attributes (``self._conn``, ``self._lock``, ``self._execute_write``,
``self._insert_message_rows``, ``self._merge_model_config_json``, ...)
established by ``SessionDB.__init__``. Module-level imports must not include
``hermes_state`` (cycle) — the host-defined busy error this module raises on a
lost compression lease is resolved lazily inside that cold branch.

Everything here is durable local bookkeeping in the EXISTING database: no new
table/column, schema version, attached store, queue, service, reaper or live
maintenance. Staging rows are ordinary ``messages`` rows of a hidden internal
session (``source='hermes-compaction-stage-v1'``, ``hidden=1``, ``archived=1``,
reserved ``hermes-compaction-stage-v1-*`` id) committed with
``active = 0, compacted = 0`` — the existing rewind marking every public read
path already hides.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from hermes_state_common import (
    COMPACTION_PUBLICATION_BATCH_BYTES,
    COMPACTION_PUBLICATION_BATCH_ROWS,
    COMPACTION_PUBLICATION_LEASE_S,
    COMPACTION_PUBLICATION_MARKER_PREFIX,
    COMPACTION_PUBLICATION_MAX_ROUNDS,
    COMPACTION_PUBLICATION_VERSION,
    COMPACTION_PUBLICATION_YIELD_S,
    COMPACTION_STAGING_SESSION_PREFIX,
    COMPACTION_STAGING_SESSION_SOURCE,
)

# Moved methods logged under the "hermes_state" logger before the split; keep
# that logger identity so log filtering/capture behavior is unchanged.
logger = logging.getLogger("hermes_state")

ConnT = sqlite3.Connection


class PublicationConflictError(RuntimeError):
    """A publication could not proceed against the store's current state.

    Raised when the durable per-target ownership is held by another live
    publication, when the stable source prefix was invalidated by a concurrent
    writer, when the generation's staged identity no longer agrees with the
    store, or when the bounded catch-up/validation round budget is exhausted
    under continuous same-session mutation. Callers keep the original
    transcript (and every committed append); nothing is published.
    """


class PublicationLeaseLostError(PublicationConflictError):
    """The compression lease that authorized an in-place publication moved on."""


class _VersionFenceTrippedError(RuntimeError):
    """Internal: an external commit landed between validation and BEGIN."""


# ``_insert_message_rows`` owns the canonical INSERT (session_id first). This
# is the same column list, used to read stored rows back for relocation and
# prefix digests. ``_assert_copy_columns_known`` fails the publication rather
# than silently dropping a column someone later adds to ``messages``.
_MESSAGE_INSERT_COLUMNS: Tuple[str, ...] = (
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "effect_disposition",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "platform_message_id",
    "observed",
    "active",
    "compacted",
    "api_content",
    "display_kind",
    "display_metadata",
)

_TOOL_CALLS_INDEX = _MESSAGE_INSERT_COLUMNS.index("tool_calls")

#: Marker row values are private local runtime metadata, never public logs.
_MARKER_PHASE_STAGING = "staging"
_MARKER_PHASE_CLEANUP = "cleanup_failed"

#: Maximum ids bound into one metadata UPDATE inside the cutover transaction
#: (portable across SQLite builds with a 999-variable limit and far below the
#: modern 32766 default).
_ACTIVATION_ID_CHUNK = 480


class PublicationSnapshot:
    """Immutable identity of the target prefix a publication replaces.

    Produced by :meth:`SessionCompactionPublicationMixin.capture_publication_snapshot`
    and re-validated by the writer immediately before the cutover. It is a
    private, non-durable fingerprint (row count + digest of the persisted row
    values) — not a copy of transcript content and not model-supplied identity.
    """

    __slots__ = ("target", "watermark", "row_count", "digest", "target_exists")

    def __init__(
        self,
        target: str,
        watermark: int,
        row_count: int,
        digest: str,
        target_exists: bool,
    ) -> None:
        self.target = target
        self.watermark = int(watermark)
        self.row_count = int(row_count)
        self.digest = digest
        self.target_exists = bool(target_exists)


class _PreparedMessage:
    """Private, self-contained row plan for one transcript message.

    Built OUTSIDE the SQLite writer transaction so per-row encoding/scrubbing
    happens once, then inserted by ``SessionDB._insert_message_rows`` through
    its existing seam. ``values`` is already in INSERT order (session_id is
    passed separately by the caller of the seam). The caller's own dict is
    never mutated while the publication is in flight: ``source`` is stamped
    with ``_row_id`` only after a successful cutover.
    """

    __slots__ = ("values", "tool_call_count", "row_id", "source", "size_bytes")

    def __init__(
        self,
        values: Tuple[Any, ...],
        tool_call_count: int,
        source: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.values = values
        self.tool_call_count = int(tool_call_count)
        self.row_id: Optional[int] = None
        self.source = source
        self.size_bytes = _row_payload_bytes(values)


def _row_payload_bytes(values: Sequence[Any]) -> int:
    """Approximate serialized size of a prepared row for the batch budget."""
    total = 0
    for value in values:
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, (bytes, bytearray)):
            total += len(value)
        else:
            total += 8
    return total


def _tool_call_count_from_stored(value: Any) -> int:
    """Exact tool-call count of a STORED ``tool_calls`` column value.

    ``_insert_message_rows`` counts a live Python list per element; a stored
    row holds the serialized JSON text, which must be parsed back to keep the
    relocated tail's counter contribution exact (D4).
    """
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return 1
    if isinstance(value, list):
        return len(value)
    if value is None:
        return 0
    return 1


def _publication_marker_key(session_id: str) -> str:
    """Durable per-target ownership key: prefix + SHA-256 of the exact id."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{COMPACTION_PUBLICATION_MARKER_PREFIX}{digest}"


def _publication_owner_token() -> str:
    """Structured owner token, matching the native lock-holder convention.

    ``pid=<n>`` first so the native dead-local-process probe (and an operator
    reading diagnostics) can tell a crashed publication owner from a live one.
    """
    return (
        f"pid={os.getpid()}:tid={threading.get_ident()}:"
        f"compaction-publication:{secrets.token_hex(8)}"
    )


def _parse_publication_marker(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _serialize_publication_marker(marker: Dict[str, Any]) -> str:
    return json.dumps(marker, sort_keys=True, separators=(",", ":"))


def _iter_publication_batches(
    rows: Sequence[_PreparedMessage],
    max_rows: int = COMPACTION_PUBLICATION_BATCH_ROWS,
    max_bytes: int = COMPACTION_PUBLICATION_BATCH_BYTES,
) -> Iterable[List[_PreparedMessage]]:
    """Yield bounded row batches: ``max_rows`` or ``max_bytes``, whichever first.

    A single already-supported oversized row is yielded alone — never
    truncated and never rejected by a new size limit (D3).
    """
    batch: List[_PreparedMessage] = []
    batch_bytes = 0
    for row in rows:
        row_bytes = row.size_bytes
        if batch and (len(batch) >= max_rows or batch_bytes + row_bytes > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(row)
        batch_bytes += row_bytes
    if batch:
        yield batch


def _chunk_ids(
    ids: Sequence[int], chunk_rows: int = COMPACTION_PUBLICATION_BATCH_ROWS
) -> Iterable[Sequence[int]]:
    for start in range(0, len(ids), chunk_rows):
        yield ids[start : start + chunk_rows]


def _callable_accepts_keyword(fn: Any, name: str) -> bool:
    """True when *fn* declares keyword *name* (or takes ``**kwargs``)."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C callables: stay conservative
        return False
    for parameter in signature.parameters.values():
        if parameter.name == name:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def invoke_archive_and_compact(
    session_db: Any,
    session_id: str,
    compacted_messages: List[Dict[str, Any]],
    *,
    model_config_patch: Optional[Dict[str, Any]] = None,
    known_holder: Optional[str] = None,
    snapshot: Optional["PublicationSnapshot"] = None,
) -> int:
    """Call ``session_db.archive_and_compact`` with private publication context.

    Native callers pass the compression lease they already own (and a snapshot
    when they captured one before computing the replacement). Duck-typed
    third-party stores keep receiving exactly the historic call shape: the
    publication keyword arguments are forwarded ONLY when the resolved callable
    declares them, so an out-of-tree store never sees an unexpected argument.
    """
    fn = getattr(session_db, "archive_and_compact", None)
    if not callable(fn):
        raise TypeError("session store does not support archive_and_compact")
    extra: Dict[str, Any] = {}
    for name, value in (("snapshot", snapshot), ("known_holder", known_holder)):
        if value is not None and _callable_accepts_keyword(fn, name):
            extra[name] = value
    return fn(
        session_id,
        compacted_messages,
        model_config_patch=model_config_patch,
        **extra,
    )


class SessionCompactionPublicationMixin:
    """Bounded publication half of ``archive_and_compact`` (TS-382 D1-D7)."""

    # ── Public entry points ────────────────────────────────────────────────

    def capture_publication_snapshot(self, session_id: str) -> PublicationSnapshot:
        """Fingerprint the target's live prefix before a replacement is built.

        Native callers may capture this before they compute the replacement so
        the writer can prove the prefix it archives is the one the replacement
        was derived from. Without it the writer captures the same snapshot at
        method entry. Read-only; needs no writer lock.
        """
        with self._read_ctx() as conn:
            exists = (
                conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
                ).fetchone()
                is not None
            )
            watermark_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            watermark = int(watermark_row[0] or 0)
            row_count, digest = self._digest_publication_prefix(
                conn, session_id, watermark
            )
        return PublicationSnapshot(session_id, watermark, row_count, digest, exists)

    def archive_and_compact(
        self,
        session_id: str,
        compacted_messages: List[Dict[str, Any]],
        model_config_patch: Optional[Dict[str, Any]] = None,
        *,
        snapshot: Optional[PublicationSnapshot] = None,
        known_holder: Optional[str] = None,
    ) -> int:
        """Non-destructive in-place compaction for a single durable session id.

        Soft-archives every currently-active message (``active = 0,
        compacted = 1``) and inserts *compacted_messages* as fresh active rows —
        atomically, in one final metadata-only cutover transaction. The
        conversation keeps ONE session id for life (#38763) WITHOUT destroying
        history:

        - The live-context load (:meth:`get_messages_as_conversation`,
          :meth:`get_messages`) filters ``active = 1`` by default, so the model
          reloads ONLY the compacted set.
        - The archived pre-compaction turns stay on disk (active=0) and stay
          DISCOVERABLE: they are marked compacted=1, and search_messages()
          includes compacted=1 rows by default — so session_search still finds
          them, unlike rewind/undo rows (active=0, compacted=0) which stay
          hidden. They remain in the FTS index (the messages_fts* triggers
          index on INSERT / drop on DELETE and don't key on active/compacted;
          flipping to active=0 is a content-preserving UPDATE) and are
          recoverable via get_messages(..., include_inactive=True).

        This is the durability-preserving alternative to :meth:`replace_messages`
        for compaction. ``message_count`` is set to the ACTIVE (compacted) count,
        matching what the live load returns. ``model_config_patch`` is merged
        into the session's JSON config in the same cutover transaction; a
        ``None`` value removes that key. Returns the new active count.

        The publication is BOUNDED (TS-382): the rewritten transcript is
        staged in hidden rows through several short transactions and published
        by one metadata-only cutover, so the store's single write lock is never
        held for the whole replacement. Concurrent appends that land during
        staging are preserved exactly once, in append order, after the
        replacement. ``known_holder`` carries the caller's own compression
        lease holder when the caller already owns one (in-place compaction);
        the writer verifies it in every batch and in the cutover but never
        acquires, steals, releases or extends a compression/turn lease itself.
        """
        if not session_id:
            raise ValueError("archive_and_compact requires a session_id")

        target_snapshot = snapshot or self.capture_publication_snapshot(session_id)
        if target_snapshot.target != session_id:
            raise PublicationConflictError(
                "publication snapshot does not match the target session"
            )

        marker_key = _publication_marker_key(session_id)
        owner = _publication_owner_token()
        stage_id = f"{COMPACTION_STAGING_SESSION_PREFIX}{secrets.token_hex(16)}"
        prepared = self._prepare_publication_rows(compacted_messages)

        marker: Dict[str, Any] = {
            "v": COMPACTION_PUBLICATION_VERSION,
            "target": session_id,
            "stage": stage_id,
            "owner": owner,
            "expires_at": time.time() + COMPACTION_PUBLICATION_LEASE_S,
            "watermark": target_snapshot.watermark,
            "source_digest": target_snapshot.digest,
            "source_rows": target_snapshot.row_count,
            "phase": _MARKER_PHASE_STAGING,
            "staged_rows": 0,
            "staged_tool_calls": 0,
            "staged_max_id": None,
            "tail_watermark": target_snapshot.watermark,
            "tail_rows": 0,
            "tail_tool_calls": 0,
        }

        self._claim_publication(marker_key, marker)

        try:
            marker = self._stage_publication_rows(
                marker_key, marker, prepared, known_holder
            )
            active_count = self._finalize_publication(
                marker_key,
                marker,
                target_snapshot,
                known_holder,
                model_config_patch,
            )
        except BaseException as exc:  # noqa: BLE001 - cleanup must not mask it
            self._abandon_publication(marker_key, marker, exc)
            raise

        # Post-commit bookkeeping only: nothing below may fail the publication
        # or rewrite the published view (D6).
        try:
            for row in prepared:
                if row.source is not None and row.row_id is not None:
                    row.source["_row_id"] = row.row_id
        except Exception:  # pragma: no cover - a caller dict refusing writes
            logger.debug("post-publication row-id stamping failed", exc_info=True)
        return active_count

    # ── Preparation ────────────────────────────────────────────────────────

    def _prepare_publication_rows(
        self,
        messages: Sequence[Any],
        *,
        with_source: bool = True,
    ) -> List[_PreparedMessage]:
        """Encode *messages* into private prepared rows, outside any txn."""
        prepared: List[_PreparedMessage] = []
        now_ts = time.time()
        for message in messages:
            if isinstance(message, _PreparedMessage):
                prepared.append(message)
                continue
            row, now_ts = self._prepare_message_row(message, now_ts)
            if with_source and isinstance(message, dict):
                row.source = message
            prepared.append(row)
        return prepared

    # ── Ownership claim / lazy reclamation ─────────────────────────────────

    def _read_publication_marker_row(
        self, marker_key: str
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Read the durable ownership row: ``(present, raw, parsed)``.

        ``present`` distinguishes "no publication" from "a row this code cannot
        understand": unknown/malformed ownership is an explicit conflict, never
        adopted and never silently replaced (D1).
        """
        with self._read_ctx() as conn:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (marker_key,)
            ).fetchone()
        if row is None:
            return False, None, None
        raw = row[0]
        return True, str(raw), _parse_publication_marker(raw)

    def _read_publication_marker(self, marker_key: str) -> Optional[Dict[str, Any]]:
        present, _raw, parsed = self._read_publication_marker_row(marker_key)
        return parsed if present else None

    def _marker_is_reclaimable(self, marker: Dict[str, Any]) -> bool:
        """True only for an expired lease or a proven-dead local owner (D1).

        A fresh/live owner is never reaped merely by age, a different caller or
        a malformed marker; unknown or incomplete ownership is treated as a
        conflict and left alone.
        """
        owner = marker.get("owner")
        stage = marker.get("stage")
        if not isinstance(owner, str) or not isinstance(stage, str):
            return False
        try:
            expired = float(marker.get("expires_at")) < time.time()
        except (TypeError, ValueError):
            return False
        if expired:
            return True
        return bool(self._compaction_owner_process_is_dead(owner))

    def _claim_publication(self, marker_key: str, marker: Dict[str, Any]) -> None:
        """Reclaim a dead generation if needed, then claim ownership atomically."""
        present, _raw, existing = self._read_publication_marker_row(marker_key)
        if present:
            if existing is None:
                raise PublicationConflictError(
                    "publication marker is unreadable; refusing to adopt or "
                    "replace unknown ownership"
                )
            if not self._marker_is_reclaimable(existing):
                raise PublicationConflictError(
                    "another live publication owns this session's transcript "
                    "publication marker"
                )
            self._reap_publication_generation(marker_key, existing)

        def _do(conn: ConnT) -> None:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (marker_key,)
            ).fetchone()
            if row is not None:
                current = _parse_publication_marker(row[0])
                if current is None:
                    raise PublicationConflictError(
                        "publication marker is unreadable; refusing to adopt or "
                        "replace unknown ownership"
                    )
                if not self._marker_is_reclaimable(current):
                    raise PublicationConflictError(
                        "another live publication claimed this session while this "
                        "one was preparing"
                    )
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, message_count, "
                "tool_call_count, model_config, hidden, archived) "
                "VALUES (?, ?, ?, 0, 0, ?, 1, 1)",
                (
                    marker["stage"],
                    COMPACTION_STAGING_SESSION_SOURCE,
                    time.time(),
                    _serialize_publication_marker(
                        {
                            "v": COMPACTION_PUBLICATION_VERSION,
                            "target": marker["target"],
                            "owner": marker["owner"],
                        }
                    ),
                ),
            )
            self._write_publication_marker(conn, marker_key, marker)

        self._execute_write(_do)

    def _reap_publication_generation(
        self, marker_key: str, marker: Dict[str, Any]
    ) -> None:
        """Delete ONLY this exact generation's hidden rows/session/marker (D6)."""
        stage = marker.get("stage")
        if not isinstance(stage, str):
            raise PublicationConflictError(
                "unreadable publication generation left in place for its owner"
            )
        self._delete_publication_rows(stage)
        raw_marker = _serialize_publication_marker(marker)

        def _do(conn: ConnT) -> None:
            remaining = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (stage,)
            ).fetchone()
            if remaining is not None:
                raise PublicationConflictError(
                    "publication generation still holds rows; not reaping"
                )
            conn.execute(
                "DELETE FROM sessions WHERE id = ? AND source = ?",
                (stage, COMPACTION_STAGING_SESSION_SOURCE),
            )
            conn.execute(
                "DELETE FROM state_meta WHERE key = ? AND value = ?",
                (marker_key, raw_marker),
            )

        self._execute_write(_do)

    def _delete_publication_rows(
        self, stage: str, batch_rows: int = COMPACTION_PUBLICATION_BATCH_ROWS
    ) -> None:
        """Delete one generation's rows in bounded batches, releasing the lock."""
        while True:
            deleted = self._execute_write(
                lambda conn, stage=stage, batch_rows=batch_rows: conn.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "SELECT id FROM messages WHERE session_id = ? "
                    "ORDER BY id LIMIT ?)",
                    (stage, batch_rows),
                ).rowcount
            )
            if deleted < batch_rows:
                return
            time.sleep(COMPACTION_PUBLICATION_YIELD_S)

    # ── Staging ────────────────────────────────────────────────────────────

    def _stage_publication_rows(
        self,
        marker_key: str,
        marker: Dict[str, Any],
        prepared: Sequence[_PreparedMessage],
        known_holder: Optional[str],
    ) -> Dict[str, Any]:
        current = marker
        for batch in _iter_publication_batches(prepared):
            current = self._execute_write(
                lambda conn, batch=batch, current=current: self._stage_publication_batch(
                    conn, marker_key, current, batch, known_holder
                )
            )
            # Release both the instance lock and SQLite's writer slot, then
            # yield so same-instance and separate-instance writers get an
            # opportunity between batches (D3).
            time.sleep(COMPACTION_PUBLICATION_YIELD_S)
        return current

    def _stage_publication_batch(
        self,
        conn: ConnT,
        marker_key: str,
        base_marker: Dict[str, Any],
        batch: Sequence[_PreparedMessage],
        known_holder: Optional[str],
    ) -> Dict[str, Any]:
        marker, raw = self._owned_publication_row(
            conn, marker_key, base_marker["owner"]
        )
        self._verify_known_holder(conn, marker["target"], known_holder)
        inserted, tool_calls = self._insert_message_rows(
            conn, marker["stage"], list(batch)
        )
        # ``_insert_message_rows`` inserts live rows; staging rows must be
        # inactive before this batch commits and no reader may observe the
        # intermediate state (same transaction, so it cannot).
        conn.execute(
            "UPDATE messages SET active = 0, compacted = 0 "
            "WHERE session_id = ? AND active = 1",
            (marker["stage"],),
        )
        marker["staged_rows"] = int(marker.get("staged_rows") or 0) + inserted
        marker["staged_tool_calls"] = (
            int(marker.get("staged_tool_calls") or 0) + tool_calls
        )
        staged_ids = [row.row_id for row in batch if row.row_id is not None]
        if staged_ids:
            marker["staged_max_id"] = max(
                int(marker.get("staged_max_id") or 0), max(staged_ids)
            )
        marker["phase"] = _MARKER_PHASE_STAGING
        self._renew_publication_marker(conn, marker_key, raw, marker)
        return marker

    # ── Tail preservation, validation and cutover ──────────────────────────

    def _finalize_publication(
        self,
        marker_key: str,
        marker: Dict[str, Any],
        snapshot: PublicationSnapshot,
        known_holder: Optional[str],
        model_config_patch: Optional[Dict[str, Any]],
    ) -> int:
        current = marker
        rounds = 0
        while True:
            rounds += 1
            if rounds > COMPACTION_PUBLICATION_MAX_ROUNDS:
                raise PublicationConflictError(
                    "publication could not reach a stable tail within its "
                    "bounded catch-up/validation rounds"
                )
            if self._copy_publication_tail(marker_key, current, known_holder) is not None:
                current = self._read_publication_marker(marker_key) or current
                continue
            try:
                return self._publish_publication_cutover(
                    marker_key,
                    current,
                    snapshot,
                    known_holder,
                    model_config_patch,
                )
            except _VersionFenceTrippedError:
                # An external commit slipped in after validation. Re-plan and
                # re-validate within the same bounded round budget (D5/D4).
                continue

    def _copy_publication_tail(
        self,
        marker_key: str,
        marker: Dict[str, Any],
        known_holder: Optional[str],
    ) -> Optional[int]:
        """Relocate target rows interleaved with the staged ids, exactly once.

        Target rows whose ids exceed the last staged id already follow the
        whole staged set and stay in place. Returns the number of relocated
        rows, or ``None`` when there is nothing to relocate (the caller then
        validates and publishes).
        """
        staged_max_id = marker.get("staged_max_id")
        if staged_max_id is None:
            return None
        lower = max(int(marker.get("tail_watermark") or 0), int(marker["watermark"]))
        pending = self._read_interleaved_tail_ids(
            marker["target"], lower, int(staged_max_id)
        )
        if not pending:
            return None

        relocated = 0
        for id_batch in _chunk_ids(pending):
            marker = self._execute_write(
                lambda conn, id_batch=id_batch, marker=marker: self._relocate_tail_batch(
                    conn, marker_key, marker, id_batch, known_holder
                )
            )
            relocated += len(id_batch)
            time.sleep(COMPACTION_PUBLICATION_YIELD_S)
        return relocated

    def _read_interleaved_tail_ids(
        self, target: str, lower: int, upper: int
    ) -> List[int]:
        if upper <= lower:
            return []
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND id > ? "
                "AND id <= ? AND active = 1 ORDER BY id",
                (target, lower, upper),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def _relocate_tail_batch(
        self,
        conn: ConnT,
        marker_key: str,
        base_marker: Dict[str, Any],
        message_ids: Sequence[int],
        known_holder: Optional[str],
    ) -> Dict[str, Any]:
        marker, raw = self._owned_publication_row(
            conn, marker_key, base_marker["owner"]
        )
        self._verify_known_holder(conn, marker["target"], known_holder)
        self._assert_copy_columns_known(conn)
        columns = ", ".join(_MESSAGE_INSERT_COLUMNS)
        staged: List[_PreparedMessage] = []
        for message_id in message_ids:
            row = conn.execute(
                f"SELECT {columns} FROM messages WHERE id = ? AND session_id = ?",
                (message_id, marker["target"]),
            ).fetchone()
            if row is None:
                # The row vanished (concurrent delete/replace) — it is no longer
                # part of the live tail and must not be copied.
                continue
            values = tuple(row[index] for index in range(len(_MESSAGE_INSERT_COLUMNS)))
            staged.append(
                _PreparedMessage(
                    values,
                    _tool_call_count_from_stored(values[_TOOL_CALLS_INDEX]),
                )
            )
        if not staged:
            return marker
        inserted, tool_calls = self._insert_message_rows(conn, marker["stage"], staged)
        # Relocated copies preserve their STORED visibility flags (an interleaved
        # live row is active=1, compacted as stored); only activation is parked
        # while they sit in staging.
        conn.execute(
            "UPDATE messages SET active = 0 "
            "WHERE session_id = ? AND active = 1",
            (marker["stage"],),
        )
        staged_ids = [row.row_id for row in staged if row.row_id is not None]
        if staged_ids:
            marker["staged_max_id"] = max(
                int(marker.get("staged_max_id") or 0), max(staged_ids)
            )
        marker["tail_watermark"] = max(
            int(marker.get("tail_watermark") or 0), max(int(i) for i in message_ids)
        )
        marker["tail_rows"] = int(marker.get("tail_rows") or 0) + inserted
        marker["tail_tool_calls"] = (
            int(marker.get("tail_tool_calls") or 0) + tool_calls
        )
        self._renew_publication_marker(conn, marker_key, raw, marker)
        return marker

    def _publish_publication_cutover(
        self,
        marker_key: str,
        marker: Dict[str, Any],
        snapshot: PublicationSnapshot,
        known_holder: Optional[str],
        model_config_patch: Optional[Dict[str, Any]],
    ) -> int:
        """Validate outside a writer transaction, then publish in one cutover."""
        state: Dict[str, Any] = {}

        def pre_begin() -> None:
            # Runs under the instance lock, BEFORE BEGIN IMMEDIATE: no local
            # writer can interleave between validation and the cutover, and the
            # connection's data_version fences an external commit.
            version = self._read_data_version()
            plan = self._validate_publication_plan(
                marker_key, marker, snapshot, known_holder
            )
            if self._read_data_version() != version:
                raise _VersionFenceTrippedError()
            state["version"] = version
            state["plan"] = plan

        def _cutover(conn: ConnT) -> int:
            if self._read_data_version() != state.get("version"):
                # An external commit landed between validation and BEGIN; the
                # validated plan is stale and must not be mutated against (D5).
                raise _VersionFenceTrippedError()
            plan = state["plan"]
            current, _raw = self._owned_publication_row(
                conn, marker_key, marker["owner"]
            )
            self._verify_known_holder(conn, current["target"], known_holder)
            if plan["target_exists"] and not self._target_row_exists(
                conn, current["target"]
            ):
                raise PublicationConflictError(
                    "target session row disappeared during publication"
                )
            staged_ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM messages WHERE session_id = ? ORDER BY id",
                    (current["stage"],),
                ).fetchall()
            ]
            if len(staged_ids) != int(plan["staged_rows"]):
                raise PublicationConflictError(
                    "staged generation identity no longer matches the store"
                )
            # 1. Archive the proven source prefix (metadata-only: no update
            #    trigger keys on active/compacted, so no FTS work happens here).
            conn.execute(
                "UPDATE messages SET active = 0, compacted = 1 "
                "WHERE session_id = ? AND id <= ? AND active = 1",
                (current["target"], snapshot.watermark),
            )
            # 2. Hide the exact relocated-tail originals: they were copied into
            #    the staged set, so the originals are recoverable (active=0,
            #    compacted=0) rather than duplicated into the live transcript.
            tail_watermark = int(
                current.get("tail_watermark") or snapshot.watermark
            )
            if tail_watermark > snapshot.watermark:
                conn.execute(
                    "UPDATE messages SET active = 0, compacted = 0 "
                    "WHERE session_id = ? AND id > ? AND id <= ? AND active = 1",
                    (current["target"], snapshot.watermark, tail_watermark),
                )
            # 3. Move the exact staging generation onto the target and make it
            #    live; unrelocated tail rows stay active where they are.
            moved = 0
            for id_batch in _chunk_ids(staged_ids, _ACTIVATION_ID_CHUNK):
                placeholders = ",".join("?" for _ in id_batch)
                moved += conn.execute(
                    "UPDATE messages SET session_id = ?, active = 1 "
                    f"WHERE session_id = ? AND id IN ({placeholders})",
                    (current["target"], current["stage"], *id_batch),
                ).rowcount
            if int(moved) != int(plan["staged_rows"]):
                raise PublicationConflictError(
                    "staged row count changed during cutover"
                )
            # 4. Reconcile live counters and merge the model_config patch
            #    against the CURRENT target config (None removes a key).
            live_count = int(plan["staged_rows"]) + int(plan["unrelocated_rows"])
            tool_calls = int(plan["staged_tool_calls"]) + int(
                plan["unrelocated_tool_calls"]
            )
            if model_config_patch is None:
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ? "
                    "WHERE id = ?",
                    (live_count, tool_calls, current["target"]),
                )
            else:
                patched = self._merge_model_config_json(
                    conn, current["target"], model_config_patch, on_missing="raise"
                )
                conn.execute(
                    "UPDATE sessions SET message_count = ?, tool_call_count = ?, "
                    "model_config = ? WHERE id = ?",
                    (live_count, tool_calls, patched, current["target"]),
                )
            # 5. Remove the now-empty staging session and this operation marker.
            conn.execute(
                "DELETE FROM sessions WHERE id = ? AND source = ?",
                (current["stage"], COMPACTION_STAGING_SESSION_SOURCE),
            )
            conn.execute(
                "DELETE FROM state_meta WHERE key = ? AND value = ?",
                (marker_key, _serialize_publication_marker(current)),
            )
            return live_count

        # A private pre-BEGIN preflight recognized by ``_execute_write``: it
        # runs under the existing instance lock before BEGIN, so it never nests
        # a non-reentrant lock and never holds SQLite's writer slot while
        # hashing the transcript.
        _cutover._publication_preflight = pre_begin  # type: ignore[attr-defined]
        return self._execute_write(_cutover)

    def _validate_publication_plan(
        self,
        marker_key: str,
        marker: Dict[str, Any],
        snapshot: PublicationSnapshot,
        known_holder: Optional[str],
    ) -> Dict[str, Any]:
        """Prove the source prefix and staged generation are still the ones we own.

        Runs read-only on the writer connection, outside any writer transaction.
        Any disagreement is an explicit conflict: the caller keeps the original
        transcript and every committed append.
        """
        conn = self._conn
        current, _raw = self._owned_publication_row(
            conn, marker_key, marker["owner"]
        )
        if current.get("phase") != _MARKER_PHASE_STAGING:
            raise PublicationConflictError("publication not in staging phase")
        plan = {
            "target_exists": self._target_row_exists(conn, current["target"]),
            # The staging session holds the replacement rows AND the relocated
            # interleaved-tail copies; both become live at the cutover.
            "staged_rows": int(current.get("staged_rows") or 0)
            + int(current.get("tail_rows") or 0),
            "staged_tool_calls": int(current.get("staged_tool_calls") or 0)
            + int(current.get("tail_tool_calls") or 0),
            "unrelocated_rows": 0,
            "unrelocated_tool_calls": 0,
        }
        if plan["target_exists"] != snapshot.target_exists:
            raise PublicationConflictError(
                "target session existence changed during publication"
            )
        # No explicit source snapshot may be silently replaced by a newer one.
        row_count, digest = self._digest_publication_prefix(
            conn, current["target"], snapshot.watermark
        )
        if row_count != snapshot.row_count or digest != snapshot.digest:
            raise PublicationConflictError(
                "source transcript prefix changed during publication"
            )
        staged_rows, staged_max_id, staged_inactive = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0), "
            "COALESCE(SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END), 0) "
            "FROM messages WHERE session_id = ?",
            (current["stage"],),
        ).fetchone()
        if int(staged_rows) != plan["staged_rows"]:
            raise PublicationConflictError(
                "staged row count no longer matches the publication marker"
            )
        if int(staged_inactive) != int(staged_rows):
            raise PublicationConflictError(
                "staged rows are not all inactive; refusing to publish"
            )
        if int(staged_max_id) and int(current.get("staged_max_id") or 0) and int(
            staged_max_id
        ) != int(current["staged_max_id"]):
            raise PublicationConflictError(
                "staged generation id range no longer matches the marker"
            )
        tail_watermark = int(current.get("tail_watermark") or snapshot.watermark)
        unrelocated = conn.execute(
            "SELECT tool_calls FROM messages WHERE session_id = ? AND id > ? "
            "AND active = 1",
            (current["target"], tail_watermark),
        ).fetchall()
        plan["unrelocated_rows"] = len(unrelocated)
        plan["unrelocated_tool_calls"] = sum(
            _tool_call_count_from_stored(row[0]) for row in unrelocated
        )
        if known_holder:
            self._verify_known_holder(conn, current["target"], known_holder)
        return plan

    # ── Host seams ─────────────────────────────────────────────────────────

    def _owned_publication_row(
        self, conn: ConnT, marker_key: str, owner: str
    ) -> Tuple[Dict[str, Any], str]:
        """Re-read the marker inside a transaction and prove we still own it."""
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (marker_key,)
        ).fetchone()
        raw = row[0] if row is not None else None
        marker = _parse_publication_marker(raw)
        if marker is None:
            raise PublicationConflictError("publication marker disappeared")
        if marker.get("owner") != owner:
            raise PublicationConflictError("publication ownership was reassigned")
        try:
            expires_at = float(marker.get("expires_at"))
        except (TypeError, ValueError):
            raise PublicationConflictError("publication marker has no lease")
        if expires_at < time.time():
            raise PublicationConflictError("publication lease expired")
        return marker, str(raw)

    def _renew_publication_marker(
        self,
        conn: ConnT,
        marker_key: str,
        raw_expected: str,
        marker: Dict[str, Any],
    ) -> None:
        """Renew the publication lease, but only against the exact owned row.

        Compare-and-set on the committed value: a reclaimed owner (or any
        other writer that touched the marker) cannot have its lease silently
        extended by a writer that no longer owns the generation.
        """
        renewal = dict(marker)
        renewal["expires_at"] = time.time() + COMPACTION_PUBLICATION_LEASE_S
        updated = conn.execute(
            "UPDATE state_meta SET value = ? WHERE key = ? AND value = ?",
            (_serialize_publication_marker(renewal), marker_key, raw_expected),
        ).rowcount
        if not updated:
            raise PublicationConflictError(
                "publication marker changed underneath this writer"
            )
        marker["expires_at"] = renewal["expires_at"]

    def _write_publication_marker(
        self, conn: ConnT, marker_key: str, marker: Dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (marker_key, _serialize_publication_marker(marker)),
        )

    def _abandon_publication(
        self, marker_key: str, marker: Dict[str, Any], error: BaseException
    ) -> None:
        """Best-effort cleanup of ONLY this generation; never masks *error* (D6)."""
        try:
            latest = self._read_publication_marker(marker_key) or marker
            if latest.get("owner") == marker.get("owner"):
                self._reap_publication_generation(marker_key, latest)
        except Exception as cleanup_error:  # noqa: BLE001
            logger.warning(
                "publication cleanup failed after %s: %s: %s",
                type(error).__name__,
                type(cleanup_error).__name__,
                cleanup_error,
            )
            try:
                self._mark_publication_cleanup_failed(
                    marker_key, marker, cleanup_error
                )
            except Exception:  # pragma: no cover - best effort only
                logger.debug(
                    "could not record the publication cleanup marker",
                    exc_info=True,
                )

    def _mark_publication_cleanup_failed(
        self, marker_key: str, marker: Dict[str, Any], cleanup_error: BaseException
    ) -> None:
        def _do(conn: ConnT) -> None:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (marker_key,)
            ).fetchone()
            raw = row[0] if row is not None else None
            current = _parse_publication_marker(raw)
            if current is None or current.get("owner") != marker.get("owner"):
                return
            current["phase"] = _MARKER_PHASE_CLEANUP
            current["cleanup_error"] = type(cleanup_error).__name__
            conn.execute(
                "UPDATE state_meta SET value = ? WHERE key = ? AND value = ?",
                (_serialize_publication_marker(current), marker_key, str(raw)),
            )

        self._execute_write(_do)

    def _verify_known_holder(
        self, conn: ConnT, session_id: str, known_holder: Optional[str]
    ) -> None:
        """Prove the caller's own compression lease is still the live owner.

        Never acquires, steals, releases or extends a lease: a foreign live
        holder is handled exactly as the native append guard handles it, and a
        caller that supplied a holder which no longer owns the row fails fast
        with the native permanent (non-retryable) busy error.
        """
        if not known_holder:
            return
        row = conn.execute(
            "SELECT holder FROM compression_locks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        holder = None
        if row is not None:
            holder = row["holder"] if isinstance(row, sqlite3.Row) else row[0]
        if holder == known_holder:
            return
        try:
            from hermes_state import (  # local import: mixin/host cycle
                CompressionSessionBusyError,
                SessionCompressionInProgressError,
            )
        except Exception:  # pragma: no cover - host always provides these
            raise PublicationLeaseLostError(
                "compression lease is no longer owned by this publisher"
            )
        if holder is None:
            raise CompressionSessionBusyError(
                f"Session {session_id!r} compression lease disappeared; "
                "refusing transcript publication"
            )
        raise SessionCompressionInProgressError(
            f"Session {session_id!r} is being compressed by another writer"
        )

    def _read_data_version(self, conn: Optional[ConnT] = None) -> Optional[int]:
        """The connection's ``PRAGMA data_version`` fence value (D5)."""
        cursor = conn if conn is not None else self._conn
        row = cursor.execute("PRAGMA data_version").fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):  # pragma: no cover - pragma is numeric
            return None

    def _target_row_exists(self, conn: ConnT, session_id: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
            is not None
        )

    def _digest_publication_prefix(
        self, conn: ConnT, session_id: str, watermark: int
    ) -> Tuple[int, str]:
        """Count + digest of the exact persisted values of the live prefix.

        Only rows at or below *watermark* (the largest stored id when the
        snapshot was taken) that are active can be part of the replaced
        prefix; everything above is a concurrent append preserved as tail.
        """
        digest = hashlib.sha256()
        row_count = 0
        columns = ", ".join(_MESSAGE_INSERT_COLUMNS)
        cursor = conn.execute(
            f"SELECT id, {columns} FROM messages WHERE session_id = ? "
            "AND id <= ? AND active = 1 ORDER BY id",
            (session_id, int(watermark)),
        )
        for row in cursor:
            row_count += 1
            for value in row:
                if value is None:
                    digest.update(b"\x00")
                    continue
                if isinstance(value, bytes):
                    payload = value
                else:
                    payload = str(value).encode("utf-8", "surrogatepass")
                digest.update(str(len(payload)).encode("ascii"))
                digest.update(b":")
                digest.update(payload)
                digest.update(b"\x1f")
        return row_count, digest.hexdigest()

    def _assert_copy_columns_known(self, conn: ConnT) -> None:
        """Refuse to relocate rows through a column set this code doesn't know.

        A ``messages`` column added later would otherwise be silently dropped
        from the relocated tail copy; failing the publication keeps the
        original transcript and the appended rows intact instead.
        """
        known = set(_MESSAGE_INSERT_COLUMNS) | {"id", "session_id"}
        unknown = [
            row[1]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            if row[1] not in known
        ]
        if unknown:
            raise PublicationConflictError(
                "messages table has columns this publication cannot preserve: "
                + ", ".join(sorted(unknown))
            )
