import importlib
import os
import pathlib

import pytest

from conftest import run_hook

S1, S2 = "11111111-aaaa-4bbb-8ccc-000000000001", "22222222-aaaa-4bbb-8ccc-000000000002"
P1, P2 = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"


def expansion(command="compact-handoff", session=S1, prompt=P1):
    return {"hook_event_name": "UserPromptExpansion", "session_id": session, "prompt_id": prompt,
            "expansion_type": "slash_command", "command_name": command, "command_args": "",
            "prompt": "/" + command}


def stop(session=S1, prompt=P1, event="Stop", transcript=None):
    p = {"hook_event_name": event, "session_id": session, "stop_hook_active": False,
         "last_assistant_message": "It is safe to compact.", "background_tasks": []}
    if prompt is not None:
        p["prompt_id"] = prompt
    if transcript is not None:
        p["transcript_path"] = str(transcript)
    return p


def state_file(env, key="7"):
    return pathlib.Path(env["COMPACTO_SIGNAL_DIR"]) / f"{key}.handoff"


def read_fields(env, key="7"):
    return state_file(env, key).read_text().rstrip("\n").split("\t")


@pytest.fixture
def handoff(env, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("COMPACTO_", "PRECOMPACT_", "TMUX")):
            monkeypatch.delenv(k)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import handoff as mod
    return importlib.reload(mod)


def test_handoff_expansion_records_running(env):
    r = run_hook("handoff.py", expansion(), env)
    assert r.returncode == 0 and r.stdout == ""
    state, session, prompt, started, ended = read_fields(env)
    assert (state, session, prompt, ended) == ("running", S1, P1, "")
    assert started.isdigit()


def test_other_command_expansion_records_nothing(env):
    run_hook("handoff.py", expansion("review"), env)
    assert not state_file(env).exists()


def test_configured_command_name_is_used_without_its_slash(env):
    conf = pathlib.Path(env["HOME"]) / ".claude" / "precompact.conf"
    conf.write_text("handoff_command=/my-handoff\n")
    run_hook("handoff.py", expansion("compact-handoff"), env)
    assert not state_file(env).exists()
    run_hook("handoff.py", expansion("my-handoff"), env)
    assert read_fields(env)[0] == "running"


def test_stop_of_the_same_turn_finishes_the_handoff(env):
    run_hook("handoff.py", expansion(), env)
    r = run_hook("handoff.py", stop(), env)
    assert r.returncode == 0 and r.stdout == ""
    state, session, prompt, started, ended = read_fields(env)
    assert (state, session, prompt) == ("done", S1, P1)
    assert ended.isdigit() and int(ended) >= int(started)


def test_stop_of_a_later_turn_marks_the_handoff_cut_short(env):
    # An interrupted turn fires no Stop, so the next Stop carries the next prompt's id.
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(prompt=P2), env)
    assert read_fields(env)[0] == "aborted"


def test_stop_without_prompt_id_changes_nothing(env):
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(prompt=None), env)
    assert read_fields(env)[0] == "running"


def test_stop_from_another_session_changes_nothing(env):
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(session=S2, prompt=P2), env)
    assert read_fields(env)[0] == "running"


def test_stop_failure_of_the_same_turn_marks_failed(env):
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(event="StopFailure"), env)
    assert read_fields(env)[0] == "failed"


def test_stop_failure_of_a_later_turn_marks_cut_short(env):
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(prompt=P2, event="StopFailure"), env)
    assert read_fields(env)[0] == "aborted"


def test_a_finished_handoff_ignores_later_stops(env):
    run_hook("handoff.py", expansion(), env)
    run_hook("handoff.py", stop(), env)
    run_hook("handoff.py", stop(prompt=P2), env)
    assert read_fields(env)[0] == "done"


def test_stop_stamps_the_pane_with_its_session_and_turn(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    run_hook("handoff.py", stop(transcript=transcript), env)
    stamp = pathlib.Path(env["COMPACTO_SIGNAL_DIR"]) / "7.hooked"
    assert stamp.read_text() == f"{S1}\n{P1}\n"


def test_a_stop_without_a_prompt_id_stamps_only_the_session(env, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    run_hook("handoff.py", stop(prompt=None, transcript=transcript), env)
    stamp = pathlib.Path(env["COMPACTO_SIGNAL_DIR"]) / "7.hooked"
    assert stamp.read_text() == f"{S1}\n"


@pytest.mark.parametrize("transcript", ["missing.jsonl", None])
def test_a_stop_without_a_transcript_on_disk_leaves_the_pane_unstamped(env, tmp_path, transcript):
    run_hook("handoff.py", stop(transcript=transcript and tmp_path / transcript), env)
    assert not list(pathlib.Path(env["COMPACTO_SIGNAL_DIR"]).glob("*.hooked"))


def test_outside_tmux_state_is_keyed_by_session_and_dropped_when_finished(env):
    del env["TMUX_PANE"]
    run_hook("handoff.py", expansion(), env)
    key = "s_" + S1.replace("-", "")
    assert read_fields(env, key)[0] == "running"
    run_hook("handoff.py", stop(), env)
    assert not state_file(env, key).exists()
    assert not list(pathlib.Path(env["COMPACTO_SIGNAL_DIR"]).glob("*.hooked"))


def test_psmux_pane_key_carries_the_server_pid(env):
    env["TMUX"] = "/tmp/psmux-4242/default,4242,0"
    run_hook("handoff.py", expansion(), env)
    assert read_fields(env, "4242_7")[0] == "running"


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"hook_event_name": "Stop"}'])
def test_malformed_input_never_fails_the_hook(env, raw):
    r = run_hook("handoff.py", raw, env)
    assert r.returncode == 0 and r.stdout == ""


def running(prompt=P1, session=S1):
    return {"state": "running", "session": session, "prompt": prompt, "started": 1000, "ended": None}


def test_guard_blocks_a_manual_compact_during_a_running_handoff(handoff):
    reason = handoff.block_reason(running(), S1, "manual", "", now=2000, guard=None)
    assert "/compact-handoff" in reason and "again" in reason


def test_guard_lets_a_deliberate_repeat_through(handoff):
    assert handoff.block_reason(running(), S1, "manual", "", now=2010, guard=(S1, P1, 2000)) is None


def test_guard_treats_a_second_queued_compact_as_no_repeat(handoff):
    assert handoff.block_reason(running(), S1, "manual", "", now=2002, guard=(S1, P1, 2000))


def test_guard_repeat_expires(handoff):
    assert handoff.block_reason(running(), S1, "manual", "", now=2200, guard=(S1, P1, 2000))


def test_guard_repeat_only_counts_for_the_same_handoff(handoff):
    assert handoff.block_reason(running(P2), S1, "manual", "", now=2010, guard=(S1, P1, 2000))


def test_guard_always_blocks_auto_during_a_running_handoff(handoff):
    assert handoff.block_reason(running(), S1, "auto", "", now=2010, guard=(S1, P1, 2000))


def test_guard_ignores_another_sessions_handoff(handoff):
    assert handoff.block_reason(running(session=S2), S1, "manual", "", now=2000, guard=None) is None


@pytest.mark.parametrize("st", ["done", "aborted", "failed"])
def test_guard_allows_once_the_handoff_turn_is_over(handoff, st):
    state = dict(running(), state=st, ended=1500)
    assert handoff.block_reason(state, S1, "manual", "work", now=2000, guard=None) is None
    assert handoff.block_reason(None, S1, "auto", "work", now=2000, guard=None) is None


def test_equals_window_never_auto_compacts(handoff):
    done = dict(running(), state="done", ended=1500)
    assert "=" in handoff.block_reason(done, S1, "auto", "glossary=", now=2000, guard=None)


def test_equals_window_compacts_by_hand_after_a_finished_handoff(handoff):
    done = dict(running(), state="done", ended=1500)
    assert handoff.block_reason(done, S1, "manual", "glossary=", now=2000, guard=None) is None


@pytest.mark.parametrize("state", [None, "aborted", "failed", "other-session"])
def test_equals_window_needs_a_finished_handoff_or_a_repeat(handoff, state):
    st = None
    if state == "other-session":
        st = dict(running(session=S2), state="done", ended=1500)
    elif state:
        st = dict(running(), state=state, ended=1500)
    assert handoff.block_reason(st, S1, "manual", "glossary=", now=2000, guard=None)
    prompt = st["prompt"] if st and st["session"] == S1 else ""
    assert handoff.block_reason(st, S1, "manual", "glossary=", now=2010, guard=(S1, prompt, 2000)) is None


def test_bang_wins_over_equals_for_manual_compacts(handoff):
    assert handoff.block_reason(None, S1, "manual", "review=!", now=2000, guard=None) is None


def test_guard_record_round_trips(handoff):
    key = handoff.state_key(S1)
    assert handoff.read_guard(key) is None
    handoff.record_guard(key, S1, P1, 2000)
    assert handoff.read_guard(key) == (S1, P1, 2000)
