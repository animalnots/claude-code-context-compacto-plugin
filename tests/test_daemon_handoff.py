import fcntl
import json
import os
import pathlib
import pty
import shlex
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

import pytest

from conftest import run_hook

ROOT = pathlib.Path(__file__).resolve().parents[1]
DAEMON = ROOT / "plugins" / "context-compacto" / "hooks" / "compacto-resume-daemon.sh"
FAKE = pathlib.Path(__file__).with_name("fake_claude.py")
THRESH = 265000          # auto_compact_at; a "=" window hands off at min(600000, 2x) = 530000


class Rig:
    def __init__(self, tmp_path):
        self.tmux = ["tmux", "-L", f"cct-d-{os.getpid()}-{uuid.uuid4().hex[:6]}"]
        self.home, self.signals = tmp_path / "home", tmp_path / "signals"
        (self.home / ".claude").mkdir(parents=True)
        self.signals.mkdir()
        (self.home / ".claude" / "precompact.conf").write_text(f"auto_compact_at={THRESH}\nauto_resume=1\n")
        self.log, self.transcript = tmp_path / "fake.log", tmp_path / "t.jsonl"
        self.transcript.write_text('{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}\n')
        self.daemon_out = tmp_path / "daemon.out"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPACTO_", "PRECOMPACT_", "TMUX"))}
        self.env.update(HOME=str(self.home), COMPACTO_SIGNAL_DIR=str(self.signals),
                        COMPACTO_TMUX=" ".join(self.tmux), CLAUDE_PROJECT_DIR=str(tmp_path / "proj"),
                        COMPACTO_POLL_SECS="0.2", COMPACTO_CONTINUE_SETTLE="1",
                        COMPACTO_MIN_COMPACT_INTERVAL="1", COMPACTO_REARM_GRACE="1",
                        COMPACTO_COMPACT_COOLDOWN="60", COMPACTO_HANDOFF_START_TIMEOUT="3",
                        COMPACTO_PYTHON=sys.executable, COMPACTO_LIMIT_CHECK_SECS="1")
        self.daemon = None
        self.target = None
        self.clients = []

    def start(self, window, msgs, handoff="ok", hooked=True, draft="", daemon=True, resume="ok", ctx=True,
              compact="ok", old_hooks=False, done_for="3s", dialog=False, context=None, limit="no"):
        cmd = [sys.executable, str(FAKE), "--log", str(self.log), "--transcript", str(self.transcript),
               "--msgs", str(msgs), "--handoff", handoff, "--resume", resume, "--compact", compact,
               "--done-for", done_for, "--limit", limit] \
            + (["--hooked"] if hooked else []) + ([] if ctx else ["--no-ctx"]) \
            + (["--old-hooks"] if old_hooks else []) + (["--dialog"] if dialog else []) \
            + (["--ctx", str(context)] if context is not None else [])
        subprocess.run(self.tmux + ["new-session", "-d", "-s", "t", "-n", window, "-x", "120", "-y", "30",
                                    shlex.join(cmd)], env=self.env, check=True)
        self.target = subprocess.run(self.tmux + ["display-message", "-p", "-t", "t", "#{pane_id}"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.wait_for("READY")
        if draft:
            self.keys(draft)
        if daemon:
            self.start_daemon()

    def start_daemon(self):
        self.daemon = subprocess.Popen(["bash", str(DAEMON)], env=self.env, stdout=open(self.daemon_out, "w"),
                                       stderr=subprocess.STDOUT)

    def keys(self, *keys):
        subprocess.run(self.tmux + ["send-keys", "-t", self.target, *keys], check=True)

    def pane(self, fmt, target=None):
        return subprocess.run(self.tmux + ["display-message", "-p", "-t", target or self.target, fmt],
                              capture_output=True, text=True, check=True).stdout.strip()

    def scroll(self):
        """Scrolls the pane up as the wheel does, which puts it in copy-mode."""
        subprocess.run(self.tmux + ["copy-mode", "-e", "-t", self.target], check=True)
        self.keys("-X", "-N", "5", "scroll-up")

    def scrolled(self):
        return self.pane("#{pane_in_mode}") == "1"

    def attach(self, session="t"):
        """A client showing the session, so its current window counts as viewed."""
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 120, 0, 0))
        self.clients.append(subprocess.Popen(self.tmux + ["attach-session", "-t", session], stdin=slave, stdout=slave,
                                             stderr=slave, env={**self.env, "TERM": "xterm-256color"},
                                             start_new_session=True))
        os.close(slave)
        threading.Thread(target=self._drain, args=(master,), daemon=True).start()
        end = time.time() + 10
        while self.pane("#{session_attached}", session) != "1" and time.time() < end:
            time.sleep(0.05)
        assert self.pane("#{session_attached}", session) == "1"

    @staticmethod
    def _drain(fd):
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            pass
        os.close(fd)

    def other_window(self):
        subprocess.run(self.tmux + ["new-window", "-d", "-t", "t:9", "cat"], env=self.env, check=True)

    def show_other_window(self):
        subprocess.run(self.tmux + ["select-window", "-t", "t:9"], check=True)

    def tmux_without(self, fmt):
        """Renames fmt in the daemon's tmux commands to one tmux doesn't know, so the real server prints nothing for it."""
        shim = self.home / "tmux-without"
        shim.write_text(f'#!/bin/bash\nexec {shlex.join(self.tmux)} "${{@//{fmt}/not_a_format}}"\n')
        shim.chmod(0o755)
        self.env["COMPACTO_TMUX"] = str(shim)

    def watch_markers(self):
        """Turns markers on: they need a format that prints @cc_state, and the fake's name to count as Claude."""
        self.env["COMPACTO_CLAUDE_CMD_REGEX"] = "."
        subprocess.run(self.tmux + ["set-option", "-g", "window-status-format", "#{@cc_state}#W"], check=True)

    def wait_marker(self, mark, timeout=10):
        end = time.time() + timeout
        while self.pane("#{@cc_state}") != mark and time.time() < end:
            time.sleep(0.05)
        assert self.pane("#{@cc_state}") == mark

    def events(self):
        if not self.log.exists():
            return []
        return [ln.split("\t", 1)[1] for ln in self.log.read_text().splitlines()]

    def times(self, prefix):
        return [float(ln.split("\t", 1)[0]) for ln in self.log.read_text().splitlines()
                if ln.split("\t", 1)[1].startswith(prefix)]

    def daemon_log(self):
        p = self.signals / "events.log"
        return p.read_text() if p.exists() else ""

    def wait_log(self, text, timeout=15):
        end = time.time() + timeout
        while text not in self.daemon_log() and time.time() < end:
            time.sleep(0.2)
        assert text in self.daemon_log()

    def wait_for(self, prefix, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if any(e.startswith(prefix) for e in self.events()):
                return
            time.sleep(0.1)
        raise AssertionError(f"no {prefix!r} in {self.events()}\ndaemon: {self.daemon_out.read_text() if self.daemon_out.exists() else ''}")

    def typed(self):
        return [e[len("TYPED "):] for e in self.events() if e.startswith("TYPED ")]

    def wait_typed(self, n, timeout=30):
        end = time.time() + timeout
        while len(self.typed()) < n and time.time() < end:
            time.sleep(0.1)
        return self.typed()

    def signal(self, suffix):
        return self.signals / f"{self.target.lstrip('%')}.{suffix}"

    def want(self, size):
        """A compaction left to the daemon at this size, as the hook leaves Claude Code's own one."""
        self.signal("compactwant").write_text(f"{size}\n")

    def stuck(self):
        """The session's transcript as one at the context limit leaves it: its last request, after a tool result, refused."""
        base = {"isSidechain": False, "sessionId": "s-limit", "cwd": "/w", "version": "2.1.322", "userType": "external",
                "timestamp": "2026-11-11T12:00:00.000Z"}
        rows = [dict(base, type="user", uuid="u1", parentUuid=None, message={"role": "user", "content": "build it"}),
                dict(base, type="assistant", uuid="a1", parentUuid="u1", message={
                    "role": "assistant", "model": "claude-opus-5-5",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}),
                dict(base, type="user", uuid="r1", parentUuid="a1", message={
                    "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}),
                dict(base, type="assistant", uuid="e1", parentUuid="r1", isApiErrorMessage=True, message={
                    "role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "Prompt is too long"}]})]
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def fork(self, fork, age=0):
        """The hook's /resume signal for a fork written age seconds ago."""
        p = self.signal("resume")
        p.write_text(f"{self.target}\t{fork}\n")
        os.utime(p, (time.time() - age, time.time() - age))

    def stop(self):
        if self.daemon:
            self.daemon.terminate()
            self.daemon.wait(timeout=10)
        subprocess.run(self.tmux + ["kill-server"], capture_output=True)
        for client in self.clients:
            client.wait(timeout=10)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.stop()
    assert not [e for e in r.events() if e.startswith("TYPED_WHILE_BUSY")], r.events()


def test_equals_window_hands_off_then_compacts_then_resumes(rig):
    rig.start("gloss=", 600000)
    rig.wait_for("COMPACTED")
    rig.wait_for("TYPED /resume ")
    assert rig.typed()[:2] == ["/compact-handoff", "/compact"]
    assert rig.times("COMPACTED")[0] > rig.times("IDLE")[0]
    log = rig.daemon_log()
    assert "handoff-sent" in log and "handoff-compact" in log
    time.sleep(3)
    assert len(rig.typed()) == 3          # the resumed fork is small: nothing more to do
    assert not (rig.signals / "0.handoffsent").exists()


def test_a_handoff_typed_by_hand_is_followed_by_compact_not_a_second_handoff(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.keys("/compact-handoff", "Enter")
    rig.wait_for("IDLE")
    rig.start_daemon()
    rig.wait_for("COMPACTED")
    assert rig.typed()[:2] == ["/compact-handoff", "/compact"]
    assert "handoff-sent" not in rig.daemon_log()


def test_a_cut_short_handoff_is_retried_after_the_back_off(rig):
    rig.env["COMPACTO_COMPACT_COOLDOWN"] = "3"
    rig.start("gloss=", 600000, handoff="interrupt,ok")
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:3] == ["/compact-handoff", "/compact-handoff", "/compact"]
    assert "was cut short (fail #1)" in rig.daemon_log()


def test_compact_waits_until_the_handoff_turn_really_ends(rig):
    rig.start("gloss=", 600000, handoff="slowstop")
    rig.wait_for("COMPACTED")
    assert rig.times("TYPED /compact")[-1] > rig.times("IDLE")[0]
    assert rig.typed()[:2] == ["/compact-handoff", "/compact"]   # the second Stop ends the same turn


def test_a_turn_that_ends_between_the_handoff_and_compact_brings_a_fresh_handoff(rig):
    rig.start("gloss=", 600000, handoff="late,ok")
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:3] == ["/compact-handoff", "/compact-handoff", "/compact"]
    assert "handoff-redo" in rig.daemon_log()


def test_with_hooks_that_stamp_no_turn_a_handoff_is_still_followed_by_compact(rig):
    rig.start("gloss=", 600000, handoff="late", old_hooks=True)
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:2] == ["/compact-handoff", "/compact"]


@pytest.mark.parametrize("handoff,why", [("interrupt", "cut short"), ("apierror", "API error"),
                                          ("ignore", "never started")])
def test_a_handoff_that_does_not_finish_is_never_followed_by_compact(rig, handoff, why):
    rig.start("gloss=", 600000, handoff=handoff)
    rig.wait_for("TYPED /compact-handoff")
    end = time.time() + 15
    while why not in rig.daemon_log() and time.time() < end:
        time.sleep(0.2)
    assert why in rig.daemon_log()
    time.sleep(2)
    assert rig.typed() == ["/compact-handoff"]


def test_after_a_handoff_that_never_started_only_a_newer_stop_allows_a_retry(rig):
    rig.env["COMPACTO_COMPACT_COOLDOWN"] = "1"
    rig.start("gloss=", 600000, handoff="ignore,ok")
    rig.wait_for("IGNORED")
    time.sleep(7)
    assert rig.typed() == ["/compact-handoff"]
    assert "never started" in rig.daemon_log()
    run_hook("handoff.py", {"hook_event_name": "Stop", "session_id": str(uuid.uuid4()), "prompt_id": str(uuid.uuid4()),
                            "transcript_path": str(rig.transcript)},
             dict(rig.env, TMUX_PANE=rig.target))
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:3] == ["/compact-handoff", "/compact-handoff", "/compact"]


def test_equals_window_waits_for_double_the_threshold(rig):
    rig.start("gloss=", 400000)
    time.sleep(4)
    assert rig.typed() == []


def test_plain_window_still_compacts_at_the_threshold(rig):
    rig.start("gloss", 300000)
    rig.wait_for("COMPACTED")
    assert rig.typed()[0] == "/compact"


@pytest.mark.parametrize("window,msgs,first", [("gloss=", 600000, "/compact-handoff"), ("gloss", 300000, "/compact")])
def test_a_finished_turn_timed_in_days_counts_as_idle(rig, window, msgs, first):
    rig.start(window, msgs, done_for="1d 1h 7m")
    rig.wait_for("COMPACTED")
    assert rig.typed()[0] == first


def test_bang_wins_over_equals(rig):
    rig.start("gloss=!", 600000)
    time.sleep(4)
    assert rig.typed() == []


def test_session_without_the_hooks_is_left_alone(rig):
    rig.start("gloss=", 600000, hooked=False)
    time.sleep(4)
    assert rig.typed() == []
    assert "handoff-unhooked" in rig.daemon_log()


def test_a_draft_in_the_input_box_holds_the_handoff(rig):
    rig.start("gloss=", 600000, draft="hello")
    time.sleep(4)
    assert rig.typed() == []
    rig.keys(*["BSpace"] * 5)
    rig.wait_for("TYPED /compact-handoff")


def test_claude_codes_own_compaction_mid_turn_runs_at_the_next_idle_prompt(rig):
    rig.start("gloss", 100000)
    rig.keys("turn", "Enter")
    typed = rig.wait_typed(3)
    assert typed[:2] == ["turn", "/compact"] and typed[2].startswith("/resume ")
    assert "AUTO_COMPACT_BLOCKED precompact: the resume daemon compacts this pane once the turn ends" in rig.events()
    assert rig.times("TYPED /compact")[0] > rig.times("IDLE")[0]
    assert ", asked by Claude Code" in rig.daemon_log()
    assert not rig.signal("compactwant").exists()


def test_a_left_compaction_is_dropped_once_the_session_is_smaller(rig):
    rig.start("gloss", 100000, daemon=False)
    rig.want(200000)
    rig.start_daemon()
    time.sleep(3)
    assert rig.typed() == []
    assert not rig.signal("compactwant").exists()


@pytest.mark.parametrize("window", ["gloss=", "gloss!"])
def test_equals_and_bang_windows_drop_a_left_compaction(rig, window):
    rig.start(window, 100000, daemon=False)
    rig.want(100000)
    rig.start_daemon()
    time.sleep(3)
    assert rig.typed() == []
    assert not rig.signal("compactwant").exists()


def test_a_fork_older_than_the_panes_last_turn_is_compacted_afresh_instead_of_resumed(rig):
    rig.start("gloss", 100000, daemon=False)    # its Stop stamps the pane's last turn now
    rig.fork("stale-fork", age=5)
    rig.start_daemon()
    typed = rig.wait_typed(2)
    assert typed[0] == "/compact" and typed[1].startswith("/resume ")
    assert "/resume stale-fork" not in typed
    assert "resume-stale" in rig.daemon_log()


def test_a_fork_newer_than_the_panes_last_turn_is_resumed_before_a_left_compaction(rig):
    rig.start("gloss", 100000, daemon=False)
    time.sleep(1.1)
    rig.fork("fresh-fork")
    rig.want(100000)
    rig.start_daemon()
    rig.wait_for("TYPED /resume fresh-fork")
    time.sleep(3)
    assert rig.typed() == ["/resume fresh-fork"]
    assert not rig.signal("compactwant").exists()


@pytest.mark.parametrize("why", ["no size export", "threshold compaction off", "a ! window", "no size reported yet",
                                 "msgs it can't read"])
def test_where_the_daemon_cannot_compact_afresh_a_stale_fork_is_still_resumed(rig, why):
    if why == "threshold compaction off":
        (rig.home / ".claude" / "precompact.conf").write_text("auto_compact_at=0\nauto_resume=1\n")
    window = "gloss!" if why == "a ! window" else "gloss"
    rig.start(window, 0 if why == "no size reported yet" else 100000,
              daemon=False, ctx=why not in ("no size export", "msgs it can't read"))
    if why == "msgs it can't read":
        rig.signal("ctx").write_text(f"{rig.target}\t100000\t100k\n")
    rig.fork("stale-fork", age=5)
    rig.start_daemon()
    rig.wait_for("TYPED /resume stale-fork")


@pytest.mark.parametrize("handoff", ["none", "another session's"])
def test_in_an_equals_window_a_stale_fork_is_dropped_and_a_pane_under_its_threshold_left_alone(rig, handoff):
    rig.start("gloss=", 100000, daemon=False)
    if handoff == "another session's":
        now = int(time.time())
        rig.signal("handoff").write_text(f"done\t{uuid.uuid4()}\tp1\t{now - 1000}\t{now - 990}\n")
    rig.fork("stale-fork", age=5)
    rig.start_daemon()
    end = time.time() + 10
    while "resume-stale" not in rig.daemon_log() and time.time() < end:
        time.sleep(0.2)
    time.sleep(2)
    assert "resume-stale" in rig.daemon_log()
    assert rig.typed() == []


def test_in_an_equals_window_over_its_threshold_a_stale_fork_is_handed_off_afresh(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.fork("stale-fork", age=5)
    rig.start_daemon()
    typed = rig.wait_typed(3)
    assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")
    assert "/resume stale-fork" not in typed


def test_a_resume_the_session_swallowed_is_not_resent_after_the_turn_it_ran(rig):
    rig.start("gloss", 300000, resume="eat,ok")
    typed = rig.wait_typed(4)
    resumes = [t for t in typed if t.startswith("/resume ")]
    assert typed[:4] == ["/compact", resumes[0], "/compact", resumes[1]] and resumes[0] != resumes[1]
    assert "resume-stale" in rig.daemon_log()


def test_the_daemon_keeps_a_heartbeat_only_while_it_runs(rig):
    rig.start("gloss", 1000)
    beat = rig.signals / ".daemon.lock" / "heartbeat"
    end = time.time() + 5
    while not beat.exists() and time.time() < end:
        time.sleep(0.1)
    first = beat.stat().st_mtime
    time.sleep(1.5)
    assert beat.stat().st_mtime > first
    rig.daemon.terminate()
    rig.daemon.wait(timeout=10)
    assert not beat.exists()


def test_in_an_equals_window_a_fork_from_before_a_handoff_is_compacted_afresh_after_it(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.fork("pre-handoff-fork", age=5)
    rig.keys("/compact-handoff", "Enter")
    rig.wait_for("IDLE")
    rig.start_daemon()
    typed = rig.wait_typed(3)
    assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")
    assert "/resume pre-handoff-fork" not in typed
    assert "resume-stale" in rig.daemon_log()


def test_a_compaction_asked_for_soon_after_the_last_one_is_not_held_by_its_line_on_screen(rig):
    rig.start("gloss", 100000, daemon=False)
    rig.want(100000)
    rig.start_daemon()
    rig.wait_for("TYPED /resume ")       # the fork shows that "❯ /compact" in its tail
    time.sleep(2)                        # past the re-arm grace, as a fork that grows back takes far longer
    rig.want(1000)
    typed = rig.wait_typed(4)
    assert typed[2] == "/compact" and typed[3].startswith("/resume ")


def test_a_compaction_asked_for_again_while_the_last_one_is_stuck_in_the_queue_is_not_stacked(rig):
    rig.start("gloss", 100000, daemon=False, compact="freeze")
    rig.want(100000)
    rig.start_daemon()
    rig.wait_for("FROZEN")
    time.sleep(0.5)                      # the daemon drops the reading it fired on; the statusline writes the next
    rig.signal("ctx").write_text(f"{rig.target}\t100000\t100000\n")
    rig.want(100000)                     # Claude Code asked again, in a turn the stuck /compact waited behind
    time.sleep(4)                        # past the re-arm grace and the floor between compactions
    assert rig.typed() == ["/compact"]


def test_a_fork_written_while_the_daemon_holds_an_older_one_is_not_overwritten(rig):
    rig.start("gloss", 100000, daemon=False)
    rig.fork("old-fork")
    rig.start_daemon()
    end = time.time() + 10
    while not rig.signal("resume.busy").exists() and time.time() < end:
        time.sleep(0.05)
    rig.fork("new-fork")                 # the hook's next fork, while the daemon settles before typing the old one
    rig.keys("work", "Enter")
    rig.wait_for("TYPED /resume ")
    assert rig.typed() == ["work", "/resume new-fork"]


@pytest.mark.parametrize("old_hooks", [False, True])
def test_in_an_equals_window_a_fork_a_turn_outlived_after_its_handoff_is_handed_off_afresh(rig, old_hooks):
    rig.start("gloss=", 600000, resume="eat,ok", old_hooks=old_hooks)
    typed = rig.wait_typed(6)
    resumes = [t for t in typed if t.startswith("/resume ")]
    assert typed[:6] == ["/compact-handoff", "/compact", resumes[0], "/compact-handoff", "/compact", resumes[1]]
    assert resumes[0] != resumes[1]
    assert "resume-stale" in rig.daemon_log()


def test_a_draft_in_the_input_box_holds_a_left_compaction(rig):
    rig.start("gloss", 100000, daemon=False, draft="hello")
    rig.want(100000)
    rig.start_daemon()
    time.sleep(3)
    assert rig.typed() == []
    rig.keys(*["BSpace"] * 5)
    rig.wait_for("TYPED /compact")


@pytest.mark.parametrize("client", [False, True], ids=["nobody attached", "another window on screen"])
def test_a_scrolled_pane_nobody_is_viewing_is_scrolled_back_and_handed_off(rig, client):
    rig.start("gloss=", 600000, daemon=False)
    if client:
        rig.other_window()
        rig.attach()
        rig.show_other_window()
    rig.scroll()
    rig.start_daemon()
    rig.wait_for("TYPED /compact-handoff")
    assert not rig.scrolled()
    assert "unscrolled" in rig.daemon_log()


def test_the_window_on_screen_stays_scrolled_with_a_marker_and_hands_off_once_scrolled_back(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.watch_markers()
    rig.attach()
    rig.scroll()
    rig.start_daemon()
    rig.wait_marker("^")
    time.sleep(2)
    assert rig.scrolled() and rig.typed() == []
    assert rig.daemon_log().count("handoff-scrolled") == 1
    rig.keys("-X", "cancel")
    rig.wait_for("TYPED /compact-handoff")
    rig.wait_marker("")
    assert "unscrolled" not in rig.daemon_log()


def test_leaving_a_scrolled_window_lets_its_handoff_run(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.other_window()
    rig.attach()
    rig.scroll()
    rig.start_daemon()
    time.sleep(2)
    assert rig.scrolled() and rig.typed() == []
    rig.show_other_window()
    rig.wait_for("TYPED /compact-handoff")
    assert not rig.scrolled()


def test_a_scrolled_pane_keeps_its_place_until_its_turn_ends(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.keys("work", "Enter")
    rig.wait_for("TYPED work")
    rig.scroll()
    assert int(rig.pane("#{scroll_position}")) > 0
    rig.watch_markers()
    rig.start_daemon()
    rig.wait_marker("^")
    assert rig.scrolled() and rig.typed() == ["work"]
    rig.wait_for("TYPED /compact-handoff")
    assert not rig.scrolled()


def test_a_scrolled_pane_with_a_draft_keeps_its_place(rig):
    rig.start("gloss=", 600000, draft="hello", daemon=False)
    rig.scroll()
    rig.start_daemon()
    time.sleep(3)
    assert rig.scrolled() and rig.typed() == []


@pytest.mark.parametrize("window,msgs,hooked", [("gloss", 600000, True), ("gloss!", 600000, True),
                                                ("gloss=", 300000, True), ("gloss=", 600000, False)],
                         ids=["plain window", "opted out", "under the threshold", "no hooks"])
def test_a_pane_the_daemon_would_not_hand_off_keeps_its_place_and_gets_no_marker(rig, window, msgs, hooked):
    rig.start(window, msgs, hooked=hooked, daemon=False)
    rig.watch_markers()
    rig.scroll()
    rig.start_daemon()
    time.sleep(3)
    assert rig.scrolled() and rig.typed() == []
    assert rig.pane("#{@cc_state}") == ""


def test_a_pane_scrolled_during_its_handoff_is_scrolled_back_for_its_compact(rig):
    rig.start("gloss=", 600000)
    rig.wait_for("TYPED /compact-handoff")
    rig.scroll()
    assert rig.wait_typed(2)[:2] == ["/compact-handoff", "/compact"]


def test_a_mode_that_copy_commands_cannot_leave_is_held_and_logged_once(rig):
    rig.start("gloss=", 600000, daemon=False)
    subprocess.run(rig.tmux + ["choose-tree", "-t", rig.target], check=True)
    assert rig.scrolled()
    rig.start_daemon()
    time.sleep(2)
    assert rig.scrolled() and rig.typed() == []
    log = rig.daemon_log()
    assert log.count("handoff-scrolled") == 1 and "unscrolled" not in log


def test_a_chooser_opened_over_the_scrolled_window_on_screen_keeps_its_marker_and_gets_no_keys(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.watch_markers()
    rig.attach()
    rig.scroll()
    rig.start_daemon()
    rig.wait_marker("^")
    subprocess.run(rig.tmux + ["choose-tree", "-t", rig.target], check=True)
    assert rig.pane("#{pane_in_mode}") == "2"
    time.sleep(3)
    assert rig.pane("#{pane_in_mode} #{pane_mode} #{@cc_state}") == "2 tree-mode ^"
    assert rig.typed() == []
    log = rig.daemon_log()
    assert log.count("handoff-scrolled") == 1 and "handoff-not-typed" not in log


def test_a_chooser_over_a_scrolled_window_nobody_is_viewing_is_held_too(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.watch_markers()
    rig.scroll()
    subprocess.run(rig.tmux + ["choose-tree", "-t", rig.target], check=True)
    rig.start_daemon()
    rig.wait_marker("^")
    time.sleep(2)
    assert rig.pane("#{pane_in_mode} #{pane_mode}") == "2 tree-mode" and rig.typed() == []
    log = rig.daemon_log()
    assert log.count("handoff-scrolled") == 1 and "unscrolled" not in log and "handoff-not-typed" not in log


def test_a_chooser_over_a_scrolled_plain_window_gets_no_compact(rig):
    rig.start("gloss", 600000, daemon=False)
    rig.scroll()
    subprocess.run(rig.tmux + ["choose-tree", "-t", rig.target], check=True)
    rig.start_daemon()
    time.sleep(3)
    assert rig.pane("#{pane_in_mode} #{pane_mode}") == "2 tree-mode" and rig.typed() == []
    assert "fire" not in rig.daemon_log() and not rig.signal("compacting").exists()


def start_daemon_and_expect_held(rig):
    rig.watch_markers()
    rig.start_daemon()
    rig.wait_marker("^")
    time.sleep(2)
    assert rig.scrolled() and rig.typed() == []
    log = rig.daemon_log()
    assert log.count("handoff-scrolled") == 1 and "unscrolled" not in log


def test_a_window_on_screen_also_linked_into_a_detached_session_keeps_its_scroll(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.attach()
    subprocess.run(rig.tmux + ["new-session", "-d", "-s", "other", "cat"], env=rig.env, check=True)
    subprocess.run(rig.tmux + ["link-window", "-s", "t:0", "-t", "other:5"], check=True)
    rig.scroll()
    assert rig.pane("#{session_name} #{window_active} #{session_attached}") == "other 1 0"
    start_daemon_and_expect_held(rig)


def test_a_window_on_screen_in_one_session_of_a_group_keeps_its_scroll(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.other_window()
    subprocess.run(rig.tmux + ["new-session", "-d", "-s", "t2", "-t", "t"], env=rig.env, check=True)
    subprocess.run(rig.tmux + ["select-window", "-t", "t2:9"], check=True)
    rig.attach()
    rig.attach("t2")
    rig.scroll()
    assert rig.pane("#{session_name} #{window_active} #{session_attached}") == "t2 0 1"
    start_daemon_and_expect_held(rig)


def test_a_tmux_that_cannot_count_viewers_holds_a_scrolled_pane(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.tmux_without("window_active_clients")
    rig.scroll()
    start_daemon_and_expect_held(rig)


def test_a_tmux_that_cannot_report_modes_still_gets_its_compact(rig):
    rig.start("gloss", 300000, daemon=False)
    rig.tmux_without("pane_in_mode")
    rig.start_daemon()
    rig.wait_for("TYPED /compact")


def test_the_marker_goes_when_the_window_loses_its_equals(rig):
    rig.start("gloss=", 600000, daemon=False)
    rig.watch_markers()
    rig.attach()
    rig.scroll()
    rig.start_daemon()
    rig.wait_marker("^")
    subprocess.run(rig.tmux + ["rename-window", "-t", rig.target, "gloss"], check=True)
    rig.wait_marker("")
    assert rig.scrolled()


def test_each_scroll_that_holds_a_handoff_back_is_logged_once(rig):
    rig.start("gloss=", 600000, draft="hello", daemon=False)
    rig.attach()
    rig.scroll()
    rig.start_daemon()
    time.sleep(1.5)
    rig.keys("-X", "cancel")
    time.sleep(1)
    rig.scroll()
    time.sleep(1.5)
    assert rig.daemon_log().count("handoff-scrolled") == 2


@pytest.mark.parametrize("window", ["gloss!", "gloss"])
def test_past_force_handoff_at_any_window_hands_off_then_compacts(rig, window):
    rig.start(window, 300000, context=900000)
    typed = rig.wait_typed(3)
    assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")
    assert "force_handoff_at" in rig.daemon_out.read_text()


def test_under_force_handoff_at_a_bang_window_is_left_alone(rig):
    rig.start("gloss!", 300000, context=790000)
    time.sleep(4)
    assert rig.typed() == []


@pytest.mark.parametrize("window,first", [("gloss!", None), ("gloss", "/compact")])
def test_with_force_handoff_at_off_windows_keep_their_own_rules(rig, window, first):
    (rig.home / ".claude" / "precompact.conf").write_text(f"auto_compact_at={THRESH}\nauto_resume=1\nforce_handoff_at=0\n")
    rig.start(window, 300000, context=900000)
    time.sleep(4)
    assert rig.typed()[:1] == ([first] if first else [])


@pytest.mark.parametrize("window,compacts", [("gloss", True), ("gloss!", False), ("gloss=", False)],
                         ids=["plain window", "opted out", "equals window"])
def test_past_force_handoff_at_a_handoff_that_never_starts_leaves_the_pane_to_its_windows_rule(rig, window, compacts):
    rig.start(window, 900000, handoff="ignore")
    if compacts:
        typed = rig.wait_typed(3)
        assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")
    else:
        rig.wait_log("never started")
        time.sleep(3)
        assert rig.typed() == ["/compact-handoff"]
    assert "never started" in rig.daemon_log()


def test_past_force_handoff_at_claude_codes_own_compaction_runs_when_the_handoff_never_starts(rig):
    rig.start("gloss", 100000, context=900000, handoff="ignore", daemon=False)
    rig.want(900000)
    rig.start_daemon()
    typed = rig.wait_typed(3)
    assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")
    assert ", asked by Claude Code" in rig.daemon_log()


def test_a_session_whose_handoff_never_started_is_not_reminded_or_forced_again(rig):
    rig.env["COMPACTO_COMPACT_COOLDOWN"] = "1"
    rig.start("gloss!", 300000, context=900000, handoff="ignore")
    rig.wait_log("never started")
    rig.keys("toolturn", "Enter")
    rig.wait_for("IDLE")
    rig.keys("turn", "Enter")
    rig.wait_for("AUTO_COMPACT_BLOCKED")
    time.sleep(4)
    assert rig.typed() == ["/compact-handoff", "toolturn", "turn"]
    assert not [e for e in rig.events() if e.startswith("REMINDED")]
    assert "AUTO_COMPACT_BLOCKED precompact: '!' in the tmux window name opts this session out of auto-compaction" \
        in rig.events()


def test_a_handoff_that_never_started_in_another_session_does_not_count(rig):
    rig.start("gloss!", 300000, context=900000, daemon=False)
    rig.signal("nohandoff").write_text(f"{uuid.uuid4()}\n")
    rig.start_daemon()
    typed = rig.wait_typed(3)
    assert typed[:2] == ["/compact-handoff", "/compact"] and typed[2].startswith("/resume ")


def test_past_force_handoff_at_a_session_without_the_hooks_keeps_its_windows_rule(rig):
    rig.start("gloss", 900000, hooked=False)
    typed = rig.wait_typed(2, timeout=15)
    assert typed[:1] == ["/compact"] and typed[1].startswith("/resume ")


@pytest.mark.parametrize("runs,owed", [("its source", True), ("the copy", True), ("another session", False)])
def test_a_slim_copy_is_handed_off_only_while_the_pane_runs_it_or_its_source(rig, runs, owed):
    rig.start("gloss!", 1000, daemon=False)
    current = rig.signal("hooked").read_text().split("\n")[0]
    other, copy = str(uuid.uuid4()), str(uuid.uuid4())
    source, copy = {"its source": (current, copy), "the copy": (other, current), "another session": (other, copy)}[runs]
    rig.signal("slim").write_text(f"{source}\t{copy}\n")
    rig.start_daemon()
    if owed:
        assert rig.wait_typed(2)[:2] == ["/compact-handoff", "/compact"]
    else:
        time.sleep(4)
        assert rig.typed() == [] and rig.signal("slim").exists()


def test_a_handoff_typed_by_hand_is_not_doubled_while_its_turn_ends(rig):
    rig.start("gloss=", 600000, handoff="lateidle", daemon=False)
    rig.keys("/compact-handoff", "Enter")
    rig.wait_for("IDLE_BEFORE_STOP")
    rig.start_daemon()
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:2] == ["/compact-handoff", "/compact"]
    assert "handoff-sent" not in rig.daemon_log()


def start_turn_then_daemon(rig, turn):
    """The daemon would hand off an idle pane past force_handoff_at at once, so it starts once the turn is under way."""
    rig.keys(turn, "Enter")
    rig.wait_for(f"TYPED {turn}")
    rig.start_daemon()


def test_a_handoff_claude_runs_itself_is_followed_by_compact_not_a_second_one(rig):
    rig.start("gloss!", 300000, context=900000, daemon=False)
    start_turn_then_daemon(rig, "skillturn")
    rig.wait_for("COMPACTED", timeout=30)
    assert rig.typed()[:2] == ["skillturn", "/compact"]
    assert "handoff-sent" not in rig.daemon_log()


def test_past_force_handoff_at_claude_is_reminded_mid_turn_and_handed_off_after(rig):
    rig.start("gloss!", 300000, context=900000, daemon=False)
    start_turn_then_daemon(rig, "toolturn")
    rig.wait_for("REMINDED", timeout=10)
    assert "900k" in next(e for e in rig.events() if e.startswith("REMINDED"))
    typed = rig.wait_typed(4)
    assert typed[:3] == ["toolturn", "/compact-handoff", "/compact"]


def test_a_handoff_refused_at_the_context_limit_runs_in_a_slim_copy(rig):
    rig.stuck()
    rig.start("gloss", 300000, context=974000, limit="yes,no")
    typed = rig.wait_typed(5, timeout=60)
    assert typed[0] == "/compact-handoff" and typed[1].startswith("/resume ")
    assert typed[2:4] == ["/compact-handoff", "/compact"] and typed[4].startswith("/resume ")
    slim = typed[1].split()[1]
    assert (rig.transcript.parent / f"{slim}.jsonl").exists() and typed[4] != typed[1]
    log = rig.daemon_log()
    assert "limit-slim" in log and f"limit-resume {rig.target} {slim}" in log
    assert not rig.signal("slim").exists()


def test_a_slim_copy_refused_at_the_limit_too_is_left_alone(rig):
    rig.stuck()
    rig.start("gloss=", 600000, context=974000, limit="yes")
    end = time.time() + 60
    while "limit-wedged" not in rig.daemon_log() and time.time() < end:
        time.sleep(0.2)
    rig.signal("ctx").write_text(f"{rig.target}\t974000\t600000\n")   # past force_handoff_at too, which mustn't undo the wedge
    time.sleep(4)
    typed = rig.typed()
    assert typed[0] == "/compact-handoff" and typed[1].startswith("/resume ") and typed[2:] == ["/compact-handoff"]
    log = rig.daemon_log()
    assert log.count("limit-wedged") == 1 and log.count("limit-slim") == 1


def test_after_a_wedge_its_source_session_is_not_sent_back_to_the_refused_copy(rig):
    rig.stuck()
    rig.start("gloss", 300000, context=974000, limit="yes", daemon=False)
    rig.keys("/compact-handoff", "Enter")
    rig.wait_for("REFUSED")
    source = rig.signal("handoff").read_text().split("\t")[1]
    copy = "00000000-0000-4000-8000-000000000000"
    (rig.transcript.parent / f"{copy}.jsonl").write_text("")
    rig.signal("slim").write_text(f"{source}\t{copy}\twedged\n")
    rig.start_daemon()
    time.sleep(5)
    assert not [t for t in rig.typed() if t.startswith("/resume")]


def test_the_daemon_says_when_it_cannot_run_slim_fork(rig):
    rig.env["COMPACTO_PYTHON"] = str(rig.home / "no-python")
    rig.start_daemon()
    end = time.time() + 10
    while "stays stuck" not in rig.daemon_out.read_text() and time.time() < end:
        time.sleep(0.2)
    assert "a handoff refused at the context limit stays stuck" in rig.daemon_out.read_text()


def test_a_waiting_dialog_keeps_its_marker_in_a_scrolled_window(rig):
    rig.start("gloss=", 600000, dialog=True, daemon=False)
    rig.watch_markers()
    rig.attach()
    rig.start_daemon()
    rig.wait_marker("?")
    rig.scroll()
    time.sleep(2)
    assert "handoff-scrolled" in rig.daemon_log()
    assert rig.pane("#{@cc_state}") == "?" and rig.typed() == []
