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
#   COMPACTO_REARM_GRACE      secs a pane must stay below threshold before re-arming (default 15)
#   COMPACTO_RESUME_RETRY_SECS  secs between /resume re-sends until it lands (default 8)
#   COMPACTO_RESUME_GIVEUP    secs to keep retrying /resume before giving up (default 180)
#   COMPACTO_MAX_COMPACT_FAILS  genuine /compact failures before a pane is treated as wedged (default 3)
#   COMPACTO_WEDGE_BACKOFF    retry interval (secs) once a pane is wedged (default 1800)
#
# Assumes a single tmux server; pane ids are unique per server.
set -u

SIGNAL_DIR="${COMPACTO_SIGNAL_DIR:-$HOME/.claude/compacto-signals}"
POLL="${COMPACTO_POLL_SECS:-1}"
TMUX_CMD="${COMPACTO_TMUX:-tmux}"
BUSY_REGEX="${COMPACTO_BUSY_REGEX:-esc to interrupt}"
COOLDOWN="${COMPACTO_COMPACT_COOLDOWN:-300}"    # safety valve to clear a .compacting stuck by a FAILED compaction (no fork). A successful compaction is debounced by its pending .resume signal, not this timer — a big session can compact longer than COOLDOWN.
SETTLE="${COMPACTO_CONTINUE_SETTLE:-3}"
# Hard floor between auto-compacts on the SAME pane. Backstop for spam: if a /resume
# fails to land (pane momentarily unreadable — copy-mode, a render frame), .ctx stays
# high and behavior 2 would re-fire every second. This caps it to once per interval so
# a stuck pane degrades to a slow retry, never a storm. Stamped on both /compact and
# /resume, so a fresh fork gets time to render its low size before it's eligible again.
MIN_INTERVAL="${COMPACTO_MIN_COMPACT_INTERVAL:-30}"
# Seconds a pane must stay CONFIRMED below the threshold before .compacting re-arms. This
# is what makes the debounce honest: a /compact isn't "done" when we type /resume (the
# resume can fail to switch a busy autonomous pane), it's done when the size actually and
# durably drops. Must exceed the brief .ctx flicker a resume transition produces.
REARM_GRACE="${COMPACTO_REARM_GRACE:-15}"
# Resume retry. A self-continuing agent starts its next turn in the brief idle frame we
# resumed into, so a single /resume is often eaten and the pane never switches to the fork.
# After sending /resume we watch the pane's size; if it hasn't dropped we re-send (only
# while idle, at most every RESUME_RETRY_SECS) until it does, or give up after RESUME_GIVEUP.
RESUME_RETRY_SECS="${COMPACTO_RESUME_RETRY_SECS:-8}"
RESUME_GIVEUP="${COMPACTO_RESUME_GIVEUP:-180}"
# Wedge back-off. If /compact keeps genuinely failing (fires into an idle pane but no fork
# ever comes — the classic cause is orphaned queued input from a prior resumed-away session
# jamming the pane's input), stop shoving a /compact in every COOLDOWN. After MAX_FAILS
# genuine failures, stretch the retry interval to WEDGE_BACKOFF and print a one-line alert so
# the pane can be cleared by hand. The counter resets the moment the pane compacts normally.
MAX_FAILS="${COMPACTO_MAX_COMPACT_FAILS:-3}"
WEDGE_BACKOFF="${COMPACTO_WEDGE_BACKOFF:-1800}"
DEBUG="${COMPACTO_DEBUG:-}"                      # set to 1 to log pane state at each auto-compact fire
CONF="$HOME/.claude/precompact.conf"

command -v ${TMUX_CMD%% *} >/dev/null 2>&1 || { echo "compacto-resume-daemon: '${TMUX_CMD%% *}' not found on PATH" >&2; exit 1; }
mkdir -p "$SIGNAL_DIR"

# Single-instance lock. launchd (KeepAlive) plus a stray manual start would otherwise run
# two pollers that both fire /compact and /resume into the same panes — a self-inflicted
# double-action. mkdir is the atomic lock; the holder pid lets a restart reclaim a lock left
# stale by a SIGKILL (OOM, which can't run the cleanup trap). A duplicate start exits BEFORE
# the trap arms, so it never deletes the real owner's lock.
LOCK="$SIGNAL_DIR/.daemon.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    holder="$(cat "$LOCK/pid" 2>/dev/null)"
    # kill -0 alone isn't enough: after a reboot the lock dir survives on disk and the
    # holder pid can be recycled by an unrelated process. Require the name to match too.
    if [ -n "${holder:-}" ] && ps -p "$holder" -o command= 2>/dev/null | grep -q compacto-resume-daemon; then
        echo "compacto-resume-daemon: already running (pid $holder); this instance is exiting." >&2
        exit 0
    fi
    rm -rf "$LOCK"                                   # stale (holder dead) — reclaim it
    mkdir "$LOCK" 2>/dev/null || { echo "compacto-resume-daemon: cannot acquire lock $LOCK" >&2; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"; echo "compacto-resume-daemon: stopped."; exit 0' INT TERM

conf_get() { [ -f "$CONF" ] && grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
is_num()   { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
pane_alive() { $TMUX_CMD list-panes -a -F '#{pane_id}' 2>/dev/null | grep -Fxq "$1"; }
pane_idle() {
    # In tmux copy-mode the pane is scrolled up, so capture-pane returns the scrolled
    # viewport, not the live prompt — the busy/queued markers below sit off-screen and
    # the pane reads as falsely idle. Worse, keys sent to a copy-mode pane are eaten by
    # copy-mode (/ starts a search), so /compact and /resume never land. Treat any pane
    # the user is scrolling as not-ready: don't type into it, and retry once they exit.
    [ "$($TMUX_CMD display-message -p -t "$1" '#{pane_in_mode}' 2>/dev/null)" = 1 ] && return 1
    local cap; cap="$($TMUX_CMD capture-pane -p -t "$1" 2>/dev/null | tail -25)"
    # Not ready if generating ($BUSY_REGEX) OR a command is already queued. The queued
    # check stops the 300s cooldown from stacking /compact behind a pane that's busy but
    # not "generating" — e.g. a long-running background agent.
    ! grep -qiE "$BUSY_REGEX" <<<"$cap" && ! grep -qiE 'queued message' <<<"$cap"
}
pane_pending_compact() {
    # A /compact we sent can freeze in the pane's input queue: the agent goes idle
    # WITHOUT draining it, the "queued message" caption disappears, and the pane passes
    # pane_idle — yet the /compact is still pending and will execute whenever the queue
    # drains. To the marker/timer logic that state is indistinguishable from "the
    # /compact was lost", which is how the 300s release re-fired and produced a proven
    # double-compact (two previews of one session, 45s apart). But the frozen queue IS
    # visible: each pending command renders as its own "❯ /compact" line. Whole-line
    # match so prose merely mentioning /compact can't trip it; a false positive only
    # delays a retry (wedge alert still fires), never stacks a second compact.
    $TMUX_CMD capture-pane -p -t "$1" 2>/dev/null | grep -qE '^\s*❯?\s*/compact\s*$'
}
# Durable event ledger. tmux scrollback truncates within hours under DEBUG (which is how
# two investigations lost the fire history), so every consequential action also appends
# here. Reconcile against the executions ledger compactions.log written by precompact.py:
# a "fire" with no matching execution line = the /compact was eaten or is still queued.
evlog() { echo "$(date '+%F %T') $*" >> "$SIGNAL_DIR/events.log" 2>/dev/null || true; }
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
                rm -f "$cf" "$SIGNAL_DIR/$key.compacting" "$SIGNAL_DIR/$key.await-continue" "$SIGNAL_DIR/$key.lastauto" "$SIGNAL_DIR/$key.belowsince" "$SIGNAL_DIR/$key.resumewant" "$SIGNAL_DIR/$key.failcount"
                continue
            fi
            # A fork from the last /compact is still waiting to be resumed. Do NOT fire a
            # second /compact on top of it — that stacks compactions and buries the resume
            # behind them. .compacting (set below) holds until the size is confirmed durably
            # down, so a pane whose resume didn't land can't be re-compacted for a full
            # COOLDOWN rather than the moment the floor lapses.
            if [ -e "$SIGNAL_DIR/$key.resume" ] || [ -e "$SIGNAL_DIR/$key.resume.busy" ] || [ -e "$SIGNAL_DIR/$key.resumewant" ]; then
                continue                            # a resume is pending/being confirmed — don't compact over it
            fi
            case "$METRIC" in ctx) val="${cctx:-0}";; *) val="${cmsg:-0}";; esac
            is_num "$val" || continue

            cm="$SIGNAL_DIR/$key.compacting"        # debounce: one compaction in flight per pane
            bs="$SIGNAL_DIR/$key.belowsince"
            # Re-arm .compacting only after the pane has been CONFIRMED below the threshold
            # for REARM_GRACE seconds — i.e. a fork really loaded and the size really dropped.
            # Clearing it sooner is exactly what re-fired the second /compact:
            #   - clearing when /resume is SENT is optimistic — the resume can fail to switch
            #     a busy autonomous pane, so the big session stays and re-compacts ~a minute
            #     later (both compacts land on the same session id — the proven signature);
            #   - clearing on a single sub-threshold reading trips on the resume-transition
            #     .ctx flicker.
            # Sustained-below defeats both: a failed resume keeps size high so .compacting
            # holds (until COOLDOWN), and a 1-2s flicker never reaches the grace window.
            if [ "$val" -lt "$THRESH" ]; then
                if [ -e "$cm" ]; then               # only meaningful while a compaction is pending
                    [ -e "$bs" ] || : > "$bs"
                    # Confirmed recovered: the fork loaded and the size really dropped. Clear
                    # the debounce AND the wedge fail-counter — this pane is healthy again.
                    [ "$(file_age "$bs")" -ge "$REARM_GRACE" ] && rm -f "$cm" "$bs" "$SIGNAL_DIR/$key.failcount"
                fi
                continue
            fi
            rm -f "$bs"                              # back above threshold: reset the sustained-below timer
            la="$SIGNAL_DIR/$key.lastauto"          # hard floor between compacts on this pane
            if [ -e "$la" ] && [ "$(file_age "$la")" -lt "$MIN_INTERVAL" ]; then
                continue
            fi
            fc="$SIGNAL_DIR/$key.failcount"
            if [ -e "$cm" ]; then
                # Safety release is ONLY for a genuinely FAILED compaction — the /compact
                # vanished and no fork ever came. We can claim "failed" only when the pane is
                # IDLE and still oversized after COOLDOWN. If the pane is BUSY, a /compact we
                # already sent is almost certainly QUEUED behind the agent's turn and still
                # pending — NOT failed. Releasing on the timer alone fires a SECOND /compact
                # that also queues, and the two drain back-to-back as a double-compact (proven:
                # two forks, same parent, ~55s apart). So gate the release on pane_idle too:
                # hold the debounce until the queued compaction actually runs (→ fork → resume
                # clears .compacting) or the pane genuinely idles with nothing having happened.
                n="$(cat "$fc" 2>/dev/null)"; is_num "$n" || n=0
                # A wedged pane keeps failing this way (usually orphaned queued input jamming
                # its input so /compact can't run). Past MAX_FAILS, stop probing every COOLDOWN
                # — stretch to WEDGE_BACKOFF so we're not shoving /compact into a stuck pane.
                gate="$COOLDOWN"; [ "$n" -ge "$MAX_FAILS" ] && gate="$WEDGE_BACKOFF"
                if [ "$(file_age "$cm")" -gt "$gate" ] && pane_idle "$cpane" && ! pane_pending_compact "$cpane"; then
                    rm -f "$cm"
                    n=$((n + 1)); echo "$n" > "$fc"
                    if [ "$n" -eq "$MAX_FAILS" ]; then
                        # ${n} must stay braced: bash pulls the multibyte × into a bare $n's
                        # name, and under set -u that unset "n×" variable killed the daemon.
                        echo "compacto-resume-daemon: pane $cpane WEDGED — /compact fired ${n}× but never executed (likely stuck queued input); backing off to ${WEDGE_BACKOFF}s. Clear the pane to recover." >&2
                        evlog "WEDGED $cpane fails=$n backoff=${WEDGE_BACKOFF}s"
                    else
                        [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: release .compacting $cpane — idle+oversized ${gate}s, no fork (genuine fail #$n; will retry)" >&2
                        evlog "release $cpane fail#$n age>$gate"
                    fi
                else
                    [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: suppress $cpane val=$val — compaction in flight/queued ($(file_age "$cm")s, fails=$n)" >&2
                    continue
                fi
            fi
            if ! pane_idle "$cpane"; then           # never interrupt an in-progress turn
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $cpane val=$val — pane busy (would queue behind the turn)" >&2
                continue
            fi
            # Belt-and-braces independent of marker state: if a /compact is already visibly
            # queued in this pane, another one can only stack behind it.
            if pane_pending_compact "$cpane"; then
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $cpane val=$val — a /compact is already queued in the pane" >&2
                continue
            fi

            if [ -n "$DEBUG" ]; then
                echo "compacto-resume-daemon[dbg]: fire $cpane val=$val in_mode=$($TMUX_CMD display-message -p -t "$cpane" '#{pane_in_mode}' 2>/dev/null) tail=[$($TMUX_CMD capture-pane -p -t "$cpane" 2>/dev/null | grep -vE '^\s*$' | tail -1 | cut -c1-60)]" >&2
            fi
            $TMUX_CMD send-keys -t "$cpane" "/compact" Enter
            evlog "fire $cpane $METRIC=$val"
            : > "$cm"; : > "$la"
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
        if [ -z "${pane:-}" ] || [ -z "${fork:-}" ]; then
            rm -f "$busy"
            echo "compacto-resume-daemon: malformed signal skipped (pane='${pane:-}' fork='${fork:-}')" >&2
            continue
        fi
        key="${pane//[^a-zA-Z0-9]/}"
        # .compacting is NOT cleared in this loop at all — only behavior 2 clears it, and
        # only after the pane's size is confirmed durably below the threshold. That way a
        # /resume that doesn't actually land can't re-arm a second /compact.

        if ! pane_alive "$pane"; then
            rm -f "$busy" "$SIGNAL_DIR/$key.await-continue" "$SIGNAL_DIR/$key.lastauto"
            echo "compacto-resume-daemon: pane $pane gone; dropped resume $fork" >&2
            continue
        fi
        # The hook drops this signal at the very end of compaction, while the pane
        # still shows "esc to interrupt". Typing /resume then lands the keystrokes in
        # a busy TUI and they're lost — the resume never comes. Put the signal back
        # and retry on the next poll until the pane is back at a ready prompt. Same
        # guard behavior 2 uses before /compact, so we also never yank the session
        # out from under a turn the user started.
        if ! pane_idle "$pane"; then
            mv "$busy" "$f" 2>/dev/null || rm -f "$busy"
            continue
        fi

        sleep "$SETTLE"                              # absorb the render frame before typing
        # Re-check AFTER settling — this is the fix for the double-compact "spam". An
        # autonomous agent (an out-of-focus pane doing a job) can start a new turn during
        # SETTLE. /resume typed into a now-busy pane is queued behind that turn or dropped,
        # so the pane stays on the big pre-compact session; behavior 2 then re-compacts it
        # ~a minute later, and the first fork is orphaned. If the pane went busy, restore
        # the signal and retry on a later idle frame instead of firing a resume that's lost.
        if ! pane_alive "$pane"; then
            rm -f "$busy" "$SIGNAL_DIR/$key.await-continue" "$SIGNAL_DIR/$key.lastauto"
            continue
        fi
        if ! pane_idle "$pane"; then
            mv "$busy" "$f" 2>/dev/null || rm -f "$busy"
            continue
        fi
        rm -f "$busy"
        $TMUX_CMD send-keys -t "$pane" "/resume $fork" Enter
        echo "compacto-resume-daemon: resumed $fork in pane $pane"
        evlog "resume-sent $pane $fork"
        # Deliberately do NOT clear .compacting here. Sending /resume is not proof it landed
        # — on a busy autonomous pane the keystroke can be queued or dropped and the pane
        # stays on the big session. Behavior 2 re-arms only once the size is CONFIRMED down
        # for REARM_GRACE. Drop the stale pre-compaction .ctx and stamp the floor so a last
        # high render from the old session can't re-trigger before the fork reports its size.
        rm -f "$SIGNAL_DIR/$key.ctx"; : > "$SIGNAL_DIR/$key.lastauto"
        # Arm confirmation/retry (behavior 1b) so a resume the agent ate gets re-sent until
        # it actually lands. Only when threshold-compact is active — that's the sole path
        # that can tell "landed" from "not" by watching the size fall back under THRESH.
        if is_num "$THRESH" && [ "$THRESH" -gt 0 ]; then
            printf '%s\t%s\t%s\n' "$pane" "$fork" "$(date +%s)" > "$SIGNAL_DIR/$key.resumewant"
        fi
    done

    # ---- behavior 1b: confirm the resume landed, else retry (+3 continue) --
    # The proven failure: on a self-continuing agent, /resume is eaten by the next turn and
    # the pane stays on the big session, which then re-compacts. Here we watch the size and
    # re-send /resume (paced, idle-only) until it actually drops, then run the optional
    # continue. Give up after RESUME_GIVEUP so a wedged pane falls back to behavior 2.
    for w in "$SIGNAL_DIR"/*.resumewant; do
        [ -e "$w" ] || continue
        IFS=$'\t' read -r rpane rfork rsince < "$w"
        [ -n "${rpane:-}" ] && [ -n "${rfork:-}" ] || { rm -f "$w"; continue; }
        rkey="${rpane//[^a-zA-Z0-9]/}"
        if ! is_num "$THRESH" || [ "$THRESH" -le 0 ]; then rm -f "$w"; continue; fi   # can't confirm without a threshold
        if ! pane_alive "$rpane"; then rm -f "$w" "$SIGNAL_DIR/$rkey.await-continue"; continue; fi

        # Confirmed when the fork's own render reports a size back under the threshold.
        rcf="$SIGNAL_DIR/$rkey.ctx"
        if [ -e "$rcf" ]; then
            IFS=$'\t' read -r _ rctx rmsg < "$rcf"
            case "$METRIC" in ctx) rval="${rctx:-0}";; *) rval="${rmsg:-0}";; esac
            if is_num "$rval" && [ "$rval" -lt "$THRESH" ]; then
                rm -f "$w"
                echo "compacto-resume-daemon: resume confirmed for $rpane ($METRIC=$rval)"
                evlog "resume-confirmed $rpane $METRIC=$rval"
                ac="$SIGNAL_DIR/$rkey.await-continue"
                if [ -e "$ac" ] && [ -n "$CONTINUE_MSG" ] && pane_idle "$rpane"; then
                    $TMUX_CMD send-keys -t "$rpane" "$CONTINUE_MSG" Enter
                    echo "compacto-resume-daemon: continued $rpane with: $CONTINUE_MSG"
                    rm -f "$ac"
                fi
                continue
            fi
        fi

        now="$(date +%s)"; is_num "${rsince:-}" || rsince="$now"
        if [ "$(( now - rsince ))" -ge "$RESUME_GIVEUP" ]; then
            rm -f "$w" "$SIGNAL_DIR/$rkey.await-continue"
            echo "compacto-resume-daemon: resume gave up for $rpane after $(( now - rsince ))s ($rfork)" >&2
            evlog "resume-giveup $rpane $rfork after=$(( now - rsince ))s"
            continue
        fi
        # Re-send, paced by the marker's mtime, only into an idle frame.
        if [ "$(file_age "$w")" -ge "$RESUME_RETRY_SECS" ] && pane_idle "$rpane"; then
            $TMUX_CMD send-keys -t "$rpane" "/resume $rfork" Enter
            touch "$w"
            echo "compacto-resume-daemon: resume retry for $rpane ($rfork)"
            evlog "resume-retry $rpane $rfork"
        fi
    done

    sleep "$POLL"
done
