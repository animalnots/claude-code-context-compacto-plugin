#!/usr/bin/env python3
"""SessionStart hook: one-time post-install notice for context-compacto.

Claude Code has no true "post-install" hook, so this fires on SessionStart and
shows a single `systemMessage` (a user-visible field) the first time it runs on
a machine that has no precompact config yet — i.e. a fresh install. Returning
users (who already have ~/.claude/precompact.conf) and subsequent sessions are
skipped via a marker file.

It is intentionally bulletproof: stdin is drained, every error is swallowed, and
it always exits 0, so it can never disrupt session start.
"""
import json
import pathlib
import sys

# Drain the SessionStart payload on stdin (unused) so the CLI never blocks.
try:
    sys.stdin.read()
except Exception:
    pass

try:
    claude_dir = pathlib.Path.home() / ".claude"
    marker = claude_dir / ".cc-notice-shown"
    conf = claude_dir / "precompact.conf"
    # Show once, and only for a fresh install (no config yet). A pre-existing
    # config means a returning user / plugin update, who already knows the tool.
    if not marker.exists() and not conf.exists():
        msg = (
            "context-compacto active: /compact now keeps the last 25k tokens verbatim "
            "(head=0) and summarizes the middle (sonnet; opus[1m] for oversized middles). "
            "Tune with /cc:end /cc:begin /cc:model200k /cc:model1m, or see /cc:help. After "
            "compacting, load the compressed session in this chat with the printed "
            "`/resume <id>` (or run `/resume` and pick the newest `compact …` entry)."
        )
        print(json.dumps({"systemMessage": msg}))
        claude_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown\n", encoding="utf-8")
except Exception:
    pass

sys.exit(0)
