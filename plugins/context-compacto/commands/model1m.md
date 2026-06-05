---
description: Set the 1M-context summarizer model — used when the middle is too big for 200k. E.g. /cc:model1m opus[1m]. Note [1m] models need the long-context beta / usage credits.
argument-hint: "MODEL"
---

Print ONLY the output below verbatim. No commentary, no preamble, no trailing remarks.

```
!`python "${CLAUDE_PLUGIN_ROOT}/hooks/pcconf.py" model1m $ARGUMENTS`
```
