#!/usr/bin/env python3
"""What compacto-resume-daemon.sh runs for a session at the context limit, where no turn, so no handoff, can run.

  slim_fork.py at-limit <transcript> <epoch>
      exits 0 when the session's last reply is Claude Code's error for a prompt over the context limit, written
      at <epoch> or later, else 1. Claude Code ends such a turn without a Stop, so a handoff refused that way
      would otherwise stay "running" for good.
  slim_fork.py make <transcript>
      writes a slim copy of the session beside it (rewrite_transcript.slim), links its subagent transcripts in,
      and prints the copy's session id. The daemon resumes it and hands off there instead.
  slim_fork.py no-tool-running <transcript>
      exits 0 when every tool call in the session's last reply has its result, so an Esc stops only Claude's
      next request, else 1, which includes a transcript that tells nothing.
  slim_fork.py newest-prompt-id <transcript>
      prints the prompt_id on the session's newest prompt or tool result, which names the turn that runs, or
      else the last one that ran, and exits 0, else 1, which includes a transcript that tells nothing.
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


def no_tool_running(path):
    # Claude Code writes each block of a reply as its own entry, and sends the next request only once every call
    # in a reply has its result, so only the last reply can have a call still running.
    answered, reply, seen = set(), None, False
    for e in rewrite_transcript.entries_from_end(path):
        if e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
            continue
        message = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if e["type"] == "user":
            ids = {b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"}
            if not ids:
                return True
            answered |= ids
            continue
        if seen and message.get("id") != reply:
            return True
        seen, reply = True, message.get("id")
        if any(b.get("type") == "tool_use" and b.get("id") not in answered for b in blocks):
            return False
    return seen


def newest_prompt_id(path):
    # Claude Code stamps each main-thread user entry, prompt or tool result, with its turn's prompt_id; replies carry
    # none. The newest one alone decides: an older entry's id can name a turn that has ended.
    try:
        for e in rewrite_transcript.entries_from_end(path):
            if e.get("type") == "user" and not e.get("isSidechain"):
                prompt = e.get("promptId")
                return prompt if isinstance(prompt, str) and prompt else None
    except OSError:
        pass
    return None


def main(argv):
    try:
        if len(argv) == 3 and argv[0] == "at-limit":
            return 0 if at_limit(argv[1], float(argv[2])) else 1
        if len(argv) == 2 and argv[0] == "no-tool-running":
            return 0 if no_tool_running(argv[1]) else 1
        if len(argv) == 2 and argv[0] == "newest-prompt-id":
            prompt = newest_prompt_id(argv[1])
            if prompt is None:
                return 1
            print(prompt)
            return 0
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
