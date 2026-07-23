---
description: Show precompact slash commands (begin, begin-pct, end, end-pct, both, both-pct, model, autoresume, autocompact, continue, show, reset).
---

Print ONLY the block below verbatim — no preamble, no commentary, no surrounding markdown, no trailing remarks. Do NOT describe what it is. Just print it.

```
Precompact slash commands (all under the /cc: plugin namespace)

Absolute token budgets (N = raw tiktoken count):
  /cc:begin N        set head-window           (e.g. /cc:begin 25000)
  /cc:end N          set tail-window           (e.g. /cc:end 30000)
  /cc:both N         set both head and tail    (e.g. /cc:both 15000)
  N is a tiktoken count; Claude's own tokenizer runs ~1.5-1.66x higher on
  code/tool-heavy sessions, so N preserves ~1.5-1.66xN tokens of real context
  (set N to ~0.6x of a target real size). Percentage budgets are unaffected.

Percentage budgets (N = percent of total convo tokens, head_pct + tail_pct ≤ 100):
  /cc:begin-pct N    set head as %             (e.g. /cc:begin-pct 10  → head_pct=10)
  /cc:end-pct N      set tail as %             (e.g. /cc:end-pct 20    → tail_pct=20)
  /cc:both-pct N     set both                  (e.g. /cc:both-pct 25   → head_pct=tail_pct=25; N ≤ 50)

Other (the middle is summarized by a model picked automatically from its size):
  /cc:model200k ID   model when the middle fits standard context  (e.g. /cc:model200k sonnet)
  /cc:model1m ID     model when the middle needs 1M context        (e.g. /cc:model1m opus[1m])
  /cc:model ID       alias for /cc:model200k
  /cc:autoresume on|off  after each compact, a daemon types /resume <fork> into your tmux pane (default off)
  /cc:autocompact N [msgs|ctx]  auto-/compact this pane at N tokens (default off; needs daemon + statusline export)
  /cc:continue MSG|off  after an auto-compacted resume, auto-send MSG e.g. continue (default off)
  /cc:show           print current config
  /cc:reset          clear config (defaults restored)
  /cc:help           this help
  [1m] models need the long-context beta / usage credits (opus[1m] works on Claude Max).

Auto-resume needs: /cc:autoresume on + running inside tmux + compacto-resume-daemon.sh running once.
  Off or daemon not running -> nothing auto-resumes; the block message's /resume line is the manual fallback.
  Keyed on $TMUX_PANE, so parallel sessions each resume their own window.

Autonomous loop (UNATTENDED, no cap): /cc:autocompact N + /cc:continue MSG make a pane compact at N
  tokens, auto-resume, then auto-send MSG -- looping with no human in it until you stop the daemon
  (Ctrl-C). Metric is msgs (the statusline number) unless you pass ctx. Needs the statusline export.

Mutex per window (last command wins): /cc:begin clears head_pct; /cc:begin-pct clears head_tokens. Same for tail.
Scope: ~/.claude/precompact.conf is global. Changes apply on the NEXT compact in any session — current, other running, or new. There is no per-session snapshot.
Defaults: head_tokens=0  tail_tokens=25000  model_200k=sonnet  model_1m=opus[1m]  auto_resume=off  auto_compact_at=off  resume_continue=off
Config file: ~/.claude/precompact.conf

Precedence per window (TOKENS beats PCT, env beats file):
  1. env  PRECOMPACT_HEAD_TOKENS / PRECOMPACT_TAIL_TOKENS
  2. file head_tokens / tail_tokens
  3. env  PRECOMPACT_HEAD_PCT    / PRECOMPACT_TAIL_PCT
  4. file head_pct    / tail_pct
  5. PRECOMPACT_WINDOW_TOKENS (legacy symmetric)
  6. built-in default (head=0, tail=25000)
```
