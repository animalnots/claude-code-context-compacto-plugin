import json
import os
import pathlib
import re
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

        def served(self, ctx=55000, msgs=None, heartbeat_age=0, conf="auto_resume=1\n"):
            """The resume daemon watches this pane: it runs, auto-resume is on and the pane exports its size."""
            (pathlib.Path(env["HOME"]) / ".claude" / "precompact.conf").write_text(conf)
            if ctx is not None:
                (signals / f"{key}.ctx").write_text(f"{pane}\t{ctx}\t{ctx if msgs is None else msgs}\n")
            if heartbeat_age is not None:
                aged(signals / ".daemon.lock" / "heartbeat", "", heartbeat_age)

        def want(self):
            p = signals / f"{key}.compactwant"
            return p.read_text() if p.exists() else None
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


def test_a_subagent_last_written_seconds_before_the_compaction_is_not_the_one_compacting(big):
    big.parent(CUTOFF + 15000, age=9)
    big.subagent(CUTOFF + 30000, age=6)
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
    big.subagent(CUTOFF + 30000, age=1)
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


@pytest.fixture
def forking(big, env, tmp_path):
    """A session long enough to fork, with a stand-in summarizer, and a finished subagent."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tmux").symlink_to(shutil.which("tmux"))
    summarizer = tmp_path / "bin" / "claude"
    summarizer.write_text("#!/bin/sh\ncat >/dev/null\necho '## Narrative'\necho 'What happened.'\n")
    summarizer.chmod(0o755)
    env["PATH"] = os.pathsep.join([str(tmp_path / "bin"), "/usr/bin", "/bin"])
    aged(tmp_path / "t.jsonl", "".join(
        json.dumps({"type": "user", "uuid": f"u{i}", "cwd": str(tmp_path), "version": "2.1.310",
                    "message": {"role": "user", "content": "x " * 8000}}) + "\n"
        + assistant(CUTOFF + 15000 + i) for i in range(20)), age=30)
    big.subagent(CUTOFF - 1, age=30)
    return big


def test_a_fork_gets_the_subagent_transcripts_of_the_session_it_was_made_from(forking, tmp_path):
    reason = forking.decide("manual")
    fork = re.search(r"/resume (\S+)", reason).group(1)
    carried = tmp_path / fork / "subagents" / "agent-a1b2c3d4e5f6a7b8c.jsonl"
    assert os.path.samefile(carried, tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.jsonl")


def test_a_fork_whose_subagent_files_cannot_be_carried_is_still_written(forking, tmp_path):
    meta = tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.meta.json"
    meta.write_text('{"agentType": "reader"}')
    meta.chmod(0)
    try:
        reason = forking.decide("manual")
    finally:
        meta.chmod(0o644)
    assert re.search(r"/resume \S+", reason)
    assert ("Subagent transcripts not carried into the fork (OSError: 1 of 2 files failed: "
            "agent-a1b2c3d4e5f6a7b8c.meta.json (Permission denied))") in reason


def test_a_subagent_write_that_lands_while_the_hook_decides_is_seen(big, tmp_path):
    def subagent_writes():
        with (tmp_path / "t" / "subagents" / "agent-a1b2c3d4e5f6a7b8c.jsonl").open("a") as f:
            f.write(json.dumps({"type": "user", "uuid": "s2", "isSidechain": True}) + "\n")

    big.subagent(CUTOFF + 30000, age=90)   # its last write was before a slow tool call
    assert big.decide_racing(subagent_writes) is None


LEFT_TO_DAEMON = "the resume daemon compacts this pane once the turn ends"


@pytest.mark.parametrize("conf", ["auto_resume=1\n", "auto_compact_at=265000\nauto_resume=1\n"])
def test_in_a_pane_the_daemon_watches_auto_compaction_waits_for_its_idle_prompt(big, tmp_path, conf):
    big.served(conf=conf)
    assert LEFT_TO_DAEMON in big.decide()
    assert big.want() == "55000\n"
    [line] = big.log()
    assert f" decision=refuse rule=daemon parent_ctx={CUTOFF + 15000} cutoff={CUTOFF} parent_age=" in line
    assert not list((tmp_path / "proj" / ".claude" / "summaries").iterdir())


def test_without_a_window_size_a_watched_pane_still_waits_for_the_daemon(hook):
    hook.served()
    assert LEFT_TO_DAEMON in hook.run("auto")


@pytest.mark.parametrize("setup", [
    dict(heartbeat_age=None),                          # no daemon, or one that predates the heartbeat
    dict(heartbeat_age=120),                           # a daemon that died
    dict(conf="auto_compact_at=0\nauto_resume=1\n"),   # threshold compaction off
    dict(conf="auto_compact_at=300k\nauto_resume=1\n"),
    dict(conf="auto_compact_at=265000 \nauto_resume=1\n"),       # the daemon reads that as no number: off
    dict(conf="auto_compact_at=265000\r\nauto_resume=1\r\n"),
    dict(conf=""),                                     # auto-resume off
    dict(ctx=None),                                    # the pane doesn't export its size
    dict(ctx="n/a"),
    dict(ctx=0),                                       # a session that hasn't reported its size yet
    dict(ctx="55000 "),                                # the daemon reads that as no number
    dict(msgs="55k"),                                  # the daemon skips a pane whose msgs it can't read
])
def test_where_the_daemon_wont_compact_at_idle_the_hook_forks_as_before(big, setup):
    big.served(**setup)
    assert PASSED in big.decide()
    assert big.want() is None
    assert " decision=refuse rule=main-session " in big.log()[0]


def test_a_compaction_typed_in_a_watched_pane_runs_at_once(big):
    big.served()
    assert PASSED in big.decide("manual")
    assert big.want() is None


@pytest.mark.parametrize("name, rule", [("gloss!", "bang-window"), ("gloss=", "handoff")])
def test_bang_and_equals_windows_keep_their_own_rules_in_a_watched_pane(big, name, rule):
    big.window(name)
    big.served()
    assert big.decide() is not None
    assert f" decision=refuse rule={rule} " in big.log()[0]
    assert big.want() is None


def test_a_subagent_still_compacts_natively_in_a_watched_pane(big):
    big.served()
    big.subagent(CUTOFF + 30000, age=1)
    assert big.decide() is None
    assert big.want() is None


def test_when_the_daemon_cannot_be_told_the_hook_forks_as_before(big, env):
    big.served()
    signals = pathlib.Path(env["COMPACTO_SIGNAL_DIR"])
    signals.chmod(0o555)
    try:
        assert PASSED in big.decide()
    finally:
        signals.chmod(0o755)
    assert big.want() is None
