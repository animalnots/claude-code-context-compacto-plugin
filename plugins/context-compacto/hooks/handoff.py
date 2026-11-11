#!/usr/bin/env python3
"""Tracks /compact-handoff runs for the PreCompact guard and the daemon's handoffs.

Claude Code runs this on UserPromptExpansion, PostToolUse, Stop and StopFailure. A handoff starts
when its command is typed, or when Claude runs it with the Skill tool, and has finished only when
that same turn ends with Stop. Every hook event of one turn carries the same prompt_id, including
messages typed and background-task notifications that arrive mid-turn. An interrupted turn fires
no Stop, so a Stop or StopFailure carrying another prompt_id means the handoff's turn was cut short.

The state is one tab-separated line in <signal dir>/<key>.handoff:
  state  session_id  prompt_id  started_epoch  ended_epoch
where state is running, done, aborted or failed. Inside tmux the key is the pane's, as for the
daemon's .ctx and .resume files. Outside tmux it is "s_<session id>", and the file is dropped
once the turn is over, since only the guard reads it there.

Every Stop of a session that keeps a transcript also writes the session id to <key>.hooked, and the
prompt_id of the turn that ended on a second line. The daemon types the handoff command only into
panes that have one, because a session started before this plugin version runs no such hooks, and
after a handoff that never started it waits for a newer one. When the second line names another turn
than the handoff's, a turn ran after the handoff, so the daemon hands off again before compacting.
A start inside tmux also writes <key>.transcript, "session_id<TAB>transcript path", for the daemon to
check whether the handoff's request was refused at the context limit (see slim_fork.py).

Past force_handoff_at, the daemon hands off and compacts a pane whatever its window's name, but only
at an idle prompt. So on PostToolUse in a pane it watches, once the pane's exported context reaches
that size, this tells Claude to hand off and end the turn: once a turn, and again each REMIND_STEP
tokens more. <key>.forcenote holds the last such reminder, "session_id<TAB>prompt_id<TAB>context".

Config (~/.claude/precompact.conf): handoff_command=<skill or command> (default compact-handoff),
force_handoff_at=<context tokens> (default 800000, 0 = off). The hook always exits 0 and prints
nothing but that reminder, so it can't disturb a turn.
"""
import json
import os
import pathlib
import re
import sys
import time

DEFAULT_COMMAND = "compact-handoff"
# A manual /compact repeated this many seconds after a block is taken as deliberate. The lower
# bound stops a second /compact queued right behind the first from counting as the repeat.
REPEAT_MIN_SECS, REPEAT_MAX_SECS = 5, 120
FIELDS = ("state", "session", "prompt", "started", "ended")
# compacto-resume-daemon.sh falls back to the same number on its own; change both together.
FORCE_HANDOFF_DEFAULT = 800000
REMIND_STEP = 50000
# The daemon rewrites .daemon.lock/heartbeat every poll. Daemons from before 1.5.16 don't, and they
# would never run a compaction left to them, so only a fresh one counts as a daemon that will.
DAEMON_HEARTBEAT_MAX_AGE = 60


def signal_dir():
    return pathlib.Path(os.environ.get("COMPACTO_SIGNAL_DIR")
                        or pathlib.Path.home() / ".claude" / "compacto-signals")


def pane_key():
    """The daemon's key for this pane, or "" outside tmux.

    psmux numbers panes per server, so there the server pid from $TMUX prefixes the key.
    """
    pane = "".join(c for c in os.environ.get("TMUX_PANE", "") if c.isalnum())
    if not pane:
        return ""
    psmux = re.search(r"psmux-(\d+)", os.environ.get("TMUX", "").split(",")[0])
    return f"{psmux.group(1)}_{pane}" if psmux else pane


def state_key(session_id):
    return pane_key() or "s_" + "".join(c for c in session_id if c.isalnum())


def _conf_value(key):
    try:
        lines = (pathlib.Path.home() / ".claude" / "precompact.conf").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    val = ""
    for ln in lines:
        k, sep, v = ln.partition("=")
        if sep and k.strip() == key:
            val = v.strip()
    return val


def handoff_command():
    """The handoff's skill or command name, without its slash (the daemon drops it the same way)."""
    name = _conf_value("handoff_command")
    return (name[1:] if name.startswith("/") else name) or DEFAULT_COMMAND


def daemon_conf(key):
    """key's value as the daemon's conf_get reads it: from the last "key=" line, nothing stripped, "" when
    there's none. None when the file can't be read.

    Spaces and a CR are kept so that wherever this and the daemon could differ, a value reads as no number
    and callers fall back to what they did without the daemon, rather than count on a step it won't take.
    """
    try:
        text = (pathlib.Path.home() / ".claude" / "precompact.conf").read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return None
    values = [ln[len(key) + 1:] for ln in text.split("\n") if ln.startswith(key + "=")]
    return values[-1] if values else ""


def daemon_threshold_on():
    """Whether the daemon's threshold compaction, which also runs its handoffs, is on: auto_compact_at isn't 0."""
    at = daemon_conf("auto_compact_at")
    return at is not None and (at == "" or (re.fullmatch(r"[0-9]+", at) is not None and int(at) > 0))


def force_handoff_at():
    """The context size at which the daemon hands off and compacts a pane in any window, or 0 when that's off."""
    at = daemon_conf("force_handoff_at")
    if at is None or not daemon_threshold_on():
        return 0
    if at == "":
        return FORCE_HANDOFF_DEFAULT
    return int(at) if re.fullmatch(r"[0-9]+", at) else 0


def daemon_alive():
    try:
        return time.time() - (signal_dir() / ".daemon.lock" / "heartbeat").stat().st_mtime <= DAEMON_HEARTBEAT_MAX_AGE
    except OSError:
        return False


def pane_sizes(key):
    """(context, msgs) from the pane's statusline export, or None unless both read as plain numbers the way the
    daemon reads them (a trailing CR dropped): the daemon skips a pane whose metric isn't one."""
    try:
        line = (signal_dir() / f"{key}.ctx").read_bytes().decode("utf-8").split("\n")[0]
    except (OSError, ValueError):
        return None
    sizes = [f[:-1] if f.endswith("\r") else f for f in line.split("\t")[1:3]]
    if len(sizes) < 2 or not all(re.fullmatch(r"[0-9]+", s) for s in sizes):
        return None
    return int(sizes[0]), int(sizes[1])


def _write(path, line):
    # Swapped in whole because the daemon polls these files. Bytes, because text mode on
    # Windows would end the line with CRLF.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(line.encode("utf-8"))
    os.replace(tmp, path)


def read_state(key):
    try:
        parts = (signal_dir() / f"{key}.handoff").read_text(encoding="utf-8").rstrip("\n").split("\t")
        st = dict(zip(FIELDS, parts)) if len(parts) == len(FIELDS) else None
        if st:
            st["started"] = int(st["started"])
            st["ended"] = int(st["ended"]) if st["ended"] else None
        return st
    except (OSError, ValueError):
        return None


def write_state(key, st):
    _write(signal_dir() / f"{key}.handoff",
           "\t".join("" if st[f] is None else str(st[f]) for f in FIELDS) + "\n")


def record_start(key, session_id, prompt, now, transcript):
    write_state(key, {"state": "running", "session": session_id, "prompt": prompt, "started": now, "ended": None})
    if pane_key() and isinstance(transcript, str) and transcript:
        _write(signal_dir() / f"{key}.transcript", f"{session_id}\t{transcript}\n")


def read_note(key):
    try:
        session, prompt, ctx = (signal_dir() / f"{key}.forcenote").read_text(encoding="utf-8").rstrip("\n").split("\t")
        return session, prompt, int(ctx)
    except (OSError, ValueError):
        return None


def past_force_handoff_at(key):
    """(context, force_handoff_at) while the daemon would hand this pane off and compact it at its next idle
    prompt whatever its window's name, else None."""
    at = force_handoff_at() if pane_key() else 0
    sizes = pane_sizes(key) if at else None
    return (sizes[0], at) if sizes and sizes[0] >= at and daemon_alive() else None


def handoff_reminder(key, session_id, prompt):
    """What to tell Claude when this turn should hand off now, or None."""
    due = past_force_handoff_at(key) if prompt else None
    if not due:
        return None
    ctx, at = due
    st = read_state(key)
    if st and st["state"] == "running" and (st["session"], st["prompt"]) == (session_id, prompt):
        return None
    note = read_note(key)
    if note and note[:2] == (session_id, prompt) and ctx < note[2] + REMIND_STEP:
        return None
    _write(signal_dir() / f"{key}.forcenote", f"{session_id}\t{prompt}\t{ctx}\n")
    return (f"context-compacto: this session's context is {ctx // 1000}k tokens, past force_handoff_at "
            f"({at // 1000}k). Finish only the step in progress, then run the {handoff_command()} skill to write "
            "the handoff, and end your turn without starting anything else. The resume daemon compacts the "
            "session as soon as this turn ends.")


def read_guard(key):
    try:
        session, prompt, at = (signal_dir() / f"{key}.guard").read_text(encoding="utf-8").rstrip("\n").split("\t")
        return session, prompt, int(at)
    except (OSError, ValueError):
        return None


def record_guard(key, session_id, prompt, now):
    _write(signal_dir() / f"{key}.guard", f"{session_id}\t{prompt}\t{now}\n")


def block_reason(state, session_id, trigger, window_name, now, guard):
    """Why a compaction must not run now, or None to let it run.

    A handoff still running in this session blocks every compaction: a /compact queued behind it
    runs the moment its turn ends, interrupted or not. In a "=" window (without "!") auto
    compaction is refused, and a manual one needs this session's handoff to have finished. A
    manual /compact repeated within the REPEAT window of a block of the same state runs anyway.
    """
    ours = state if state and state.get("session") == session_id else None
    equals = "=" in window_name and "!" not in window_name
    cmd = "/" + handoff_command()
    if ours and ours["state"] == "running":
        started = time.strftime("%H:%M:%S", time.localtime(ours["started"]))
        reason = (f"precompact: {cmd} started at {started} and hasn't finished (still running, or its "
                  "turn was interrupted), so compacting now could lose what it was saving.")
    elif equals and trigger == "auto":
        return f"precompact: '=' in the tmux window name: compaction waits for {cmd}, which the resume daemon runs."
    elif equals and not (ours and ours["state"] == "done"):
        reason = f"precompact: '=' in the tmux window name: compact after {cmd} finishes."
    else:
        return None
    # Claude Code retries its own compaction on every turn, so only a typed /compact can repeat.
    if trigger != "manual":
        return reason
    prompt = ours["prompt"] if ours else ""
    if (guard and guard[:2] == (session_id, prompt)
            and REPEAT_MIN_SECS <= now - guard[2] <= REPEAT_MAX_SECS):
        return None
    return f"{reason} Run /compact again within {REPEAT_MAX_SECS // 60} minutes to compact anyway."


def check_compaction(session_id, trigger, window_name):
    """block_reason for this session's handoff state, recording a manual block so a repeat can pass."""
    key, now = state_key(session_id), int(time.time())
    state = read_state(key)
    reason = block_reason(state, session_id, trigger, window_name, now, read_guard(key))
    if reason and trigger == "manual":
        ours = state if state and state.get("session") == session_id else None
        # A PreCompact hook that crashes lets Claude Code compact natively, so the block must
        # survive a failed write; only the repeat override is lost.
        try:
            record_guard(key, session_id, ours["prompt"] if ours else "", now)
        except OSError:
            pass
    return reason


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict) or not payload.get("session_id"):
        return
    event, session = payload.get("hook_event_name"), payload["session_id"]
    prompt = payload.get("prompt_id") or ""
    key, now = state_key(session), int(time.time())
    if event == "UserPromptExpansion":
        if prompt and payload.get("command_name") == handoff_command():
            record_start(key, session, prompt, now, payload.get("transcript_path"))
        return
    if event == "PostToolUse":
        # A subagent's tool call carries its parent's session, transcript and prompt_id.
        if payload.get("agent_id"):
            return
        tool_input = payload.get("tool_input")
        skill = tool_input.get("skill") if isinstance(tool_input, dict) else None
        if (payload.get("tool_name") == "Skill" and isinstance(skill, str) and prompt
                and (skill[1:] if skill.startswith("/") else skill) == handoff_command()):
            st = read_state(key)
            if not (st and st["state"] == "running" and (st["session"], st["prompt"]) == (session, prompt)):
                record_start(key, session, prompt, now, payload.get("transcript_path"))
            return
        text = handoff_reminder(key, session, prompt)
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
        return
    if event not in ("Stop", "StopFailure"):
        return
    # Older precompact.py versions start their summarizer (claude -p --no-session-persistence) with
    # this pane's TMUX_PANE; only a Stop whose transcript is on disk comes from the pane's own Claude.
    transcript = payload.get("transcript_path")
    if event == "Stop" and pane_key() and isinstance(transcript, str) and os.path.isfile(transcript):
        _write(signal_dir() / f"{key}.hooked", session + "\n" + (prompt + "\n" if prompt else ""))
    st = read_state(key)
    if not (prompt and st and st["state"] == "running" and st["session"] == session):
        return
    if prompt != st["prompt"]:
        st["state"] = "aborted"
    else:
        st["state"] = "done" if event == "Stop" else "failed"
    st["ended"] = now
    if pane_key():
        write_state(key, st)
    else:
        (signal_dir() / f"{key}.handoff").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
