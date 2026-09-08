"""Aether #305: bound legacy FTS writes without discarding canonical history."""

from hermes_state import SessionDB

PREFIX = 8192
MARKER = "aether_legacy_fts_tool_full_content_high_water"


def legacy_database(path, historical=False):
    db = SessionDB(path)
    db.create_session("session", source="test")
    db._drop_fts_triggers(db._conn)
    db._conn.executescript("""
        DROP TABLE messages_fts;
        DROP TABLE messages_fts_trigram;
        DROP VIEW IF EXISTS messages_fts_trigram_src;
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram');
    """)
    for table in ("messages_fts", "messages_fts_trigram"):
        db._conn.executescript(f"""
            CREATE TRIGGER {table}_insert AFTER INSERT ON messages BEGIN
                INSERT INTO {table}(rowid,content) VALUES(new.id,new.content);
            END;
            CREATE TRIGGER {table}_delete AFTER DELETE ON messages BEGIN
                DELETE FROM {table} WHERE rowid=old.id;
            END;
            CREATE TRIGGER {table}_update AFTER UPDATE OF content,tool_name,tool_calls ON messages BEGIN
                DELETE FROM {table} WHERE rowid=old.id;
                INSERT INTO {table}(rowid,content) VALUES(new.id,new.content);
            END;
        """)
    if historical:
        db.append_message("session", "tool", long_text("historicaltail"))
    db.close()
    return SessionDB(path)


def long_text(tail):
    return "prefixmarker " + "padding " * 2000 + " " + tail


def test_legacy_new_tool_index_is_bounded_but_canonical_message_is_complete(tmp_path):
    db = legacy_database(tmp_path / "state.db")
    try:
        payload = long_text("freshuniquetail")
        row_id = db.append_message("session", "tool", payload, tool_name="terminal")
        assert (
            db._conn.execute(
                "SELECT content FROM messages WHERE id=?", (row_id,)
            ).fetchone()[0]
            == payload
        )
        for table in ("messages_fts", "messages_fts_trigram"):
            value = db._conn.execute(
                f"SELECT content FROM {table} WHERE rowid=?", (row_id,)
            ).fetchone()[0]
            assert len(value) <= PREFIX + 32
            assert "freshuniquetail" not in value
        assert db.search_messages("freshuniquetail") == []
        assert [
            r["id"] for r in db.search_messages("freshuniquetail", role_filter=["tool"])
        ] == [row_id]
    finally:
        db.close()


def test_legacy_migration_preserves_historical_index_and_marker_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    db = legacy_database(path, historical=True)
    try:
        historical = db._conn.execute("SELECT id,content FROM messages").fetchone()
        assert db.search_messages("historicaltail")
        marker = db._conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (MARKER,)
        ).fetchone()
        assert marker is not None
        assert int(marker[0]) == historical[0]
        assert (
            db._conn.execute(
                "SELECT content FROM messages_fts WHERE rowid=?", (historical[0],)
            ).fetchone()[0]
            == historical[1]
        )
        new_id = db.append_message("session", "tool", long_text("newtail"))
        version = db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        db.close()
        db = SessionDB(path)
        assert (
            int(
                db._conn.execute(
                    "SELECT value FROM state_meta WHERE key=?", (MARKER,)
                ).fetchone()[0]
            )
            == historical[0]
        )
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == version
        )
        assert (
            db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='messages_fts_trigram_src'"
            ).fetchone()
            is None
        )
        assert [
            r["id"] for r in db.search_messages("newtail", role_filter=["tool"])
        ] == [new_id]
    finally:
        db.close()


def test_legacy_role_change_update_delete_and_explicit_boolean_search(tmp_path):
    db = legacy_database(tmp_path / "state.db")
    try:
        tool_id = db.append_message(
            "session", "tool", long_text("alphaunique betatoken")
        )
        user_id = db.append_message("session", "user", long_text("useruniquetail"))
        assert db.search_messages("useruniquetail", role_filter=["user"])
        assert [
            r["id"]
            for r in db.search_messages(
                "alphaunique AND betatoken", role_filter=["tool", "user"]
            )
        ] == [tool_id]
        db._execute_write(
            lambda c: c.execute(
                "UPDATE messages SET role='user' WHERE id=?", (tool_id,)
            )
        )
        assert db.search_messages("alphaunique", role_filter=["user"])
        db._execute_write(
            lambda c: c.execute(
                "UPDATE messages SET role='tool' WHERE id=?", (user_id,)
            )
        )
        assert not db.search_messages("useruniquetail")
        assert db.search_messages("useruniquetail", role_filter=["tool"])
        db._execute_write(
            lambda c: c.execute("DELETE FROM messages WHERE id=?", (tool_id,))
        )
        assert not db.search_messages("alphaunique", role_filter=["tool", "user"])
        for table in ("messages_fts", "messages_fts_trigram"):
            db._conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
    finally:
        db.close()


def test_fresh_external_content_layout_is_not_changed(tmp_path):
    db = SessionDB(tmp_path / "fresh.db")
    try:
        assert not db._db_has_legacy_inline_fts(db._conn)
        assert (
            db._conn.execute(
                "SELECT 1 FROM state_meta WHERE key=?", (MARKER,)
            ).fetchone()
            is None
        )
        db.create_session("fresh", source="test")
        row_id = db.append_message("fresh", "tool", long_text("externaluniquetail"))
        assert [r["id"] for r in db.search_messages("externaluniquetail")] == [row_id]
    finally:
        db.close()


def test_legacy_migration_failure_rolls_back_marker_and_triggers(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "state.db"
    db = legacy_database(path, historical=True)
    try:
        db._conn.execute("DELETE FROM state_meta WHERE key=?", (MARKER,))
        old_sql = "CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(rowid,content) VALUES(new.id,new.content); END"
        db._conn.execute("DROP TRIGGER messages_fts_insert")
        db._conn.execute(old_sql)
        db._conn.commit()
        monkeypatch.setattr(
            "hermes_state_schema.LEGACY_FTS_SQL", "INTENTIONALLY INVALID DDL;"
        )
        import pytest

        with pytest.raises(sqlite3.OperationalError):
            db._migrate_legacy_tool_fts_bounds()
        assert (
            db._conn.execute(
                "SELECT 1 FROM state_meta WHERE key=?", (MARKER,)
            ).fetchone()
            is None
        )
        assert (
            db._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='messages_fts_insert'"
            ).fetchone()[0]
            == old_sql
        )
        assert db.search_messages("historicaltail")
    finally:
        db.close()


def test_legacy_migration_only_changes_triggers_not_historical_rows(tmp_path):
    path = tmp_path / "state.db"
    db = legacy_database(path, historical=True)
    try:
        before = [tuple(r) for r in db._conn.execute("SELECT * FROM messages")]
        statements = []
        db._conn.set_trace_callback(statements.append)
        db._init_schema()
        db._conn.set_trace_callback(None)
        assert before == [tuple(r) for r in db._conn.execute("SELECT * FROM messages")]
        assert not any(
            q.strip().upper().startswith("DELETE FROM MESSAGES_FTS") for q in statements
        )
        assert not any("DROP TABLE MESSAGES_FTS" in q.upper() for q in statements)
    finally:
        db.close()
