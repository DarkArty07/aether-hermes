"""Responses terminal/phase fidelity and ``tool_choice`` passthrough for the
auxiliary Codex adapter (Aether issue #420, contract AC-07).

The auxiliary ``_CodexCompletionsAdapter`` is the boundary between Hermes and
the maintained Responses route.  Before this regression suite existed, the
adapter:

* synthesized ``finish_reason="stop"`` whenever the response carried no tool
  calls, discarding ``status`` and ``incomplete_details`` — so a truncated
  (``max_output_tokens``), content-filtered or cancelled terminal was
  indistinguishable from a completed answer;
* concatenated text from *every* ``message`` item, so ``commentary`` /
  ``analysis`` narration contaminated the final payload;
* ignored ``output_text`` when ``output`` was empty;
* never forwarded ``tool_choice``.

These tests drive the real adapter (and, for the passthrough check, the real
``call_llm`` -> ``_build_call_kwargs`` -> adapter chain) with synthetic
Responses objects.  No network, no provider, no credential.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent.auxiliary_client as aux
from agent.auxiliary_client import (
    CodexAuxiliaryClient,
    _CodexCompletionsAdapter,
    _build_call_kwargs,
    call_llm,
)

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_graph_fragment",
        "description": "Submit one validated graph fragment.",
        "parameters": {
            "type": "object",
            "properties": {"nodes": {"type": "array"}},
            "required": ["nodes"],
        },
    },
}


def _message_item(text, *, phase=None, status=None, part_type="output_text"):
    item = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type=part_type, text=text)],
    )
    if phase is not None:
        item.phase = phase
    if status is not None:
        item.status = status
    return item


def _function_call_item(name, arguments, *, call_id="call_1", status=None):
    item = SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )
    if status is not None:
        item.status = status
    return item


def _final(
    *,
    status,
    output=None,
    output_text="",
    incomplete_details=None,
    usage=None,
    error=None,
):
    return SimpleNamespace(
        output=[] if output is None else output,
        output_text=output_text,
        usage=usage,
        status=status,
        incomplete_details=incomplete_details,
        error=error,
    )


class _CapturingResponses:
    """Fake ``client.responses`` that records the request it was handed."""

    def __init__(self, final):
        self.final = final
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.final


def _adapter(final, model="gpt-5.6-luna"):
    responses = _CapturingResponses(final)
    client = SimpleNamespace(responses=responses, close=lambda: None)
    return _CodexCompletionsAdapter(client, model), responses


def _extract(final, **kwargs):
    adapter, _ = _adapter(final)
    return adapter.create(messages=[{"role": "user", "content": "extract"}], **kwargs)


class TestResponsesTerminalFidelity:
    """A terminal that is not a completed answer must never read as ``stop``."""

    def test_incomplete_max_output_tokens_is_length_not_stop(self):
        final = _final(
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
            output=[_message_item('{"nodes": [')],
        )

        response = _extract(final)

        assert response.choices[0].finish_reason == "length"
        assert response.status == "incomplete"
        assert response.incomplete_reason == "max_output_tokens"
        # The partial text is preserved for diagnosis; classifying it as a
        # complete answer is the caller's decision, not the adapter's.
        assert response.choices[0].message.content == '{"nodes": ['

    def test_unknown_incomplete_reason_is_still_length_not_stop(self):
        final = _final(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="safety_something"),
            output=[_message_item("partial")],
        )

        response = _extract(final)

        assert response.choices[0].finish_reason == "length"
        assert response.incomplete_reason == "safety_something"

    def test_content_filter_incomplete_is_not_stop(self):
        final = _final(
            status="incomplete",
            incomplete_details={"reason": "content_filter"},
            output=[],
        )

        response = _extract(final)

        assert response.choices[0].finish_reason == "content_filter"
        assert response.choices[0].message.content is None
        assert response.status == "incomplete"
        assert response.incomplete_reason == "content_filter"

    def test_cancelled_terminal_raises_typed_failure(self):
        final = _final(status="cancelled", output=[_message_item("partial")])
        error_type = getattr(aux, "AuxiliaryResponsesTerminalError", None)

        assert error_type is not None, (
            "a cancelled Responses terminal must surface a typed failure, "
            "never a successful finish_reason"
        )
        with pytest.raises(error_type) as excinfo:
            _extract(final)

        assert excinfo.value.status == "cancelled"
        assert "cancelled" in str(excinfo.value)

    def test_failed_terminal_raises_typed_failure_with_provider_error(self):
        final = _final(
            status="failed",
            error={"code": "server_error", "message": "upstream exploded"},
        )
        error_type = getattr(aux, "AuxiliaryResponsesTerminalError", None)

        assert error_type is not None, (
            "a failed Responses terminal must surface a typed failure, "
            "never a successful finish_reason"
        )
        with pytest.raises(error_type) as excinfo:
            _extract(final)

        assert excinfo.value.status == "failed"
        assert "server_error" in str(excinfo.value)
        assert "upstream exploded" in str(excinfo.value)

    def test_stream_incomplete_frame_preserves_reason(self):
        events = [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(
                type="response.output_item.done",
                item=_message_item('{"nodes": ['),
            ),
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                    id="resp_incomplete",
                    usage=None,
                ),
            ),
        ]

        class _CreateStream:
            def __iter__(self):
                return iter(events)

            def close(self):
                pass

        class _Responses:
            def create(self, **kwargs):
                return _CreateStream()

        adapter = _CodexCompletionsAdapter(
            SimpleNamespace(responses=_Responses()), "gpt-5.6-luna"
        )
        response = adapter.create(messages=[{"role": "user", "content": "extract"}])

        assert response.choices[0].finish_reason == "length"
        assert response.incomplete_reason == "max_output_tokens"


class TestResponsesPhaseFidelity:
    """Commentary/analysis narration must not reach the final payload."""

    def test_commentary_phase_does_not_contaminate_final_answer(self):
        final = _final(
            status="completed",
            output=[
                _message_item(
                    "I will inspect the sources before answering.",
                    phase="commentary",
                ),
                _message_item('{"nodes": [{"id": "a"}]}', phase="final_answer"),
            ],
        )

        response = _extract(final)
        message = response.choices[0].message

        assert message.content == '{"nodes": [{"id": "a"}]}'
        assert "I will inspect the sources" not in message.content
        assert message.reasoning == "I will inspect the sources before answering."
        assert response.choices[0].finish_reason == "stop"

    def test_analysis_phase_is_not_final_content(self):
        final = _final(
            status="completed",
            output=[_message_item("thinking out loud", phase="analysis")],
            output_text="thinking out loud",
        )

        response = _extract(final)

        assert response.choices[0].message.content is None
        assert response.choices[0].finish_reason == "incomplete"

    def test_analysis_plus_final_answer_is_a_completed_answer(self):
        final = _final(
            status="completed",
            output=[
                _message_item("reasoning first", phase="analysis"),
                _message_item('{"nodes": []}', phase="final_answer"),
            ],
        )

        response = _extract(final)

        assert response.choices[0].message.content == '{"nodes": []}'
        assert response.choices[0].finish_reason == "stop"


class TestResponsesOutputFallback:
    def test_empty_output_with_output_text_is_recovered(self):
        final = _final(
            status="completed",
            output=[],
            output_text='{"nodes": [], "edges": []}',
        )

        response = _extract(final)

        assert response.choices[0].message.content == '{"nodes": [], "edges": []}'
        assert response.choices[0].finish_reason == "stop"

    def test_commentary_only_response_is_not_recovered_as_final(self):
        final = _final(
            status="completed",
            output=[_message_item("narrating progress", phase="commentary")],
            output_text="narrating progress",
        )

        response = _extract(final)

        assert response.choices[0].message.content is None
        assert response.choices[0].finish_reason == "incomplete"

    def test_completed_empty_response_stays_empty_not_incomplete(self):
        final = _final(status="completed", output=[], output_text="")

        response = _extract(final)

        assert response.choices[0].message.content is None
        assert response.choices[0].finish_reason == "stop"
        assert response.status == "completed"


class TestResponsesItemShape:
    def test_completed_function_call_is_tool_calls(self):
        final = _final(
            status="completed",
            output=[_function_call_item("submit_graph_fragment", '{"nodes": []}')],
        )

        response = _extract(final)

        assert response.choices[0].finish_reason == "tool_calls"
        tool_calls = response.choices[0].message.tool_calls
        assert tool_calls and tool_calls[0].function.name == "submit_graph_fragment"
        assert tool_calls[0].function.arguments == '{"nodes": []}'

    def test_in_progress_function_call_is_not_a_completed_tool_call(self):
        final = _final(
            status="completed",
            output=[
                _function_call_item(
                    "submit_graph_fragment", '{"nodes": []}', status="in_progress"
                )
            ],
        )

        response = _extract(final)

        assert response.choices[0].message.tool_calls is None
        assert response.choices[0].finish_reason == "incomplete"

    def test_incomplete_message_item_never_reports_stop(self):
        final = _final(
            status="completed",
            output=[_message_item("half a sentence", status="incomplete")],
        )

        response = _extract(final)

        assert response.choices[0].finish_reason == "incomplete"

    def test_malformed_items_do_not_fabricate_content(self):
        final = _final(
            status="completed",
            output=[
                SimpleNamespace(type="message", content="not-a-list"),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text=None)],
                ),
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
                SimpleNamespace(type="future_unknown_item"),
            ],
        )

        response = _extract(final)

        assert response.choices[0].message.content == "ok"
        assert response.choices[0].finish_reason == "stop"

    def test_non_list_output_is_treated_as_empty(self):
        final = _final(status="completed", output="not-a-list", output_text="")

        response = _extract(final)

        assert response.choices[0].message.content is None
        assert response.choices[0].finish_reason == "stop"


class TestToolChoicePassthrough:
    """``tool_choice`` reaches the Responses request; unsupported fields do not."""

    def test_function_tool_choice_is_translated_to_responses_shape(self):
        adapter, responses = _adapter(
            _final(status="completed", output=[_message_item("ok")])
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            tools=[copy.deepcopy(TOOL_SCHEMA)],
            tool_choice={
                "type": "function",
                "function": {"name": "submit_graph_fragment"},
            },
        )

        assert responses.kwargs["tool_choice"] == {
            "type": "function",
            "name": "submit_graph_fragment",
        }
        assert responses.kwargs["tools"][0]["name"] == "submit_graph_fragment"

    def test_string_tool_choice_is_forwarded(self):
        adapter, responses = _adapter(
            _final(status="completed", output=[_message_item("ok")])
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            tools=[copy.deepcopy(TOOL_SCHEMA)],
            tool_choice="required",
        )

        assert responses.kwargs["tool_choice"] == "required"

    def test_unrepresentable_tool_choice_is_not_sent(self):
        adapter, responses = _adapter(
            _final(status="completed", output=[_message_item("ok")])
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            tools=[copy.deepcopy(TOOL_SCHEMA)],
            tool_choice={"type": "allowed_tools", "mode": "auto"},
        )

        assert "tool_choice" not in responses.kwargs
        assert responses.kwargs["tools"]

    def test_tool_choice_without_tools_is_not_sent(self):
        adapter, responses = _adapter(
            _final(status="completed", output=[_message_item("ok")])
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            tool_choice="required",
        )

        assert "tool_choice" not in responses.kwargs
        assert "tools" not in responses.kwargs

    def test_unsupported_router_fields_are_not_sent(self):
        adapter, responses = _adapter(
            _final(status="completed", output=[_message_item("ok")])
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            tools=[copy.deepcopy(TOOL_SCHEMA)],
            tool_choice="required",
            # Chat-style fields the certified Responses subset rejects.
            max_output_tokens=512,
            response_format={"type": "json_object"},
            text={"format": {"type": "json_schema"}},
        )

        for unsupported in ("max_output_tokens", "response_format", "text"):
            assert unsupported not in responses.kwargs
        assert responses.kwargs["tool_choice"] == "required"


class TestBuildCallKwargsToolChoice:
    def test_tool_choice_is_carried_alongside_tools(self):
        kwargs = _build_call_kwargs(
            "openai",
            "gpt-5.5",
            [{"role": "user", "content": "extract"}],
            tools=[copy.deepcopy(TOOL_SCHEMA)],
            tool_choice="required",
            timeout=30.0,
        )

        assert kwargs["tool_choice"] == "required"

    def test_tool_choice_is_dropped_without_tools(self):
        kwargs = _build_call_kwargs(
            "openai",
            "gpt-5.5",
            [{"role": "user", "content": "extract"}],
            tool_choice="required",
            timeout=30.0,
        )

        assert "tool_choice" not in kwargs
        assert "tools" not in kwargs


class TestCallLlmToolChoicePassthrough:
    def test_call_llm_delivers_tool_choice_to_the_responses_adapter(self):
        responses = _CapturingResponses(
            _final(
                status="completed",
                output=[_function_call_item("submit_graph_fragment", '{"nodes": []}')],
            )
        )
        real_client = SimpleNamespace(
            responses=responses,
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            close=lambda: None,
        )
        codex_client = CodexAuxiliaryClient(real_client, "gpt-5.5")

        with (
            patch(
                "agent.auxiliary_client._resolve_task_provider_model",
                return_value=("openai-codex", "gpt-5.5", None, "test-key", None),
            ),
            patch(
                "agent.auxiliary_client._get_cached_client",
                return_value=(codex_client, "gpt-5.5"),
            ),
            patch(
                "agent.auxiliary_client._get_task_extra_body",
                return_value={},
            ),
        ):
            response = call_llm(
                task="web_extract",
                messages=[{"role": "user", "content": "extract"}],
                tools=[copy.deepcopy(TOOL_SCHEMA)],
                tool_choice={
                    "type": "function",
                    "function": {"name": "submit_graph_fragment"},
                },
                timeout=30.0,
            )

        assert responses.kwargs["tool_choice"] == {
            "type": "function",
            "name": "submit_graph_fragment",
        }
        assert response.choices[0].finish_reason == "tool_calls"
