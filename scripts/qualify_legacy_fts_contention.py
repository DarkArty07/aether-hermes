"""Disposable reproduction: real SessionDB batch vs a concurrent lease writer."""

import hashlib, json, os, random, sys, tempfile, threading, time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from hermes_state import SessionDB
from hermes_state_common import LEGACY_FTS_SQL, LEGACY_FTS_TRIGRAM_SQL

count = int(sys.argv[2]) if len(sys.argv) > 2 else 24
with tempfile.TemporaryDirectory(prefix="aether-305-writer-") as d:
    p = Path(d) / "state.db"
    db = SessionDB(p)
    db.create_session("writer", source="test")
    db.create_session("lease", source="test")
    db._drop_fts_triggers(db._conn)
    db._conn.executescript(
        "DROP TABLE messages_fts; DROP TABLE messages_fts_trigram; DROP VIEW IF EXISTS messages_fts_trigram_src;"
        + LEGACY_FTS_SQL
        + LEGACY_FTS_TRIGRAM_SQL
    )
    db.close()
    db = SessionDB(p)
    waiter = SessionDB(p)
    holder = f"pid={os.getpid()}:turn=probe"
    assert waiter.try_acquire_session_turn_lease("lease", holder)
    rng = random.Random(305)
    payloads = [
        {
            "role": "tool",
            "content": " ".join(
                hashlib.sha256(rng.randbytes(32)).hexdigest() for _ in range(8192)
            ),
            "tool_name": "read_file",
        }
        for _ in range(count)
    ]
    entered = threading.Event()
    done = threading.Event()
    result = {
        "batch_rows": count,
        "payload_bytes": sum(len(x["content"]) for x in payloads),
        "source": sys.argv[1],
    }
    original = db._insert_message_rows

    def observed(conn, *args, **kw):
        entered.set()
        start = time.monotonic()
        try:
            return original(conn, *args, **kw)
        finally:
            result["fts_insert_seconds"] = time.monotonic() - start

    db._insert_message_rows = observed

    def write():
        start = time.monotonic()
        try:
            result["inserted"] = db.append_messages_batch("writer", payloads)
        except Exception as e:
            result["writer_error"] = str(e)
        finally:
            result["writer_seconds"] = time.monotonic() - start
            done.set()

    t = threading.Thread(target=write)
    t.start()
    assert entered.wait(10)
    start = time.monotonic()
    try:
        result["refresh_success"] = waiter.refresh_session_turn_lease("lease", holder)
    except Exception as e:
        result["refresh_error"] = type(e).__name__ + ": " + str(e)
    result["refresh_seconds"] = time.monotonic() - start
    t.join(180)
    assert done.is_set()
    result["canonical_rows"] = db._conn.execute(
        "SELECT count(*) FROM messages WHERE session_id='writer'"
    ).fetchone()[0]
    assert result.get("refresh_success") is True, result
    assert result["canonical_rows"] == count, result
    print(json.dumps(result, indent=2), flush=True)
    db.close()
    waiter.close()
