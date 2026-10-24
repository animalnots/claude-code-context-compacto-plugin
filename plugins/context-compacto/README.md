# context-compacto

Replaces Claude Code's native `/compact` with a token-budget head/middle/tail compression. Head and tail are kept verbatim; the middle is summarized into a structured 4-section block by a separate `claude -p` call.

## What it does

1. The `PreCompact` hook intercepts auto and manual compactions of the main session. A subagent's compaction is left to Claude Code's native compaction, because the hook is only handed the parent's transcript. Claude Code 2.1.300 and later don't mark a subagent's compaction (no `agent_id`), so the hook infers it on an auto trigger. The cutoff is 85% of `CLAUDE_CODE_AUTO_COMPACT_WINDOW` or 60k tokens short of it, whichever is lower; the parent's own auto compaction fires about 25–33k short of the window. Set that variable with `/cc:bigwindow` (for example `/cc:bigwindow 600k`, which writes it into the `env` block of `~/.claude/settings.json`); without it, subagent compactions are blocked and a long subagent runs into the context limit. The trigger is a subagent's when the parent's context is under the cutoff, or when the transcript written last is a subagent's (written in the last 60 seconds, with a context at or over the cutoff) while the parent's transcript has been still for 5 seconds. The hook checks that after a half-second wait, so transcript writes Claude Code still has pending can land. Two known limits: a subagent on a model whose own window is under `CLAUDE_CODE_AUTO_COMPACT_WINDOW` compacts below the cutoff, so it's blocked while its parent is over the cutoff; and a subagent that compacts while its parent is writing is blocked until a turn when the parent has been still for 5 seconds. Each decision on an auto compaction is a line in `~/.claude/compacto-signals/compactions.log`. A tmux window whose name contains `!` is never auto-compacted: the hook refuses the auto compaction without forking or arming `/resume` (the same opt-out the resume daemon honours); a subagent's compaction still runs there, and a manual `/compact` there still works. While a `/compact-handoff` you typed hasn't finished (it's still running, or its turn was interrupted), every compaction is refused, so a `/compact` queued behind it can't compact away what it was saving; type `/compact` again within 2 minutes to compact anyway. Windows named with `=` have their own rules, see [Hand off first with `=`](#autonomous-loop-auto-compact--continue--advanced-unattended).
2. It reads the live transcript JSONL, splits it into `head_tokens` first + `tail_tokens` last, and ships the middle to a summarizer. The model is chosen by the middle's size: `model_200k` (default `sonnet`) when it fits a standard window, otherwise `model_1m` (default `opus[1m]`); if the 200k call errors it escalates to the 1M model.
3. The summarizer outputs a fixed-shape markdown block:
   - `## Narrative`
   - `## Achievements`
   - `## State at end of middle section`
   - `## Notable tool calls`
4. A new forked `<sid>.jsonl` is written under your projects dir with `[head verbatim] → [synthetic summary entry] → [tail verbatim]`. UUIDs are remapped so the fork is self-contained.
5. The hook returns `{"decision": "block", "reason": "...resume with: claude --resume <new-sid>"}` — Claude Code does not run its own compaction.
6. A human-readable preview is also written to `<project>/.claude/summaries/<sid>-<ts>.md` for inspection.

## Slash commands

All commands are namespaced under the plugin's prefix `/cc:` (Claude Code does this automatically — it's how multiple plugins coexist without colliding).

### Absolute token budgets (N = raw token count)

| Command          | Effect |
|------------------|--------|
| `/cc:begin N`    | head_tokens = N  (e.g. `/cc:begin 25000` keeps 25k head verbatim) |
| `/cc:end N`      | tail_tokens = N  (e.g. `/cc:end 30000` keeps 30k tail verbatim) |
| `/cc:both N`     | both head and tail = N |

> **Budgets are tiktoken counts, not Claude tokens.** Windows are sized with tiktoken (`cl100k_base`); Claude's own tokenizer counts more — roughly **1.5–1.66×** on the code/tool-heavy transcripts typical of Claude Code (less on prose-heavy ones). So a budget of `N` preserves about `1.5–1.66×N` tokens of real context — e.g. `/cc:end 30000` lands around 45–50k real. Targeting a specific real size? Set `N` to ~0.6× of it. Percentage budgets (below) are unaffected: numerator and denominator use the same tokenizer, so the ratio cancels.

### Percentage budgets (N = percent of total convo tokens)

| Command            | Effect |
|--------------------|--------|
| `/cc:begin-pct N`  | head_pct = N  (e.g. `/cc:begin-pct 10` keeps the first 10% verbatim) |
| `/cc:end-pct N`    | tail_pct = N  (e.g. `/cc:end-pct 20` keeps the last 20% verbatim) |
| `/cc:both-pct N`   | both head and tail percent (max N=50, since head+tail ≤ 100) |

**Validation:** `head_pct + tail_pct` must stay ≤ 100. If a `/cc:begin-pct` or `/cc:end-pct` call would push the sum above 100 it's rejected (no mutation), with a hint about lowering the other side.

**Mutex per window — last command wins:** TOKENS and PCT are exclusive for the same window. Each setter clears the other mode for that window:

| Last command for HEAD     | Result in conf file |
|---------------------------|------|
| `/cc:begin 25000`         | `head_tokens=25000` (any prior `head_pct` removed) |
| `/cc:begin-pct 10`        | `head_pct=10` (any prior `head_tokens` removed) |

(Same for tail with `/cc:end` and `/cc:end-pct`.) Mix modes **across** windows freely — e.g. `head_tokens=20000` + `tail_pct=15` is fine.

### Other

| Command           | Effect |
|-------------------|--------|
| `/cc:model200k ID` | standard-context summarizer, used when the middle fits ≤200k (e.g. `sonnet`) |
| `/cc:model1m ID`   | 1M-context summarizer, used when the middle is bigger (e.g. `opus[1m]`) |
| `/cc:model ID`     | alias for `/cc:model200k` |
| `/cc:autoresume on\|off` | arm/disarm tmux auto-resume — a daemon types `/resume <fork>` into your pane after each compact (default **off**; see [Auto-resume](#auto-resume-optional-off-by-default)) |
| `/cc:autocompact N [msgs\|ctx]` | auto-`/compact` this pane once it reaches N tokens (default **300000**, `off` disables; see [Autonomous loop](#autonomous-loop-auto-compact--continue--advanced-unattended)) |
| `/cc:continue MSG\|off` | after an auto-triggered compaction resumes, auto-send MSG (e.g. `continue`) so the task keeps going (default **off**) |
| `/cc:bigwindow N\|off` | Claude Code's own auto-compact window, for every session including running ones: sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in `~/.claude/settings.json` (N = 100k–1M, e.g. `600k`; with no N it shows the value). It prints the subagent cutoff from step 1 and any pane at or over it. While it's set, Claude Code's own `/autocompact` can't change the window |
| `/cc:show`        | print current config |
| `/cc:reset`       | delete config (revert to defaults) |
| `/cc:help`        | inline help |

`/cc:begin 0` skips the head entirely — the synthetic summary becomes the conversation root, followed only by the tail. `/cc:end 0` symmetrically skips the tail.

## Config file

Stored at `~/.claude/precompact.conf` (plain `key=value`):

```
# Mode A — absolute tokens
head_tokens=25000
tail_tokens=20000

# Mode B — percent of total convo tokens
head_pct=10
tail_pct=20

model_200k=sonnet
model_1m=opus[1m]
```

You can mix the two modes across head and tail (e.g. `head_tokens=...` + `tail_pct=...`), but TOKENS and PCT for the **same** window shouldn't both be set — TOKENS will win.

Defaults if everything is unset: `head_tokens=0  tail_tokens=25000  model_200k=sonnet  model_1m=opus[1m]` — i.e. summarize everything except the last 25k tokens, using `sonnet` for normal-size middles and escalating to `opus[1m]` for oversized ones. Override with `/cc:begin /cc:end /cc:begin-pct /cc:end-pct /cc:model200k /cc:model1m` (or env vars) when you want different behavior. (Legacy `model=` is still read as `model_200k`.)

## Precedence

For each window, head and tail are resolved independently. **TOKENS beats PCT** wherever each comes from, and within either kind **env beats file**.

```
1. env  PRECOMPACT_HEAD_TOKENS  / PRECOMPACT_TAIL_TOKENS
2. file head_tokens             / tail_tokens
3. env  PRECOMPACT_HEAD_PCT     / PRECOMPACT_TAIL_PCT
4. file head_pct                / tail_pct
5. PRECOMPACT_WINDOW_TOKENS     (legacy symmetric, env or file)
6. built-in default             (head=0, tail=25000)
```

**How percent is computed:** `pct% × (total token cost of the conversation post-dedup, pre-compression)`. Example: 200,000-token session with `head_pct=10` → head budget = 20,000 tokens.

**When to use which:**
- **Tokens** — predictable across sessions of any size (recommended default).
- **Percent** — scales with session size. Useful if you always want "the first/last N% of the session" regardless of how long it grew.

## Scope: when do config changes take effect?

The config file at `~/.claude/precompact.conf` is **global to your machine** — there's no per-session copy and no "snapshot at session start". Slash commands write to the file immediately, and the PreCompact hook re-reads it every time a compact runs. So:

- **Current session** — your next `/compact` (or auto-compact) picks up the new value.
- **Other sessions running right now in other terminals** — they also pick it up on their next compact. The file is the single source of truth across sessions.
- **New sessions started later** — same. They read the file fresh when their first compact happens.
- **Env vars** (`PRECOMPACT_HEAD_TOKENS`, etc.) are evaluated each time the hook runs too, so changing your shell's env between compacts is honored. But env still **overrides** the file (per the precedence ladder above), so if you have an env var set, editing the file won't change behavior for that shell until you `unset` it.

In short: there is no concept of "per-session config". One file, read live on every compact.

## Env var overrides

```
PRECOMPACT_HEAD_TOKENS=20000
PRECOMPACT_TAIL_TOKENS=20000
PRECOMPACT_HEAD_PCT=25
PRECOMPACT_TAIL_PCT=20
PRECOMPACT_MODEL_200K=sonnet
PRECOMPACT_MODEL_1M=opus[1m]
PRECOMPACT_MAX_BLOCK_TOKENS=3000      # truncation cap for tool_result / attachment file content
PRECOMPACT_STD_CTX_LIMIT=180000       # max middle tokens for the 200k slot before switching to 1M
PRECOMPACT_LONG_CTX_LIMIT=900000      # max middle tokens sent to the 1M slot
PRECOMPACT_AUTO_RESUME=1              # arm tmux auto-resume (default off; see Auto-resume)
```

## Resume after compaction

The hook's block message prints the fork's session id:

```
Compressed fork written. Resume in this chat with:  /resume <new-sid>   (or  claude --resume <new-sid>  from a new shell).
```

`/resume <new-sid>` swaps your current chat to the fork in place — no `/exit`, no new shell. See the top-level README for the picker fallback (forks are titled `compact MM-DD HH:MM`).

## Auto-resume (optional, off by default)

Automates that one keystroke. When armed, after every compaction a small daemon types `/resume <new-sid>` into your terminal for you, so a long session compacts and continues on its own while you keep working — you're still a human at the keyboard reviewing everything; only the mechanical resume is automated.

**It is opt-in on two independent switches — with either one untouched, nothing auto-resumes and the plugin behaves exactly as before:**

1. **Arm it:** `/cc:autoresume on` (writes `auto_resume=1`). Off by default.
2. **Run the daemon once:** `"${CLAUDE_PLUGIN_ROOT}/hooks/compacto-resume-daemon.sh" &` (or give it its own tmux pane). Nothing auto-resumes unless it's running.

**Requires tmux.** The hook only arms a resume when it runs inside a tmux pane, because the daemon drives the resume by sending keystrokes to that pane. Outside tmux, `auto_resume=on` is inert and the block message is your manual fallback.

**How it stays parallel-safe.** On each compaction the hook drops a signal file at `~/.claude/compacto-signals/<pane>.resume` containing `<tmux-pane-id>\t<fork-id>`. The key is `$TMUX_PANE`, which is globally unique on the tmux server, so N sessions compacting at once each get their own signal and the daemon resumes each fork into *its own* window — session A never lands in session B's chat. One daemon serves every pane.

```
/cc:autoresume on                                             # arm (default off)
"${CLAUDE_PLUGIN_ROOT}/hooks/compacto-resume-daemon.sh" &     # run once, serves all panes
# ... work in one or more tmux panes; each compaction now self-resumes ...
/cc:autoresume off                                            # disarm anytime
```

> Timing: a compaction returns you to an idle prompt, which is when the daemon types `/resume` — safe. If you manually `/compact` with half a line already typed, the injected `/resume` can interleave with it; v1 doesn't guard that edge.
>
> Assumes a single default tmux server (pane ids are unique per server). psmux on Windows is handled separately, see [Windows (psmux)](#windows-psmux).

## Autonomous loop (auto-compact + continue) — advanced, unattended

Chains everything above into a hands-off loop: **compact at a token threshold → auto-resume → auto-send a message** so a long task keeps running across compaction boundaries with no one at the keyboard. This removes the human from the loop by design — read the warning before arming it.

**Requires the auto-resume daemon (above) plus one extra piece: a statusline export** so the daemon can read your live context size. Add these lines to your Claude Code statusline command (the script behind `statusLine` in `~/.claude/settings.json`), right after it computes the current token count:

```bash
# --- context-compacto: export live context size for the auto-compact watcher ---
if [ -n "${TMUX_PANE:-}" ]; then
    _cc_sig="$HOME/.claude/compacto-signals"
    if mkdir -p "$_cc_sig" 2>/dev/null; then
        _cc_key="${TMUX_PANE//[^a-zA-Z0-9]/}"
        printf '%s\t%s\t%s\n' "$TMUX_PANE" "$CTX" "$MSG" > "$_cc_sig/${_cc_key}.ctx.tmp" 2>/dev/null \
            && mv -f "$_cc_sig/${_cc_key}.ctx.tmp" "$_cc_sig/${_cc_key}.ctx" 2>/dev/null
    fi
fi
```

`$CTX` = total context tokens (`.context_window.total_input_tokens` from the JSON Claude Code pipes to the statusline); `$MSG` = whatever "message tokens" figure you display. No export → the loop has nothing to read and never triggers. A PowerShell statusline needs the version in [Windows (psmux)](#windows-psmux).

**Arm it.** With the export above in place the trigger is already on at 300000 by default; the auto-send is off until you set it:

```
/cc:autocompact 180000        # compact when msgs (the statusline number) reaches 180k
/cc:autocompact 150000 ctx    # ...or trigger on raw context tokens instead of msgs
/cc:continue continue         # after each AUTO compaction resumes, send "continue"
/cc:autocompact off           # disarm the trigger
/cc:continue off              # disarm the auto-send
```

Run the **same daemon** as auto-resume — it does all three jobs.

**⚠ This is UNBOUNDED.** compact → resume → "continue" → context grows → compact … repeats with no cap and no human in it, until you **stop the daemon (`Ctrl-C`)** or run `/cc:autocompact off`. It spends real tokens the whole time (your Max weekly allowance, or API credits). There is deliberately no auto-stop — *you* are the bound.

**Only auto-triggered compactions continue.** A manual `/compact` you run to inspect still auto-resumes but does **not** auto-send the message (the daemon only continues compactions it started).

**Opt a window out with `!`.** Put `!` anywhere in a tmux window's name (`prefix ,` or `tmux rename-window 'review!'`) and the daemon never auto-`/compact`s a pane in that window. The name is re-read every poll, so renaming the window back re-enables auto-compact within a second. Auto-resume is untouched: a `/compact` you type there yourself still compacts through the plugin, and the daemon still types its `/resume`.

**Hand off first with `=`.** Put `=` in a window's name instead (`!` wins if both are there) and the daemon saves your work before compacting. Once a pane there reaches `min(600000, 2 × auto_compact_at)`, that is 530000 at `auto_compact_at=265000`, the daemon:

1. types `/compact-handoff` into the empty input box of an idle pane;
2. waits until the plugin's hooks see that handoff's own turn end normally (`Stop` with the same prompt id);
3. types `/compact` once the pane is idle again, and the usual `/resume` follows.

`/compact-handoff` is a skill or command you provide; name another one with `handoff_command=<name>` in `~/.claude/precompact.conf`. The daemon presses Enter only after the box shows exactly what it typed, so it never submits a draft of yours. A handoff that is interrupted, ends in an API error, or never starts is never followed by `/compact`. It is retried on the same back-off as a failed `/compact`, and after one that never started the daemon waits until Claude there finishes another turn. In a `=` window the hook refuses Claude Code's own auto compaction of the main session (a subagent's still runs, see step 1 of [What it does](#what-it-does)), and refuses a `/compact` typed before this session's handoff has finished (type it again within 2 minutes to compact anyway). The hooks load when a Claude session starts, so a session started before this version is left alone: the daemon says so in its log.

**Window markers: `?`, `$`, `&`.** The daemon also labels every tmux window running Claude by setting the window option `@cc_state`:

- `?` a question, permission or trust dialog is waiting for you;
- `$` Claude finished while you weren't viewing that window (it clears once you look);
- `&` Claude is idle, but background agents or shells are still running, and it will wake itself when they finish.

The daemon's own `/compact` + `/resume` never produces a `$`. Nothing shows until your status line prints the option. For tmux's default window format, add to `~/.tmux.conf`:

```tmux
set -g window-status-format '#I:#{@cc_state}#W#{?window_flags,#{window_flags}, }'
set -g window-status-current-format '#I:#{@cc_state}#W#{?window_flags,#{window_flags}, }'
```

With a themed format, put `#{@cc_state}` wherever you want the marker. Window names are never changed, so `!` opt-outs, `prefix ,` and tmux-resurrect saves are unaffected.

**The fragile part — idle detection.** The daemon only types (`/compact`, `/resume`, `continue`) into Claude's plain input box, a `❯` line directly under a `───` rule, and only when the status line above that box shows a finished turn: `✻ Baked for 5m 16s · done 23:03`, `✻ Waiting for 2 background agents to finish`, or no status line at all. Any other status line means Claude is mid-turn, however it's worded: `✳ Scurrying… (3m 53s …)`, a hook's `(Checking UI changes… · 30m 43s …)`, `Waiting for API response · will retry in 2m 40s`. `esc to interrupt` or a queued-message caption anywhere near the box also means busy. A question, permission or trust dialog replaces the box, so the daemon waits instead of pressing Enter on it, which would pick option 1 for you. If your Claude Code words a finished turn differently, the daemon waits instead of typing. Check what it sees:

```bash
# once Claude is idle in pane %N, this SHOULD print a finished line like "✻ Baked for 5m 16s · done 23:03":
tmux capture-pane -p -t %N | grep -E '^(·|✢|✳|✶|✻|✽|\*) ' | tail -1
# to also treat some other on-screen text as busy:
COMPACTO_BUSY_REGEX='your busy text' /path/to/compacto-resume-daemon.sh &
```

**Daemon env knobs:**

```
COMPACTO_TMUX=tmux                       # tmux command (e.g. "tmux -L socket")
COMPACTO_BUSY_REGEX='esc to interrupt'   # "pane is generating" marker (idle = absent)
COMPACTO_CLAUDE_CMD_REGEX='^([0-9]+\.[0-9]+\.[0-9]+|claude|node)$'   # process name of a Claude pane (window markers)
COMPACTO_COMPACT_COOLDOWN=300            # secs before a stuck in-flight compaction is retried (> the 240s summarizer timeout)
COMPACTO_CONTINUE_SETTLE=3               # secs to let a resume render before typing the continue message
COMPACTO_POLL_SECS=1                     # poll interval
COMPACTO_HANDOFF_MAX=600000              # cap on a "=" window's threshold
COMPACTO_HANDOFF_START_TIMEOUT=60        # secs a typed handoff may take to start before it counts as failed
```

Config keys (in `~/.claude/precompact.conf`, read live on every poll): `auto_compact_at`, `auto_compact_metric` (`msgs` | `ctx`), `resume_continue`, `handoff_command` (default `compact-handoff`).

## Windows (psmux)

Auto-resume and the autonomous loop also run on native Windows inside [psmux](https://github.com/psmux/psmux), a Windows tmux. It installs a `tmux` command, so the hook and the daemon find it as is. What differs:

- **The daemon runs in Git Bash.** Inside psmux the SessionStart hook starts it for you with the `bash.exe` next to `git.exe` (set `COMPACTO_BASH` to use another one): a bare `bash` on Windows is the WSL launcher. To start it by hand from a psmux pane:

  ```powershell
  & 'C:\Program Files\Git\bin\bash.exe' "$HOME\.claude\plugins\cache\context-compacto-marketplace\cc\<version>\hooks\compacto-resume-daemon.sh"
  ```

- **The statusline export in PowerShell.** Insert after your script computes the two numbers (`$CTX` = total context tokens, `$MSG` = the msgs figure you display):

  ```powershell
  # --- context-compacto: export live context size for the auto-compact watcher ---
  if ($env:TMUX_PANE) {
      try {
          $sig = Join-Path $HOME '.claude/compacto-signals'
          New-Item -ItemType Directory -Force -Path $sig | Out-Null
          $key  = $env:TMUX_PANE -replace '[^a-zA-Z0-9]', ''
          $line = "{0}`t{1}`t{2}" -f $env:TMUX_PANE, [int64]$CTX, [int64]$MSG
          # psmux numbers panes per session, so its server pid (in $TMUX) joins the key and the line.
          if ($env:TMUX -match 'psmux-(\d+)') { $key = "$($Matches[1])_$key"; $line += "`t$($Matches[1])" }
          $tmp = Join-Path $sig "$key.ctx.tmp"
          # No BOM (it would glue onto the pane id), hence WriteAllText rather than Out-File.
          [System.IO.File]::WriteAllText($tmp, "$line`n", (New-Object System.Text.UTF8Encoding($false)))
          Move-Item -Force -Path $tmp -Destination (Join-Path $sig "$key.ctx")
      } catch { }
  }
  ```

  No PowerShell statusline yet? Copy [`examples/statusline-command.ps1`](examples/statusline-command.ps1) outside the plugin cache and point `statusLine.command` at it by absolute path; `${CLAUDE_PLUGIN_ROOT}` doesn't expand there.

- **Several psmux sessions are fine.** psmux runs a server per session and numbers panes per server, so `%1` exists in every session, and a command aimed at a closed session's `%1` lands in another session's pane. So on psmux the hook and the export above add the server pid (from `$TMUX`) to each signal, and the daemon addresses a pane as `=<session>:.%N`, which fails instead of falling through. A signal without the pid, from an older export, is acted on only while its `%N` exists in a single session.
- **Window markers are off on psmux:** it runs only the first command of a `;` chain and never updates `window_activity`.
- **Faster polling (optional):** `COMPACTO_TMUX=/c/ProgramData/chocolatey/lib/psmux/tools/tmux.exe` skips the Chocolatey shim, roughly halving each tmux call.
- **Claude back in its panes after a reboot.** psmux-resurrect restores sessions, windows, panes and their directories, but psmux reports a Claude pane as one of its MCP children (`node`), so it can't relaunch Claude. [`psmux/claude-resume.ps1`](psmux/claude-resume.ps1) does, the way tmux-resurrect hooks do on macOS. Every few minutes it runs psmux-resurrect's save and records each Claude pane's slot, directory, command line and session id. After a fresh psmux start it runs psmux-resurrect's restore, then types `Set-Location '<dir>'; & '<claude.exe>' <its flags> --resume '<id>'` into each of those panes that sits at a PowerShell prompt. It needs psmux-resurrect and PowerShell 7 (`pwsh`), and takes the place of psmux-continuum. When `pwsh` isn't on the PATH psmux started with, psmux quietly runs the hook in Windows PowerShell; the script then finds `pwsh` itself, and `-ExecutionPolicy Bypass` keeps Windows PowerShell's default policy from blocking it. Copy it to `~/.psmux/claude-resume/` and add to `~/.psmux.conf` (with your own user path):

  ```tmux
  set -g @plugin 'psmux-plugins/psmux-resurrect'
  # psmux-resurrect would otherwise type "node" into Claude panes
  set -g @resurrect-processes 'false'
  set -g @claude-resume-save-interval '5'
  set-hook -g session-created 'run-shell "pwsh -NoProfile -ExecutionPolicy Bypass -File \"C:/Users/<you>/.psmux/claude-resume/claude-resume.ps1\" -Restore"'
  set-hook -g client-attached 'run-shell "pwsh -NoProfile -ExecutionPolicy Bypass -File \"C:/Users/<you>/.psmux/claude-resume/claude-resume.ps1\" -Loop"'
  ```

  It restores only when no other psmux session is alive, so after a reboot or after you close them all. Flags that pick a conversation (`--resume`, `--continue`, `--session-id`) and a launch prompt are not repeated. The log is `~/.psmux/claude-resume/claude-resume.log`; `claude-resume.ps1 -Resume -DryRun` logs the commands it would type.

## Requirements

- **Python 3.8+** on PATH (`python` resolvable). On Windows, use the [python.org installer](https://www.python.org/downloads/) which adds `python` to PATH; on macOS/Linux, install via your package manager.
- **Claude CLI** (`claude`) on PATH — the hook calls `claude -p` to do the summarization.
- Optional: `pip install tiktoken` for accurate token counting (otherwise a 3-chars-per-token estimate is used).

## What gets preserved vs. compressed

- **Preserved verbatim:** all `text`, `tool_use`, `tool_result`, `image` blocks in the head and tail windows. `message.usage` is stripped (it leaks parent-session counts).
- **Dropped:** `thinking` / `redacted_thinking` blocks (empty-text under Opus 4.8's default display — only a large opaque signature would remain, which is same-model replay-validation weight that can't be partially kept). Assistant entries that are *only* thinking are removed whole, so the fork carries real conversation instead of signature mass.
- **Truncated:** `tool_result` blocks > `PRECOMPACT_MAX_BLOCK_TOKENS` (default 3000) and embedded file content in attachments. **Plain text blocks are never truncated.**
- **Compressed:** middle entries are sent raw to the summarizer (no per-block truncation applied to the summarizer's input — only to the fork on disk).

## Layout

```
plugins/context-compacto/
├── .claude-plugin/plugin.json
├── hooks/
│   ├── hooks.json                  PreCompact registration (auto + manual)
│   ├── precompact.py               hook entry: writes preview + fork (+ auto-resume signal)
│   ├── pcconf.py                   /cc:begin /cc:end /cc:both /cc:model /cc:autoresume /cc:autocompact /cc:continue /cc:bigwindow /cc:show /cc:reset config helper
│   ├── rewrite_transcript.py       JSONL surgery: head + synth + tail with parentUuid relinking
│   └── compacto-resume-daemon.sh   optional watcher: auto-resume + threshold auto-compact + post-resume continue
├── commands/                       slash commands
│   ├── begin.md end.md both.md begin-pct.md end-pct.md both-pct.md model.md autoresume.md autocompact.md continue.md bigwindow.md show.md reset.md help.md
└── README.md
```

## License

MIT
