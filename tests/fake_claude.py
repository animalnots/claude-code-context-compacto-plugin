#!/usr/bin/env python3
"""A stand-in for Claude Code in a tmux pane, for the daemon tests.

It draws the screen the daemon reads, takes the lines the daemon types, and runs the plugin's
real hooks the way Claude Code would. Everything it sees goes to --log, one event per line.
"""
import argparse
import datetime
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
ap.add_argument("--ctx", type=int, help="the context size its statusline exports beside msgs (default: --msgs)")
# How each typed handoff plays out, comma-separated in order (the last one repeats): ok, slowstop
# (another Stop hook keeps the turn going after ours ran), interrupt (Esc, so no Stop, then a
# later turn's Stop), late (a background result that arrived meanwhile runs its own turn straight
# after), lateidle (the screen shows the turn finished a while before its Stop hook runs),
# apierror (StopFailure), ignore.
ap.add_argument("--handoff", default="ok")
# Whether each session, the first one and then each resumed in turn (the last one repeats), is at the
# context limit: yes, where every prompt's request is refused and its turn ends with no Stop, or no.
ap.add_argument("--limit", default="no")
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
ap.add_argument("--dialog", action="store_true", help="a question dialog stands where the input box would be")
args = ap.parse_args()

pane = os.environ["TMUX_PANE"]
key = "".join(c for c in pane if c.isalnum())
signals = pathlib.Path(os.environ["COMPACTO_SIGNAL_DIR"])
session = str(uuid.uuid4())
msgs = args.msgs
ctx = args.msgs if args.ctx is None else args.ctx
scenarios = args.handoff.split(",")
resumes = args.resume.split(",")
compacts = args.compact.split(",")
limits = args.limit.split(",")
# The session's own transcript, which the hooks are given. PreCompact gets a one-line one, so it never summarizes.
transcript = pathlib.Path(args.transcript)
short = pathlib.Path(args.log).with_name("precompact.jsonl")
short.write_text('{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}\n')
# Lines of the conversation still on screen. A /compact that ran stays there, as in a resumed fork.
history = []


def next_of(plays):
    return plays.pop(0) if len(plays) > 1 else plays[0]


at_limit = next_of(limits) == "yes"


def log(event):
    with open(args.log, "a") as f:
        f.write(f"{time.time():.3f}\t{event}\n")


def render(busy):
    status = "✳ Working… (3s · ↓ 1.2k tokens)" if busy else f"✻ Worked for {args.done_for} · done 11:09"
    box = "Proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel\n" if args.dialog else "─" * 40 + "\n❯\u00a0"
    sys.stdout.write("\033[2J\033[H⏺ fake claude\n" + "".join(h + "\n" for h in history) + "\n" + status + "\n" + box)
    sys.stdout.flush()
    if not args.no_ctx:
        (signals / f"{key}.ctx").write_text(f"{pane}\t{ctx}\t{msgs}\n")


def hook(script, payload):
    r = subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def stop(prompt, event="Stop", active=False):
    hook("handoff.py", {"hook_event_name": event, "session_id": session, "prompt_id": prompt,
                        "transcript_path": str(transcript), "stop_hook_active": active, "background_tasks": []})
    stamp = signals / f"{key}.hooked"
    if args.old_hooks and stamp.exists():
        stamp.write_text(session + "\n")


def busy_for(secs):
    render(True)
    end = time.time() + secs
    while (left := end - time.time()) > 0:
        if select.select([sys.stdin], [], [], left)[0]:
            log("TYPED_WHILE_BUSY " + sys.stdin.readline().strip())


def refuse(prompt, text):
    """Claude Code at the context limit: the prompt lands in the transcript, its request is refused, and the turn ends with no Stop."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with transcript.open("a") as f:
        for e in ({"type": "user", "uuid": str(uuid.uuid4()), "promptId": prompt, "message": {"role": "user", "content": text}},
                  {"type": "assistant", "uuid": str(uuid.uuid4()), "isApiErrorMessage": True, "error": "invalid_request",
                   "message": {"role": "assistant", "model": "<synthetic>",
                               "content": [{"type": "text", "text": "Prompt is too long"}]}}):
            f.write(json.dumps(dict(e, parentUuid=None, isSidechain=False, sessionId=session, timestamp=now)) + "\n")
    render(False)
    log("REFUSED")


def tool_call(prompt, tool, tool_input):
    return hook("handoff.py", {"hook_event_name": "PostToolUse", "session_id": session, "prompt_id": prompt,
                               "transcript_path": str(transcript), "tool_name": tool, "tool_input": tool_input,
                               "tool_response": {}, "tool_use_id": "toolu_1"})


def handoff(command):
    how = next_of(scenarios)
    if how == "ignore":
        render(False)
        log("IGNORED")
        return
    prompt = str(uuid.uuid4())
    hook("handoff.py", {"hook_event_name": "UserPromptExpansion", "session_id": session, "prompt_id": prompt,
                        "transcript_path": str(transcript), "expansion_type": "slash_command",
                        "command_name": command, "command_args": "", "prompt": "/" + command})
    if at_limit:
        refuse(prompt, "/" + command)
        return
    busy_for(1.5)
    if how == "ok":
        stop(prompt)
    elif how == "lateidle":
        render(False)
        log("IDLE_BEFORE_STOP")
        time.sleep(3)
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
                                 "transcript_path": str(short), "trigger": trigger,
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


def tool_turn(handoff_skill):
    """A turn with one tool call: the handoff skill Claude runs by itself, or a Bash command whose hook may remind it to."""
    prompt = str(uuid.uuid4())
    busy_for(2)           # long enough for a daemon started with the turn to write its first heartbeat
    if handoff_skill:
        tool_call(prompt, "Skill", {"skill": "compact-handoff"})
    else:
        out = tool_call(prompt, "Bash", {"command": "ls"})
        if out:
            log("REMINDED " + json.loads(out)["hookSpecificOutput"]["additionalContext"])
    busy_for(1)
    stop(prompt)
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
        if next_of(compacts) == "freeze":
            history.append("❯ /compact")
            render(False)
            log("FROZEN")
        else:
            compact()
    elif cmd == "turn":
        turn()
    elif cmd in ("skillturn", "toolturn"):
        tool_turn(cmd == "skillturn")
    elif cmd == "work":
        busy_for(3)       # a turn cut short with Esc: no Stop
        render(False)
    elif cmd.startswith("/resume "):
        if next_of(resumes) == "eat":
            busy_for(1)
            stop(str(uuid.uuid4()))
            log("ATE_RESUME")
        else:
            time.sleep(0.3)   # loading the fork, so its size lands after the daemon drops the old one
            fork = cmd.split(None, 1)[1]
            if (transcript.parent / f"{fork}.jsonl").exists():
                # A slim copy is due its handoff whatever its size, so it reads 0 here, under every threshold.
                session, transcript, msgs, ctx = fork, transcript.parent / f"{fork}.jsonl", 0, 0
            else:
                session, msgs, ctx = str(uuid.uuid4()), 1000, 1000
            at_limit = next_of(limits) == "yes"
        render(False)
    elif cmd.startswith("/"):
        handoff(cmd[1:])
    else:
        render(False)
