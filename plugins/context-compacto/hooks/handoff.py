#!/usr/bin/env python3
"""Tracks /compact-handoff runs for the PreCompact guard and the daemon's "=" windows.

Claude Code runs this on UserPromptExpansion, Stop and StopFailure. A handoff starts when its
command is typed and has finished only when that same turn ends with Stop. Every hook event of
one turn carries the same prompt_id, including messages typed and background-task notifications
that arrive mid-turn. An interrupted turn fires no Stop, so a Stop or StopFailure carrying
another prompt_id means the handoff's turn was cut short.

The state is one tab-separated line in <signal dir>/<key>.handoff:
  state  session_id  prompt_id  started_epoch  ended_epoch
where state is running, done, aborted or failed. Inside tmux the key is the pane's, as for the
daemon's .ctx and .resume files. Outside tmux it is "s_<session id>", and the file is dropped
once the turn is over, since only the guard reads it there.

Every Stop of a session that keeps a transcript also writes the session id to <key>.hooked. The
daemon types the handoff command only into panes that have one, because a session started before
this plugin version runs no such hooks, and after a handoff that never started it waits for a
newer one.

Config (~/.claude/precompact.conf): handoff_command=<skill or command> (default compact-handoff).
The hook prints nothing and always exits 0, so it can't disturb a turn.
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
            write_state(key, {"state": "running", "session": session, "prompt": prompt,
                              "started": now, "ended": None})
        return
    if event not in ("Stop", "StopFailure"):
        return
    # Older precompact.py versions start their summarizer (claude -p --no-session-persistence) with
    # this pane's TMUX_PANE; only a Stop whose transcript is on disk comes from the pane's own Claude.
    transcript = payload.get("transcript_path")
    if event == "Stop" and pane_key() and isinstance(transcript, str) and os.path.isfile(transcript):
        _write(signal_dir() / f"{key}.hooked", session + "\n")
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
