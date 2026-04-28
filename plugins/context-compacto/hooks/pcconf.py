#!/usr/bin/env python3
"""Cross-platform precompact config helper.

Invoked by the begin / end / both / begin-pct / end-pct / both-pct / model /
show / reset / help slash-command files (each lives under the `cc` plugin, so
they're typed as /cc:begin /cc:end etc.). Reads & writes the persistent config
at ~/.claude/precompact.conf.

Usage:
  python pcconf.py <cmd> [arg]
    begin N      head_tokens = N         (raw token count; clears head_pct)
    end N        tail_tokens = N         (raw token count; clears tail_pct)
    both N       head_tokens = tail_tokens = N  (clears head_pct, tail_pct)
    begin-pct N  head_pct = N            (percent of total; clears head_tokens)
    end-pct N    tail_pct = N            (percent of total; clears tail_tokens)
    both-pct N   head_pct = tail_pct = N (clears head_tokens, tail_tokens; N ≤ 50)
    model MODEL  summarizer model id
    show         print the current config (or defaults note)
    reset        delete the config file
    help         print usage

Percent commands enforce: 0 ≤ N ≤ 100 and head_pct + tail_pct ≤ 100.
"""
import pathlib
import sys


# Force UTF-8 on stdout/stderr so Windows consoles (cp1252 default) don't
# choke on Unicode characters in the help text (→, ≤, etc.).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


CONF = pathlib.Path.home() / ".claude" / "precompact.conf"


def _read_lines():
    if not CONF.exists():
        return []
    return CONF.read_text(encoding="utf-8").splitlines()


def _write_lines(lines):
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _set(key, val):
    lines = [ln for ln in _read_lines() if not ln.startswith(key + "=")]
    lines.append(f"{key}={val}")
    _write_lines(lines)


def _clear(key):
    if not CONF.exists():
        return
    lines = [ln for ln in _read_lines() if not ln.startswith(key + "=")]
    _write_lines(lines)


def _read_conf():
    """Return current conf as {key: str_value}."""
    out = {}
    for ln in _read_lines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_pct(arg, cmd):
    """Validate a percentage arg. Return (n, error_message). Error is None on success."""
    if arg is None:
        return None, f"usage: /cc:{cmd} N  (percent of total convo tokens)"
    try:
        n = int(arg)
    except ValueError:
        return None, f"/cc:{cmd}: N must be an integer percent (got {arg!r})"
    if n < 0 or n > 100:
        return None, f"/cc:{cmd}: N must be in 0..100 (got {n})"
    return n, None


def _check_sum(side_key, n):
    """Ensure head_pct + tail_pct ≤ 100 with `side_key` set to n. Return error string or None."""
    other_key = "tail_pct" if side_key == "head_pct" else "head_pct"
    other_cmd = "end-pct" if other_key == "tail_pct" else "begin-pct"
    conf = _read_conf()
    try:
        other = int(conf.get(other_key, "0") or "0")
    except ValueError:
        other = 0
    total = n + other
    if total > 100:
        return (f"head_pct + tail_pct must be ≤ 100; "
                f"setting {side_key}={n} with existing {other_key}={other} would sum to {total}. "
                f"Lower {other_key} first (e.g. /cc:{other_cmd} {100 - n}).")
    return None


def _show():
    if CONF.exists():
        sys.stdout.write(CONF.read_text(encoding="utf-8"))
    else:
        print("(defaults: head_tokens=0  tail_tokens=30000  model=sonnet[1m])")


def _reset():
    if CONF.exists():
        CONF.unlink()
    print("config cleared (defaults restored)")


HELP = """Precompact slash commands (all under the /cc: plugin namespace)

Absolute token budgets (N = raw token count):
  /cc:begin N        set head-window           (e.g. /cc:begin 25000  -> head_tokens=25000)
  /cc:end N          set tail-window           (e.g. /cc:end 30000    -> tail_tokens=30000)
  /cc:both N         set both head and tail    (e.g. /cc:both 15000)

Percentage budgets (N = percent of total convo tokens, head_pct + tail_pct ≤ 100):
  /cc:begin-pct N    set head as %             (e.g. /cc:begin-pct 10  -> head_pct=10)
  /cc:end-pct N      set tail as %             (e.g. /cc:end-pct 20    -> tail_pct=20)
  /cc:both-pct N     set both                  (e.g. /cc:both-pct 25   -> head_pct=tail_pct=25; N ≤ 50)

Other:
  /cc:model MODEL    set summarizer model      (e.g. /cc:model opus[1m])
  /cc:show           print current config
  /cc:reset          clear config (defaults restored)
  /cc:help           this help

Mutex per window (last command wins): /cc:begin clears head_pct; /cc:begin-pct clears head_tokens.
  Mixing modes ACROSS windows is fine (e.g. head_tokens=20000 + tail_pct=15).

Scope: ~/.claude/precompact.conf is GLOBAL. Edits take effect on the NEXT compact
  in any session — current, other running sessions, or future ones. No per-session
  snapshot. Env vars (if set) override the file for that shell.

Defaults (no config, no env): head_tokens=0  tail_tokens=30000  model=sonnet[1m]
Config file: ~/.claude/precompact.conf

Precedence per window (head and tail resolved independently):
  TOKENS beats PCT regardless of where each comes from. Within each, env beats file.

  1. env  PRECOMPACT_HEAD_TOKENS / PRECOMPACT_TAIL_TOKENS
  2. file head_tokens / tail_tokens
  3. env  PRECOMPACT_HEAD_PCT    / PRECOMPACT_TAIL_PCT
  4. file head_pct    / tail_pct
  5. PRECOMPACT_WINDOW_TOKENS (legacy symmetric, env or file)
  6. built-in default (head=0, tail=30000)
"""


def main():
    if len(sys.argv) < 2:
        cmd = "show"
        arg = None
    else:
        cmd = sys.argv[1]
        arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "begin":
        if arg is None:
            sys.stdout.write("usage: /cc:begin N  (raw token count, e.g. /cc:begin 30000 keeps 30000 head tokens)\n")
            return
        _set("head_tokens", str(int(arg)))
        _clear("head_pct")
    elif cmd == "end":
        if arg is None:
            sys.stdout.write("usage: /cc:end N  (raw token count, e.g. /cc:end 30000 keeps 30000 tail tokens)\n")
            return
        _set("tail_tokens", str(int(arg)))
        _clear("tail_pct")
    elif cmd == "both":
        if arg is None:
            sys.stdout.write("usage: /cc:both N  (raw token count, e.g. /cc:both 15000 sets head+tail to 15000 each)\n")
            return
        _set("head_tokens", str(int(arg)))
        _set("tail_tokens", str(int(arg)))
        _clear("head_pct")
        _clear("tail_pct")
    elif cmd == "begin-pct":
        n, err = _parse_pct(arg, "begin-pct")
        if err:
            sys.stdout.write(err + "\n"); return
        sum_err = _check_sum("head_pct", n)
        if sum_err:
            sys.stdout.write(sum_err + "\n"); return
        _set("head_pct", str(n))
        _clear("head_tokens")
    elif cmd == "end-pct":
        n, err = _parse_pct(arg, "end-pct")
        if err:
            sys.stdout.write(err + "\n"); return
        sum_err = _check_sum("tail_pct", n)
        if sum_err:
            sys.stdout.write(sum_err + "\n"); return
        _set("tail_pct", str(n))
        _clear("tail_tokens")
    elif cmd == "both-pct":
        n, err = _parse_pct(arg, "both-pct")
        if err:
            sys.stdout.write(err + "\n"); return
        if 2 * n > 100:
            sys.stdout.write(f"/cc:both-pct: N must be ≤ 50 so head_pct+tail_pct ≤ 100 (got {n}, would sum to {2*n})\n")
            return
        _set("head_pct", str(n)); _set("tail_pct", str(n))
        _clear("head_tokens"); _clear("tail_tokens")
    elif cmd == "model":
        _set("model", arg if arg is not None else "sonnet[1m]")
    elif cmd == "show":
        _show()
        return
    elif cmd == "reset":
        _reset()
        return
    elif cmd == "help":
        sys.stdout.write(HELP)
        return
    else:
        sys.stderr.write("usage: pcconf.py begin|end|both|begin-pct|end-pct|both-pct N | model MODEL | show | reset | help\n")
        sys.exit(1)

    # mutating commands fall through and print the resulting config
    _show()


if __name__ == "__main__":
    main()
