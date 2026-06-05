---
description: Set the standard-context (≤200k) summarizer model — used when the compressed middle fits a normal context window. E.g. /cc:model200k sonnet.
argument-hint: "MODEL"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" model200k $ARGUMENTS`
```
