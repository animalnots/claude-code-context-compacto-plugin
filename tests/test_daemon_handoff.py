import os
import pathlib
import subprocess
import sys
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
                        COMPACTO_COMPACT_COOLDOWN="60", COMPACTO_HANDOFF_START_TIMEOUT="3")
        self.daemon = None
        self.target = None

    def start(self, window, msgs, handoff="ok", hooked=True, draft="", daemon=True, resume="ok", ctx=True,
              compact="ok", old_hooks=False):
        cmd = [sys.executable, str(FAKE), "--log", str(self.log), "--transcript", str(self.transcript),
               "--msgs", str(msgs), "--handoff", handoff, "--resume", resume, "--compact", compact] \
            + (["--hooked"] if hooked else []) + ([] if ctx else ["--no-ctx"]) \
            + (["--old-hooks"] if old_hooks else [])
        subprocess.run(self.tmux + ["new-session", "-d", "-s", "t", "-n", window, "-x", "120", "-y", "30",
                                    " ".join(cmd)], env=self.env, check=True)
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
