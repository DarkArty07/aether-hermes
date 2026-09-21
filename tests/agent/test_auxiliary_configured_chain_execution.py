"""Behavioral coverage for ordered auxiliary fallback-chain execution."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import agent.auxiliary_client as aux
import pytest


def _response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _rate_limit(message: str):
    error = RuntimeError(message)
    error.status_code = 429
    return error


def _payment_error(message: str):
    error = RuntimeError(message)
    error.status_code = 402
    return error


def _auth_error(message: str):
    error = RuntimeError(message)
    error.status_code = 401
    return error


def _status_error(status_code: int, message: str):
    error = RuntimeError(message)
    error.status_code = status_code
    return error


@pytest.fixture(autouse=True)
def _clear_auxiliary_unhealthy_cache():
    aux._aux_unhealthy_until.clear()
    aux._aux_unhealthy_logged_at.clear()
    yield
    aux._aux_unhealthy_until.clear()
    aux._aux_unhealthy_logged_at.clear()


def _sync_client(*, result=None, error=None):
    create = MagicMock(return_value=result, side_effect=error)
    return SimpleNamespace(
        base_url="https://router.example.invalid/v1",
        api_key="synthetic-test-key",
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )


def _async_client(*, result=None, error=None):
    create = AsyncMock(return_value=result, side_effect=error)
    return SimpleNamespace(
        base_url="https://router.example.invalid/v1",
        api_key="synthetic-test-key",
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )


def _task_config():
    return {
        "fallback_chain": [
            {
                "provider": "custom:aether-commandcode",
                "model": "commandcode/deepseek/deepseek-v4.1-flash",
                "base_url": "https://router.example.invalid/v1",
            },
            {
                "provider": "custom:aether-codex",
                "model": "gpt-5.6-luna",
                "base_url": "https://router.example.invalid/v1",
            },
        ]
    }


@pytest.mark.parametrize(
    "primary_error",
    [
        pytest.param(_rate_limit("gemini busy"), id="primary-rate-limit"),
        pytest.param(_auth_error("gemini credential rejected"), id="primary-auth"),
    ],
)
@pytest.mark.parametrize(
    "deepseek_error",
    [
        pytest.param(_rate_limit("deepseek busy"), id="rate-limit"),
        pytest.param(_payment_error("deepseek credits exhausted"), id="payment"),
        pytest.param(
            _status_error(403, "commandcode plan is not authorized"),
            id="plan-gate",
        ),
        pytest.param(
            _status_error(502, "commandcode upstream failed"),
            id="upstream-outage",
        ),
    ],
)
def test_sync_request_failure_advances_to_next_configured_entry(
    primary_error, deepseek_error
):
    primary = _sync_client(error=primary_error)
    deepseek = _sync_client(error=deepseek_error)
    luna = _sync_client(result=_response("served by luna"))
    clients = {
        "commandcode/deepseek/deepseek-v4.1-flash": deepseek,
        "gpt-5.6-luna": luna,
    }

    def resolve_entry(entry):
        model = entry["model"]
        return clients[model], model

    with (
        patch.object(
            aux,
            "_resolve_task_provider_model",
            return_value=(
                "custom:aether-antigravity",
                "gemini-3.8-flash-low",
                None,
                None,
                None,
            ),
        ),
        patch.object(
            aux,
            "_get_cached_client",
            return_value=(primary, "gemini-3.8-flash-low"),
        ),
        patch.object(aux, "_get_auxiliary_task_config", return_value=_task_config()),
        patch.object(aux, "_resolve_fallback_entry", side_effect=resolve_entry),
        patch.object(aux, "_transient_retry_count", return_value=0),
        patch.object(aux, "_try_main_agent_model_fallback") as main_fallback,
    ):
        result = aux.call_llm(
            task="title_generation",
            messages=[{"role": "user", "content": "title this"}],
        )

    assert result.choices[0].message.content == "served by luna"
    assert deepseek.chat.completions.create.call_count == 1
    assert luna.chat.completions.create.call_count == 1
    main_fallback.assert_not_called()


@pytest.mark.parametrize(
    "primary_error",
    [
        pytest.param(_rate_limit("gemini busy"), id="primary-rate-limit"),
        pytest.param(_auth_error("gemini credential rejected"), id="primary-auth"),
    ],
)
def test_async_request_failure_advances_to_next_configured_entry(primary_error):
    primary = _async_client(error=primary_error)
    deepseek_sync = _sync_client()
    luna_sync = _sync_client()
    deepseek_async = _async_client(error=_rate_limit("deepseek busy"))
    luna_async = _async_client(result=_response("served by luna async"))
    sync_clients = {
        "commandcode/deepseek/deepseek-v4.1-flash": deepseek_sync,
        "gpt-5.6-luna": luna_sync,
    }
    async_clients = {
        id(deepseek_sync): deepseek_async,
        id(luna_sync): luna_async,
    }

    def resolve_entry(entry):
        model = entry["model"]
        return sync_clients[model], model

    def to_async(client, model, is_vision=False):
        del is_vision
        return async_clients[id(client)], model

    with (
        patch.object(
            aux,
            "_resolve_task_provider_model",
            return_value=(
                "custom:aether-antigravity",
                "gemini-3.8-flash-low",
                None,
                None,
                None,
            ),
        ),
        patch.object(
            aux,
            "_get_cached_client",
            return_value=(primary, "gemini-3.8-flash-low"),
        ),
        patch.object(aux, "_get_auxiliary_task_config", return_value=_task_config()),
        patch.object(aux, "_resolve_fallback_entry", side_effect=resolve_entry),
        patch.object(aux, "_to_async_client", side_effect=to_async),
        patch.object(aux, "_transient_retry_count", return_value=0),
        patch.object(aux, "_try_main_agent_model_fallback") as main_fallback,
    ):
        result = asyncio.run(
            aux.async_call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "title this"}],
            )
        )

    assert result.choices[0].message.content == "served by luna async"
    assert deepseek_async.chat.completions.create.await_count == 1
    assert luna_async.chat.completions.create.await_count == 1
    main_fallback.assert_not_called()
