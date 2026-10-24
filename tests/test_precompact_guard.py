import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from conftest import HOOKS, run_hook

S1 = "11111111-aaaa-4bbb-8ccc-000000000001"
P1 = "aaaaaaaa-0000-4000-8000-000000000001"
PASSED = "transcript too short"
CUTOFF = 40000   # min(0.85 x 100000, 100000 - 60000) for CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000


def assistant(tokens, sidechain=False):
    return json.dumps({"type": "assistant", "uuid": f"a{tokens}", "isSidechain": sidechain, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 1}}}) + "\n"


def aged(path, text, age):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    t = time.time() - age
    os.utime(path, (t, t))


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
        pane_id = pane

        def window(self, name):
            subprocess.run(cmd + ["rename-window", "-t", "t", name], check=True)

        def state(self, state, session=S1):
            (signals / f"{key}.handoff").write_text(f"{state}\t{session}\t{P1}\t{int(time.time()) - 60}\t\n")

        def backdate_guard(self, secs=10):
            g = signals / f"{key}.guard"
            session, prompt, at = g.read_text().rstrip("\n").split("\t")
            g.write_text(f"{session}\t{prompt}\t{int(at) - secs}\n")

        def run(self, trigger="manual"):
            reason = self.decide(trigger)
            assert reason is not None
            return reason

        def payload(self, trigger):
            return {"hook_event_name": "PreCompact", "session_id": S1, "prompt_id": P1,
                    "transcript_path": str(transcript), "trigger": trigger, "custom_instructions": None}

        def decide(self, trigger="auto"):
            """The block reason, or None when the hook leaves the compaction to Claude Code."""
            r = run_hook("precompact.py", self.payload(trigger), env)
            return self.reason(r.returncode, r.stdout, r.stderr)

        def decide_racing(self, write):
            """decide(), with write() run 0.2 s after the hook gets its payload."""
            r, w = os.pipe()
            p = subprocess.Popen([sys.executable, str(HOOKS / "precompact.py")], stdin=r,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            os.close(r)
            time.sleep(2)   # past interpreter start-up, so the hook decides as soon as stdin closes
            os.write(w, json.dumps(self.payload("auto")).encode())
            os.close(w)
            time.sleep(0.2)
            write()
            out, err = p.communicate(timeout=60)
            return self.reason(p.returncode, out, err)

        def reason(self, returncode, stdout, stderr):
            assert returncode == 0, stderr
            if not stdout.strip():
                return None
            out = json.loads(stdout)
            assert out["decision"] == "block"
            return out["reason"]

        def parent(self, tokens, age):
            aged(transcript, assistant(tokens), age)

        def subagent(self, tokens, age, agent="a1b2c3d4e5f6a7b8c"):
            self.subagent_file(assistant(tokens, sidechain=True), age, agent)

        def subagent_file(self, text, age, agent="a1b2c3d4e5f6a7b8c"):
            aged(tmp_path / "t" / "subagents" / f"agent-{agent}.jsonl", text, age)

        def log(self):
            p = signals / "compactions.log"
            return p.read_text().splitlines() if p.exists() else []
    return Hook()


@pytest.fixture
def big(hook, env):
    """A main session past the subagent cutoff, whose transcript hasn't changed for half a minute."""
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "100000"
    hook.parent(CUTOFF + 15000, age=30)
    return hook


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


def test_a_fresh_big_subagent_newer_than_the_main_session_compacts_natively(big):
    big.subagent(CUTOFF + 30000, age=1)
    assert big.decide() is None


def test_a_subagent_written_before_the_main_session_is_not_the_one_compacting(big):
    big.subagent(CUTOFF + 30000, age=40)
    assert PASSED in big.decide()


def test_a_subagent_quiet_for_over_a_minute_is_not_the_one_compacting(big):
    big.parent(CUTOFF + 15000, age=180)
    big.subagent(CUTOFF + 30000, age=90)
    assert PASSED in big.decide()


@pytest.mark.parametrize("tokens, native", [(CUTOFF - 1, False), (CUTOFF, True)])
def test_only_a_subagent_at_or_over_the_cutoff_compacts_natively(big, tokens, native):
    big.subagent(tokens, age=1)
    assert (big.decide() is None) is native


def test_without_subagent_transcripts_the_main_session_compacts_as_before(big):
    assert PASSED in big.decide()


def test_a_main_session_under_the_cutoff_still_means_a_subagent(big):
    big.parent(CUTOFF - 1, age=30)
    big.subagent(CUTOFF + 30000, age=1)
    assert big.decide() is None
    [line] = big.log()
    assert f" decision=pass rule=parent-under-cutoff parent_ctx={CUTOFF - 1} cutoff={CUTOFF} parent_age=" in line


def test_a_manual_compaction_always_goes_to_the_main_session(big):
    big.subagent(CUTOFF + 30000, age=1)
    assert PASSED in big.decide("manual")
    assert big.log() == []


@pytest.mark.parametrize("name", ["gloss!", "gloss="])
def test_a_subagent_compacts_natively_in_windows_that_never_auto_compact_the_main_session(big, name):
    big.window(name)
    big.subagent(CUTOFF + 30000, age=1)
    assert big.decide() is None


@pytest.mark.parametrize("text", [
    b"",
    b"\x00\xff not json\n{\"type\": \"assistant\", \"isSidechain\"\n",
    json.dumps({"type": "assistant", "isSidechain": True, "message": "oops"}) + "\n",
    json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": "many"}}}) + "\n",
])
def test_a_subagent_transcript_that_cannot_be_read_is_not_a_subagent(big, text):
    big.subagent_file(text, age=1)
    assert PASSED in big.decide()


def test_an_unreadable_subagent_transcript_is_not_a_subagent(big, tmp_path):
    big.subagent(CUTOFF + 30000, age=1)
    (tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.jsonl").chmod(0)
    assert PASSED in big.decide()


def test_a_main_session_written_seconds_ago_may_be_the_one_compacting(big):
    big.parent(CUTOFF + 15000, age=2)
    big.subagent(CUTOFF + 30000, age=1)
    assert PASSED in big.decide()


def test_a_main_session_write_that_lands_while_the_hook_decides_means_the_main_session(big, tmp_path):
    def main_session_writes():
        with (tmp_path / "t.jsonl").open("a") as f:
            f.write(json.dumps({"type": "attachment", "uuid": "t2", "isSidechain": False}) + "\n")

    big.subagent(CUTOFF + 30000, age=1)
    assert PASSED in big.decide_racing(main_session_writes)


def test_only_the_newest_subagent_transcript_counts(big):
    big.subagent(CUTOFF + 30000, age=3, agent="abig")
    big.subagent(CUTOFF - 1, age=1, agent="asmall")
    assert PASSED in big.decide()


def test_the_meta_file_next_to_a_subagent_transcript_is_ignored(big, tmp_path):
    big.subagent(CUTOFF + 30000, age=2)
    aged(tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.meta.json", '{"agentType": "reader"}', age=0)
    assert big.decide() is None


def test_a_pass_by_subagent_is_logged_with_the_facts_that_decided_it(big):
    big.subagent(CUTOFF + 30000, age=1)
    assert big.decide() is None
    [line] = big.log()
    head, facts = line.split(" parent_age=")
    assert head.endswith(f" session={S1} trigger=auto pane={big.pane_id} decision=pass rule=subagent-transcript "
                         f"parent_ctx={CUTOFF + 15000} cutoff={CUTOFF}")
    parent_age, rest = facts.split(" ", 1)
    assert 30 < float(parent_age) < 40
    assert rest.startswith(f"subagent=a1b2c3d4e5f6a7b8c subagent_ctx={CUTOFF + 30000} subagent_age=")
    assert 1 < float(rest.rsplit("=", 1)[1]) < 10


@pytest.mark.parametrize("name, rule", [("gloss!", "bang-window"), ("gloss=", "handoff"), ("work", "main-session")])
def test_a_refusal_is_logged_with_the_rule_that_made_it(big, name, rule):
    big.window(name)
    big.subagent(CUTOFF - 1, age=1)
    assert big.decide() is not None
    [line] = big.log()
    assert f" decision=refuse rule={rule} parent_ctx={CUTOFF + 15000} cutoff={CUTOFF} parent_age=" in line
    assert f" subagent=a1b2c3d4e5f6a7b8c subagent_ctx={CUTOFF - 1} subagent_age=" in line


def test_without_a_window_size_a_refusal_is_logged_with_no_cutoff(hook):
    hook.window("gloss!")
    assert "opts this session out" in hook.run("auto")
    [line] = hook.log()
    assert " decision=refuse rule=bang-window parent_ctx=0 cutoff=- parent_age=" in line
    assert line.endswith(" subagent=- subagent_ctx=- subagent_age=-")


def test_a_compaction_the_plugin_runs_still_gets_its_executions_line(big, env, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tmux").symlink_to(shutil.which("tmux"))
    env["PATH"] = os.pathsep.join([str(tmp_path / "bin"), "/usr/bin", "/bin"])
    assert shutil.which("claude", path=env["PATH"]) is None   # so no summarizer call
    aged(tmp_path / "t.jsonl", "".join(
        json.dumps({"type": "user", "uuid": f"u{i}", "message": {"role": "user", "content": "x " * 8000}}) + "\n"
        + assistant(CUTOFF + 15000 + i) for i in range(20)), age=30)
    assert "Fork not created: no summary, fork skipped" in big.decide()
    decision, executed = big.log()
    assert " decision=refuse rule=main-session " in decision
    assert executed.endswith(f" session={S1} trigger=auto pane={big.pane_id} msgs=40 "
                             "fork=FAILED(no summary, fork skipped)")


def test_a_subagent_write_that_lands_while_the_hook_decides_is_seen(big, tmp_path):
    def subagent_writes():
        with (tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.jsonl").open("a") as f:
            f.write(json.dumps({"type": "user", "uuid": "s2", "isSidechain": True}) + "\n")

    big.subagent(CUTOFF + 30000, age=90)   # its last write was before a slow tool call
    assert big.decide_racing(subagent_writes) is None
