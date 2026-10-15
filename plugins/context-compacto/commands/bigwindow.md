---
description: Set Claude Code's own auto-compact window for every session, running ones too (CLAUDE_CODE_AUTO_COMPACT_WINDOW in ~/.claude/settings.json; N = 100k..1M, e.g. 600k). Prints the subagent cutoff and panes at or over it; no N shows the value, off removes it. Usage: /cc:bigwindow N | off
argument-hint: "N (100k..1M) | off"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" bigwindow $ARGUMENTS`
```
