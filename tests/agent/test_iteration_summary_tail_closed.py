"""A failed or empty iteration-limit summary never leaves its request as the transcript tail.

``handle_max_iterations`` appends a synthetic user turn asking for a wrap-up "without
calling any more tools".  If nothing answered it, the durable transcript would end on that
user row and the resume-time alternation repair would concatenate it onto the user's NEXT
real prompt, carrying a stale stop-calling-tools instruction into a turn whose budget has
reset.  The finalizer's "delivered final_response => assistant row" invariant closes the
tail with the fallback text instead, so the request is always an answered, historical turn.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from run_agent import AIAgent
from tests.agent.test_run_agent import _make_tool_defs, _mock_response, _mock_tool_call


@pytest.fixture(autouse=True)
def _mock_plugin_discovery(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
        a.client = MagicMock()
        return a


def _summary_raises(*_args, **_kwargs):
    raise RuntimeError("provider down")


def _summary_empty(*_args, **_kwargs):
    return _mock_response(content="")


@pytest.mark.parametrize("summary", [_summary_raises, _summary_empty], ids=["raises", "empty"])
def test_unanswered_summary_request_is_closed_and_never_merges_into_the_next_prompt(agent, monkeypatch, summary):
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.max_iterations = 1
    tool_resp = _mock_response(
        content="", finish_reason="tool_calls",
        tool_calls=[_mock_tool_call(name="web_search", arguments="{}", call_id="c1")],
    )
    calls = []

    def provider(*args, **kwargs):
        calls.append(kwargs)
        return tool_resp if len(calls) == 1 else summary()

    request_client = MagicMock()
    request_client.chat.completions.create.side_effect = provider
    agent.client.chat.completions.create.side_effect = provider
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: request_client)
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)

    with (
        patch("model_tools.handle_function_call", return_value="ok"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the work")

    assert len(calls) >= 2, "the summary request never reached the provider fixture"
    messages = result["messages"]
    nudges = [i for i, m in enumerate(messages) if m.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST]
    assert len(nudges) == 1, "expected exactly one summary request in the transcript"
    reply = messages[nudges[0] + 1]
    assert reply["role"] == "assistant" and reply["content"] == result["final_response"]
    assert messages[-1] is reply

    # Resume: the next real prompt must reach the model verbatim, not prefixed by the request.
    from agent.agent_runtime_helpers import repair_message_sequence
    resumed = [dict(m) for m in messages] + [{"role": "user", "content": "next real prompt"}]
    repair_message_sequence(agent, resumed)
    assert resumed[-1] == {"role": "user", "content": "next real prompt"}
