#!/usr/bin/env bash
# compacto-resume-daemon.sh — auto-resume watcher for the context-compacto plugin,
# with optional threshold auto-compact and post-resume continue.
#
# THREE behaviors. #1 always runs; #2 and #3 are OFF by default and configured in
# ~/.claude/precompact.conf (read fresh every poll, so /cc: changes apply live):
#
#   1. RESUME (always): when the precompact hook forks a session it drops
#      ~/.claude/compacto-signals/<panekey>.resume = "<pane-id>\t<fork-id>".
#      This types `/resume <fork-id>` into that exact pane — the keystroke you'd
#      type by hand. Keyed on $TMUX_PANE, so N parallel sessions never cross-wire.
#
#   2. THRESHOLD AUTO-COMPACT (auto_compact_at=<tokens>): the statusline export
#      (see README) drops <panekey>.ctx = "<pane-id>\t<ctx>\t<msgs>" every render.
#      When a pane's live size crosses the threshold AND the pane is idle, this
#      types `/compact`. Metric: auto_compact_metric=msgs|ctx (default msgs).
#
#   3. POST-RESUME CONTINUE (resume_continue=<message>): after an auto-triggered
#      compaction resumes, types <message> (e.g. "continue") so the task keeps
#      going. UNBOUNDED — runs until you stop this daemon. Only fires for compacts
#      THIS daemon triggered (behavior 2), never for a manual /compact.
#
# Run ONE of these (serves every pane on the tmux server). Ctrl-C to stop —
# stopping it is the off-switch for the whole autonomous loop.
#
# Config (env):
#   COMPACTO_SIGNAL_DIR      signal dir            (default ~/.claude/compacto-signals)
#   COMPACTO_POLL_SECS       poll interval         (default 1)
#   COMPACTO_TMUX            tmux command           (default "tmux"; e.g. "tmux -L sock")
#   COMPACTO_BUSY_REGEX      "pane is generating" marker (default "esc to interrupt")
#   COMPACTO_COMPACT_COOLDOWN secs before a stuck .compacting marker clears (default 300)
#   COMPACTO_CONTINUE_SETTLE  secs to let a resume render before typing continue (default 3)
#
# Assumes a single tmux server; pane ids are unique per server.
set -u

SIGNAL_DIR="${COMPACTO_SIGNAL_DIR:-$HOME/.claude/compacto-signals}"
POLL="${COMPACTO_POLL_SECS:-1}"
TMUX_CMD="${COMPACTO_TMUX:-tmux}"
BUSY_REGEX="${COMPACTO_BUSY_REGEX:-esc to interrupt}"
COOLDOWN="${COMPACTO_COMPACT_COOLDOWN:-300}"    # > hook's 240s summarizer timeout, so we never double-compact
SETTLE="${COMPACTO_CONTINUE_SETTLE:-3}"
CONF="$HOME/.claude/precompact.conf"

command -v ${TMUX_CMD%% *} >/dev/null 2>&1 || { echo "compacto-resume-daemon: '${TMUX_CMD%% *}' not found on PATH" >&2; exit 1; }
mkdir -p "$SIGNAL_DIR"
trap 'echo "compacto-resume-daemon: stopped."; exit 0' INT TERM

conf_get() { [ -f "$CONF" ] && grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
is_num()   { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
pane_alive() { $TMUX_CMD list-panes -a -F '#{pane_id}' 2>/dev/null | grep -Fxq "$1"; }
pane_idle()  { ! $TMUX_CMD capture-pane -p -t "$1" 2>/dev/null | tail -25 | grep -qiE "$BUSY_REGEX"; }
file_age()   { local f="$1" m; m=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0); echo $(( $(date +%s) - m )); }

echo "compacto-resume-daemon: watching $SIGNAL_DIR (poll ${POLL}s, tmux='$TMUX_CMD'). Ctrl-C to stop."

while true; do
    THRESH="$(conf_get auto_compact_at)"
    METRIC="$(conf_get auto_compact_metric)"; [ -n "$METRIC" ] || METRIC="msgs"
    CONTINUE_MSG="$(conf_get resume_continue)"

    # ---- behavior 2: threshold auto-compact -------------------------------
    if is_num "$THRESH" && [ "$THRESH" -gt 0 ]; then
        for cf in "$SIGNAL_DIR"/*.ctx; do
            [ -e "$cf" ] || continue
            IFS=$'\t' read -r cpane cctx cmsg < "$cf"
            [ -n "${cpane:-}" ] || continue
            key="${cpane//[^a-zA-Z0-9]/}"
            if ! pane_alive "$cpane"; then          # session closed -> clean up its files
                rm -f "$cf" "$SIGNAL_DIR/$key.compacting" "$SIGNAL_DIR/$key.await-continue"
                continue
            fi
            case "$METRIC" in ctx) val="${cctx:-0}";; *) val="${cmsg:-0}";; esac
            is_num "$val" || continue
            [ "$val" -ge "$THRESH" ] || continue

            cm="$SIGNAL_DIR/$key.compacting"        # debounce: one compaction in flight per pane
            if [ -e "$cm" ]; then
                [ "$(file_age "$cm")" -gt "$COOLDOWN" ] && rm -f "$cm" || continue
            fi
            pane_idle "$cpane" || continue          # never interrupt an in-progress turn

            $TMUX_CMD send-keys -t "$cpane" "/compact" Enter
            : > "$cm"
            [ -n "$CONTINUE_MSG" ] && : > "$SIGNAL_DIR/$key.await-continue"
            rm -f "$cf"    # consume this reading; .compacting debounces until the fork resumes
            echo "compacto-resume-daemon: auto-compact $cpane ($METRIC=$val >= $THRESH)"
        done
    fi

    # ---- behavior 1 (+3): resume, then optional continue ------------------
    for f in "$SIGNAL_DIR"/*.resume; do
        [ -e "$f" ] || continue
        busy="$f.busy"
        mv "$f" "$busy" 2>/dev/null || continue     # claim atomically
        IFS=$'\t' read -r pane fork < "$busy"
        rm -f "$busy"
        if [ -z "${pane:-}" ] || [ -z "${fork:-}" ]; then
            echo "compacto-resume-daemon: malformed signal skipped (pane='${pane:-}' fork='${fork:-}')" >&2
            continue
        fi
        key="${pane//[^a-zA-Z0-9]/}"
        rm -f "$SIGNAL_DIR/$key.compacting"          # compaction cycle finished

        if ! pane_alive "$pane"; then
            rm -f "$SIGNAL_DIR/$key.await-continue"
            echo "compacto-resume-daemon: pane $pane gone; dropped resume $fork" >&2
            continue
        fi

        $TMUX_CMD send-keys -t "$pane" "/resume $fork" Enter
        echo "compacto-resume-daemon: resumed $fork in pane $pane"
        # Drop the pre-compaction size reading; the fork's next statusline render
        # writes the new (low) value, so we don't immediately re-trigger on stale data.
        rm -f "$SIGNAL_DIR/$key.ctx"

        ac="$SIGNAL_DIR/$key.await-continue"
        if [ -e "$ac" ] && [ -n "$CONTINUE_MSG" ]; then
            sleep "$SETTLE"                          # let the fork render before typing
            if pane_alive "$pane"; then
                $TMUX_CMD send-keys -t "$pane" "$CONTINUE_MSG" Enter
                echo "compacto-resume-daemon: continued $pane with: $CONTINUE_MSG"
            fi
            rm -f "$ac"
        fi
    done

    sleep "$POLL"
done
