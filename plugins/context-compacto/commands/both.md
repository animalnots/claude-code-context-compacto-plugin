---
description: Set head AND tail window token budget (tiktoken count; real context ~1.5-1.66x N). E.g. /cc:both 15000.
argument-hint: "N tokens (real ~1.5-1.66x)"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" both $ARGUMENTS`
```
