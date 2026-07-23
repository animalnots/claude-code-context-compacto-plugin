# claude-code-context-compacto-plugin

A Claude Code plugin marketplace publishing **`cc`** (the **context-compacto** plugin) — a smarter `/compact` replacement that keeps your head & tail of the session verbatim and turns the middle into a structured summary you can resume from.

## Why

Claude Code's built-in `/compact` collapses everything into one summary message. That works, but you lose the original tool calls and exact phrasing in the head and tail of the session — the parts you most often want to scroll back to. `cc` keeps a configurable token budget at each end verbatim and only summarizes what's between them. It also writes a forked transcript with UUIDs remapped so resumed sessions don't accidentally pull the original chain back in.

## Install (for users)

In Claude Code, run:

```
/plugin marketplace add animalnots/claude-code-context-compacto-plugin
/plugin install cc@context-compacto-marketplace
```

That's it. Restart Claude Code if needed.

## Usage

### 1. Configure budgets once (optional — defaults are sensible)

```
/cc:show                       # see current config
/cc:begin 25000                # keep first 25k tokens verbatim   (default 0)
/cc:end 25000                  # keep last 25k tokens verbatim    (default 25k)
/cc:model200k sonnet           # model for normal-size middles    (default sonnet)
/cc:model1m opus[1m]           # model for oversized middles      (default opus[1m])
/cc:help                       # all commands
```

Config lives at `~/.claude/precompact.conf` and is read fresh on every compact, in any session.

> Budgets are **tiktoken** counts, not Claude tokens. Claude's tokenizer runs ~1.5–1.66× higher on code/tool-heavy sessions, so `/cc:begin 25000` and `/cc:end 25000` each preserve roughly 38–42k tokens of real context. Set the number to ~0.6× of a target real size. (Percentage budgets — `/cc:end-pct` etc. — are unaffected.)

### 2. Compact a session

When your context gets full (or you just want to trim it), run:

```
/compact
```

The PreCompact hook intercepts it — Claude Code's native one-blob `/compact` is **blocked**, and instead the hook writes a forked transcript: `[head verbatim] → [structured summary] → [tail verbatim]`, with UUIDs remapped so it's self-contained. You'll see a message like:

```
precompact wrote fork: <project>/<new-sid>.jsonl ; resume with: claude --resume <new-sid>
```

### 3. Resume from the fork

The current session is **untouched** (the hook blocks but doesn't rewrite it in place). The block message prints the fork's session id, so the easiest path is to load it in place — no need to leave the chat:

```
/resume <fork-id>              # the id printed in the block message
```

`/resume` is built in: it swaps your current chat for another session in the same terminal (no `/exit` + relaunch). No id handy? Run `/resume` with no argument and pick the newest **`compact …`** entry — every fork is titled `compact MM-DD HH:MM`. A fresh-shell `claude --resume` opens the same picker.

You're now in the forked session with head + summary + tail loaded. The original is preserved on disk in case you want to go back.

> Tip: a human-readable preview of every fork is also written to `<project>/.claude/summaries/<sid>-<ts>.md` so you can inspect what the summarizer produced before resuming.

### 4. Auto-resume (optional, off by default)

Don't want to type `/resume` yourself every time? Arm auto-resume and a small daemon does that one keystroke for you after each compaction — a long session compacts and continues on its own while you stay at the keyboard reviewing. Two switches, both off by default:

```
/cc:autoresume on                                             # arm it
"${CLAUDE_PLUGIN_ROOT}/hooks/compacto-resume-daemon.sh" &     # run the watcher once (needs tmux)
```

Run Claude Code inside **tmux** and each compaction now types `/resume <fork-id>` into that pane automatically. It's parallel-safe: signals are keyed on the tmux pane id, so multiple sessions compacting at once each resume their own window. Turn it off with `/cc:autoresume off`, or just don't run the daemon. Full details in the [plugin README](plugins/context-compacto/README.md#auto-resume-optional-off-by-default).

## Requirements

- **Python 3.8+** on PATH (the hook is pure Python; no extra deps required, optional `pip install tiktoken` for accurate token counting).
- **`claude` CLI** on PATH — the summarizer calls `claude -p` with `--system-prompt` to compress the middle.
- **`tmux`** — only for optional auto-resume (§4). Everything else works without it.
- Tested on macOS / Linux. Windows works under Git Bash (which Claude Code uses for hook shell-exec on Windows). Pure cmd / PowerShell support depends on whether your Claude Code build resolves `python` and `${CLAUDE_PLUGIN_ROOT}` correctly — file an issue if not. Auto-resume's daemon is a bash script and expects tmux, so it's macOS/Linux/Git-Bash.

## Update the plugin

```
/plugin marketplace update
```

Then re-run `/plugin install` if there are version changes.

## Uninstall

```
/plugin uninstall cc
/plugin marketplace remove context-compacto-marketplace
```

## What's in the repo

```
.
├── .claude-plugin/marketplace.json     marketplace catalog (lists 1 plugin)
├── plugins/
│   └── context-compacto/               the plugin itself
│       ├── .claude-plugin/plugin.json  manifest
│       ├── hooks/
│       │   ├── hooks.json              PreCompact registration
│       │   ├── precompact.py           hook entry point
│       │   ├── pcconf.py               config helper for slash commands
│       │   ├── rewrite_transcript.py   JSONL fork writer
│       │   └── compacto-resume-daemon.sh  optional tmux auto-resume watcher
│       ├── commands/                   /cc:begin /cc:end /cc:both /cc:begin-pct /cc:end-pct /cc:both-pct /cc:model /cc:autoresume /cc:show /cc:reset /cc:help
│       └── README.md                   plugin docs (full usage + config)
└── README.md                           this file
```

## Develop locally

Clone the repo and add it as a local marketplace (no need to publish to GitHub first):

```
git clone https://github.com/animalnots/claude-code-context-compacto-plugin
/plugin marketplace add /absolute/path/to/claude-code-context-compacto-plugin
/plugin install cc@context-compacto-marketplace
```

Edit files in `plugins/context-compacto/`, then `/plugin marketplace update` to refresh.
