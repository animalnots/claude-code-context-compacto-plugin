# context-compacto

Replaces Claude Code's native `/compact` with a token-budget head/middle/tail compression. Head and tail are kept verbatim; the middle is summarized into a structured 4-section block by a separate `claude -p` call.

## What it does

1. The `PreCompact` hook intercepts auto and manual compactions.
2. It reads the live transcript JSONL, splits it into `head_tokens` first + `tail_tokens` last, and ships the middle to a summarizer (default `sonnet[1m]`).
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
| `/cc:model ID`    | summarizer model (`sonnet[1m]`, `opus[1m]`, `haiku`, …) |
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

model=sonnet[1m]
```

You can mix the two modes across head and tail (e.g. `head_tokens=...` + `tail_pct=...`), but TOKENS and PCT for the **same** window shouldn't both be set — TOKENS will win.

Defaults if everything is unset: `head_tokens=0  tail_tokens=30000  model=sonnet[1m]` — i.e. summarize everything except the last 30k tokens. Override with `/cc:begin /cc:end /cc:begin-pct /cc:end-pct` (or env vars) when you want different behavior.

## Precedence

For each window, head and tail are resolved independently. **TOKENS beats PCT** wherever each comes from, and within either kind **env beats file**.

```
1. env  PRECOMPACT_HEAD_TOKENS  / PRECOMPACT_TAIL_TOKENS
2. file head_tokens             / tail_tokens
3. env  PRECOMPACT_HEAD_PCT     / PRECOMPACT_TAIL_PCT
4. file head_pct                / tail_pct
5. PRECOMPACT_WINDOW_TOKENS     (legacy symmetric, env or file)
6. built-in default             (head=0, tail=30000)
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
PRECOMPACT_MODEL=opus[1m]
PRECOMPACT_MAX_BLOCK_TOKENS=3000      # truncation cap for tool_result / attachment file content
PRECOMPACT_MAX_CHARS=200000           # max chars sent to summarizer
```

## Resume after compaction

The hook prints a JSON response that includes:

```
Compressed fork written. Resume with:  claude --resume <new-sid>
```

Copy that into a new shell.

## Requirements

- **Python 3.8+** on PATH (`python` resolvable). On Windows, use the [python.org installer](https://www.python.org/downloads/) which adds `python` to PATH; on macOS/Linux, install via your package manager.
- **Claude CLI** (`claude`) on PATH — the hook calls `claude -p` to do the summarization.
- Optional: `pip install tiktoken` for accurate token counting (otherwise a 3-chars-per-token estimate is used).

## What gets preserved vs. compressed

- **Preserved verbatim:** all `text`, `tool_use`, `tool_result`, `thinking`, `image` blocks in the head and tail windows. `message.usage` is stripped (it leaks parent-session counts).
- **Truncated:** `tool_result` blocks > `PRECOMPACT_MAX_BLOCK_TOKENS` (default 3000) and embedded file content in attachments. **Plain text blocks are never truncated.**
- **Compressed:** middle entries are sent raw to the summarizer (no per-block truncation applied to the summarizer's input — only to the fork on disk).

## Layout

```
plugins/context-compacto/
├── .claude-plugin/plugin.json
├── hooks/
│   ├── hooks.json                  PreCompact registration (auto + manual)
│   ├── precompact.py               hook entry: writes preview + fork
│   ├── pcconf.py                   /cc:begin /cc:end /cc:both /cc:model /cc:show /cc:reset config helper
│   └── rewrite_transcript.py       JSONL surgery: head + synth + tail with parentUuid relinking
├── commands/                       slash commands
│   ├── begin.md end.md both.md begin-pct.md end-pct.md both-pct.md model.md show.md reset.md help.md
└── README.md
```

## License

MIT
