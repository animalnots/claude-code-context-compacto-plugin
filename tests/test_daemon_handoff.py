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

    def start(self, window, msgs, handoff="ok", hooked=True, draft="", daemon=True):
        cmd = [sys.executable, str(FAKE), "--log", str(self.log), "--transcript", str(self.transcript),
               "--msgs", str(msgs), "--handoff", handoff] + (["--hooked"] if hooked else [])
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
    run_hook("handoff.py", {"hook_event_name": "Stop", "session_id": str(uuid.uuid4()), "prompt_id": str(uuid.uuid4())},
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
