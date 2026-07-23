---
description: Arm/disarm tmux auto-resume. When on, after each compaction the resume daemon types `/resume <fork-id>` into this pane so you never resume by hand. Off by default; needs tmux + compacto-resume-daemon.sh. Usage: /cc:autoresume on|off
argument-hint: "on|off"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" autoresume $ARGUMENTS`
```
