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
import termios
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
ap.add_argument("--suggest", default="", help="the prompt Claude Code suggests next, shown in the idle box with its "
                "first letter under the cursor, in reverse video, and the rest faint")
ap.add_argument("--blurred", action="store_true", help="draw the whole suggestion faint, as Claude Code does in a pane "
                "it was told lost focus")
ap.add_argument("--faint-rule", action="store_true", help="the input box's top border is drawn faint")
ap.add_argument("--echo-after", default="0", help="seconds before each typed line's keys show in the box, as a busy "
                "machine draws them, comma-separated in order, the last repeating; later keys on a shown line show at once")
ap.add_argument("--session", help="the first session's id (default: a new one)")
ap.add_argument("--raw", action="store_true", help="read keys as they come, Esc included, instead of whole lines")
# A turn that starts on its own (a background result's, say) and runs this many seconds. An Esc closes a dialog
# that stands over it, or else ends it with no Stop, as Claude Code does; other keys typed during it are only logged.
ap.add_argument("--turn", type=float, default=0)
ap.add_argument("--tool", type=float, default=0, help="the turn's tool call writes its result to the transcript after "
                "this many seconds; under 0, never")
ap.add_argument("--turn-skill", action="store_true", help="the turn runs the handoff skill as it starts")
ap.add_argument("--turn-dialog", action="store_true", help="a dialog, with no spinner, stands over the turn")
ap.add_argument("--permission", action="store_true", help="that dialog asks to run the turn's tool call, so an Esc "
                "refuses the call, which ends the turn with no Stop")
ap.add_argument("--turn-prompt", default="", help="the turn's prompt_id, on each of its user entries (default: a new "
                "one); a Stop hook that keeps the handoff's own turn going keeps the handoff's")
ap.add_argument("--queued", default="", help="a command typed before the turn's first frame, run once it ends")
ap.add_argument("--deaf", action="store_true", help="an Esc doesn't end the turn")
ap.add_argument("--compacting", default="", help="the busy spinner's text while a compaction runs, e.g. Compacting "
                "conversation (Claude Code's own) or Running PreCompact hooks (the plugin's, run inside that hook)")
ap.add_argument("--note", default="", help="a line of the conversation on screen")
ap.add_argument("--stale-dialog", action="store_true", help="a dialog left from before stands over the prompt once a "
                "request is refused at the context limit, until an Esc")
args = ap.parse_args()

pane = os.environ["TMUX_PANE"]
key = "".join(c for c in pane if c.isalnum())
signals = pathlib.Path(os.environ["COMPACTO_SIGNAL_DIR"])
session = args.session or str(uuid.uuid4())
msgs = args.msgs
ctx = args.msgs if args.ctx is None else args.ctx
scenarios = args.handoff.split(",")
resumes = args.resume.split(",")
compacts = args.compact.split(",")
limits = args.limit.split(",")
echo_delays = [float(d) for d in args.echo_after.split(",")]
# With a delay the fake reads the keys itself, and its box draws "shown", the part of the typed line on screen so far.
raw = any(echo_delays) or args.raw or bool(args.turn)
shown = ""
dialog = args.dialog
queued = args.queued
# The session's own transcript, which the hooks are given. PreCompact gets a one-line one, so it never summarizes.
transcript = pathlib.Path(args.transcript)
short = pathlib.Path(args.log).with_name("precompact.jsonl")
short.write_text('{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}\n')
# Lines of the conversation still on screen. A /compact that ran stays there, as in a resumed fork.
history = [args.note] if args.note else []


def next_of(plays):
    return plays.pop(0) if len(plays) > 1 else plays[0]


at_limit = next_of(limits) == "yes"


def log(event):
    with open(args.log, "a") as f:
        f.write(f"{time.time():.3f}\t{event}\n")


def render(busy):
    spinner = f"✳ {args.compacting or 'Working'}… (3s · ↓ 1.2k tokens)"
    status = ("" if dialog else spinner) if busy else f"✻ Worked for {args.done_for} · done 11:09"
    rule = f"\033[2m{'─' * 40}\033[0m" if args.faint_rule else "─" * 40
    waiting = f"❯ {queued}\n  ctrl+enter to send now\n" if busy and queued else ""
    typed = "Press up to edit queued messages" if waiting else shown
    box = "Proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel\n" if dialog else rule + "\n❯\u00a0" + typed
    if args.suggest and not busy and not dialog and not shown:
        s = args.suggest
        hint = f"\033[2m{s}\033[0m" if args.blurred else f"\033[7m{s[0]}\033[27m\033[2m{s[1:]}\033[22m"
        # The cursor goes back to its start, so typed keys draw over it as typing replaces it in Claude Code.
        box += f"{hint}\033[{len(s)}D"
    sys.stdout.write("\033[2J\033[H⏺ fake claude\n" + "".join(h + "\n" for h in history) + "\n" + waiting + status
                     + "\n" + box)
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
            got = os.read(sys.stdin.fileno(), 1024).decode(errors="replace") if raw else sys.stdin.readline()
            log("TYPED_WHILE_BUSY " + got.strip())


def write_entries(*entries):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with transcript.open("a") as f:
        for e in entries:
            f.write(json.dumps(dict(e, uuid=str(uuid.uuid4()), parentUuid=None, isSidechain=False, sessionId=session,
                                    timestamp=now)) + "\n")


def refuse(prompt, text):
    """Claude Code at the context limit: the prompt lands in the transcript, its request is refused, and the turn ends with no Stop."""
    global dialog
    write_entries({"type": "user", "promptId": prompt, "message": {"role": "user", "content": text}},
                  {"type": "assistant", "isApiErrorMessage": True, "error": "invalid_request",
                   "message": {"role": "assistant", "model": "<synthetic>",
                               "content": [{"type": "text", "text": "Prompt is too long"}]}})
    dialog = dialog or args.stale_dialog
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


def background_turn():
    global dialog, queued
    prompt = args.turn_prompt or str(uuid.uuid4())
    write_entries({"type": "user", "promptId": prompt, "message": {"role": "user", "content": "a task ended"}})
    if args.turn_skill:
        tool_call(prompt, "Skill", {"skill": "compact-handoff"})
    if args.tool:
        write_entries({"type": "assistant", "message": {"role": "assistant", "id": "m-tool", "content": [
            {"type": "tool_use", "id": "t-tool", "name": "Bash", "input": {"command": "make"}}]}})
    dialog = dialog or args.turn_dialog
    render(True)
    log("TURN")
    fd, stopped = sys.stdin.fileno(), False
    end = time.time() + args.turn
    answer_at = time.time() + args.tool if args.tool > 0 else None
    while not stopped and (left := end - time.time()) > 0:
        if select.select([fd], [], [], left if answer_at is None else max(0.0, min(left, answer_at - time.time())))[0]:
            got = os.read(fd, 1024).decode(errors="replace")
            for _ in range(got.count("\x1b")):
                log("ESC")
                if dialog:
                    dialog, stopped = False, args.permission
                    log("DIALOG_CLOSED")
                    render(True)
                elif not args.deaf:
                    stopped = True
            if got.replace("\x1b", "").strip():
                log("TYPED_WHILE_BUSY " + got.replace("\x1b", "").strip())
        if answer_at is not None and time.time() >= answer_at:
            write_entries({"type": "user", "promptId": prompt, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t-tool", "content": "ok"}]}})
            answer_at = None
            log("TOOL_DONE")
    if stopped:
        log("INTERRUPTED")
    else:
        stop(prompt)
    render(False)
    log("IDLE")
    if queued:
        cmd, queued = queued, ""
        time.sleep(1)       # the daemon reads the screen in between, as it can before Claude Code runs it
        log(f"RAN_QUEUED {cmd}")
        handoff(cmd[1:])


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


def typed_lines():
    global shown
    if not raw:
        yield from sys.stdin
        return
    fd = sys.stdin.fileno()
    line, due, delay = "", None, next_of(echo_delays)
    while True:
        if select.select([fd], [], [], None if due is None else max(0.0, due - time.time()))[0]:
            for ch in os.read(fd, 1024).decode(errors="replace"):
                if ch == "\x1b":
                    yield ch
                elif ch in "\r\n":
                    shown, due = "", None
                    yield line + "\n"
                    line, delay = "", next_of(echo_delays)
                elif ch in "\x7f\b":
                    line = line[:-1]
                else:
                    line += ch
            if line != shown and due is None:
                due = time.time() + delay
        if due is not None and time.time() >= due:
            shown, due, delay = line, None, 0
            render(False)


if raw:
    tty = termios.tcgetattr(sys.stdin.fileno())
    tty[3] &= ~(termios.ICANON | termios.ECHO)
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tty)
if args.hooked:
    stop(str(uuid.uuid4()))
render(False)
log("READY")
if args.turn:
    background_turn()
for line in typed_lines():
    if line == "\x1b":
        log("ESC")
        if dialog:
            dialog = False
            log("DIALOG_CLOSED")
        render(False)
        continue
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
