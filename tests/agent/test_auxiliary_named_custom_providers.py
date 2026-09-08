"""Tests for named custom provider and 'main' alias resolution in auxiliary_client."""

import http.server
import json
import os
import threading
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect HERMES_HOME and clear module caches."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Write a minimal config so load_config doesn't fail
    (hermes_home / "config.yaml").write_text("model:\n  default: test-model\n")


def _write_config(tmp_path, config_dict):
    """Write a config.yaml to the test HERMES_HOME."""
    import yaml
    config_path = tmp_path / ".hermes" / "config.yaml"
    config_path.write_text(yaml.dump(config_dict))


class TestNormalizeVisionProvider:
    """_normalize_vision_provider should resolve 'main' to actual main provider."""


    def test_main_resolves_to_openrouter(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "anthropic/claude-sonnet-4", "provider": "openrouter"},
        })
        from agent.auxiliary_client import _normalize_vision_provider
        assert _normalize_vision_provider("main") == "openrouter"






    def test_auto_unchanged(self):
        from agent.auxiliary_client import _normalize_vision_provider
        assert _normalize_vision_provider("auto") == "auto"
        assert _normalize_vision_provider(None) == "auto"


class TestResolveProviderClientMainAlias:
    """resolve_provider_client('main', ...) should resolve to actual main provider."""

    def test_main_resolves_to_named_custom_provider(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "my-model", "provider": "beans"},
            "custom_providers": [
                {"name": "beans", "base_url": "http://beans.local/v1", "api_key": "k"},
            ],
        })
        from agent.auxiliary_client import resolve_provider_client
        client, model = resolve_provider_client("main", "override-model")
        assert client is not None
        assert model == "override-model"
        assert "beans.local" in str(client.base_url)

    def test_main_with_custom_colon_prefix(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "my-model", "provider": "custom:beans"},
            "custom_providers": [
                {"name": "beans", "base_url": "http://beans.local/v1", "api_key": "k"},
            ],
        })
        from agent.auxiliary_client import resolve_provider_client
        client, model = resolve_provider_client("main", "test")
        assert client is not None
        assert "beans.local" in str(client.base_url)

    def test_main_resolves_github_copilot_alias(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "gpt-5.4", "provider": "github-copilot"},
        })
        with (
            patch("hermes_cli.auth.resolve_api_key_provider_credentials", return_value={
                "api_key": "ghu_test_token",
                "base_url": "https://api.githubcopilot.com",
            }),
            patch("agent.auxiliary_client.OpenAI") as mock_openai,
        ):
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, model = resolve_provider_client("main", "gpt-5.4")

        assert client is not None
        assert model == "gpt-5.4"
        assert mock_openai.called


class TestResolveProviderClientNamedCustom:
    """resolve_provider_client should resolve named custom providers directly."""

    def test_named_custom_provider(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "test-model"},
            "custom_providers": [
                {"name": "beans", "base_url": "http://beans.local/v1", "api_key": "k"},
            ],
        })
        from agent.auxiliary_client import resolve_provider_client
        client, model = resolve_provider_client("beans", "my-model")
        assert client is not None
        assert model == "my-model"
        assert "beans.local" in str(client.base_url)


    def test_named_custom_no_api_key_uses_fallback(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "test"},
            "custom_providers": [
                {"name": "local", "base_url": "http://localhost:8080/v1"},
            ],
        })
        from agent.auxiliary_client import resolve_provider_client
        client, model = resolve_provider_client("local", "test")
        assert client is not None
        # no-key-required should be used



class TestResolveProviderClientModelNormalization:
    """Direct-provider auxiliary routing should normalize models like main runtime."""

    def test_matching_native_prefix_is_stripped_for_main_provider(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "zai/glm-5.1", "provider": "zai"},
        })
        with (
            patch("hermes_cli.auth.resolve_api_key_provider_credentials", return_value={
                "api_key": "glm-key",
                "base_url": "https://api.z.ai/api/paas/v4",
            }),
            patch("agent.auxiliary_client.OpenAI") as mock_openai,
        ):
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, model = resolve_provider_client("main", "zai/glm-5.1")

        assert client is not None
        assert model == "glm-5.1"


    def test_aggregator_vendor_slug_is_preserved(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_provider_client

            client, model = resolve_provider_client(
                "openrouter", "anthropic/claude-sonnet-4.6"
            )

        assert client is not None
        assert model == "anthropic/claude-sonnet-4.6"


class TestResolveVisionProviderClientModelNormalization:
    """Vision auto-routing should reuse the same provider-specific normalization."""

    def test_vision_auto_strips_matching_main_provider_prefix(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "zai/glm-5.1", "provider": "zai"},
        })
        with (
            patch("agent.auxiliary_client._read_nous_auth", return_value=None),
            patch("hermes_cli.auth.resolve_api_key_provider_credentials", return_value={
                "api_key": "glm-key",
                "base_url": "https://api.z.ai/api/paas/v4",
            }),
            patch("agent.auxiliary_client.OpenAI") as mock_openai,
        ):
            mock_openai.return_value = MagicMock()
            from agent.auxiliary_client import resolve_vision_provider_client

            provider, client, model = resolve_vision_provider_client()

        assert provider == "zai"
        assert client is not None
        assert model == "glm-5v-turbo"  # zai has dedicated vision model in _PROVIDER_VISION_MODELS


class TestVisionPathApiMode:
    """Vision path should propagate api_mode to _get_cached_client."""

    def test_explicit_provider_passes_api_mode(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"default": "test-model"},
            "auxiliary": {"vision": {"api_mode": "chat_completions"}},
        })
        with patch("agent.auxiliary_client._get_cached_client") as mock_gcc:
            mock_gcc.return_value = (MagicMock(), "test-model")
            from agent.auxiliary_client import resolve_vision_provider_client

            provider, client, model = resolve_vision_provider_client(provider="deepseek")

        mock_gcc.assert_called_once()
        _, kwargs = mock_gcc.call_args
        assert kwargs.get("api_mode") == "chat_completions"


class TestProvidersDictApiModeAnthropicMessages:
    """Regression guard for #15033.

    Named providers declared under the ``providers:`` dict with
    ``api_mode: anthropic_messages`` must route auxiliary calls through
    the Anthropic Messages API (via AnthropicAuxiliaryClient), not
    through an OpenAI chat-completions client.

    The bug had two halves: the providers-dict branch of
    ``_get_named_custom_provider`` dropped the ``api_mode`` field, and
    ``resolve_provider_client``'s named-custom branch never read it.
    """

    def test_providers_dict_propagates_api_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYRELAY_API_KEY", "sk-test")
        _write_config(tmp_path, {
            "providers": {
                "myrelay": {
                    "name": "myrelay",
                    "base_url": "https://example-relay.test/anthropic",
                    "key_env": "MYRELAY_API_KEY",
                    "api_mode": "anthropic_messages",
                    "default_model": "claude-opus-4-7",
                },
            },
        })
        from hermes_cli.runtime_provider import _get_named_custom_provider
        entry = _get_named_custom_provider("myrelay")
        assert entry is not None
        assert entry.get("api_mode") == "anthropic_messages"
        assert entry.get("base_url") == "https://example-relay.test/anthropic"
        assert entry.get("api_key") == "sk-test"



    def test_resolve_provider_client_returns_anthropic_client(self, tmp_path, monkeypatch):
        """Named custom provider with api_mode=anthropic_messages must
        route through AnthropicAuxiliaryClient."""
        monkeypatch.setenv("MYRELAY_API_KEY", "sk-test")
        _write_config(tmp_path, {
            "providers": {
                "myrelay": {
                    "name": "myrelay",
                    "base_url": "https://example-relay.test/anthropic",
                    "key_env": "MYRELAY_API_KEY",
                    "api_mode": "anthropic_messages",
                    "default_model": "claude-opus-4-7",
                },
            },
        })
        from agent.auxiliary_client import (
            resolve_provider_client,
            AnthropicAuxiliaryClient,
            AsyncAnthropicAuxiliaryClient,
        )
        sync_client, sync_model = resolve_provider_client("myrelay", async_mode=False)
        assert isinstance(sync_client, AnthropicAuxiliaryClient), (
            f"expected AnthropicAuxiliaryClient, got {type(sync_client).__name__}"
        )
        assert sync_model == "claude-opus-4-7"

        async_client, async_model = resolve_provider_client("myrelay", async_mode=True)
        assert isinstance(async_client, AsyncAnthropicAuxiliaryClient), (
            f"expected AsyncAnthropicAuxiliaryClient, got {type(async_client).__name__}"
        )
        assert async_model == "claude-opus-4-7"




class TestCustomProviderAliasCollision:
    """A user-declared custom_providers entry whose name matches a built-in
    *alias* (not a canonical provider) must win over the built-in.

    Regression guard for #15743: users who defined fallback_model pointing at
    a custom_providers entry named ``kimi`` were having requests routed to
    the built-in kimi-coding endpoint because ``_normalize_aux_provider``
    rewrote ``kimi`` → ``kimi-coding`` before the named-custom lookup.
    """

    def test_custom_named_kimi_wins_over_builtin_alias(self, tmp_path):
        _write_config(tmp_path, {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
            "custom_providers": [
                {
                    "name": "kimi",
                    "base_url": "https://my-custom-kimi.example.com/v1",
                    "api_key": "my-kimi-key",
                    "models": {"my-kimi-model": {"context_length": 200000}},
                },
            ],
        })
        from agent.auxiliary_client import resolve_provider_client
        from openai import OpenAI
        client, model = resolve_provider_client("kimi", model="my-kimi-model", raw_codex=True)
        assert isinstance(client, OpenAI)
        assert "my-custom-kimi.example.com" in str(client.base_url)
        assert client.api_key == "my-kimi-key"
        assert model == "my-kimi-model"

    def test_bare_kimi_without_custom_still_routes_to_builtin(self, tmp_path, monkeypatch):
        """Regression guard: bare 'kimi' with no custom entry must still
        reach the built-in kimi-coding provider."""
        _write_config(tmp_path, {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
        })
        monkeypatch.setenv("KIMI_API_KEY", "builtin-kimi-key")
        from agent.auxiliary_client import resolve_provider_client
        client, _ = resolve_provider_client("kimi", model="kimi-k2-0905-preview", raw_codex=True)
        assert client is not None
        base_url = str(client.base_url)
        # Built-in kimi-coding points at api.moonshot.ai
        assert "moonshot" in base_url or "kimi" in base_url, f"unexpected base_url {base_url!r}"

    def test_explicit_overrides_applied_on_api_key_branch(self, tmp_path, monkeypatch):
        """Explicit base_url/api_key from the caller must override the
        registered provider's defaults on the API-key branch.  Used by
        _try_activate_fallback to route a fallback through a built-in
        provider name but targeting a user-supplied endpoint."""
        _write_config(tmp_path, {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
        })
        monkeypatch.setenv("KIMI_API_KEY", "builtin-kimi-key")
        from agent.auxiliary_client import resolve_provider_client
        from openai import OpenAI
        client, _ = resolve_provider_client(
            "kimi-coding", model="kimi-k2", raw_codex=True,
            explicit_base_url="https://override.example.com",
            explicit_api_key="override-key",
        )
        assert isinstance(client, OpenAI)
        assert "override.example.com" in str(client.base_url)
        assert client.api_key == "override-key"


class TestResolveProviderClientMainRuntimeCustom:
    """When the main agent uses a named custom provider (custom:<name>),
    resolve_provider_client('custom', ..., main_runtime=...) must reuse the
    main_runtime's base_url + api_key instead of re-resolving from the bare
    'custom' provider name.  Re-resolution loses the provider name and falls
    back to OpenRouter or a wrong API-key provider. (#45472)"""

    def test_custom_provider_main_runtime_used_directly(self, tmp_path, monkeypatch):
        """main_runtime with base_url + api_key for a named custom provider
        is used directly, bypassing the _try_custom_endpoint / API-key
        fallback chain."""
        from agent.auxiliary_client import resolve_provider_client
        main_runtime = {
            "provider": "custom",
            "base_url": "https://my-gateway.example.com/v1",
            "api_key": "***",
            "model": "glm-5.1",
        }
        client, model = resolve_provider_client(
            "custom",
            model="explicit-glm-5.1",
            main_runtime=main_runtime,
        )
        assert client is not None
        assert model == "explicit-glm-5.1"
        assert "my-gateway.example.com" in str(client.base_url)
        assert client.api_key == "***"

    def test_custom_provider_main_runtime_no_credentials_falls_through(self, tmp_path, monkeypatch):
        """When main_runtime has no base_url or no api_key, the existing
        _try_custom_endpoint / _resolve_api_key_provider fallback chain is
        still tried."""
        # Ensure no env-provided credentials interfere
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from agent.auxiliary_client import resolve_provider_client
        # main_runtime with key but no base_url → must fall through
        client, model = resolve_provider_client(
            "custom",
            main_runtime={"api_key": "k", "base_url": ""},
        )
        # Should fall through to _try_custom_endpoint → return None,None
        # because no OPENAI_BASE_URL is set and no custom endpoint is configured
        assert client is None

    def test_custom_provider_main_runtime_respects_explicit_base_url(self, tmp_path):
        """explicit_base_url still wins over main_runtime — the caller's
        explicit argument is the strongest signal."""
        from agent.auxiliary_client import resolve_provider_client
        main_runtime = {
            "base_url": "https://main-runtime.example.com/v1",
            "api_key": "sk-main",
            "model": "ignored-model",
        }
        client, model = resolve_provider_client(
            "custom",
            model="explicit-model",
            explicit_base_url="https://explicit.example.com/v1",
            explicit_api_key="sk-explicit",
            main_runtime=main_runtime,
        )
        assert client is not None
        assert model == "explicit-model"
        assert "explicit.example.com" in str(client.base_url)
        assert client.api_key == "sk-explicit"


class _B292RecordingHandler(http.server.BaseHTTPRequestHandler):
    received_requests = []

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""
        parsed_body = {}
        if body:
            try:
                parsed_body = json.loads(body.decode("utf-8"))
            except Exception:
                pass
        self.__class__.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": parsed_body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response_payload = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "named-custom-response"},
                    "finish_reason": "stop",
                }
            ],
        }
        self.wfile.write(json.dumps(response_payload).encode("utf-8"))

    def log_message(self, format, *args):
        pass


@pytest.fixture
def b292_recorder():
    import http.server
    _B292RecordingHandler.received_requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _B292RecordingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/v1"
    try:
        yield {"server": server, "base_url": base_url, "requests": _B292RecordingHandler.received_requests}
    finally:
        server.shutdown()
        import agent.auxiliary_client as aux
        aux.shutdown_cached_clients()


class TestNamedCustomQualificationB292:
    """B292: Qualify named custom provider resolution and preservation."""

    def test_direct_named_custom_both_spellings(self, b292_recorder):
        """Both custom:<name> and bare <name> spellings route to named endpoint and credential."""
        import agent.auxiliary_client as aux
        base_url = b292_recorder["base_url"]
        requests = b292_recorder["requests"]

        cfg = {
            "providers": {
                "named-eval-prov": {
                    "name": "named-eval-prov",
                    "base_url": base_url,
                    "key_env": "NAMED_EVAL_KEY",
                    "api_mode": "chat_completions",
                    "default_model": "bare-custom-model",
                }
            }
        }

        with patch.dict(os.environ, {
            "NAMED_EVAL_KEY": "synth-named-key-456",
            "OPENAI_API_KEY": "unrelated-global-openai-key",
            "ANTHROPIC_API_KEY": "unrelated-global-anthropic-key",
        }):
            with patch("hermes_cli.config.load_config", return_value=cfg), \
                 patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
                 patch("hermes_cli.runtime_provider.load_config", return_value=cfg):

                aux.shutdown_cached_clients()
                resp1 = aux.call_llm(
                    task="compression",
                    provider="custom:named-eval-prov",
                    model="bare-custom-model",
                    api_mode="chat_completions",
                    messages=[{"role": "user", "content": "hello spelling 1"}],
                )
                assert resp1.choices[0].message.content == "named-custom-response"

                aux.shutdown_cached_clients()
                resp2 = aux.call_llm(
                    task="compression",
                    provider="named-eval-prov",
                    model="bare-custom-model",
                    api_mode="chat_completions",
                    messages=[{"role": "user", "content": "hello spelling 2"}],
                )
                assert resp2.choices[0].message.content == "named-custom-response"

        assert len(requests) == 2
        for req in requests:
            auth_hdr = req["headers"].get("Authorization", "")
            assert auth_hdr == "Bearer synth-named-key-456"
            assert "unrelated-global" not in auth_hdr
            assert req["body"].get("model") == "bare-custom-model"

    def test_configured_fallback_named_custom_both_spellings(self, b292_recorder):
        """Configured fallback routes to named custom provider with both spellings."""
        import agent.auxiliary_client as aux
        base_url = b292_recorder["base_url"]
        requests = b292_recorder["requests"]

        def run_fallback(spelling: str):
            cfg = {
                "providers": {
                    "named-eval-prov": {
                        "name": "named-eval-prov",
                        "base_url": base_url,
                        "key_env": "NAMED_EVAL_KEY",
                        "api_mode": "chat_completions",
                        "default_model": "fallback-bare-model",
                    }
                },
                "auxiliary": {
                    "test_task": {
                        "fallback_chain": [
                            {"provider": spelling, "model": "fallback-bare-model"}
                        ]
                    }
                },
            }
            aux.shutdown_cached_clients()
            with patch.dict(os.environ, {
                "PRIMARY_KEY": "synth-pri-key-111",
                "NAMED_EVAL_KEY": "synth-named-key-456",
            }):
                with patch("hermes_cli.config.load_config", return_value=cfg), \
                     patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
                     patch("hermes_cli.runtime_provider.load_config", return_value=cfg), \
                     patch("agent.auxiliary_client._transient_retry_count", return_value=0):
                    return aux.call_llm(
                        task="test_task",
                        provider="custom",
                        model="pri-model",
                        base_url="http://127.0.0.1:1/v1",
                        api_key="synth-pri-key-111",
                        api_mode="chat_completions",
                        messages=[{"role": "user", "content": f"fallback test {spelling}"}],
                    )

        r1 = run_fallback("custom:named-eval-prov")
        assert r1.choices[0].message.content == "named-custom-response"

        r2 = run_fallback("named-eval-prov")
        assert r2.choices[0].message.content == "named-custom-response"

        assert len(requests) == 2
        for req in requests:
            auth_hdr = req["headers"].get("Authorization", "")
            assert auth_hdr == "Bearer synth-named-key-456"
            assert "synth-pri-key-111" not in auth_hdr
            assert req["body"].get("model") == "fallback-bare-model"

    def test_missing_named_custom_not_silently_another_provider(self):
        """Missing named custom config must raise rather than silently resolving to another provider."""
        import agent.auxiliary_client as aux
        cfg = {"providers": {}}
        aux.shutdown_cached_clients()
        with patch("hermes_cli.config.load_config", return_value=cfg), \
             patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
             patch("hermes_cli.runtime_provider.load_config", return_value=cfg):

            with pytest.raises(RuntimeError, match="not found|no API key"):
                aux.call_llm(
                    task="compression",
                    provider="custom:definitely-nonexistent-prov",
                    model="some-model",
                    messages=[{"role": "user", "content": "hi"}],
                )

            with pytest.raises(RuntimeError, match="not found|no API key"):
                aux.call_llm(
                    task="compression",
                    provider="definitely-nonexistent-prov",
                    model="some-model",
                    messages=[{"role": "user", "content": "hi"}],
                )

    def test_anonymous_custom_preservation(self, b292_recorder):
        """Anonymous custom (provider='custom' + explicit base_url) is preserved."""
        import agent.auxiliary_client as aux
        base_url = b292_recorder["base_url"]
        requests = b292_recorder["requests"]

        aux.shutdown_cached_clients()
        resp = aux.call_llm(
            task="compression",
            provider="custom",
            base_url=base_url,
            api_key="anon-synth-key-789",
            model="anon-model",
            api_mode="chat_completions",
            messages=[{"role": "user", "content": "hi anon"}],
        )
        assert resp.choices[0].message.content == "named-custom-response"
        assert len(requests) == 1
        assert requests[0]["headers"].get("Authorization") == "Bearer anon-synth-key-789"

    def test_builtin_provider_preservation_not_shadowed(self):
        """Built-in provider (e.g. openrouter) is preserved and not shadowed by custom provider lookup."""
        from hermes_cli.runtime_provider import _get_named_custom_provider

        assert _get_named_custom_provider("openrouter") is None
        assert _get_named_custom_provider("openai") is None
        assert _get_named_custom_provider("auto") is None
