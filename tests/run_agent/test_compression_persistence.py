"""Tests for context compression persistence in the gateway.

Verifies that when context compression fires during run_conversation(),
the compressed messages are properly persisted to both SQLite (via the
agent) and JSONL (via the gateway).

Bug scenario (pre-fix):
  1. Gateway loads 200-message history, passes to agent
  2. Agent's run_conversation() compresses to ~30 messages mid-run
  3. _compress_context() resets _last_flushed_db_idx = 0
  4. On exit, _flush_messages_to_session_db() calculates:
     flush_from = max(len(conversation_history=200), _last_flushed_db_idx=0) = 200
  5. messages[200:] is empty (only ~30 messages after compression)
  6. Nothing written to new session's SQLite — compressed context lost
  7. Gateway's history_offset was still 200, producing empty new_messages
  8. Fallback wrote only user/assistant pair — summary lost
"""

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch



# ---------------------------------------------------------------------------
# Part 1: Agent-side — _flush_messages_to_session_db after compression
# ---------------------------------------------------------------------------

class TestFlushAfterCompression:
    """Verify that compressed messages are flushed to the new session's SQLite
    even when conversation_history (from the original session) is longer than
    the compressed messages list."""

    def _make_agent(self, session_db):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id="original-session",
                skip_context_files=True,
                skip_memory=True,
            )
        return agent

    def test_flush_after_compression_with_long_history(self):
        """The actual bug: conversation_history longer than compressed messages.

        Before the fix, flush_from = max(len(conversation_history), 0) = 200,
        but messages only has ~30 entries, so messages[200:] is empty.
        After the fix, conversation_history is cleared to None after compression,
        so flush_from = max(0, 0) = 0, and ALL compressed messages are written.
        """
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            # Simulate the original long history (200 messages)
            original_history = [
                {"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"message {i}"}
                for i in range(200)
            ]

            # First, flush original messages to the original session
            agent._flush_messages_to_session_db(original_history, [])
            original_rows = db.get_messages("original-session")
            assert len(original_rows) == 200

            # Now simulate compression: new session, reset idx, shorter messages
            agent.session_id = "compressed-session"
            db.create_session(session_id="compressed-session", source="test")
            agent._last_flushed_db_idx = 0

            # The compressed messages (summary + tail + new turn)
            compressed_messages = [
                {"role": "user", "content": "[CONTEXT COMPACTION] Summary of work..."},
                {"role": "user", "content": "What should we do next?"},
                {"role": "assistant", "content": "Let me check..."},
                {"role": "user", "content": "new question"},
                {"role": "assistant", "content": "new answer"},
            ]

            # THE BUG: passing the original history as conversation_history
            # causes flush_from = max(200, 0) = 200, skipping everything.
            # After the fix, conversation_history should be None.
            agent._flush_messages_to_session_db(compressed_messages, None)

            new_rows = db.get_messages("compressed-session")
            assert len(new_rows) == 5, (
                f"Expected 5 compressed messages in new session, got {len(new_rows)}. "
                f"Compression persistence bug: messages not written to SQLite."
            )

    def test_flush_with_stale_history_loses_messages(self):
        """Stale conversation_history no longer causes data loss."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            # Simulate compression reset
            agent.session_id = "new-session"
            db.create_session(session_id="new-session", source="test")
            agent._last_flushed_db_idx = 0

            compressed = [
                {"role": "user", "content": "summary"},
                {"role": "assistant", "content": "continuing..."},
            ]

            # Stale history longer than messages: the old positional flush
            # sliced past the end and dropped both messages (#46053).
            stale_history = [{"role": "user", "content": f"msg{i}"} for i in range(100)]
            agent._flush_messages_to_session_db(compressed, stale_history)

            rows = db.get_messages("new-session")
            assert len(rows) == 2
            assert [row["content"] for row in rows] == ["summary", "continuing..."]

    def test_in_place_compression_rebaseline_prevents_duplicate_compacted_rows(self):
        """In-place compaction already persisted the compacted transcript.

        Regression for the 2026-06-26 SRE compression loop: archive_and_compact()
        inserted a compacted active block, then the same turn continued with
        conversation_history=None and _flush_messages_to_session_db() appended
        the compacted dicts again, doubling live context.
        """
        from agent.conversation_compression import conversation_history_after_compression
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)
            agent._ensure_db_session()

            original_history = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original_history, [])
            assert [row["content"] for row in db.get_messages("original-session")] == [
                "old question",
                "old answer",
            ]

            compacted = [
                {"role": "assistant", "content": "[CONTEXT COMPACTION] summary"},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
            ]
            db.archive_and_compact("original-session", compacted)
            setattr(agent, "_last_compaction_in_place", True)
            agent._last_flushed_db_idx = 0

            # Same agent turn continues after compaction. The compacted dicts
            # must be treated as already-persisted history; only later appends
            # should be flushed.
            post_compaction_history = conversation_history_after_compression(
                agent, compacted
            )
            assert post_compaction_history is not None
            assert post_compaction_history is not compacted
            assert post_compaction_history == compacted

            messages = compacted + [
                {"role": "tool", "content": "tool result"},
                {"role": "assistant", "content": "final answer"},
            ]
            agent._flush_messages_to_session_db(messages, post_compaction_history)

            rows = db.get_messages("original-session")
            assert [row["content"] for row in rows] == [
                "[CONTEXT COMPACTION] summary",
                "recent question",
                "recent answer",
                "tool result",
                "final answer",
            ]

    def test_abort_after_in_place_compaction_preserves_flush_baseline(self):
        """An aborted retry must survive flush, restart, and resume."""
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        from hermes_state import SessionDB

        class SuccessCompressor:
            _last_compress_aborted = False
            _last_summary_error = None
            compression_count = 1
            _last_compression_made_progress = True
            _last_summary_fallback_used = False
            last_compression_rough_tokens = 0
            last_prompt_tokens = 0
            last_completion_tokens = 0
            awaiting_real_usage_after_compression = False

            def compress(self, _messages, **_kwargs):
                return [
                    {"role": "user", "content": "[summary] earlier state"},
                    {"role": "assistant", "content": "retained tail"},
                ]

        class AbortCompressor:
            _last_compress_aborted = False
            _last_summary_error = "simulated auxiliary timeout"
            compression_count = 2
            _last_compression_made_progress = False
            _last_summary_fallback_used = False
            last_compression_rough_tokens = 0
            last_prompt_tokens = 0
            last_completion_tokens = 0
            awaiting_real_usage_after_compression = False

            def compress(self, messages, **_kwargs):
                self._last_compress_aborted = True
                return messages

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)
            agent = self._make_agent(db)
            agent.compression_in_place = True
            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original, [])

            agent.context_compressor = SuccessCompressor()
            compacted, _ = compress_context(
                agent, original, "system", approx_tokens=100_000
            )
            history = conversation_history_after_compression(
                agent, compacted, None
            )

            messages = compacted + [
                {"role": "user", "content": "new request"},
                {"role": "assistant", "content": "new answer"},
            ]
            agent.context_compressor = AbortCompressor()
            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )
            history = conversation_history_after_compression(
                agent, returned, history
            )
            agent._flush_messages_to_session_db(returned, history)

            db.close()
            resumed_db = SessionDB(db_path=db_path)
            assert [message["content"] for message in resumed_db.get_messages_as_conversation(
                agent.session_id
            )] == [
                "[summary] earlier state",
                "retained tail",
                "new request",
                "new answer",
            ]
            resumed_db.close()

    def test_rotation_child_session_flushes_full_compressed_transcript_with_markers(self):
        """Regression for #57491: live cached-agent markers must not block child flush."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)
            parent_sid = "20260701_152840_parent"
            db.create_session(parent_sid, "gateway", model="test/model")

            agent = self._make_agent(db)
            agent.session_id = parent_sid
            agent.compression_in_place = False
            agent._ensure_db_session()

            # Plain marked messages only: the exact-equality assertion below
            # relies on `compressed` containing no message that _flush filters
            # for a reason INDEPENDENT of _db_persisted (ephemeral scaffolding,
            # synthetic recovery turns). Keep this fixture free of such messages
            # or the row count would legitimately differ from len(compressed).
            # The transcript must also be large enough that the provider-less
            # static fallback net-shrinks it (middle drops must outweigh the
            # fixed compaction marker overhead), or the no-growth commit guard
            # correctly refuses the rotation this test exercises.
            messages = [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"message {i} " + "x" * 200,
                    "_db_persisted": True,
                }
                for i in range(40)
            ]

            with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("no provider")):
                compressed, _ = compress_context(
                    agent, messages, approx_tokens=100_000, system_message="sys"
                )

            assert agent.session_id != parent_sid
            child_sid = agent.session_id

            agent._flush_messages_to_session_db(compressed, None)

            child_rows = db.get_messages(child_sid)
            assert len(child_rows) == len(compressed), (
                f"Expected {len(compressed)} rows in child session, got {len(child_rows)}. "
                f"_db_persisted marker propagation bug (#57491)."
            )
            db.close()

    def test_affinity_compression_preserves_exact_session_when_rotation_configured(
        self, monkeypatch
    ):
        """Affinity overrides legacy rotate-on-compress without changing session id."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent.compression_in_place = False
            agent._ensure_db_session()
            exact_session = agent.session_id

            messages = [
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"message {i} " + "x" * 200,
                }
                for i in range(40)
            ]
            agent._flush_messages_to_session_db(messages, [])
            monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease-token")

            with patch(
                "agent.context_compressor.call_llm",
                side_effect=RuntimeError("no provider"),
            ):
                compressed, _ = compress_context(
                    agent,
                    messages,
                    approx_tokens=100_000,
                    system_message="sys",
                )

            assert compressed
            assert agent.session_id == exact_session
            assert getattr(agent, "_last_compaction_in_place", False) is True
            row = db.get_session(exact_session)
            assert row is not None
            assert row.get("ended_at") is None
            db.close()

    def test_affinity_restore_keeps_stored_prompt_when_tools_target_candidate(
        self, monkeypatch
    ):
        """A resumed review restores its canonical prompt without a provider call.

        This covers the startup restore helper, not a full CLI process: the
        session identity remains the supervisor workspace while file/terminal
        resolution intentionally stays on the review candidate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            canonical = root / "supervisor"
            candidate = root / "candidate"
            canonical.mkdir()
            candidate.mkdir()
            (candidate / "review.txt").write_text("candidate evidence\n")
            monkeypatch.setenv("HERMES_HOME", str(root / "profile"))
            monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease")
            monkeypatch.setenv("HERMES_KANBAN_SESSION_WORKSPACE", str(canonical))
            monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(candidate))
            monkeypatch.setenv("TERMINAL_CWD", str(candidate))
            monkeypatch.setenv("TERMINAL_ENV", "local")

            # Keep all state/profile isolation in place before importing the
            # real restoration path or SessionDB.
            from agent.conversation_loop import _restore_or_build_system_prompt
            from agent.runtime_cwd import (
                resolve_agent_cwd,
                resolve_session_identity_cwd,
            )
            from hermes_state import SessionDB
            from tools.file_tools import read_file_tool
            from tools.terminal_tool import terminal_tool

            session_id = "supervisor-review-session"
            stored = (
                "User home directory: /isolated-home\n"
                f"Current working directory: {canonical}\n"
                "Model: test/model\n"
                "Provider: openrouter\n"
                "Platform: kanban"
            )
            expected_hash = sha256(stored.encode("utf-8")).hexdigest()
            db = SessionDB(db_path=root / "state.db")
            db.create_session(
                session_id,
                "kanban",
                model="test/model",
                system_prompt=stored,
                cwd=str(canonical),
            )
            agent = self._make_agent(db)
            agent.session_id = session_id
            agent.platform = "kanban"
            agent.provider = "openrouter"
            agent._build_system_prompt = lambda _message: (_ for _ in ()).throw(
                AssertionError("stored prompt must be restored, not rebuilt")
            )

            assert resolve_session_identity_cwd() == canonical
            assert resolve_agent_cwd() == candidate
            with patch.object(db, "update_system_prompt", wraps=db.update_system_prompt) as update:
                _restore_or_build_system_prompt(
                    agent, None, [{"role": "user", "content": "review"}]
                )

            # Exercise the supported tools rather than only asserting their
            # resolver: relative file reads and local terminal commands must
            # both remain in the candidate workspace.
            assert "candidate evidence" in read_file_tool(
                "review.txt", task_id=session_id
            )
            pwd = json.loads(terminal_tool(command="pwd", task_id=session_id))
            assert pwd["exit_code"] == 0
            assert Path(pwd["output"].strip()).resolve() == candidate.resolve()

            restored = db.get_session(session_id)
            assert agent.session_id == session_id
            assert agent._cached_system_prompt.encode("utf-8") == stored.encode("utf-8")
            assert (
                sha256(agent._cached_system_prompt.encode("utf-8")).hexdigest()
                == expected_hash
            )
            assert restored is not None
            assert restored["system_prompt"].encode("utf-8") == stored.encode("utf-8")
            assert (
                sha256(restored["system_prompt"].encode("utf-8")).hexdigest()
                == expected_hash
            )
            update.assert_not_called()
            db.close()



# ---------------------------------------------------------------------------
# Part 2: Gateway-side — history_offset after session split
# ---------------------------------------------------------------------------

class TestGatewayHistoryOffsetAfterSplit:
    """Verify that when the agent creates a new session during compression,
    the gateway uses history_offset=0 so all compressed messages are written
    to the JSONL transcript."""

    def test_history_offset_zero_on_session_split(self):
        """When agent.session_id differs from the original, history_offset must be 0."""
        # This tests the logic in gateway/run.py run_sync():
        # _session_was_split = agent.session_id != session_id
        # _effective_history_offset = 0 if _session_was_split else len(agent_history)

        original_session_id = "session-abc"
        agent_session_id = "session-compressed-xyz"  # Different = compression happened
        agent_history_len = 200

        # Simulate the gateway's offset calculation (post-fix)
        _session_was_split = (agent_session_id != original_session_id)
        _effective_history_offset = 0 if _session_was_split else agent_history_len

        assert _session_was_split is True
        assert _effective_history_offset == 0


    def test_new_messages_extraction_after_split(self):
        """After compression with offset=0, new_messages should be ALL agent messages."""
        # Simulates the gateway's new_messages calculation
        agent_messages = [
            {"role": "user", "content": "[CONTEXT COMPACTION] Summary..."},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ]
        history_offset = 0  # After fix: 0 on session split

        new_messages = agent_messages[history_offset:] if len(agent_messages) > history_offset else []
        assert len(new_messages) == 5, (
            f"Expected all 5 messages with offset=0, got {len(new_messages)}"
        )



class TestStoredPromptCwdDrift:
    """Verify that stored system prompts are rejected when cwd changed."""

    def _make_agent(self, model="test/model", provider="openrouter"):
        class _Agent:
            pass

        agent = _Agent()
        agent.model = model
        agent.provider = provider
        return agent

    @staticmethod
    def _host_block(cwd: str) -> str:
        """A stored prompt fragment shaped like the real host-info block.

        ``build_environment_hints`` always emits ``User home directory:``
        immediately before the working-directory line, and the staleness check
        anchors on that pair so user project files can't shadow the real value.
        Fixtures must therefore include the anchor or they stop exercising the
        cwd path at all.
        """
        return (
            "Host: Linux (6.16.0)\n"
            "User home directory: /home/tester\n"
            f"Current working directory: {cwd}\n"
        )

    def test_stored_prompt_stale_when_cwd_differs(self):
        """Different cwd should force a prompt rebuild."""
        from unittest.mock import patch
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = self._make_agent()
        stored_prompt = (
            self._host_block("/project/old")
            + "Model: test/model\n"
            "Provider: openrouter\n"
        )

        with patch("os.getcwd", return_value="/project/new"):
            assert _stored_prompt_matches_runtime(agent, stored_prompt) is False, (
                "Expected False when stored cwd differs from current cwd"
            )

    def test_stored_prompt_fresh_when_cwd_matches(self):
        """Matching cwd should allow prompt reuse."""
        from unittest.mock import patch
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = self._make_agent()
        current_cwd = "/project/current"
        stored_prompt = (
            self._host_block(current_cwd)
            + "Model: test/model\n"
            "Provider: openrouter\n"
        )

        with patch("os.getcwd", return_value=current_cwd):
            assert _stored_prompt_matches_runtime(agent, stored_prompt) is True, (
                "Expected True when stored cwd matches current cwd"
            )

    def test_affinity_review_keeps_stored_prompt_when_task_cwd_differs(
        self, monkeypatch, tmp_path
    ):
        """Reviewing another worktree must not invalidate the Supervisor prefix."""
        from agent.conversation_loop import _stored_prompt_matches_runtime

        canonical = tmp_path / "supervisor"
        candidate = tmp_path / "candidate"
        canonical.mkdir()
        candidate.mkdir()
        agent = self._make_agent()
        stored_prompt = (
            self._host_block(str(canonical))
            + "Model: test/model\n"
            "Provider: openrouter\n"
        )
        monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease")
        monkeypatch.setenv("HERMES_KANBAN_SESSION_WORKSPACE", str(canonical))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(candidate))
        monkeypatch.setenv("TERMINAL_CWD", str(candidate))

        assert _stored_prompt_matches_runtime(agent, stored_prompt) is True
    def test_project_context_cannot_force_a_rebuild(self):
        """🔴 CACHE INVARIANT: user project text must never invalidate the prompt.

        The prompt embeds AGENTS.md / CLAUDE.md / .cursorrules in the context
        tier, which sits AFTER the host-info block. A whole-prompt scan for
        ``Current working directory:`` therefore matched the user's own file
        and compared runtime state against project prose. That mismatch never
        clears, so the check rejected the stored prompt on EVERY turn —
        rebuilding the system prompt each message and destroying the prefix
        cache for the entire session. Strictly worse than the staleness this
        check exists to catch.
        """
        from unittest.mock import patch
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = self._make_agent()
        current_cwd = "/project/current"
        stored_prompt = (
            self._host_block(current_cwd)
            + "\n# AGENTS.md\n\n"
            "Our deploy convention:\n\n"
            "Current working directory: /srv/decoy\n\n"
            "Always run make before pushing.\n\n"
            "Model: test/model\n"
            "Provider: openrouter\n"
        )

        with patch("os.getcwd", return_value=current_cwd):
            assert _stored_prompt_matches_runtime(agent, stored_prompt) is True, (
                "A project file that merely MENTIONS 'Current working "
                "directory:' must not invalidate the prompt — that would "
                "rebuild every turn and break the prefix cache"
            )

    def test_project_context_cannot_mask_real_drift(self):
        """The inverse: project text must not fake a match either.

        A stored prompt built in /project/old whose embedded AGENTS.md happens
        to name the NEW cwd must still be rejected — otherwise project prose
        could suppress genuine drift detection.
        """
        from unittest.mock import patch
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = self._make_agent()
        stored_prompt = (
            self._host_block("/project/old")
            + "\n# AGENTS.md\n\n"
            "Current working directory: /project/new\n\n"
            "Model: test/model\n"
            "Provider: openrouter\n"
        )

        with patch("os.getcwd", return_value="/project/new"):
            assert _stored_prompt_matches_runtime(agent, stored_prompt) is False, (
                "Embedded project text naming the new cwd must not mask real "
                "drift in the host-info block"
            )





    def test_built_prompt_contains_platform_line(self):
        """The built system prompt must carry a Platform: line so drift detection works."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from hermes_state import SessionDB
        from run_agent import AIAgent
        from agent.system_prompt import build_system_prompt_parts

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
                agent = AIAgent(
                    api_key="test-key",
                    base_url="https://openrouter.ai/api/v1",
                    model="test/model",
                    provider="openrouter",
                    quiet_mode=True,
                    session_db=db,
                    session_id="platform-test",
                    skip_context_files=True,
                    skip_memory=True,
                )
            agent.platform = "cli"
            parts = build_system_prompt_parts(agent)
            assert "Platform: cli" in parts["volatile"], (
                "Built prompt missing 'Platform: cli' — drift detection cannot read it"
            )


def test_affinity_worker_forces_in_place_compression(monkeypatch):
    """Exact flow affinity cannot rotate to a child session id."""
    from agent.conversation_compression import _compression_in_place_for_agent

    class Agent:
        compression_in_place = False

    monkeypatch.delenv("HERMES_KANBAN_AFFINITY_TOKEN", raising=False)
    assert _compression_in_place_for_agent(Agent()) is False

    monkeypatch.setenv("HERMES_KANBAN_AFFINITY_TOKEN", "lease-token")
    assert _compression_in_place_for_agent(Agent()) is True
