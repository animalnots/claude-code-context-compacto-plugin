import importlib
import json
import os
import pathlib
import time

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


def tool_call(tool="Bash", tool_input=None, session=S1, prompt=P1, agent=None, transcript="/p/s1.jsonl"):
    p = {"hook_event_name": "PostToolUse", "session_id": session, "prompt_id": prompt, "transcript_path": transcript,
         "tool_name": tool, "tool_input": tool_input or {}, "tool_response": {}, "tool_use_id": "toolu_1"}
    if agent:
        p.update(agent_id=agent, agent_type="general-purpose")
    return p


def handoff_skill(**kw):
    return tool_call("Skill", {"skill": "compact-handoff"}, **kw)


def signals(env):
    return pathlib.Path(env["COMPACTO_SIGNAL_DIR"])


def state_file(env, key="7"):
    return signals(env) / f"{key}.handoff"


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


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"hook_event_name": "Stop"}',
                                 '{"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": "Skill", "tool_input": []}'])
def test_malformed_input_never_fails_the_hook(env, raw):
    r = run_hook("handoff.py", raw, env)
    assert r.returncode == 0 and r.stdout == ""


def test_claude_running_the_handoff_skill_itself_records_running(env):
    r = run_hook("handoff.py", handoff_skill(), env)
    assert r.returncode == 0 and r.stdout == ""
    state, session, prompt, started, ended = read_fields(env)
    assert (state, session, prompt, ended) == ("running", S1, P1, "")
    assert started.isdigit()


def test_the_turn_that_ran_the_handoff_skill_finishes_it(env):
    run_hook("handoff.py", handoff_skill(), env)
    run_hook("handoff.py", stop(), env)
    assert read_fields(env)[0] == "done"


def test_the_configured_handoff_runs_as_a_skill_too(env):
    (pathlib.Path(env["HOME"]) / ".claude" / "precompact.conf").write_text("handoff_command=/my-handoff\n")
    run_hook("handoff.py", handoff_skill(), env)
    assert not state_file(env).exists()
    run_hook("handoff.py", tool_call("Skill", {"skill": "my-handoff"}), env)
    assert read_fields(env)[0] == "running"


@pytest.mark.parametrize("call", [tool_call("Skill", {"skill": "review"}), tool_call("Bash", {"command": "compact-handoff"}),
                                  handoff_skill(agent="a20afdcddd057fde7"), handoff_skill(prompt=None)],
                         ids=["another skill", "another tool", "a subagent", "no prompt id"])
def test_other_tool_calls_record_no_handoff(env, call):
    if call["prompt_id"] is None:
        del call["prompt_id"]
    run_hook("handoff.py", call, env)
    assert not state_file(env).exists()


def test_a_second_handoff_skill_call_in_the_same_turn_keeps_its_start(env):
    run_hook("handoff.py", handoff_skill(), env)
    first = read_fields(env)
    time.sleep(1.1)
    run_hook("handoff.py", handoff_skill(), env)
    assert read_fields(env) == first


def test_a_handoff_start_records_the_sessions_transcript(env):
    run_hook("handoff.py", dict(expansion(), transcript_path="/p/s1.jsonl"), env)
    assert (signals(env) / "7.transcript").read_text() == f"{S1}\t/p/s1.jsonl\n"
    run_hook("handoff.py", handoff_skill(session=S2, prompt=P2, transcript="/p/s2.jsonl"), env)
    assert (signals(env) / "7.transcript").read_text() == f"{S2}\t/p/s2.jsonl\n"


def test_outside_tmux_a_handoff_start_records_no_transcript(env):
    del env["TMUX_PANE"]
    run_hook("handoff.py", dict(expansion(), transcript_path="/p/s1.jsonl"), env)
    assert not list(signals(env).glob("*.transcript"))


def served(env, ctx, msgs=None, beat_age=0, conf=None):
    """A pane the resume daemon watches: it exports ctx, and the daemon's heartbeat is beat_age seconds old."""
    sig = signals(env)
    sig.mkdir(parents=True, exist_ok=True)
    (sig / "7.ctx").write_text(f"%7\t{ctx}\t{ctx if msgs is None else msgs}\n")
    if beat_age is not None:
        beat = sig / ".daemon.lock" / "heartbeat"
        beat.parent.mkdir(exist_ok=True)
        beat.write_text("")
        os.utime(beat, (time.time() - beat_age, time.time() - beat_age))
    if conf is not None:
        (pathlib.Path(env["HOME"]) / ".claude" / "precompact.conf").write_text(conf)


def reminder(r):
    assert r.returncode == 0
    if not r.stdout:
        return None
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    return out["additionalContext"]


def test_past_force_handoff_at_claude_is_asked_to_hand_off_and_end_its_turn(env):
    served(env, 812345)
    text = reminder(run_hook("handoff.py", tool_call(), env))
    assert "812k" in text and "800k" in text and "compact-handoff" in text and "end your turn" in text


def test_under_force_handoff_at_claude_is_told_nothing(env):
    served(env, 799999)
    assert reminder(run_hook("handoff.py", tool_call(), env)) is None


def test_the_reminder_comes_once_a_turn_and_again_each_50k_tokens(env):
    served(env, 810000)
    assert reminder(run_hook("handoff.py", tool_call(), env))
    assert reminder(run_hook("handoff.py", tool_call(), env)) is None
    served(env, 859999)
    assert reminder(run_hook("handoff.py", tool_call(), env)) is None
    served(env, 860000)
    assert reminder(run_hook("handoff.py", tool_call(), env))
    assert reminder(run_hook("handoff.py", tool_call(prompt=P2), env))


def test_force_handoff_at_sets_the_point(env):
    served(env, 500000, conf="force_handoff_at=500000\n")
    assert "500k" in reminder(run_hook("handoff.py", tool_call(), env))


def test_no_reminder_once_the_handoff_runs_in_this_turn(env):
    served(env, 900000)
    run_hook("handoff.py", handoff_skill(), env)
    assert reminder(run_hook("handoff.py", tool_call(), env)) is None


@pytest.mark.parametrize("named,reminded", [(S1, False), (S2, True), ("", True)],
                         ids=["this session", "another session", "none"])
def test_a_session_whose_handoff_never_started_gets_no_reminder(env, named, reminded):
    served(env, 900000)
    (signals(env) / "7.nohandoff").write_text(named + "\n")
    assert bool(reminder(run_hook("handoff.py", tool_call(), env))) is reminded


def test_a_handoff_left_running_by_an_earlier_turn_still_gets_the_reminder(env):
    served(env, 900000)
    run_hook("handoff.py", handoff_skill(prompt=P2), env)
    assert reminder(run_hook("handoff.py", tool_call(), env))


@pytest.mark.parametrize("why", ["force_handoff_at=0", "auto_compact_at=0", "force_handoff_at not a number", "no daemon",
                                 "a dead daemon", "a subagent", "outside tmux", "ctx not a number", "msgs not a number",
                                 "no prompt id"])
def test_no_reminder_where_the_daemon_would_not_hand_off(env, why):
    conf = {"force_handoff_at=0": "force_handoff_at=0\n", "auto_compact_at=0": "auto_compact_at=0\n",
            "force_handoff_at not a number": "force_handoff_at=800k\n"}.get(why)
    served(env, "900k" if why == "ctx not a number" else 900000, msgs="1k" if why == "msgs not a number" else None,
           beat_age={"no daemon": None, "a dead daemon": 120}.get(why, 0), conf=conf)
    call = tool_call(agent="a20afdcddd057fde7" if why == "a subagent" else None)
    if why == "outside tmux":
        del env["TMUX_PANE"]
    if why == "no prompt id":
        del call["prompt_id"]
    assert reminder(run_hook("handoff.py", call, env)) is None


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
