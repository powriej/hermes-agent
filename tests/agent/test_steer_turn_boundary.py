"""The pre-API ``/steer`` drain never reaches back past the current turn's user row.

The drain delivers a pending steer as a standalone user row after the newest tool result.
On an iteration with no fresh tool batch (the turn's first response truncated or came back
empty, so the loop re-entered its top) the newest tool row belongs to an EARLIER turn:
landing the steer there rewrites already-sent history (prompt-cache prefix, append-only
durable order) and puts the user's correction where the model will not act on it.  The
steer must instead stay pending for this turn's first real tool batch.
"""
from copy import deepcopy

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

STEER = "focus on error handling"


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return AIAgent(session_db=SessionDB(db_path=tmp_path / "proof.db"),
                   model="test-model", provider="openai-compat", api_key="test",
                   base_url="http://127.0.0.1:1/v1", max_iterations=4,
                   quiet_mode=True, skip_context_files=True, skip_memory=True)


def _tool_round(call_id):
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "out"},
    ]


def _previous_turn():
    return [{"role": "user", "content": "first ask"}, *_tool_round("c1"),
            {"role": "assistant", "content": "first answer"}]


def _prepare(agent, messages, **kwargs):
    from agent.turn_context import _reset_per_turn_agent_state
    from agent.turn_iteration_prep import prepare_iteration

    _reset_per_turn_agent_state(agent)
    agent.steer(STEER)
    return prepare_iteration(agent, messages=messages, api_call_count=2, **kwargs)


# The recorded index is right, stale past the list (mid-turn compaction shrank it), stale onto
# a historical row, or missing: the turn boundary is re-derived from the user text each time.
@pytest.mark.parametrize("recorded_idx", [4, 99, 0, None])
def test_steer_is_not_delivered_into_a_previous_turns_tool_result(tmp_path, monkeypatch, recorded_idx):
    agent = _agent(tmp_path, monkeypatch)
    try:
        messages = [*_previous_turn(), {"role": "user", "content": "second ask"}]
        snapshot = deepcopy(messages)
        prep = _prepare(agent, messages, user_message="second ask", current_turn_user_idx=recorded_idx)
        assert prep.messages == snapshot, "an earlier turn's history was rewritten"
        assert agent._pending_steer == STEER, "the steer must stay pending, not be lost"
    finally:
        agent._session_db.close()


def test_steer_stays_pending_when_the_turn_boundary_is_unknown(tmp_path, monkeypatch):
    """No user text and no usable index: there is no floor, so nothing may be rewritten."""
    agent = _agent(tmp_path, monkeypatch)
    try:
        messages = [*_previous_turn(), {"role": "user", "content": "second ask"}]
        snapshot = deepcopy(messages)
        prep = _prepare(agent, messages)
        assert prep.messages == snapshot
        assert agent._pending_steer == STEER
    finally:
        agent._session_db.close()


@pytest.mark.parametrize("recorded_idx", [4, 99, None])
def test_steer_still_lands_after_this_turns_newest_tool_result(tmp_path, monkeypatch, recorded_idx):
    """Positive control: the bound must not swallow a steer the current turn can take."""
    from agent.prompt_builder import STEER_MARKER_OPEN

    agent = _agent(tmp_path, monkeypatch)
    try:
        history = [*_previous_turn(), {"role": "user", "content": "second ask"}, *_tool_round("c2")]
        messages = deepcopy(history)
        prep = _prepare(agent, messages, user_message="second ask", current_turn_user_idx=recorded_idx)
        assert prep.messages[:-1] == history
        assert prep.messages[-1]["role"] == "user"
        assert STEER_MARKER_OPEN in prep.messages[-1]["content"] and STEER in prep.messages[-1]["content"]
        assert agent._pending_steer is None
    finally:
        agent._session_db.close()
