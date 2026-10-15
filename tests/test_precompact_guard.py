import json
import os
import pathlib
import subprocess
import time

import pytest

from conftest import run_hook

S1 = "11111111-aaaa-4bbb-8ccc-000000000001"
P1 = "aaaaaaaa-0000-4000-8000-000000000001"
PASSED = "transcript too short"


@pytest.fixture
def tmux(tmp_path):
    sock = f"cct-guard-{os.getpid()}"
    cmd = ["tmux", "-L", sock]
    subprocess.run(cmd + ["new-session", "-d", "-s", "t", "-n", "work"], check=True)
    pane = subprocess.run(cmd + ["display-message", "-p", "-t", "t", "#{pane_id}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    yield cmd, pane
    subprocess.run(cmd + ["kill-server"], capture_output=True)


@pytest.fixture
def hook(env, tmp_path, tmux):
    cmd, pane = tmux
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "hi"}}) + "\n")
    env.update(COMPACTO_TMUX=" ".join(cmd), TMUX_PANE=pane, CLAUDE_PROJECT_DIR=str(tmp_path / "proj"))
    key = pane.lstrip("%")
    signals = pathlib.Path(env["COMPACTO_SIGNAL_DIR"])
    signals.mkdir(parents=True, exist_ok=True)

    class Hook:
        def window(self, name):
            subprocess.run(cmd + ["rename-window", "-t", "t", name], check=True)

        def state(self, state, session=S1):
            (signals / f"{key}.handoff").write_text(f"{state}\t{session}\t{P1}\t{int(time.time()) - 60}\t\n")

        def backdate_guard(self, secs=10):
            g = signals / f"{key}.guard"
            session, prompt, at = g.read_text().rstrip("\n").split("\t")
            g.write_text(f"{session}\t{prompt}\t{int(at) - secs}\n")

        def run(self, trigger="manual"):
            r = run_hook("precompact.py", {"hook_event_name": "PreCompact", "session_id": S1, "prompt_id": P1,
                                           "transcript_path": str(transcript), "trigger": trigger,
                                           "custom_instructions": None}, env)
            assert r.returncode == 0, r.stderr
            out = json.loads(r.stdout)
            assert out["decision"] == "block"
            return out["reason"]
    return Hook()


def test_auto_in_an_equals_window_waits_for_the_handoff(hook):
    hook.window("gloss=")
    hook.state("done")
    assert "'=' in the tmux window name: compaction waits" in hook.run("auto")


def test_auto_in_a_bang_window_is_still_refused(hook):
    hook.window("gloss=!")
    assert "'!' in the tmux window name opts this session out" in hook.run("auto")


def test_manual_in_an_equals_window_needs_a_finished_handoff_or_a_repeat(hook):
    hook.window("gloss=")
    assert "compact after /compact-handoff finishes" in hook.run()
    assert "compact after /compact-handoff finishes" in hook.run()   # a second queued /compact, seconds later
    hook.backdate_guard()
    assert PASSED in hook.run()


def test_manual_in_an_equals_window_runs_after_a_finished_handoff(hook):
    hook.window("gloss=")
    hook.state("done")
    assert PASSED in hook.run()


def test_manual_during_a_running_handoff_is_blocked_until_repeated(hook):
    hook.state("running")
    assert "hasn't finished" in hook.run()
    hook.backdate_guard()
    assert PASSED in hook.run()


def test_auto_during_a_running_handoff_is_always_blocked(hook):
    hook.state("running")
    assert "hasn't finished" in hook.run()
    hook.backdate_guard()
    assert "hasn't finished" in hook.run("auto")


@pytest.mark.parametrize("state", ["done", "aborted", "failed"])
def test_compaction_runs_once_the_handoff_turn_is_over(hook, state):
    hook.state(state)
    assert PASSED in hook.run()


def test_another_sessions_running_handoff_does_not_block(hook):
    hook.state("running", session="22222222-aaaa-4bbb-8ccc-000000000002")
    assert PASSED in hook.run()


def test_outside_tmux_a_running_handoff_still_blocks(hook, env):
    del env["TMUX_PANE"]
    signals = pathlib.Path(env["COMPACTO_SIGNAL_DIR"])
    (signals / f"s_{S1.replace('-', '')}.handoff").write_text(f"running\t{S1}\t{P1}\t{int(time.time())}\t\n")
    assert "hasn't finished" in hook.run()
