#!/usr/bin/env python3
"""A stand-in for Claude Code in a tmux pane, for the daemon tests.

It draws the screen the daemon reads, takes the lines the daemon types, and runs the plugin's
real hooks the way Claude Code would. Everything it sees goes to --log, one event per line.
"""
import argparse
import json
import os
import pathlib
import select
import subprocess
import sys
import time
import uuid

HOOKS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "context-compacto" / "hooks"

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--transcript", required=True)
ap.add_argument("--msgs", type=int, required=True)
# How each typed handoff plays out, comma-separated in order (the last one repeats): ok, slowstop
# (another Stop hook keeps the turn going after ours ran), interrupt (Esc, so no Stop, then a
# later turn's Stop), late (a background result that arrived meanwhile runs its own turn straight
# after), apierror (StopFailure), ignore.
ap.add_argument("--handoff", default="ok")
# How each typed /resume plays out, in the same way: ok, or eat (a turn of its own runs instead and the
# session stays, as when a self-continuing agent's next turn swallows the keys).
ap.add_argument("--resume", default="ok")
# How each typed /compact plays out, in the same way: ok, or freeze (it stays queued and never runs, while the
# pane looks idle, as a queue that froze behind a turn).
ap.add_argument("--compact", default="ok")
ap.add_argument("--hooked", action="store_true", help="this session runs the plugin's Stop hook")
ap.add_argument("--old-hooks", action="store_true", help="its Stop hook stamps the session but not the turn")
ap.add_argument("--no-ctx", action="store_true", help="this session's statusline doesn't export its size")
ap.add_argument("--done-for", default="3s", help="how long the finished turn's status line says it took")
args = ap.parse_args()

pane = os.environ["TMUX_PANE"]
key = "".join(c for c in pane if c.isalnum())
signals = pathlib.Path(os.environ["COMPACTO_SIGNAL_DIR"])
session = str(uuid.uuid4())
msgs = args.msgs
scenarios = args.handoff.split(",")
resumes = args.resume.split(",")
compacts = args.compact.split(",")
# Lines of the conversation still on screen. A /compact that ran stays there, as in a resumed fork.
history = []


def log(event):
    with open(args.log, "a") as f:
        f.write(f"{time.time():.3f}\t{event}\n")


def render(busy):
    status = "✳ Working… (3s · ↓ 1.2k tokens)" if busy else f"✻ Worked for {args.done_for} · done 11:09"
    sys.stdout.write("\033[2J\033[H⏺ fake claude\n" + "".join(h + "\n" for h in history) + "\n" + status + "\n"
                     + "─" * 40 + "\n❯\u00a0")
    sys.stdout.flush()
    if not args.no_ctx:
        (signals / f"{key}.ctx").write_text(f"{pane}\t{msgs}\t{msgs}\n")


def hook(script, payload):
    r = subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def stop(prompt, event="Stop", active=False):
    hook("handoff.py", {"hook_event_name": event, "session_id": session, "prompt_id": prompt,
                        "transcript_path": args.transcript, "stop_hook_active": active, "background_tasks": []})
    stamp = signals / f"{key}.hooked"
    if args.old_hooks and stamp.exists():
        stamp.write_text(session + "\n")


def busy_for(secs):
    render(True)
    end = time.time() + secs
    while (left := end - time.time()) > 0:
        if select.select([sys.stdin], [], [], left)[0]:
            log("TYPED_WHILE_BUSY " + sys.stdin.readline().strip())


def handoff(command):
    how = scenarios.pop(0) if len(scenarios) > 1 else scenarios[0]
    if how == "ignore":
        render(False)
        log("IGNORED")
        return
    prompt = str(uuid.uuid4())
    hook("handoff.py", {"hook_event_name": "UserPromptExpansion", "session_id": session, "prompt_id": prompt,
                        "expansion_type": "slash_command", "command_name": command, "command_args": "",
                        "prompt": "/" + command})
    busy_for(1.5)
    if how == "ok":
        stop(prompt)
    elif how == "slowstop":
        stop(prompt)
        busy_for(3)
        stop(prompt, active=True)
    elif how == "late":
        stop(prompt)
        busy_for(1.5)
        stop(str(uuid.uuid4()))
    elif how == "apierror":
        stop(prompt, event="StopFailure")
    elif how == "interrupt":
        render(False)
        log("INTERRUPTED")
        time.sleep(1)
        later = str(uuid.uuid4())
        busy_for(1)
        stop(later)
    render(False)
    log("IDLE")


def precompact(trigger):
    out = hook("precompact.py", {"hook_event_name": "PreCompact", "session_id": session, "prompt_id": "",
                                 "transcript_path": args.transcript, "trigger": trigger,
                                 "custom_instructions": None})
    return json.loads(out)["reason"] if out else ""


def turn():
    """A turn past Claude Code's own compaction point, where it asks to auto-compact before an API call."""
    busy_for(0.5)
    log(f"AUTO_COMPACT_BLOCKED {precompact('auto')}")
    busy_for(0.5)
    stop(str(uuid.uuid4()))
    render(False)
    log("IDLE")


def compact():
    history.append("❯ /compact")
    render(True)
    reason = precompact("manual")
    if "transcript too short" in reason:
        fork = str(uuid.uuid4())
        (signals / f"{key}.resume").write_text(f"{pane}\t{fork}\n")
        log(f"COMPACTED {fork}")
    else:
        log(f"COMPACT_BLOCKED {reason}")
    time.sleep(0.5)
    render(False)


if args.hooked:
    stop(str(uuid.uuid4()))
render(False)
log("READY")
for line in sys.stdin:
    cmd = line.strip()
    log(f"TYPED {cmd}")
    if cmd == "/compact":
        if (compacts.pop(0) if len(compacts) > 1 else compacts[0]) == "freeze":
            history.append("❯ /compact")
            render(False)
            log("FROZEN")
        else:
            compact()
    elif cmd == "turn":
        turn()
    elif cmd == "work":
        busy_for(3)       # a turn cut short with Esc: no Stop
        render(False)
    elif cmd.startswith("/resume "):
        if (resumes.pop(0) if len(resumes) > 1 else resumes[0]) == "eat":
            busy_for(1)
            stop(str(uuid.uuid4()))
            log("ATE_RESUME")
        else:
            time.sleep(0.3)   # loading the fork, so its size lands after the daemon drops the old one
            session, msgs = str(uuid.uuid4()), 1000
        render(False)
    elif cmd.startswith("/"):
        handoff(cmd[1:])
    else:
        render(False)
