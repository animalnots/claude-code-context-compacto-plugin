#!/usr/bin/env bash
# compacto-resume-daemon.sh — auto-resume watcher for the context-compacto plugin.
#
# When `auto_resume` is on (/cc:autoresume on) AND a session runs inside tmux,
# the precompact hook drops a per-pane signal file after it writes a fork:
#
#     ~/.claude/compacto-signals/<panekey>.resume
#     contents (one line):  <tmux-pane-id>\t<fork-session-id>
#
# This daemon polls that dir and, for each signal, types `/resume <fork-id>`
# into the exact pane that produced it — the same keystroke you'd type by hand.
# Because the pane id ($TMUX_PANE, globally unique on the tmux server) is the
# key, any number of parallel sessions each resume their OWN window; they never
# cross-wire.
#
# Run ONE of these (it serves every pane on the default tmux server):
#     "${CLAUDE_PLUGIN_ROOT}/hooks/compacto-resume-daemon.sh" &
# or give it its own tmux pane and leave it running. Ctrl-C to stop. Nothing
# auto-resumes unless this is running — that plus `auto_resume` being off by
# default is why the feature is opt-in.
#
# Config (env):
#   COMPACTO_SIGNAL_DIR   signal dir      (default ~/.claude/compacto-signals)
#   COMPACTO_POLL_SECS    poll interval   (default 1)
#
# Assumes a single default tmux server. Pane ids are unique per server, so if
# you run multiple tmux servers on different sockets, ids can collide across
# them and this (bound to one socket) could target the wrong pane.
set -u

SIGNAL_DIR="${COMPACTO_SIGNAL_DIR:-$HOME/.claude/compacto-signals}"
POLL="${COMPACTO_POLL_SECS:-1}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "compacto-resume-daemon: tmux not found on PATH — nothing to drive." >&2
  exit 1
fi

mkdir -p "$SIGNAL_DIR"
trap 'echo "compacto-resume-daemon: stopped."; exit 0' INT TERM
echo "compacto-resume-daemon: watching $SIGNAL_DIR (poll ${POLL}s). Ctrl-C to stop."

while true; do
  for f in "$SIGNAL_DIR"/*.resume; do
    [ -e "$f" ] || continue   # no matches -> literal glob, skip

    # Claim atomically: rename out of the way so the hook's next write or a
    # second daemon can't double-process this signal. Whoever wins the rename
    # owns it; losers see the source gone and move on.
    busy="$f.busy"
    mv "$f" "$busy" 2>/dev/null || continue

    IFS=$'\t' read -r pane fork < "$busy"
    rm -f "$busy"

    if [ -z "${pane:-}" ] || [ -z "${fork:-}" ]; then
      echo "compacto-resume-daemon: malformed signal skipped (pane='${pane:-}' fork='${fork:-}')" >&2
      continue
    fi

    # Only send if that pane still exists on this server; else the session was
    # closed and the fork is resumable by hand from the block message.
    if tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -Fxq "$pane"; then
      tmux send-keys -t "$pane" "/resume $fork" Enter
      echo "compacto-resume-daemon: resumed $fork in pane $pane"
    else
      echo "compacto-resume-daemon: pane $pane gone; dropped resume $fork" >&2
    fi
  done
  sleep "$POLL"
done
