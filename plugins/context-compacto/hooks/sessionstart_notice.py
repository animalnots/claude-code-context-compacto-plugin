#!/usr/bin/env python3
"""SessionStart hook: post-install notice + resume-daemon keepalive.

Claude Code has no true "post-install" hook, so this fires on SessionStart and
shows a single `systemMessage` (a user-visible field) the first time it runs on
a machine that has no precompact config yet — i.e. a fresh install. Returning
users (who already have ~/.claude/precompact.conf) and subsequent sessions are
skipped via a marker file.

It also revives compacto-resume-daemon.sh if it died (the daemon is a plain
foreground process; an OOM kill or closed pane takes it out silently and every
auto-compact/resume stops until someone notices). Every new session checks the
daemon's own lock and respawns it when dead — announcing the restart, so deaths
are visible instead of silent. Only where the daemon can run at all (inside
tmux); on native Windows there is no tmux and this is a no-op.

It is intentionally bulletproof: stdin is drained, every error is swallowed, and
it always exits 0, so it can never disrupt session start.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

# Drain the SessionStart payload on stdin (unused) so the CLI never blocks.
try:
    sys.stdin.read()
except Exception:
    pass

out = {}

try:
    claude_dir = pathlib.Path.home() / ".claude"
    marker = claude_dir / ".cc-notice-shown"
    conf = claude_dir / "precompact.conf"
    # Show once, and only for a fresh install (no config yet). A pre-existing
    # config means a returning user / plugin update, who already knows the tool.
    if not marker.exists() and not conf.exists():
        out["systemMessage"] = (
            "context-compacto active: /compact now keeps the last 25k tokens verbatim "
            "(head=0) and summarizes the middle (sonnet; opus[1m] for oversized middles). "
            "Tune with /cc:end /cc:begin /cc:model200k /cc:model1m, or see /cc:help. After "
            "compacting, load the compressed session in this chat with the printed "
            "`/resume <id>` (or run `/resume` and pick the newest `compact …` entry)."
        )
        claude_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown\n", encoding="utf-8")
except Exception:
    pass


def _daemon_alive(lock: pathlib.Path) -> bool:
    """True if the pid in the daemon's lock is a live compacto-resume-daemon.

    Same test the daemon uses to reclaim a stale lock: pid liveness alone lies
    after a reboot (lock survives on disk, pid gets recycled), so match the
    process name too. `ps` exists everywhere tmux does.
    """
    try:
        pid = int((lock / "pid").read_text().strip())
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return "compacto-resume-daemon" in cmd
    except Exception:
        return False


try:
    daemon = pathlib.Path(__file__).resolve().parent / "compacto-resume-daemon.sh"
    signal_dir = pathlib.Path(
        os.environ.get("COMPACTO_SIGNAL_DIR", "") or str(pathlib.Path.home() / ".claude" / "compacto-signals")
    )
    # The daemon only makes sense where this session's tmux server is reachable —
    # skip outside tmux (native Windows, plain terminals, SSH without tmux).
    if daemon.exists() and os.environ.get("TMUX") and shutil.which("tmux"):
        if not _daemon_alive(signal_dir / ".daemon.lock"):
            signal_dir.mkdir(parents=True, exist_ok=True)
            log = signal_dir / "daemon.log"
            with open(log, "a") as fh:
                # Detached so it outlives this hook and the session that spawned it.
                # A lost race with another session's spawn is fine: the daemon's own
                # singleton lock makes the loser exit cleanly.
                proc = subprocess.Popen(
                    ["bash", str(daemon)], stdout=fh, stderr=fh,
                    start_new_session=True,
                )
            note = f"context-compacto: resume daemon was not running — restarted (pid {proc.pid}, log {log})."
            out["systemMessage"] = (out.get("systemMessage", "") + " " + note).strip()
except Exception:
    pass

if out:
    print(json.dumps(out))

sys.exit(0)
