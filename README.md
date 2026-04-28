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
/cc:end 30000                  # keep last 30k tokens verbatim    (default 30k)
/cc:model sonnet[1m]           # summarizer model                 (default sonnet[1m])
/cc:help                       # all commands
```

Config lives at `~/.claude/precompact.conf` and is read fresh on every compact, in any session.

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

The current session is **untouched** (the hook blocks but doesn't rewrite it in place). To actually use the compressed fork:

```
/exit                          # leave the current (uncompacted) session
claude --resume                # opens the session picker
```

In the picker, sessions are listed in reverse-chronological order:

- **Top entry** = the session you just exited (still uncompacted — kept as a backup).
- **Second from top** = the new compressed fork. **Pick this one.**

You're now in the forked session with head + summary + tail loaded. The original is preserved on disk in case you want to go back.

> Tip: a human-readable preview of every fork is also written to `<project>/.claude/summaries/<sid>-<ts>.md` so you can inspect what the summarizer produced before resuming.

## Requirements

- **Python 3.8+** on PATH (the hook is pure Python; no extra deps required, optional `pip install tiktoken` for accurate token counting).
- **`claude` CLI** on PATH — the summarizer calls `claude -p` with `--system-prompt` to compress the middle.
- Tested on macOS / Linux. Windows works under Git Bash (which Claude Code uses for hook shell-exec on Windows). Pure cmd / PowerShell support depends on whether your Claude Code build resolves `python` and `${CLAUDE_PLUGIN_ROOT}` correctly — file an issue if not.

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
│       │   └── rewrite_transcript.py   JSONL fork writer
│       ├── commands/                   /cc:begin /cc:end /cc:both /cc:begin-pct /cc:end-pct /cc:both-pct /cc:model /cc:show /cc:reset /cc:help
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
