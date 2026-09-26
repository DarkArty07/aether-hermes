"""Unit coverage for the background-review aux-model selector + routed digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model, warm
    cache); a configured different model → routed with resolved credentials.
  • _digest_history — compact replay used ONLY on the routed path (recent tail
    verbatim + a digest of older turns), preserving role alternation.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch

import pytest

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt["max_tokens"] == 2048


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_falls_back_to_parent():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"


def _proxy_parent():
    agent = _FakeAgent(provider="custom", model="main-model")
    agent._current_main_runtime = lambda: {
        "api_key": "synthetic-parent-key",
        "base_url": "https://router.invalid/v1",
        "api_mode": "chat_completions",
    }
    return agent


def test_routed_review_recovers_auth_through_real_custom_resolver():
    cfg = {"auxiliary": {"background_review": {
        "provider": "custom", "model": "review-model",
        "base_url": "https://router.invalid/v1",
    }}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.runtime_provider._try_resolve_from_custom_pool", return_value=None), \
         patch("hermes_cli.runtime_provider._host_derived_api_key", return_value=""):
        result = br._resolve_review_runtime(_proxy_parent())
    assert result["routed"] is True
    assert result["model"] == "review-model"
    assert result["api_key"] == "synthetic-parent-key"


@pytest.mark.parametrize("requested_provider", ["custom", "named-proxy"])
@pytest.mark.parametrize("task_key", [None, ""])
@pytest.mark.parametrize("resolved_key", [None, "", "no-key-required"])
def test_routed_same_endpoint_recovers_live_parent_auth(requested_provider, task_key, resolved_key):
    agent = _proxy_parent()
    cfg = {"auxiliary": {"background_review": {
        "provider": requested_provider, "model": "review-model",
        "base_url": "https://router.invalid/v1/", "api_key": task_key,
    }}}
    resolved = {
        "provider": "custom", "base_url": "https://router.invalid/v1",
        "api_mode": "chat_completions", "api_key": resolved_key,
    }
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=resolved):
        result = br._resolve_review_runtime(agent)
    assert result["api_key"] == "synthetic-parent-key"
    assert result["model"] == "review-model"
    assert result["routed"] is True
    assert resolved["api_key"] == resolved_key


@pytest.mark.parametrize("destination", [
    "https://other.invalid/v1", "https://router.invalid:444/v1",
    "http://router.invalid/v1", "https://router.invalid/other",
])
def test_routed_review_does_not_send_parent_auth_to_other_endpoint(destination):
    cfg = {"auxiliary": {"background_review": {
        "provider": "custom", "model": "review-model", "base_url": destination,
    }}}
    resolved = {"provider": "custom", "base_url": destination, "api_key": "no-key-required"}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=resolved):
        result = br._resolve_review_runtime(_proxy_parent())
    assert result["api_key"] == "no-key-required"


@pytest.mark.parametrize("task_key,resolved_key,pool,provider", [
    ("explicit-review-key", "explicit-review-key", None, "custom"),
    (None, "configured-review-key", None, "custom"),
    (None, "no-key-required", "configured-pool", "custom"),
    (None, "no-key-required", None, "other-provider"),
    ("no-key-required", "no-key-required", None, "custom"),
])
def test_routed_review_preserves_explicit_auth_and_provider_boundary(
    task_key, resolved_key, pool, provider,
):
    task = {"provider": "custom", "model": "review-model"}
    if task_key is not None:
        task["api_key"] = task_key
    cfg = {"auxiliary": {"background_review": task}}
    resolved = {
        "provider": provider, "base_url": "https://router.invalid/v1",
        "api_key": resolved_key, "credential_pool": pool,
    }
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=resolved):
        result = br._resolve_review_runtime(_proxy_parent())
    assert result["api_key"] == resolved_key
    assert result["credential_pool"] == pool


def test_routed_review_does_not_inherit_when_requested_endpoint_differs_from_resolved():
    cfg = {"auxiliary": {"background_review": {
        "provider": "custom", "model": "review-model",
        "base_url": "https://other.invalid/v1",
    }}}
    resolved = {
        "provider": "custom", "base_url": "https://router.invalid/v1",
        "api_key": "no-key-required",
    }
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=resolved):
        result = br._resolve_review_runtime(_proxy_parent())
    assert result["api_key"] == "no-key-required"


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim.
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The verbatim tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest
