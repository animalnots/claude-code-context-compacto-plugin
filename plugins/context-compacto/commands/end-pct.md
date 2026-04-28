---
description: Set tail-window as percent of total tokens. E.g. /cc:end-pct 20. head_pct + tail_pct ≤ 100.
argument-hint: "N (0..100)"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" end-pct $ARGUMENTS`
```
