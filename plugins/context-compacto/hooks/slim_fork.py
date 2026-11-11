#!/usr/bin/env python3
"""What compacto-resume-daemon.sh runs for a session at the context limit, where no turn, so no handoff, can run.

  slim_fork.py at-limit <transcript> <epoch>
      exits 0 when the session's last reply is Claude Code's error for a prompt over the context limit, written
      at <epoch> or later, else 1. Claude Code ends such a turn without a Stop, so a handoff refused that way
      would otherwise stay "running" for good.
  slim_fork.py make <transcript>
      writes a slim copy of the session beside it (rewrite_transcript.slim), links its subagent transcripts in,
      and prints the copy's session id. The daemon resumes it and hands off there instead.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rewrite_transcript  # noqa: E402

LIMIT_ERROR = "prompt is too long"


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def at_limit(path, since):
    e = rewrite_transcript.last_entry(path, lambda e: e.get("type") == "assistant" and not e.get("isSidechain"))
    if not (e and e.get("isApiErrorMessage") and LIMIT_ERROR in rewrite_transcript._text_of(e).lower()):
        return False
    at = _epoch(e.get("timestamp"))
    return at is not None and at >= since


def main(argv):
    try:
        if len(argv) == 3 and argv[0] == "at-limit":
            return 0 if at_limit(argv[1], float(argv[2])) else 1
        if len(argv) == 2 and argv[0] == "make":
            new_id = rewrite_transcript.slim(argv[1])
            try:
                rewrite_transcript.carry_subagents(argv[1], new_id)
            except OSError as e:
                print(f"slim_fork: {new_id} can't message the session's subagents: {e}", file=sys.stderr)
            print(new_id)
            return 0
    except Exception as e:
        print(f"slim_fork: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
