#!/usr/bin/env bash
# compacto-resume-daemon.sh — auto-resume watcher for the context-compacto plugin,
# with optional threshold auto-compact and post-resume continue.
#
# FOUR behaviors. #1 and #4 always run; #2 and #3 are OFF by default and configured in
# ~/.claude/precompact.conf (read fresh every poll, so /cc: changes apply live):
#
#   1. RESUME (always): when the precompact hook forks a session it drops
#      ~/.claude/compacto-signals/<panekey>.resume = "<pane-id>\t<fork-id>".
#      This types `/resume <fork-id>` into that exact pane — the keystroke you'd
#      type by hand. Keyed on $TMUX_PANE, so N parallel sessions never cross-wire.
#      A fork written before the pane's last turn ended would lose that turn, so where
#      behavior 2 can compact the pane afresh it does that instead of the /resume; in a
#      "=" window that starts with a fresh handoff.
#
#   2. THRESHOLD AUTO-COMPACT (auto_compact_at=<tokens>, default 300000, 0 = off): the statusline export
#      (see README) drops <panekey>.ctx = "<pane-id>\t<ctx>\t<msgs>" every render.
#      When a pane's live size crosses the threshold AND the pane is idle, this
#      types `/compact`. Metric: auto_compact_metric=msgs|ctx (default msgs).
#      A "!" anywhere in the tmux window name opts that window out; it's re-read
#      every poll, so renaming the window back re-enables auto-compact. Resume (#1)
#      still runs there, so a /compact you type yourself still gets its /resume.
#      Claude Code's own auto compaction runs mid-turn, where a fork would miss the rest
#      of the turn. In a pane this watches, the hook refuses it and drops
#      <panekey>.compactwant instead, and this compacts the pane at its next idle prompt
#      whatever its size. The hook only does that while .daemon.lock/heartbeat, rewritten
#      every poll, is fresh.
#      A "=" instead (and no "!") makes the window hand off first: at min(600000, 2 x the
#      threshold) this types /compact-handoff (handoff_command=<name>), waits until
#      hooks/handoff.py records that the handoff's own turn ended with Stop, and only then
#      types /compact. A handoff that never starts, is cut short or fails is retried on the
#      /compact back-off below, and never followed by a /compact. One that another turn ended
#      after (a background result, say) no longer covers the session, so it hands off again.
#
#   3. POST-RESUME CONTINUE (resume_continue=<message>): after an auto-triggered
#      compaction resumes, types <message> (e.g. "continue") so the task keeps
#      going. UNBOUNDED — runs until you stop this daemon. Only fires for compacts
#      THIS daemon triggered (behavior 2), never for a manual /compact.
#
#   4. WINDOW MARKERS (always; not on psmux): sets the tmux window option @cc_state on windows running
#      Claude: "?" a question/permission/trust dialog waits for you, "$" Claude finished
#      while you weren't viewing the window, "&" idle but background agents or shells are
#      still running. Invisible unless window-status-format prints #{@cc_state} (see README).
#
# Run ONE of these (serves every pane on the tmux server). Ctrl-C to stop —
# stopping it is the off-switch for the whole autonomous loop.
#
# Config (env):
#   COMPACTO_SIGNAL_DIR      signal dir            (default ~/.claude/compacto-signals)
#   COMPACTO_POLL_SECS       poll interval         (default 1)
#   COMPACTO_TMUX            tmux command           (default "tmux"; e.g. "tmux -L sock")
#   COMPACTO_BUSY_REGEX      "pane is generating" marker (default "esc to interrupt")
#   COMPACTO_CLAUDE_CMD_REGEX  pane_current_command of a Claude pane, for markers
#                            (default "^([0-9]+\.[0-9]+\.[0-9]+|claude|node)$")
#   COMPACTO_COMPACT_COOLDOWN secs before a stuck .compacting marker clears (default 300)
#   COMPACTO_CONTINUE_SETTLE  secs to let a resume render before typing continue (default 3)
#   COMPACTO_REARM_GRACE      secs a pane must stay below threshold before re-arming (default 15)
#   COMPACTO_RESUME_RETRY_SECS  secs between /resume re-sends until it lands (default 8)
#   COMPACTO_RESUME_GIVEUP    secs to keep retrying /resume before giving up (default 180)
#   COMPACTO_MAX_COMPACT_FAILS  genuine /compact failures before a pane is treated as wedged (default 3)
#   COMPACTO_WEDGE_BACKOFF    retry interval (secs) once a pane is wedged (default 1800)
#   COMPACTO_HANDOFF_MAX      cap on a "=" window's threshold (default 600000)
#   COMPACTO_HANDOFF_START_TIMEOUT  secs a typed handoff may take to start before it counts as failed (default 60)
#
# Assumes a single tmux server; pane ids are unique per server. psmux (Windows) runs a server per
# session instead, so there panes are addressed through their session (see pane_target).
set -u

# Git Bash rewrites arguments that look like POSIX paths when it starts a Windows program, so
# send-keys "/compact" would type "C:/Program Files/Git/compact". No effect outside MSYS.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SIGNAL_DIR="${COMPACTO_SIGNAL_DIR:-$HOME/.claude/compacto-signals}"
POLL="${COMPACTO_POLL_SECS:-1}"
TMUX_CMD="${COMPACTO_TMUX:-tmux}"
BUSY_REGEX="${COMPACTO_BUSY_REGEX:-esc to interrupt}"
# The native installer runs Claude as a binary named after its version (2.1.281); npm runs it as node.
CLAUDE_CMD_REGEX="${COMPACTO_CLAUDE_CMD_REGEX:-^([0-9]+\.[0-9]+\.[0-9]+|claude|node)\$}"
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
HANDOFF_MAX="${COMPACTO_HANDOFF_MAX:-600000}"
HANDOFF_START_TIMEOUT="${COMPACTO_HANDOFF_START_TIMEOUT:-60}"
# A handoff you typed yourself in a "=" window counts if it finished this recently, so the
# daemon types /compact instead of a second handoff.
HANDOFF_ADOPT=120
DEBUG="${COMPACTO_DEBUG:-}"                      # set to 1 to log pane state at each auto-compact fire
CONF="$HOME/.claude/precompact.conf"

command -v ${TMUX_CMD%% *} >/dev/null 2>&1 || { echo "compacto-resume-daemon: '${TMUX_CMD%% *}' not found on PATH" >&2; exit 1; }
ON_PSMUX=0; $TMUX_CMD -V 2>/dev/null | grep -qi psmux && ON_PSMUX=1
mkdir -p "$SIGNAL_DIR"
# Command line of pid $1: /proc where it exists (Linux; Git Bash, whose ps has no -o), else ps (macOS).
proc_cmdline() { if [ -r "/proc/$1/cmdline" ]; then tr '\0' ' ' < "/proc/$1/cmdline"; else ps -p "$1" -o command= 2>/dev/null; fi; }

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
    if [ -n "${holder:-}" ] && proc_cmdline "$holder" | grep -q compacto-resume-daemon; then
        echo "compacto-resume-daemon: already running (pid $holder); this instance is exiting." >&2
        exit 0
    fi
    rm -rf "$LOCK"                                   # stale (holder dead) — reclaim it
    mkdir "$LOCK" 2>/dev/null || { echo "compacto-resume-daemon: cannot acquire lock $LOCK" >&2; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'clear_markers; rm -rf "$LOCK"; echo "compacto-resume-daemon: stopped."; exit 0' INT TERM

conf_get() { [ -f "$CONF" ] && grep -E "^$1=" "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
is_num()   { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
pane_alive() { $TMUX_CMD list-panes -a -F '#{pane_id}' 2>/dev/null | grep -Fxq "$1"; }
# -t target for pane $1, or status 1 if it's gone, 2 if ambiguous. psmux runs a server per session with
# its own %N numbering, and a command for a closed session's %N lands in another session's pane, so there
# a pane is "=<session>:.%N" (which fails instead), picked by the server pid $2 its writer recorded.
pane_target() {
    if [ "$ON_PSMUX" != 1 ]; then pane_alive "$1" && printf '%s' "$1"; return; fi
    $TMUX_CMD list-panes -a -F '#{pid} #{pane_id} #{session_name}' 2>/dev/null | awk -v id="$1" -v pid="${2:-}" '
        $2 == id && (pid == "" || $1 == pid) { n++; s = $0; sub(/^[^ ]+ [^ ]+ /, "", s) }
        END { if (n == 1) printf "=%s:.%s", s, id; exit (n == 1 ? 0 : (n ? 2 : 1)) }'
}
# Classifies a Claude screen (capture-pane output on stdin) for both the typing gate and the
# window markers: busy, ready (the plain input box), background (that box while background
# agents or shells still run), dialog (a question/permission/trust dialog) or unknown.
IFS= read -r -d '' SCREEN_AWK <<'AWK'
function classify(    first, i, s, busy, box, footer, bg) {
    # Until the screen fills, Claude's input box sits right under the content with blank rows
    # below it, so read the 25 lines ending at the last non-blank one, not the bottom 25 rows.
    first = last > 25 ? last - 24 : 1
    for (i = first; i <= last; i++) {
        s = line[i]
        # Busy if generating (BUSY_REGEX) OR a command is already queued. The queued
        # check stops the 300s cooldown from stacking /compact behind a pane that's busy but
        # not "generating" — e.g. a long-running background agent.
        if (tolower(s) ~ tolower(ENVIRON["BUSY_REGEX"]) || tolower(s) ~ /queued message/) busy = 1
        # Bypass-permissions mode never shows "esc to interrupt"; the spinner line
        # "✳ Scurrying… (3m 53s · ↓ 24.5k tokens)" is its only busy marker. Column 0 and not a ⏺
        # message line (● on Windows), so prose quoting it doesn't count; a finished turn reads "✻ Baked for 5m 16s".
        if (s ~ /^[^ ]+ [^()]+… \([0-9]+[hms]/ && s !~ /^(⏺|●)/) busy = 1
        # Type only into the plain input box, whose "❯" line sits directly under a "───" rule. A
        # question, permission or trust dialog replaces the box and puts its "❯ 1." cursor under
        # the question text, where the Enter we send would pick option 1 for the user. A question's
        # last option, "❯ 4. Chat about this", does sit under a rule, so a numbered cursor is never
        # the box. The box puts a no-break space after ❯, so a draft "1. ..." is still the box.
        if (s ~ /^❯/) box = (i > first && line[i - 1] ~ /^───/ && s !~ /^❯ [0-9]+\. /) ? i : 0
    }
    if (busy) return "busy"
    if (!box) {
        # A session named with /rename keeps its "─── name ─" input border under a question
        # dialog, below the "Esc to cancel" footer.
        i = last
        while (i > first && line[i] ~ /^─/) i--
        return (line[i] ~ /Esc to cancel/) ? "dialog" : "unknown"
    }
    # Only a finished turn's status line means idle: "✻ Sautéed for 5m 51s · done 1:11" or
    # "✻ Waiting for 6 background agents to finish". A turn in progress is worded many ways, e.g.
    # "(Checking UI changes… · 30m 43s" or "Waiting for API response · will retry in 2m 40s".
    # Once background agents that outlasted turns finish, the next finished line times the whole stretch
    # from the first turn that left them running, so it can read "for 1d 1h 7m".
    for (i = box - 1; i >= 1; i--) if (line[i] ~ /^(·|✢|✳|✶|✻|✽|\*) /) break
    if (i >= 1 && line[i] !~ /^[^ ]+ [^ ]+ for [0-9]+[dhms]/ && line[i] !~ /^[^ ]+ Waiting for [0-9]+ background agents? to finish/) return "busy"
    # Claude wakes itself when background work finishes, so the box is not "done" while a
    # "✻ Waiting for 6 background agents to finish" or "… · 1 shell still running" line sits
    # above it or the footer under it shows "· 1 shell ·". Messages and prompts can quote those.
    for (i = first; i <= last; i++) {
        s = line[i]
        if (i > box && s ~ /^───/) footer = 1
        if (s ~ /^(⏺|●|❯)/) continue
        if (s ~ /^[^ ]+ Waiting for [0-9]+ background agents? to finish/ || s ~ /^[^ ]+ .*[0-9]+ shells? still running/) bg = 1
        if (footer && s ~ /· [0-9]+ shells?( |$)/) bg = 1
    }
    return bg ? "background" : "ready"
}
# One run can classify several panes: a "@@compacto-pane N" line starts the screen of pane %N,
# and each screen prints as "N state". A lone screen with no such line prints just its state.
/^@@compacto-pane [0-9]+$/ { if (id != "") print id, classify(); id = $2; n = last = 0; next }
{ line[++n] = $0; if (NF) last = n }
END { print (id != "" ? id " " : "") classify() }
AWK
screen_state() { BUSY_REGEX="$BUSY_REGEX" awk "$SCREEN_AWK"; }
pane_idle() {
    # In tmux copy-mode the pane is scrolled up, so capture-pane returns the scrolled
    # viewport, not the live prompt — the busy/queued markers below sit off-screen and
    # the pane reads as falsely idle. Worse, keys sent to a copy-mode pane are eaten by
    # copy-mode (/ starts a search), so /compact and /resume never land. Treat any pane
    # the user is scrolling as not-ready: don't type into it, and retry once they exit.
    [ "$($TMUX_CMD display-message -p -t "$1" '#{pane_in_mode}' 2>/dev/null)" = 1 ] && return 1
    # Waiting on background work counts as idle: a dev server in the background never
    # finishes, so holding /compact for it could stall a pane forever.
    case "$($TMUX_CMD capture-pane -p -t "$1" 2>/dev/null | screen_state)" in ready|background) return 0 ;; esac
    return 1
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
# The text in the pane's input box (its "❯" line plus wrapped lines), or status 1 when no box is
# on screen. Same box test as classify. Read as bytes in the C locale: the box starts with "❯"
# and a no-break space, and awks disagree on Unicode outside it.
IFS= read -r -d '' BOX_AWK <<'AWK'
BEGIN { nbsp = sprintf("%c%c", 194, 160) }
{ line[++n] = $0 }
END {
    for (i = 2; i <= n; i++) if (line[i] ~ /^❯/) box = (line[i - 1] ~ /^───/ && line[i] !~ /^❯ [0-9]+\. /) ? i : 0
    if (!box) exit 1
    text = substr(line[box], 4)
    if (index(text, nbsp) == 1) text = substr(text, 3)
    for (i = box + 1; i <= n && line[i] !~ /^───/; i++) text = text "\n" line[i]
    sub(/[ \n]+$/, "", text)
    printf "%s", text
}
AWK
pane_box_text()  { $TMUX_CMD capture-pane -p -t "$1" 2>/dev/null | LC_ALL=C awk "$BOX_AWK"; }
pane_box_empty() { local t; t="$(pane_box_text "$1")" && [ -z "$t" ]; }
# Types $2 into pane $1 without Enter; status 0 once the box holds exactly that. Enter is left to
# the caller, so keys that landed in someone's draft are never submitted, and the autocomplete menu
# has settled on the typed command. On a mismatch the typed text is erased if it ends the box.
type_text() {
    local text i; local -a erase=()
    $TMUX_CMD send-keys -t "$1" "$2"
    sleep 1
    text="$(pane_box_text "$1")" || return 1
    [ "$text" = "$2" ] && return 0
    if [[ "$text" == *"$2" ]]; then
        for ((i = 0; i < ${#2}; i++)); do erase+=(BSpace); done
        $TMUX_CMD send-keys -t "$1" "${erase[@]}"
    fi
    return 1
}
# "=" hands a window off before compacting; "!" opts it out altogether and wins.
handoff_window() { [[ "$1" == *'='* && "$1" != *'!'* ]]; }
# handoff.py lets a /compact run in a "=" window once the handoff of the pane's current session has finished.
# This daemon types one only while nothing has run since: the last Stop is the handoff's own (.hooked's
# second line; hooks from before that line can't tell), and no fork it led to was dropped as stale.
handoff_usable() {
    local st="" sess="" prompt="" cur="" last="" spent=""
    [ -e "$SIGNAL_DIR/$1.handoff" ] && IFS=$'\t' read -r st sess prompt _ < "$SIGNAL_DIR/$1.handoff"
    [ -e "$SIGNAL_DIR/$1.hooked" ] && { IFS= read -r cur; IFS= read -r last; } < "$SIGNAL_DIR/$1.hooked"
    [ -e "$SIGNAL_DIR/$1.handoffspent" ] && IFS= read -r spent < "$SIGNAL_DIR/$1.handoffspent"
    cur="${cur%$'\r'}"; last="${last%$'\r'}"; spent="${spent%$'\r'}"
    [ "$st" = done ] && [ -n "$sess" ] && [ "$sess" = "$cur" ] || return 1
    { [ -z "$last" ] || [ "$last" = "$prompt" ]; } && [ "$spent" != "$prompt" ]
}
# Durable event ledger. tmux scrollback truncates within hours under DEBUG (which is how
# two investigations lost the fire history), so every consequential action also appends
# here. Reconcile against the executions ledger compactions.log written by precompact.py:
# a "fire" with no matching execution line = the /compact was eaten or is still queued.
evlog() { echo "$(date '+%F %T') $*" >> "$SIGNAL_DIR/events.log" 2>/dev/null || true; }
# GNU stat first: its -f means "file system" and prints a status block before failing on "%m",
# and that block would land in $m.
file_mtime() { local m; m=$(stat -c %Y "$1" 2>/dev/null) || m=$(stat -f %m "$1" 2>/dev/null) || m=0; echo "$m"; }
file_age()   { echo $(( $(date +%s) - $(file_mtime "$1") )); }

# Behavior 4 keeps per-pane memory indexed by pane number (%12 -> 12). It starts empty, so
# after a daemon restart, panes that are already idle count as seen.
PANE_STATE=(); PANE_UNSEEN=(); PANE_READ_AT=(); PANE_CONTINUED=(); PANE_QUIET=(); HANDOFF_UNHOOKED=(); HANDOFF_STALE=()
# "=" panes handing off afresh after a stale fork was dropped. The compaction that made the fork still
# echoes "❯ /compact" on screen, which pane_pending_compact can't tell from a queued one.
HANDOFF_REDO=()
# The daemon's own handoff -> /compact -> /resume cycle is housekeeping, not a finished turn,
# until it types a continue that sets Claude working again.
compaction_in_flight() {
    local k="${1//[^a-zA-Z0-9]/}"
    [ "${PANE_CONTINUED[${1#%}]:-0}" = 1 ] && return 1
    [ -e "$SIGNAL_DIR/$k.compacting" ] || [ -e "$SIGNAL_DIR/$k.resume" ] || [ -e "$SIGNAL_DIR/$k.resume.busy" ] || [ -e "$SIGNAL_DIR/$k.resumewant" ] || [ -e "$SIGNAL_DIR/$k.handoffsent" ]
}
update_markers() {
    local panes now pane win inmode wactive attached activity shown cmd n w st prev r m wins="" claude="" batch=0
    local -a rank shown_now pane_win pane_viewed reads
    # "|", not a tab: read merges runs of tabs, so one empty field would shift the rest.
    panes="$($TMUX_CMD list-panes -a -F '#{pane_id}|#{window_id}|#{pane_in_mode}|#{window_active}|#{session_attached}|#{window_activity}|#{@cc_state}|#{pane_current_command}' 2>/dev/null)"
    [ -n "$panes" ] || return 0
    now="${EPOCHSECONDS:-$(date +%s)}"
    while IFS='|' read -r pane win inmode wactive attached activity shown cmd; do
        n="${pane#%}"; w="${win#@}"
        if [ -z "${rank[$w]:-}" ]; then rank[$w]=0; shown_now[$w]="$shown"; wins="$wins $w"; fi
        if ! [[ "$cmd" =~ $CLAUDE_CMD_REGEX ]]; then
            unset "PANE_STATE[$n]" "PANE_UNSEEN[$n]" "PANE_READ_AT[$n]" "PANE_QUIET[$n]"
            continue
        fi
        claude="$claude $n"; pane_win[$n]="$w"
        pane_viewed[$n]=0; [ "$wactive" = 1 ] && [ "${attached:-0}" -gt 0 ] && pane_viewed[$n]=1
        # Re-read only panes that printed something since the last read (>= so output in the
        # same second as that read still gets one more read). Keep the last state while scrolling.
        if [ "$inmode" != 1 ] && { [ -z "${PANE_STATE[$n]:-}" ] || [ "${activity:-0}" -ge "${PANE_READ_AT[$n]:-0}" ]; }; then
            [ "$batch" = 0 ] || reads+=(';')
            # Bare number, not %N: display-message runs its text through strftime, which eats "%1".
            reads+=(display-message -p "@@compacto-pane $n" ';' capture-pane -p -t "$pane"); batch=1
        fi
    done <<<"$panes"
    # One tmux call and one awk run read every pane that needs it. A pane closed since list-panes
    # aborts the rest of the chain; the panes after it keep their state and are read next poll.
    if [ "$batch" = 1 ]; then
        while read -r n st; do
            [ -n "$n" ] && [ -n "${pane_win[$n]:-}" ] || continue
            prev="${PANE_STATE[$n]:-}"; PANE_STATE[$n]="$st"; PANE_READ_AT[$n]="$now"
            # "In flight" as of the previous read: a turn that ended just before the daemon typed
            # /compact still finished; only the compaction's own busy stretch stays quiet.
            if [ "$st" = ready ]; then
                case "$prev" in busy|dialog|background)
                    [ "${pane_viewed[$n]}" = 1 ] || [ "${PANE_QUIET[$n]:-0}" = 1 ] || PANE_UNSEEN[$n]=1 ;;
                esac
            fi
            PANE_QUIET[$n]=0; compaction_in_flight "%$n" && PANE_QUIET[$n]=1
        done <<<"$($TMUX_CMD "${reads[@]}" 2>/dev/null | screen_state)"
    fi
    for n in $claude; do
        w="${pane_win[$n]}"
        [ "${pane_viewed[$n]}" = 1 ] && PANE_UNSEEN[$n]=0
        case "${PANE_STATE[$n]:-}" in
            dialog) r=3 ;;
            ready) [ "${PANE_UNSEEN[$n]:-0}" = 1 ] && r=2 || r=0 ;;
            background) r=1 ;;
            *) r=0 ;;
        esac
        [ "$r" -gt "${rank[$w]}" ] && rank[$w]=$r
    done
    for w in $wins; do
        case "${rank[$w]}" in 3) m='?' ;; 2) m='$' ;; 1) m='&' ;; *) m='' ;; esac
        [ "$m" = "${shown_now[$w]}" ] && continue
        if [ -n "$m" ]; then $TMUX_CMD set-option -w -t "@$w" @cc_state "$m"; else $TMUX_CMD set-option -wu -t "@$w" @cc_state; fi
        [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: window @$w marker '${shown_now[$w]}' -> '$m'" >&2
    done
    return 0
}
clear_markers() {
    [ "$ON_PSMUX" = 1 ] && return 0                 # markers are off there (see markers_wanted)
    local w m
    while read -r w m; do
        [ -n "$m" ] && $TMUX_CMD set-option -wu -t "$w" @cc_state
    done <<<"$($TMUX_CMD list-windows -a -F '#{window_id} #{@cc_state}' 2>/dev/null)"
}
# Markers show only through a tmux format that prints @cc_state, so skip their work until some
# option mentions it. Re-checked every 30s: adding the format lines turns markers on.
MARKERS_CHECKED_AT=0; MARKERS_WANTED=0
markers_wanted() {
    # psmux runs only the first command of a ";" chain and never updates window_activity, and the
    # pane read below depends on both.
    [ "$ON_PSMUX" = 1 ] && return 1
    local now="${EPOCHSECONDS:-$(date +%s)}"
    if [ $((now - MARKERS_CHECKED_AT)) -ge 30 ]; then
        MARKERS_CHECKED_AT=$now; MARKERS_WANTED=0
        $TMUX_CMD show-options -g \; show-options -gw 2>/dev/null | grep -q '@cc_state' && MARKERS_WANTED=1
    fi
    [ "$MARKERS_WANTED" = 1 ]
}

# Behavior 2 for a "=" pane at or over its threshold, called with the poll loop's pane variables
# set. hooks/handoff.py writes $key.handoff ("state session prompt started ended") and, at every
# Stop, $key.hooked; $hs marks a handoff this daemon typed.
handoff_step() {
    local st="" sess="" prompt="" started="" ended="" hooked="$SIGNAL_DIR/$key.hooked" n="${cpane#%}"
    if [ ! -e "$hooked" ]; then
        if [ -z "${HANDOFF_UNHOOKED[$n]:-}" ]; then
            HANDOFF_UNHOOKED[$n]=1
            echo "compacto-resume-daemon: pane $ctgt has '=' but no turn there has ended under the handoff hooks yet; it won't be compacted until one does (a Claude started before the hooks were installed needs a restart)" >&2
            evlog "handoff-unhooked $ctgt"
        fi
        return 0
    fi
    # After a handoff that never started, wait until a Stop shows this pane's Claude runs the hooks.
    if [ -n "${HANDOFF_STALE[$n]:-}" ]; then
        [ "$(file_mtime "$hooked")" -gt "${HANDOFF_STALE[$n]}" ] || return 0
        unset "HANDOFF_STALE[$n]"
    fi
    [ -e "$SIGNAL_DIR/$key.handoff" ] && IFS=$'\t' read -r st sess prompt started ended < "$SIGNAL_DIR/$key.handoff"
    is_num "${started:-}" || st=""
    if [ -e "$hs" ]; then
        if [ -z "$st" ] || [ "$started" -lt "$(file_mtime "$hs")" ]; then
            # Typed but not started: the keys were lost, the command is unknown, or this Claude
            # predates the hooks and ran it untracked. Its echo stays on screen either way, so only
            # idleness tells. One that was merely queued is adopted below once it finishes.
            if [ "$(file_age "$hs")" -ge "$HANDOFF_START_TIMEOUT" ] && pane_idle "$ctgt"; then
                HANDOFF_STALE[$n]="$(file_mtime "$hooked")"
                handoff_failed "never started (Claude there predates the handoff hooks, or handoff_command names no command)"
            fi
            return 0
        fi
        case "$st" in
            running) ;;
            done) if handoff_usable "$key"; then compact_after_handoff "$prompt"; else handoff_redo; fi ;;
            aborted) handoff_failed "was cut short" ;;
            *) handoff_failed "ended in an API error" ;;
        esac
        return 0
    fi
    if [ "$st" = done ] && is_num "${ended:-}" && [ $(( $(date +%s) - ended )) -le "$HANDOFF_ADOPT" ] \
        && handoff_usable "$key"; then
        compact_after_handoff "$prompt"
        return 0
    fi
    start_handoff
}
# The handoff this daemon typed no longer covers the session: a turn ran after it (a background result that
# came in meanwhile, say), so a /compact now would summarise what it never saved.
handoff_redo() {
    rm -f "$hs"
    evlog "handoff-redo $ctgt"
    echo "compacto-resume-daemon: a turn ran in $ctgt after its handoff; handing off again"
    start_handoff
}
start_handoff() {
    local n gate
    n="$(cat "$fc" 2>/dev/null)"; is_num "$n" || n=0
    gate="$MIN_INTERVAL"; [ "$n" -gt 0 ] && gate="$COOLDOWN"; [ "$n" -ge "$MAX_FAILS" ] && gate="$WEDGE_BACKOFF"
    if [ -e "$la" ] && [ "$(file_age "$la")" -lt "$gate" ]; then return 0; fi
    pane_idle "$ctgt" && pane_box_empty "$ctgt" \
        && { [ -n "${HANDOFF_REDO[${cpane#%}]:-}" ] || ! pane_pending_compact "$ctgt"; } || return 0
    if ! type_text "$ctgt" "/$HANDOFF_CMD"; then
        : > "$la"; evlog "handoff-not-typed $ctgt (input box changed while typing)"; return 0
    fi
    # Stamped before Enter: the handoff counts as ours only if it started at or after this mtime.
    : > "$hs"; : > "$la"; unset "PANE_CONTINUED[${cpane#%}]"
    $TMUX_CMD send-keys -t "$ctgt" Enter
    evlog "handoff-sent $ctgt $METRIC=$val"
    echo "compacto-resume-daemon: handoff $ctgt /$HANDOFF_CMD ($METRIC=$val >= $HTHRESH)"
}
# $1 = the finished handoff's prompt_id, re-checked after the settle.
compact_after_handoff() {
    local st="" prompt=""
    pane_idle "$ctgt" && pane_box_empty "$ctgt" \
        && { [ -n "${HANDOFF_REDO[${cpane#%}]:-}" ] || ! pane_pending_compact "$ctgt"; } || return 0
    # A Stop hook that blocks keeps the handoff's turn going after handoff.py marked it done, so
    # the pane must still be at rest a moment later.
    sleep "$SETTLE"
    pane_idle "$ctgt" && pane_box_empty "$ctgt" || return 0
    [ -e "$SIGNAL_DIR/$key.handoff" ] && IFS=$'\t' read -r st _ prompt _ < "$SIGNAL_DIR/$key.handoff"
    [ "$st" = done ] && [ "$prompt" = "$1" ] && handoff_usable "$key" || return 0
    type_text "$ctgt" "/compact" || { evlog "handoff-compact-not-typed $ctgt (input box changed while typing)"; return 0; }
    rm -f "$hs"; : > "$cm"; : > "$la"; unset "PANE_CONTINUED[${cpane#%}]" "HANDOFF_REDO[${cpane#%}]"
    $TMUX_CMD send-keys -t "$ctgt" Enter
    evlog "handoff-compact $ctgt $METRIC=$val"
    [ -n "$CONTINUE_MSG" ] && : > "$SIGNAL_DIR/$key.await-continue"
    rm -f "$cf"    # consume this reading; .compacting debounces until the fork resumes
    echo "compacto-resume-daemon: auto-compact $ctgt after its handoff ($METRIC=$val >= $HTHRESH)"
}
# A handoff that won't be followed by /compact. Counts toward the same wedge back-off as /compact.
handoff_failed() {
    local n
    rm -f "$hs"; : > "$la"
    n="$(cat "$fc" 2>/dev/null)"; is_num "$n" || n=0; n=$((n + 1)); echo "$n" > "$fc"
    evlog "handoff-failed $ctgt /$HANDOFF_CMD $1 (fail #$n)"
    if [ "$n" -eq "$MAX_FAILS" ]; then
        echo "compacto-resume-daemon: pane $ctgt WEDGED — /$HANDOFF_CMD failed ${n}× (last: $1); backing off to ${WEDGE_BACKOFF}s." >&2
        evlog "WEDGED $ctgt fails=$n backoff=${WEDGE_BACKOFF}s"
    else
        echo "compacto-resume-daemon: /$HANDOFF_CMD in $ctgt $1; next try in ${COOLDOWN}s" >&2
    fi
}
# A fork written before the pane's last turn ended (the hook forked mid-turn, or a prompt typed during the
# compaction ran first) would lose that turn on /resume. Where behavior 2 can compact the pane, drop the fork
# and leave it a compaction for the next idle prompt. In a "=" window that turn also came after the handoff
# that led to the fork, so that handoff stops counting and the pane hands off afresh at its threshold.
# Elsewhere the fork is all there is, so it's resumed.
# $1 pane key, $2 pane target, $3 fork, $4 the epoch the pane's last Stop has to be after.
drop_outlived_fork() {
    local c="" m="" v="" w="" st="" prompt="" ended="" how="compacting afresh instead of resuming it"
    is_num "$THRESH" && [ "$THRESH" -gt 0 ] && [ -e "$SIGNAL_DIR/$1.ctx" ] && [ -e "$SIGNAL_DIR/$1.hooked" ] \
        && [ "$(file_mtime "$SIGNAL_DIR/$1.hooked")" -gt "$4" ] || return 1
    IFS=$'\t' read -r _ c m _ < "$SIGNAL_DIR/$1.ctx"; c="${c%$'\r'}"; m="${m%$'\r'}"
    # Behavior 2 only compacts a pane whose $METRIC reads as a number, in a window without "!". A size of 0
    # hasn't been reported yet.
    case "$METRIC" in ctx) v="$c";; *) v="$m";; esac
    is_num "$c" && [ "$c" -gt 0 ] && is_num "$v" || return 1
    w="$($TMUX_CMD display-message -p -t "$2" '#{window_name}' 2>/dev/null)"
    [[ "$w" != *'!'* ]] || return 1
    if handoff_window "$w"; then
        # Only a handoff that ended before the fork led to it; one typed since is newer than that turn.
        [ -e "$SIGNAL_DIR/$1.handoff" ] && IFS=$'\t' read -r st _ prompt _ ended < "$SIGNAL_DIR/$1.handoff"
        ended="${ended%$'\r'}"
        [ "$st" = done ] && is_num "$ended" && [ "$ended" -le "$4" ] && echo "$prompt" > "$SIGNAL_DIR/$1.handoffspent"
        if [ "$v" -ge "$HTHRESH" ]; then
            HANDOFF_REDO[${1##*_}]=1; how="handing off afresh instead of resuming it"
        else
            how="dropped it, as the pane is under its threshold"
        fi
    else
        echo "$c" > "$SIGNAL_DIR/$1.compactwant"
    fi
    rm -f "$SIGNAL_DIR/$1.compacting" "$SIGNAL_DIR/$1.await-continue"
    evlog "resume-stale $2 $3"
    echo "compacto-resume-daemon: fork $3 predates the last turn in $2; $how"
}

echo "compacto-resume-daemon: watching $SIGNAL_DIR (poll ${POLL}s, tmux='$TMUX_CMD'). Ctrl-C to stop."
clear_markers                                       # left behind by a daemon that was SIGKILLed

while true; do
    { : > "$LOCK/heartbeat"; } 2>/dev/null   # precompact.py leaves compactions to this daemon only while it's fresh
    THRESH="$(conf_get auto_compact_at)"; [ -n "$THRESH" ] || THRESH=300000   # pcconf.py's AUTO_COMPACT_DEFAULT; off is a stored 0
    METRIC="$(conf_get auto_compact_metric)"; [ -n "$METRIC" ] || METRIC="msgs"
    CONTINUE_MSG="$(conf_get resume_continue)"
    # handoff.py reads the same key; both drop one leading slash.
    HANDOFF_CMD="$(conf_get handoff_command)"; HANDOFF_CMD="${HANDOFF_CMD//[[:space:]]/}"; HANDOFF_CMD="${HANDOFF_CMD#/}"
    [ -n "$HANDOFF_CMD" ] || HANDOFF_CMD="compact-handoff"

    # ---- behavior 2: threshold auto-compact -------------------------------
    if is_num "$THRESH" && [ "$THRESH" -gt 0 ]; then
        HTHRESH=$(( THRESH * 2 )); [ "$HTHRESH" -gt "$HANDOFF_MAX" ] && HTHRESH="$HANDOFF_MAX"
        LO_THRESH=$(( THRESH < HTHRESH ? THRESH : HTHRESH ))
        for cf in "$SIGNAL_DIR"/*.ctx; do
            [ -e "$cf" ] || continue
            # 4th field: the psmux server pid (see pane_target). Windows writers may end lines with CRLF.
            IFS=$'\t' read -r cpane cctx cmsg cserver < "$cf"
            cpane="${cpane%$'\r'}"; cctx="${cctx%$'\r'}"; cmsg="${cmsg%$'\r'}"; cserver="${cserver%$'\r'}"
            [ -n "${cpane:-}" ] || continue
            # From the line, not the file name, so it pairs with the hook's .resume however the
            # statusline named this file: <pane digits>, on psmux <server pid>_<pane digits>.
            key="${cserver:+${cserver}_}${cpane//[^a-zA-Z0-9]/}"
            ctgt="$(pane_target "${cpane}" "$cserver")"; rc=$?
            if [ "$rc" = 1 ]; then                  # session closed -> clean up its files
                rm -f "$cf" "$SIGNAL_DIR/$key.compacting" "$SIGNAL_DIR/$key.await-continue" "$SIGNAL_DIR/$key.lastauto" "$SIGNAL_DIR/$key.belowsince" "$SIGNAL_DIR/$key.resumewant" "$SIGNAL_DIR/$key.failcount" \
                      "$SIGNAL_DIR/$key.handoffsent" "$SIGNAL_DIR/$key.handoff" "$SIGNAL_DIR/$key.hooked" "$SIGNAL_DIR/$key.guard" "$SIGNAL_DIR/$key.compactwant" \
                      "$SIGNAL_DIR/$key.handoffspent"
                unset "HANDOFF_REDO[${cpane#%}]"
                continue
            fi
            [ "$rc" = 0 ] || continue               # same %N in several psmux sessions, no server pid to pick one
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
            hs="$SIGNAL_DIR/$key.handoffsent"
            # A compaction Claude Code asked for and the hook left here (or drop_outlived_fork did): run it at the
            # next idle prompt whatever the size, unless the session has shrunk since, compacted some other way
            # or swapped for another one.
            cw="$SIGNAL_DIR/$key.compactwant"; want=0; wsize=""
            if [ -e "$cw" ]; then
                IFS= read -r wsize < "$cw"; wsize="${wsize%$'\r'}"
                if is_num "$wsize" && is_num "${cctx:-}" && [ "$cctx" -ge "$wsize" ]; then want=1; else rm -f "$cw"; fi
            fi
            # The window name sets the threshold, so read it once the pane could be over either one.
            wname=""; handoff=0; pthresh="$THRESH"
            if [ "$val" -ge "$LO_THRESH" ] || [ "$want" = 1 ]; then
                wname="$($TMUX_CMD display-message -p -t "$ctgt" '#{window_name}' 2>/dev/null)"
                handoff_window "$wname" && { handoff=1; pthresh="$HTHRESH"; }
            fi
            # "=" and "!" windows compact by their own rules; a "=" one takes a /compact once its handoff is done.
            if [ "$want" = 1 ] && [ "$handoff" = 1 ] && handoff_usable "$key"; then handoff=0; fi
            if [ "$want" = 1 ] && { [ "$handoff" = 1 ] || [[ "$wname" == *'!'* ]]; }; then want=0; rm -f "$cw"; fi
            # A typed handoff is dropped once its window loses the "=" or its size falls (compacted
            # some other way), so a later crossing starts a fresh one.
            if [ -e "$hs" ] && ! { [ "$handoff" = 1 ] && [ "$val" -ge "$pthresh" ]; }; then rm -f "$hs"; fi
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
            if [ "$val" -lt "$pthresh" ]; then
                unset "HANDOFF_REDO[${cpane#%}]"
                # One Claude Code asked for can fire under the threshold, so .compacting holds the size it fired
                # at, and only a size under that one shows it ran.
                fired=""; [ -s "$cm" ] && IFS= read -r fired < "$cm"
                if is_num "$fired" && ! { is_num "${cctx:-}" && [ "$cctx" -lt "$fired" ]; }; then
                    rm -f "$bs"
                elif [ -e "$cm" ]; then             # only meaningful while a compaction is pending
                    [ -e "$bs" ] || : > "$bs"
                    # Confirmed recovered: the fork loaded and the size really dropped. Clear
                    # the debounce AND the wedge fail-counter — this pane is healthy again.
                    [ "$(file_age "$bs")" -ge "$REARM_GRACE" ] && rm -f "$cm" "$bs" "$SIGNAL_DIR/$key.failcount"
                fi
                [ "$want" = 1 ] || continue          # one Claude Code asked for runs whatever the size
            else
                rm -f "$bs"                          # back above threshold: reset the sustained-below timer
            fi
            la="$SIGNAL_DIR/$key.lastauto"          # hard floor between compacts on this pane
            # A "=" pane applies it in start_handoff instead: its /compact follows the handoff it typed.
            if [ "$handoff" = 0 ] && [ -e "$la" ] && [ "$(file_age "$la")" -lt "$MIN_INTERVAL" ]; then
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
                if [ "$(file_age "$cm")" -gt "$gate" ] && pane_idle "$ctgt" && ! pane_pending_compact "$ctgt"; then
                    rm -f "$cm"
                    n=$((n + 1)); echo "$n" > "$fc"
                    if [ "$n" -eq "$MAX_FAILS" ]; then
                        # ${n} must stay braced: bash pulls the multibyte × into a bare $n's
                        # name, and under set -u that unset "n×" variable killed the daemon.
                        echo "compacto-resume-daemon: pane $ctgt WEDGED — /compact fired ${n}× but never executed (likely stuck queued input); backing off to ${WEDGE_BACKOFF}s. Clear the pane to recover." >&2
                        evlog "WEDGED $ctgt fails=$n backoff=${WEDGE_BACKOFF}s"
                    else
                        [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: release .compacting $ctgt — idle+oversized ${gate}s, no fork (genuine fail #$n; will retry)" >&2
                        evlog "release $ctgt fail#$n age>$gate"
                    fi
                else
                    [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: suppress $ctgt val=$val — compaction in flight/queued ($(file_age "$cm")s, fails=$n)" >&2
                    continue
                fi
            fi
            if [[ "$wname" == *'!'* ]]; then
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $ctgt val=$val — '!' in window name (auto-compact opt-out)" >&2
                continue
            fi
            if [ "$handoff" = 1 ]; then
                handoff_step
                continue
            fi
            if ! pane_idle "$ctgt"; then           # never interrupt an in-progress turn
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $ctgt val=$val — pane busy or not at a plain prompt (working, queued input, or a dialog open)" >&2
                continue
            fi
            if ! pane_box_empty "$ctgt"; then        # /compact and Enter would submit the draft with it
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $ctgt val=$val — a draft in the input box" >&2
                continue
            fi
            # Belt-and-braces independent of marker state: if a /compact is already visibly
            # queued in this pane, another one can only stack behind it. Not for one Claude Code
            # asked for: it can follow the last compaction closely, whose own "❯ /compact" line is
            # then still on screen, and this can't tell that line from a queued one.
            if [ "$want" = 0 ] && pane_pending_compact "$ctgt"; then
                [ -n "$DEBUG" ] && echo "compacto-resume-daemon[dbg]: skip $ctgt val=$val — a /compact is already queued in the pane" >&2
                continue
            fi

            if [ -n "$DEBUG" ]; then
                echo "compacto-resume-daemon[dbg]: fire $ctgt val=$val in_mode=$($TMUX_CMD display-message -p -t "$ctgt" '#{pane_in_mode}' 2>/dev/null) tail=[$($TMUX_CMD capture-pane -p -t "$ctgt" 2>/dev/null | grep -vE '^\s*$' | tail -1 | cut -c1-60)]" >&2
            fi
            asked=""; [ "$want" = 1 ] && asked=", asked by Claude Code"
            $TMUX_CMD send-keys -t "$ctgt" "/compact" Enter
            evlog "fire $ctgt $METRIC=$val$asked"
            if [ "$want" = 1 ]; then echo "$cctx" > "$cm"; else : > "$cm"; fi
            : > "$la"; rm -f "$cw"; unset "PANE_CONTINUED[${cpane#%}]"
            [ -n "$CONTINUE_MSG" ] && : > "$SIGNAL_DIR/$key.await-continue"
            rm -f "$cf"    # consume this reading; .compacting debounces until the fork resumes
            echo "compacto-resume-daemon: auto-compact $ctgt ($METRIC=$val${asked:- >= $THRESH})"
        done
    fi

    # ---- behavior 1 (+3): resume, then optional continue ------------------
    for f in "$SIGNAL_DIR"/*.resume; do
        [ -e "$f" ] || continue
        busy="$f.busy"
        mv "$f" "$busy" 2>/dev/null || continue     # claim atomically
        # 3rd field: the psmux server pid (see pane_target). The hook on Windows may write CRLF.
        IFS=$'\t' read -r pane fork pserver < "$busy"
        fork="${fork%$'\r'}"; pserver="${pserver%$'\r'}"
        if [ -z "${pane:-}" ] || [ -z "${fork:-}" ]; then
            rm -f "$busy"
            echo "compacto-resume-daemon: malformed signal skipped (pane='${pane:-}' fork='${fork:-}')" >&2
            continue
        fi
        key="${pserver:+${pserver}_}${pane//[^a-zA-Z0-9]/}"
        # .compacting is NOT cleared in this loop at all — only behavior 2 clears it, and
        # only after the pane's size is confirmed durably below the threshold. That way a
        # /resume that doesn't actually land can't re-arm a second /compact.

        ptgt="$(pane_target "$pane" "$pserver")"; rc=$?
        if [ "$rc" = 1 ]; then
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
        if [ "$rc" != 0 ] || ! pane_idle "$ptgt"; then
            [ -e "$f" ] || mv "$busy" "$f" 2>/dev/null; rm -f "$busy"   # a fork the hook wrote meanwhile is newer and wins
            continue
        fi

        sleep "$SETTLE"                              # absorb the render frame before typing
        # Re-check AFTER settling — this is the fix for the double-compact "spam". An
        # autonomous agent (an out-of-focus pane doing a job) can start a new turn during
        # SETTLE. /resume typed into a now-busy pane is queued behind that turn or dropped,
        # so the pane stays on the big pre-compact session; behavior 2 then re-compacts it
        # ~a minute later, and the first fork is orphaned. If the pane went busy, restore
        # the signal and retry on a later idle frame instead of firing a resume that's lost.
        ptgt="$(pane_target "$pane" "$pserver")"; rc=$?
        if [ "$rc" = 1 ]; then
            rm -f "$busy" "$SIGNAL_DIR/$key.await-continue" "$SIGNAL_DIR/$key.lastauto"
            continue
        fi
        if [ "$rc" != 0 ] || ! pane_idle "$ptgt"; then
            [ -e "$f" ] || mv "$busy" "$f" 2>/dev/null; rm -f "$busy"
            continue
        fi
        if drop_outlived_fork "$key" "$ptgt" "$fork" "$(file_mtime "$busy")"; then rm -f "$busy"; continue; fi
        rm -f "$busy"
        $TMUX_CMD send-keys -t "$ptgt" "/resume $fork" Enter
        echo "compacto-resume-daemon: resumed $fork in pane $ptgt"
        evlog "resume-sent $ptgt $fork"
        # Deliberately do NOT clear .compacting here. Sending /resume is not proof it landed
        # — on a busy autonomous pane the keystroke can be queued or dropped and the pane
        # stays on the big session. Behavior 2 re-arms only once the size is CONFIRMED down
        # for REARM_GRACE. Drop the stale pre-compaction .ctx and stamp the floor so a last
        # high render from the old session can't re-trigger before the fork reports its size.
        # 1b can only see a resume land in a pane that exports its size (its .ctx, or the
        # .compacting behavior 2 left after firing on one). Elsewhere it would re-type
        # /resume every RESUME_RETRY_SECS until RESUME_GIVEUP, now that THRESH has a default.
        sized=0; { [ -e "$SIGNAL_DIR/$key.ctx" ] || [ -e "$SIGNAL_DIR/$key.compacting" ]; } && sized=1
        rm -f "$SIGNAL_DIR/$key.ctx"; : > "$SIGNAL_DIR/$key.lastauto"
        # Arm confirmation/retry (behavior 1b) so a resume the agent ate gets re-sent until
        # it actually lands. Only when threshold-compact is active — that's the sole path
        # that can tell "landed" from "not" by watching the size fall back under THRESH.
        if [ "$sized" = 1 ] && is_num "$THRESH" && [ "$THRESH" -gt 0 ]; then
            printf '%s\t%s\t%s\t%s\n' "$pane" "$fork" "$(date +%s)" "$pserver" > "$SIGNAL_DIR/$key.resumewant"
        fi
    done

    # ---- behavior 1b: confirm the resume landed, else retry (+3 continue) --
    # The proven failure: on a self-continuing agent, /resume is eaten by the next turn and
    # the pane stays on the big session, which then re-compacts. Here we watch the size and
    # re-send /resume (paced, idle-only) until it actually drops, then run the optional
    # continue. Give up after RESUME_GIVEUP so a wedged pane falls back to behavior 2.
    for w in "$SIGNAL_DIR"/*.resumewant; do
        [ -e "$w" ] || continue
        IFS=$'\t' read -r rpane rfork rsince rserver < "$w"
        [ -n "${rpane:-}" ] && [ -n "${rfork:-}" ] || { rm -f "$w"; continue; }
        rkey="${rserver:+${rserver}_}${rpane//[^a-zA-Z0-9]/}"
        if ! is_num "$THRESH" || [ "$THRESH" -le 0 ]; then rm -f "$w"; continue; fi   # can't confirm without a threshold
        rtgt="$(pane_target "${rpane}" "${rserver:-}")"; rc=$?
        if [ "$rc" = 1 ]; then rm -f "$w" "$SIGNAL_DIR/$rkey.await-continue"; continue; fi
        [ "$rc" = 0 ] || continue

        # Confirmed when the fork's own render reports a size back under the threshold.
        rcf="$SIGNAL_DIR/$rkey.ctx"
        if [ -e "$rcf" ]; then
            IFS=$'\t' read -r _ rctx rmsg _ < "$rcf"; rctx="${rctx%$'\r'}"; rmsg="${rmsg%$'\r'}"
            case "$METRIC" in ctx) rval="${rctx:-0}";; *) rval="${rmsg:-0}";; esac
            if is_num "$rval" && [ "$rval" -lt "$THRESH" ]; then
                rm -f "$w"
                echo "compacto-resume-daemon: resume confirmed for $rtgt ($METRIC=$rval)"
                evlog "resume-confirmed $rtgt $METRIC=$rval"
                ac="$SIGNAL_DIR/$rkey.await-continue"
                if [ -e "$ac" ] && [ -n "$CONTINUE_MSG" ] && pane_idle "$rtgt"; then
                    $TMUX_CMD send-keys -t "$rtgt" "$CONTINUE_MSG" Enter
                    PANE_CONTINUED[${rpane#%}]=1
                    echo "compacto-resume-daemon: continued $rtgt with: $CONTINUE_MSG"
                    rm -f "$ac"
                fi
                continue
            fi
        fi

        now="$(date +%s)"; is_num "${rsince:-}" || rsince="$now"
        # A turn that ended after the /resume went out means the session ran on, and the fork misses that turn.
        if drop_outlived_fork "$rkey" "$rtgt" "$rfork" "$rsince"; then rm -f "$w"; continue; fi
        if [ "$(( now - rsince ))" -ge "$RESUME_GIVEUP" ]; then
            rm -f "$w" "$SIGNAL_DIR/$rkey.await-continue"
            echo "compacto-resume-daemon: resume gave up for $rtgt after $(( now - rsince ))s ($rfork)" >&2
            evlog "resume-giveup $rtgt $rfork after=$(( now - rsince ))s"
            continue
        fi
        # Re-send, paced by the marker's mtime, only into an idle frame.
        if [ "$(file_age "$w")" -ge "$RESUME_RETRY_SECS" ] && pane_idle "$rtgt"; then
            $TMUX_CMD send-keys -t "$rtgt" "/resume $rfork" Enter
            touch "$w"
            echo "compacto-resume-daemon: resume retry for $rtgt ($rfork)"
            evlog "resume-retry $rtgt $rfork"
        fi
    done

    markers_wanted && update_markers
    sleep "$POLL"
done
