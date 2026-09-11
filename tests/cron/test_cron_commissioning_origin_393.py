"""Focused unit and regression tests for #393: Cron commissioning origin and Kanban auto-subscription.

Creating a cron job from a TUI/desktop or gateway session captures a trusted durable notification
origin separately from ordinary deliver metadata (TUI: durable session key plus optional live UI id;
gateway: its full stable route). At fire time that origin is restored only as a request-local context
consumed by Kanban auto-subscription. A root created by such a cron run reports subscribed=true,
persists exactly the originating subscription, and wakes that origin once on terminal flow.
An unattached cron, CLI or test creates no subscription; wrong/stale origins fail closed and are
surfaced in the creation receipt. It must not impersonate inbound authority, become a model-supplied
session id, leak through process-global environment, or alter stateless cron delivery.
"""

from __future__ import annotations

import json
import os
import pytest


@pytest.fixture(autouse=True)
def _reset_context_vars():
    from gateway.session_context import _UNSET, _VAR_MAP
    for v in _VAR_MAP.values():
        v.set(_UNSET)
    yield
    for v in _VAR_MAP.values():
        v.set(_UNSET)


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Isolated environment simulating a non-default profile (e.g. morfeo)."""
    profile_home = tmp_path / "profiles" / "morfeo"
    profile_home.mkdir(parents=True)
    (profile_home / "scripts").mkdir()
    (profile_home / "cron").mkdir()
    (profile_home / "cron" / "output").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", "morfeo")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_UI_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", profile_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", profile_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", profile_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", profile_home / "cron" / "output")

    import cron.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "_hermes_home", profile_home)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()

    return {
        "profile_home": profile_home,
    }


class TestIssue393CommissioningOrigin:
    def test_capture_tui_commissioning_origin_at_create(self, profile_env, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_KEY", "tui-origin-key-12345")
        monkeypatch.setenv("HERMES_UI_SESSION_ID", "ui-tab-678")

        from tools.cronjob_tools import cronjob

        result_raw = cronjob(action="create", schedule="every 1h", prompt="test job")
        res = json.loads(result_raw)
        assert res.get("success") is True

        from cron.jobs import get_job
        job = get_job(res["job_id"]) or {}
        notif = job.get("notification_origin")
        assert notif is not None
        assert notif.get("platform") == "tui"
        assert notif.get("chat_id") == "tui-origin-key-12345"
        assert notif.get("session_key") == "tui-origin-key-12345"
        assert notif.get("ui_session_id") == "ui-tab-678"

    def test_capture_gateway_commissioning_origin_at_create(self, profile_env):
        from gateway.session_context import set_session_vars
        set_session_vars(
            platform="telegram",
            chat_id="tg-chat-999",
            chat_name="Dev Group",
            thread_id="101",
        )

        from tools.cronjob_tools import cronjob

        result_raw = cronjob(action="create", schedule="every 1h", prompt="test job 2")
        res = json.loads(result_raw)
        assert res.get("success") is True

        from cron.jobs import get_job
        job = get_job(res["job_id"]) or {}
        notif = job.get("notification_origin")
        assert notif is not None
        assert notif.get("platform") == "telegram"
        assert notif.get("chat_id") == "tg-chat-999"
        assert notif.get("thread_id") == "101"

    def test_unattached_create_captures_no_notification_origin(self, profile_env):
        from tools.cronjob_tools import cronjob

        result_raw = cronjob(action="create", schedule="every 1h", prompt="unattached job")
        res = json.loads(result_raw)
        assert res.get("success") is True

        from cron.jobs import get_job
        job = get_job(res["job_id"]) or {}
        assert job.get("notification_origin") is None

    def test_fire_restores_request_local_context_for_kanban_auto_subscribe(self, profile_env, monkeypatch):
        from hermes_cli import kanban_db as kb
        import tools.kanban_tools as kt
        from cron.scheduler import run_job

        conn = kb.connect()
        conn.close()

        job = {
            "id": "job-with-tui-origin",
            "name": "tui-origin-job",
            "prompt": "do work",
            "schedule": {"kind": "once"},
            "notification_origin": {
                "platform": "tui",
                "chat_id": "durable-tui-session-key",
                "session_key": "durable-tui-session-key",
                "ui_session_id": "win-live-1",
            },
        }

        created_receipt = {}
        class MockAgent:
            def __init__(self, **kwargs):
                pass
            def run_conversation(self, *a, **kw):
                monkeypatch.setenv("HERMES_KANBAN_TASK", "t_root_worker")
                raw = kt._handle_create({"title": "Root cron task", "assignee": "implementer"})
                created_receipt.update(json.loads(raw))
                return {"final_response": "done", "messages": []}
            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        import sys
        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = MockAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as _rtp
        monkeypatch.setattr(
            _rtp, "resolve_runtime_provider",
            lambda **kw: {"provider": "test", "api_key": "k", "base_url": "http://test", "api_mode": "chat_completions"}
        )
        monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr("cron.scheduler._deliver_result", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        run_job(job)

        # 1. kanban_create reports subscribed=True
        assert created_receipt.get("subscribed") is True
        task_id = created_receipt["task_id"]

        # 2. Database contains exactly the originating subscription
        c = kb.connect()
        try:
            subs = kb.list_notify_subs(c, task_id=task_id)
            assert len(subs) == 1
            assert subs[0].get("platform") == "tui"
            assert subs[0].get("chat_id") == "durable-tui-session-key"
            meta = subs[0].get("delivery_metadata") or {}
            assert meta.get("ui_session_id") == "win-live-1"
        finally:
            c.close()

        # 3. Process-global env was not mutated
        assert os.environ.get("HERMES_SESSION_PLATFORM") is None
        assert os.environ.get("HERMES_SESSION_CHAT_ID") is None

    def test_fire_restores_gateway_origin_kanban_auto_subscribe_group(self, profile_env, monkeypatch):
        from hermes_cli import kanban_db as kb
        import tools.kanban_tools as kt
        from cron.scheduler import run_job

        conn = kb.connect()
        conn.close()

        job = {
            "id": "job-with-gateway-group-origin",
            "name": "gateway-group-origin-job",
            "prompt": "do work",
            "schedule": {"kind": "once"},
            "notification_origin": {
                "platform": "telegram",
                "chat_id": "tg-chat-999",
                "chat_type": "group",
                "thread_id": 101,
                "user_id": "user-42",
                "message_id": "msg-7",
            },
        }

        created_receipt = {}
        class MockAgent:
            def __init__(self, **kwargs):
                pass
            def run_conversation(self, *a, **kw):
                monkeypatch.setenv("HERMES_KANBAN_TASK", "t_root_worker_gw_group")
                raw = kt._handle_create({"title": "Root cron task gateway group", "assignee": "implementer"})
                created_receipt.update(json.loads(raw))
                return {"final_response": "done", "messages": []}
            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        import sys
        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = MockAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as _rtp
        monkeypatch.setattr(
            _rtp, "resolve_runtime_provider",
            lambda **kw: {"provider": "test", "api_key": "k", "base_url": "http://test", "api_mode": "chat_completions"}
        )
        monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr("cron.scheduler._deliver_result", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        run_job(job)

        assert created_receipt.get("subscribed") is True
        task_id = created_receipt["task_id"]

        c = kb.connect()
        try:
            subs = kb.list_notify_subs(c, task_id=task_id)
            assert len(subs) == 1
            sub = subs[0]
            assert sub.get("platform") == "telegram"
            assert sub.get("chat_id") == "tg-chat-999"
            assert sub.get("chat_type") == "group"
            assert str(sub.get("thread_id")) == "101"
            assert sub.get("user_id") == "user-42"
            assert sub.get("delivery_mode") == "notify+wake"
            meta = sub.get("delivery_metadata") or {}
            assert str(meta.get("thread_id")) == "101"
            assert meta.get("chat_type") == "group"
            assert meta.get("message_id") == "msg-7"
            assert "telegram_dm_topic_reply_fallback" not in meta
        finally:
            c.close()

    def test_fire_restores_gateway_origin_kanban_auto_subscribe_telegram_dm_with_thread(self, profile_env, monkeypatch):
        from hermes_cli import kanban_db as kb
        import tools.kanban_tools as kt
        from cron.scheduler import run_job

        conn = kb.connect()
        conn.close()

        job = {
            "id": "job-with-gateway-dm-origin",
            "name": "gateway-dm-origin-job",
            "prompt": "do work",
            "schedule": {"kind": "once"},
            "notification_origin": {
                "platform": "telegram",
                "chat_id": "tg-chat-999",
                "chat_type": "dm",
                "thread_id": 101,
                "user_id": "user-42",
                "message_id": "msg-7",
            },
        }

        created_receipt = {}
        class MockAgent:
            def __init__(self, **kwargs):
                pass
            def run_conversation(self, *a, **kw):
                monkeypatch.setenv("HERMES_KANBAN_TASK", "t_root_worker_gw_dm")
                raw = kt._handle_create({"title": "Root cron task gateway dm", "assignee": "implementer"})
                created_receipt.update(json.loads(raw))
                return {"final_response": "done", "messages": []}
            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        import sys
        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = MockAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as _rtp
        monkeypatch.setattr(
            _rtp, "resolve_runtime_provider",
            lambda **kw: {"provider": "test", "api_key": "k", "base_url": "http://test", "api_mode": "chat_completions"}
        )
        monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr("cron.scheduler._deliver_result", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        run_job(job)

        assert created_receipt.get("subscribed") is True
        task_id = created_receipt["task_id"]

        c = kb.connect()
        try:
            subs = kb.list_notify_subs(c, task_id=task_id)
            assert len(subs) == 1
            sub = subs[0]
            assert sub.get("platform") == "telegram"
            assert sub.get("chat_id") == "tg-chat-999"
            assert sub.get("chat_type") == "dm"
            assert str(sub.get("thread_id")) == "101"
            assert sub.get("user_id") == "user-42"
            assert sub.get("delivery_mode") == "notify+wake"
            meta = sub.get("delivery_metadata") or {}
            assert str(meta.get("thread_id")) == "101"
            assert meta.get("chat_type") == "dm"
            assert meta.get("message_id") == "msg-7"
            assert meta.get("telegram_dm_topic_reply_fallback") is True
            assert meta.get("direct_messages_topic_id") == "101"
            assert meta.get("telegram_reply_to_message_id") == "msg-7"
        finally:
            c.close()

    def test_unattached_cron_fire_creates_no_kanban_subscription(self, profile_env, monkeypatch):
        from hermes_cli import kanban_db as kb
        import tools.kanban_tools as kt
        from cron.scheduler import run_job

        conn = kb.connect()
        conn.close()

        job = {
            "id": "job-unattached",
            "name": "unattached",
            "prompt": "do work",
            "schedule": {"kind": "once"},
        }

        created_receipt = {}
        class MockAgent:
            def __init__(self, **kwargs):
                pass
            def run_conversation(self, *a, **kw):
                monkeypatch.setenv("HERMES_KANBAN_TASK", "t_root_worker_2")
                raw = kt._handle_create({"title": "Unattached cron task", "assignee": "implementer"})
                created_receipt.update(json.loads(raw))
                return {"final_response": "done", "messages": []}
            def get_activity_summary(self):
                return {"seconds_since_activity": 0.0}

        import sys
        fake_mod = type(sys)("run_agent")
        fake_mod.AIAgent = MockAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

        from hermes_cli import runtime_provider as _rtp
        monkeypatch.setattr(
            _rtp, "resolve_runtime_provider",
            lambda **kw: {"provider": "test", "api_key": "k", "base_url": "http://test", "api_mode": "chat_completions"}
        )
        monkeypatch.setattr("cron.scheduler._build_job_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr("cron.scheduler._deliver_result", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

        run_job(job)

        assert created_receipt.get("subscribed") is False
        task_id = created_receipt["task_id"]

        c = kb.connect()
        try:
            subs = kb.list_notify_subs(c, task_id=task_id)
            assert len(subs) == 0
        finally:
            c.close()

    def test_tui_terminal_flow_wakes_origin_once(self, profile_env):
        from hermes_cli import kanban_db as kb
        from tui_gateway.server import _collect_kanban_notifications

        c = kb.connect()
        try:
            tid = kb.create_task(c, title="tui wake task", assignee="implementer")
            kb.add_notify_sub(c, task_id=tid, platform="tui", chat_id="origin-key-test")
            kb.complete_task(c, task_id=tid, summary="task completed successfully")
        finally:
            c.close()

        session_dict = {"session_key": "origin-key-test"}
        notifications = _collect_kanban_notifications(session_dict)
        assert len(notifications) == 1
        assert "completed" in notifications[0].lower()

        # Second collect must return 0 (cursor advanced, wakes once)
        again = _collect_kanban_notifications(session_dict)
        assert len(again) == 0
