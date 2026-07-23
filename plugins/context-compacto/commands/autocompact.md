---
description: Auto-/compact this pane once its context reaches N tokens (msgs metric by default; pass ctx for raw context). Off by default; needs the resume daemon running + the statusline context export. Usage: /cc:autocompact N [msgs|ctx] | off
argument-hint: "N [msgs|ctx] | off"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" autocompact $ARGUMENTS`
```
