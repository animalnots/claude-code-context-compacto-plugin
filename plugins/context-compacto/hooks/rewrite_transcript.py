"""Token-budgeted JSONL surgery for Claude Code transcripts.

Produces a new session file shaped as:
  head window (≤ WINDOW_TOKEN_BUDGET tokens)  +
  synthetic summary user entry                 +
  tail window (≤ WINDOW_TOKEN_BUDGET tokens)

Original file is never mutated.

Beyond selecting the head/tail windows by token budget, this also:
  - drops middle-zone `attachment` and `file-history-snapshot` entries
  - drops user entries that are pure `<local-command-stdout>` / `<local-command-caveat>` wrappers
  - drops assistant entries that are only `thinking` blocks, and strips
    `thinking` / `redacted_thinking` blocks from any other preserved content
    (empty-text under Opus 4.8 display defaults, their signatures are dead
    weight across a compaction boundary; `image` blocks are kept)
  - caps individual text / tool_result / attachment-file blocks at MAX_BLOCK_TOKENS
  - strips `message.usage` (cached API token counts) and `toolUseResult` metadata
  - reassigns every UUID so Claude Code can't re-hydrate the fork from other session files

Defaults (when no env / config is set): head_tokens=0, tail_tokens=25000.
Both can be overridden via env:
  PRECOMPACT_HEAD_TOKENS / PRECOMPACT_TAIL_TOKENS  (absolute)
  PRECOMPACT_HEAD_PCT    / PRECOMPACT_TAIL_PCT     (percent of total)
  PRECOMPACT_WINDOW_TOKENS                          (legacy symmetric override)
  PRECOMPACT_MAX_BLOCK_TOKENS (default 3000)
"""
import json
import os
import uuid
import datetime
from typing import List, Dict, Optional, Tuple, Callable


MAX_BLOCK_TOKENS = int(os.environ.get("PRECOMPACT_MAX_BLOCK_TOKENS", "3000"))


DEFAULT_HEAD_TOKENS = 0
DEFAULT_TAIL_TOKENS = 25000


def resolve_budgets(total_tokens: int) -> Tuple[int, int]:
    """Resolve head/tail token budgets from env vars at call time.

    Precedence (per window, head and tail resolved independently):
      1. PRECOMPACT_HEAD_TOKENS / PRECOMPACT_TAIL_TOKENS (absolute tokens)
      2. PRECOMPACT_HEAD_PCT / PRECOMPACT_TAIL_PCT (percent of total_tokens)
      3. PRECOMPACT_WINDOW_TOKENS (legacy symmetric fallback)
      4. built-in default: head=0, tail=25000

    Percentage paths need total_tokens from the actual conversation, so
    callers must pass the summed token cost of the (prepared) convo.
    """
    def _resolve(abs_env: str, pct_env: str, default: int) -> int:
        # Note: 0 is an explicit valid value — it means "skip this window".
        # Only empty string / unset falls through to next precedence level.
        if abs_env in os.environ and os.environ[abs_env] != "":
            return max(0, int(os.environ[abs_env]))
        if pct_env in os.environ and os.environ[pct_env] != "":
            pct = float(os.environ[pct_env])
            return max(0, int(total_tokens * pct / 100))
        if os.environ.get("PRECOMPACT_WINDOW_TOKENS"):
            return max(0, int(os.environ["PRECOMPACT_WINDOW_TOKENS"]))
        return default
    return (
        _resolve("PRECOMPACT_HEAD_TOKENS", "PRECOMPACT_HEAD_PCT", DEFAULT_HEAD_TOKENS),
        _resolve("PRECOMPACT_TAIL_TOKENS", "PRECOMPACT_TAIL_PCT", DEFAULT_TAIL_TOKENS),
    )


# Legacy module-level constants — resolved at import assuming total is unknown.
# Callers that know the convo total should prefer `resolve_budgets(total)`.
HEAD_TOKEN_BUDGET, TAIL_TOKEN_BUDGET = resolve_budgets(0)
WINDOW_TOKEN_BUDGET = max(HEAD_TOKEN_BUDGET, TAIL_TOKEN_BUDGET)
IMAGE_TOKEN_COST = 1500  # flat approximation per image block


def _make_token_counter() -> Callable[[str], int]:
    """Prefer tiktoken (GPT cl100k_base, close to Claude's tokenizer). Fall back
    to a conservative 3 chars/token approximation if tiktoken isn't available.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda s: len(enc.encode(s)) if isinstance(s, str) and s else 0
    except Exception:
        return lambda s: ((len(s) + 2) // 3) if isinstance(s, str) and s else 0


_count_tokens = _make_token_counter()


def _entry_tokens(entry: Dict) -> int:
    """Estimate tokens the API sees for this convo entry (after any stripping)."""
    c = entry.get("message", {}).get("content", "")
    if isinstance(c, str):
        return _count_tokens(c)
    if not isinstance(c, list):
        return 0
    total = 0
    for b in c:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            total += _count_tokens(b.get("text", ""))
        elif bt == "tool_result":
            tc = b.get("content", "")
            if isinstance(tc, list):
                for x in tc:
                    if isinstance(x, dict):
                        total += _count_tokens(x.get("text", ""))
            elif isinstance(tc, str):
                total += _count_tokens(tc)
        elif bt == "tool_use":
            total += _count_tokens(json.dumps(b.get("input", {})))
        elif bt == "thinking":
            total += _count_tokens(b.get("thinking", ""))
        elif bt == "image":
            total += IMAGE_TOKEN_COST
    return total


def _load_entries(path: str) -> List[Dict]:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _build_synthetic(template: Dict, parent_uuid: str, summary: str,
                     new_id: str, now_iso: str) -> Dict:
    return {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "promptId": str(uuid.uuid4()),
        "type": "user",
        "message": {
            "role": "user",
            "content": ("[CONTEXT COMPRESSION] The earlier portion of this session's "
                        "transcript was dropped and replaced with the following "
                        "summary:\n\n" + summary),
        },
        "uuid": str(uuid.uuid4()),
        "timestamp": now_iso,
        "permissionMode": template.get("permissionMode", "default"),
        "userType": "external",
        "entrypoint": "cli",
        "cwd": template["cwd"],
        "sessionId": new_id,
        "version": template["version"],
        "gitBranch": template.get("gitBranch", ""),
    }


def _text_of(entry: Dict) -> str:
    c = entry.get("message", {}).get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def _is_command_stdout_artifact(entry: Dict) -> bool:
    """Detect user entries that are purely local-command wrappers (no real user content)."""
    if entry.get("type") != "user":
        return False
    text = _text_of(entry).strip()
    if not text:
        return False
    # Pure stdout wrapper
    if text.startswith("<local-command-stdout>") and text.endswith("</local-command-stdout>"):
        return True
    # Caveat wrapper with trivial follow-up (no real user text)
    if text.startswith("<local-command-caveat>"):
        close_idx = text.find("</local-command-caveat>")
        if close_idx > 0:
            tail = text[close_idx + len("</local-command-caveat>"):].strip()
            # Strip any trailing wrappers/reminders too
            if not tail or tail.startswith("<") or len(tail) < 100:
                return True
    return False


def _is_thinking_only(entry: Dict) -> bool:
    """True for an assistant entry whose content is *only* thinking /
    redacted_thinking blocks. Under Opus 4.8's default thinking display the
    text of these blocks is empty, so all that survives is a large opaque
    signature kept solely to replay-validate the turn on the same model — pure
    weight across a compaction boundary. A thinking block can't be partially
    kept (a blanked signature is a *modified* block the API rejects), so such
    entries are dropped from the fork whole, mirroring native compaction.
    Mixed entries (thinking + text/tool_use) are kept; `_strip_ephemeral`
    removes just the thinking block from those.
    """
    if entry.get("type") != "assistant":
        return False
    c = entry.get("message", {}).get("content", "")
    if not isinstance(c, list) or not c:
        return False
    return all(
        isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
        for b in c
    )


def _truncate_text(s: str, max_tokens: int = MAX_BLOCK_TOKENS) -> str:
    """Cap a string at max_tokens. Uses the active token counter; for non-tiktoken
    fallback it still errs safe by iteratively shrinking until the count fits.
    """
    if _count_tokens(s) <= max_tokens:
        return s
    original_tokens = _count_tokens(s)
    # First cut: scale by ratio with a 10% safety margin
    end = max(1, int(len(s) * max_tokens / max(original_tokens, 1) * 0.9))
    # Fine-tune down if still over budget
    while end > 0 and _count_tokens(s[:end]) > max_tokens:
        end = int(end * 0.9)
        if end < 20:
            end = 0
            break
    return s[:end] + f"\n...[truncated {original_tokens - max_tokens} tokens]"


def _truncate_block(b: Dict) -> Dict:
    """Cap oversized tool_result blocks with a truncation marker.

    Plain text blocks (user prose and assistant prose) are NEVER truncated —
    only tool outputs / attachments which can balloon from a single file read.
    """
    bt = b.get("type")
    if bt == "tool_result":
        tc = b.get("content", "")
        if isinstance(tc, list):
            changed = False
            new_tc = []
            for x in tc:
                if isinstance(x, dict) and "text" in x and _count_tokens(x["text"]) > MAX_BLOCK_TOKENS:
                    new_tc.append({**x, "text": _truncate_text(x["text"])})
                    changed = True
                else:
                    new_tc.append(x)
            if changed:
                return {**b, "content": new_tc}
        elif isinstance(tc, str) and _count_tokens(tc) > MAX_BLOCK_TOKENS:
            return {**b, "content": _truncate_text(tc)}
    return b


def _strip_ephemeral(entry: Dict) -> Dict:
    """Clean a preserved user/assistant entry for the fork.

    Strips metadata only, keeps all content block types (text, tool_use,
    tool_result, thinking, image):
      - `message.usage` — inherited parent-session token counts. Claude Code
        reads these on resume for /context; leaving them in makes a tiny fork
        report the parent session's hundred-k token usage.
      - `toolUseResult` — a top-level duplicate of the tool_result content
        block. Pure bloat.

    Oversized individual text / tool_result blocks are still capped at
    MAX_BLOCK_TOKENS so one huge file read doesn't monopolize the budget, but
    the block's type survives.
    """
    msg = entry.get("message", {})
    c = msg.get("content", "")

    changed = False
    new_msg = msg

    if isinstance(c, list):
        new_c = []
        content_changed = False
        for b in c:
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"):
                # Drop thinking blocks from preserved content: empty-text under
                # Opus 4.8 display defaults, leaving only a large opaque
                # signature that replay-validates on the same model — dead
                # weight here. Can't blank the signature in place (modified
                # block → API reject), so the whole block goes.
                content_changed = True
                continue
            if isinstance(b, dict):
                truncated = _truncate_block(b)
                if truncated is not b:
                    content_changed = True
                new_c.append(truncated)
            else:
                new_c.append(b)
        if content_changed:
            new_msg = dict(msg)
            new_msg["content"] = new_c
            changed = True
    # Plain string-content (typical for user prose) is left untouched.

    if "usage" in new_msg:
        if new_msg is msg:
            new_msg = dict(msg)
        new_msg.pop("usage", None)
        changed = True

    if not changed and "toolUseResult" not in entry:
        return entry

    e2 = dict(entry)
    if new_msg is not msg:
        e2["message"] = new_msg
    e2.pop("toolUseResult", None)
    return e2


def _cap_attachment(entry: Dict) -> Dict:
    """Cap large embedded file content inside preserved-zone attachments."""
    a = entry.get("attachment", {})
    if not isinstance(a, dict):
        return entry
    content = a.get("content", {})
    if not isinstance(content, dict):
        return entry
    file_ = content.get("file")
    if not isinstance(file_, dict):
        return entry
    body = file_.get("content")
    if not isinstance(body, str) or _count_tokens(body) <= MAX_BLOCK_TOKENS:
        return entry
    new_file = {**file_, "content": _truncate_text(body)}
    new_content = {**content, "file": new_file}
    new_attach = {**a, "content": new_content}
    return {**entry, "attachment": new_attach}


def _middle_file_range(entries: List[Dict], tail_of_first: str,
                       head_of_last: str) -> Tuple[Optional[int], Optional[int]]:
    tof_idx = hol_idx = None
    for i, e in enumerate(entries):
        if e.get("type") in ("user", "assistant"):
            u = e.get("uuid")
            if u == tail_of_first:
                tof_idx = i
            elif u == head_of_last:
                hol_idx = i
    return tof_idx, hol_idx


def rewrite(orig_path: str, summary: str, projects_dir: str = None) -> str:
    """Rewrite a Claude Code transcript into a 10/80/10 forked session.

    Returns the new sessionId (uuid4 string). Writes <projects_dir>/<new_id>.jsonl.
    Raises ValueError if the transcript is too short to compress.
    """
    if projects_dir is None:
        projects_dir = os.path.dirname(orig_path)

    entries = _load_entries(orig_path)

    # Capture the latest meaningful custom-title from the source so we can
    # carry the session's name into the fork. Skip auto-generated junk that
    # Claude Code derives from caveat/command wrappers (happens when the first
    # user "message" is a slash-command echo). Strip any existing
    # `— compact MM-DD HH:MM` suffix to avoid stacking on repeated compacts.
    import re as _re
    JUNK_TITLE_PREFIXES = (
        "<local-command-caveat>",
        "<local-command-stdout>",
        "<command-name>",
        "<command-message>",
    )
    source_title = None
    for _e in entries:
        if _e.get("type") == "custom-title":
            _ct = (_e.get("customTitle") or "").strip()
            if _ct and not _ct.startswith(JUNK_TITLE_PREFIXES):
                source_title = _re.sub(
                    r"\s*—\s*compact\s+\d{2}-\d{2}\s+\d{2}:\d{2}\s*$", "", _ct
                ).strip()

    convo_all = [e for e in entries if e.get("type") in ("user", "assistant")]

    # Dedupe by UUID and drop pure command-stdout wrappers up front so window
    # selection sees the same set of entries that will actually be preserved.
    seen = set()
    convo = []
    for e in convo_all:
        u = e.get("uuid")
        if u in seen:
            continue
        if _is_command_stdout_artifact(e) or _is_thinking_only(e):
            continue
        seen.add(u)
        convo.append(e)

    N = len(convo)
    if N < 2:
        raise ValueError(f"transcript too short to compress (N={N})")

    # Measure post-strip, post-cap token cost for each convo entry. Windows are
    # selected on this cost so budgets reflect what Claude will actually see.
    def _prepared(e):
        e = _strip_ephemeral(e)
        msg = e.get("message", {})
        c = msg.get("content", "")
        if isinstance(c, list):
            new_c = [_truncate_block(b) if isinstance(b, dict) else b for b in c]
            if new_c != c:
                e = dict(e)
                e["message"] = {**msg, "content": new_c}
        return e

    # Total convo tokens for percentage resolution — measured BEFORE per-block
    # truncation. Otherwise PRECOMPACT_HEAD_PCT=25 on a convo with large
    # tool_results would resolve to 25% of the post-truncation size, giving
    # much smaller windows than the user intended.
    total_tokens = sum(_entry_tokens(e) for e in convo)

    prepared = [_prepared(e) for e in convo]
    costs = [_entry_tokens(e) for e in prepared]

    head_budget, tail_budget = resolve_budgets(total_tokens)

    # Head window: walk from the front until adding the next entry would exceed
    # head_budget. Always include the first entry even if it alone blows the
    # budget (per-block caps already bound it). head_budget==0 means skip head.
    head_indices: List[int] = []
    if head_budget > 0:
        acc = 0
        for i, cost in enumerate(costs):
            if head_indices and acc + cost > head_budget:
                break
            head_indices.append(i)
            acc += cost

    # Tail window: walk from the back, stopping at the head boundary or budget.
    # tail_budget==0 means skip tail.
    head_set = set(head_indices)
    tail_indices: List[int] = []
    if tail_budget > 0:
        acc = 0
        for i in range(N - 1, -1, -1):
            if i in head_set:
                break
            if tail_indices and acc + costs[i] > tail_budget:
                break
            tail_indices.append(i)
            acc += costs[i]
        tail_indices.reverse()

    if len(head_indices) + len(tail_indices) == N:
        raise ValueError(
            f"transcript fits within head+tail budgets "
            f"(head={head_budget}, tail={tail_budget}); nothing to compress"
        )
    if not head_indices and not tail_indices:
        raise ValueError(
            "both head and tail budgets are 0; nothing to preserve"
        )

    keep_uuids = {convo[i]["uuid"] for i in head_indices + tail_indices}
    tail_of_first = convo[head_indices[-1]]["uuid"] if head_indices else None
    head_of_last_uuid = convo[tail_indices[0]]["uuid"] if tail_indices else None

    tof_idx, hol_idx = _middle_file_range(entries, tail_of_first, head_of_last_uuid)
    # If head is empty, the middle zone starts at file beginning (no TOF marker).
    # If tail is empty, the middle zone extends to file end (no HOL marker).
    if not head_indices:
        tof_idx = -1
    if not tail_indices:
        hol_idx = len(entries)

    new_id = str(uuid.uuid4())
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    synthetic = _build_synthetic(convo[0], tail_of_first, summary, new_id, now_iso)

    out: List[Dict] = []
    inserted = False
    emitted_convo_uuids = set()
    emitted_attach_uuids = set()
    for i, e in enumerate(entries):
        t = e.get("type")

        # Drop middle user/assistant entries AND dedupe: keep only one per UUID
        if t in ("user", "assistant"):
            u = e.get("uuid")
            if u not in keep_uuids:
                continue
            if u in emitted_convo_uuids:
                continue
            emitted_convo_uuids.add(u)

        # Drop attachments and file-history-snapshots in the middle window (by file position)
        if (tof_idx is not None and hol_idx is not None
                and tof_idx < i < hol_idx
                and t in ("attachment", "file-history-snapshot")):
            continue

        # Dedupe attachments / file-history-snapshots by UUID
        if t in ("attachment", "file-history-snapshot"):
            u = e.get("uuid")
            if u:
                if u in emitted_attach_uuids:
                    continue
                emitted_attach_uuids.add(u)

        # Drop pure local-command stdout/caveat wrapper user entries everywhere
        if t == "user" and _is_command_stdout_artifact(e):
            continue

        # Drop all source custom-title entries — we append a fresh one at the end
        if t == "custom-title":
            continue

        # Strip image/thinking blocks from preserved user/assistant content
        if t in ("user", "assistant"):
            e = _strip_ephemeral(e)
        # Cap embedded file content in preserved-zone attachments
        if t == "attachment":
            e = _cap_attachment(e)

        e2 = dict(e)
        if "sessionId" in e2:
            e2["sessionId"] = new_id
        if t in ("user", "assistant") and e.get("uuid") == head_of_last_uuid and not inserted:
            out.append(synthetic)
            e2["parentUuid"] = synthetic["uuid"]
            inserted = True
        out.append(e2)

    if not inserted:
        # No head_of_last match during walk — happens when tail is empty
        # (nothing to insert before). Append synth at the end; chain repair
        # below will link it to the preceding convo entry.
        out.append(synthetic)
        inserted = True

    # Repair the parentUuid chain so Claude Code can walk it on resume.
    # Claude Code's chain can reference attachments as parents in a native
    # transcript, but we need a valid convo-to-convo chain in the fork because
    # middle-zone attachments are dropped. Relink any convo entry whose parent
    # isn't another convo in the fork to the immediately preceding convo. The
    # first convo entry becomes the root (parentUuid=None).
    convo_uuids_in_fork = {
        e.get("uuid") for e in out
        if e.get("type") in ("user", "assistant") and e.get("uuid")
    }
    prev_convo_uuid = None
    for e in out:
        if e.get("type") not in ("user", "assistant"):
            continue
        p = e.get("parentUuid")
        if p is None or p in convo_uuids_in_fork:
            prev_convo_uuid = e.get("uuid")
            continue
        e["parentUuid"] = prev_convo_uuid  # None if no prior convo
        prev_convo_uuid = e.get("uuid")

    # Reassign every UUID to a fresh value. Claude Code resolves missing
    # parentUuids by searching other session files in the project directory;
    # if any UUID in the fork matches an entry in the original transcript, the
    # resume pulls in the full original chain and the compression is undone.
    # Remapping UUIDs makes the fork fully self-contained.
    uuid_map = {}
    for e in out:
        u = e.get("uuid")
        if u and u not in uuid_map:
            uuid_map[u] = str(uuid.uuid4())
    for e in out:
        if e.get("uuid") in uuid_map:
            e["uuid"] = uuid_map[e["uuid"]]
        p = e.get("parentUuid")
        if p is not None:
            e["parentUuid"] = uuid_map.get(p)  # None if parent wasn't in fork

    # Stamp the fork with a human-readable title so it's easy to find in `claude -r`.
    # Claude Code's picker shows the latest custom-title, so appending one at the
    # end wins over any earlier titles preserved from the source transcript.
    # Preserve the source session's name (if any) as a prefix.
    stamp = datetime.datetime.now().strftime("%m-%d %H:%M")
    fork_title = (
        f"{source_title} — compact {stamp}" if source_title else f"compact {stamp}"
    )
    out.append({
        "type": "custom-title",
        "customTitle": fork_title,
        "sessionId": new_id,
    })

    new_path = os.path.join(projects_dir, f"{new_id}.jsonl")
    with open(new_path, "w", encoding="utf-8") as f:
        for e in out:
            f.write(json.dumps(e) + "\n")

    return new_id


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: rewrite_transcript.py <orig_jsonl> <summary_file>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[2], "r", encoding="utf-8") as f:
        summary_text = f.read()
    new_id = rewrite(sys.argv[1], summary_text)
    print(new_id)
