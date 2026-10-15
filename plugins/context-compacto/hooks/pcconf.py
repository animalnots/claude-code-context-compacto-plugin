#!/usr/bin/env python3
"""Cross-platform precompact config helper.

Invoked by the begin / end / both / begin-pct / end-pct / both-pct / model /
model200k / model1m / show / reset / help slash-command files (each lives under
the `cc` plugin, so they're typed as /cc:begin /cc:end etc.). Reads & writes the
persistent config at ~/.claude/precompact.conf, and for bigwindow ~/.claude/settings.json.

Usage:
  python pcconf.py <cmd> [arg]
    begin N      head_tokens = N         (raw token count; clears head_pct)
    end N        tail_tokens = N         (raw token count; clears tail_pct)
    both N       head_tokens = tail_tokens = N  (clears head_pct, tail_pct)
    begin-pct N  head_pct = N            (percent of total; clears head_tokens)
    end-pct N    tail_pct = N            (percent of total; clears tail_tokens)
    both-pct N   head_pct = tail_pct = N (clears head_tokens, tail_tokens; N ≤ 50)
    model200k ID standard-context summarizer model (clears legacy `model`)
    model1m ID   1M-context summarizer model
    model MODEL  alias for model200k
    autoresume on|off  arm/disarm the tmux auto-resume signal (default off)
    autocompact N [msgs|ctx]  auto-/compact a pane once it reaches N tokens (default 300000; off stores 0)
    continue MSG|off  message auto-sent after an auto-compacted resume, e.g. continue (default off)
    bigwindow N|off  Claude Code's auto-compact window, CLAUDE_CODE_AUTO_COMPACT_WINDOW in
                 ~/.claude/settings.json (N = 100k..1M); no N reports it
    show         print the current config (or defaults note)
    reset        delete the config file
    help         print usage

Percent commands enforce: 0 ≤ N ≤ 100 and head_pct + tail_pct ≤ 100.
"""
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import handoff


# Force UTF-8 on stdout/stderr so Windows consoles (cp1252 default) don't
# choke on Unicode characters in the help text (→, ≤, etc.).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


CONF = pathlib.Path.home() / ".claude" / "precompact.conf"

# Threshold in force when the file has no auto_compact_at. compacto-resume-daemon.sh
# falls back to the same number on its own; change both together.
AUTO_COMPACT_DEFAULT = 300000

SETTINGS = pathlib.Path.home() / ".claude" / "settings.json"
WINDOW_VAR = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
# What Claude Code's /autocompact accepts. Claude Code raises a smaller window to its minimum,
# which precompact.py's subagent rule wouldn't know.
WINDOW_MIN, WINDOW_MAX = 100000, 1000000
WINDOW_USAGE = "usage: /cc:bigwindow N | off   (N = 100k..1M, e.g. 600k)"


def _read_lines():
    if not CONF.exists():
        return []
    return CONF.read_text(encoding="utf-8").splitlines()


def _replace_file(path, text):
    # Through the real path, so a symlinked settings.json stays a symlink, and with the old mode,
    # since settings.json can hold API keys in its env.
    path = pathlib.Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _write_lines(lines):
    # The daemon re-reads this file every second, and a half-written one would read as
    # "no threshold", which means the default rather than off. Write aside, then swap in.
    _replace_file(CONF, "\n".join(lines) + ("\n" if lines else ""))


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
        print("(defaults: head_tokens=0  tail_tokens=25000  model_200k=sonnet  model_1m=opus[1m])")
    # auto_resume and resume_continue are "off" by being ABSENT from the file, and an
    # absent auto_compact_at means the default threshold, so dumping the file alone
    # can't tell what is in force. Always spell them out.
    conf = _read_conf()
    print("automation:  auto_resume={}  auto_compact_at={} ({})  resume_continue={}".format(
        "on" if conf.get("auto_resume") else "off",
        _auto_compact_state(conf),
        conf.get("auto_compact_metric") or "msgs",
        conf.get("resume_continue") or "off",
    ))


def _auto_compact_state(conf):
    """The threshold in force: no value means the default, a stored 0 means off."""
    cur = conf.get("auto_compact_at")
    if not cur:
        return "{} (default)".format(AUTO_COMPACT_DEFAULT)
    return "off" if cur == "0" else cur


def _report_state(key, usage, extra=None, state=None):
    """Bare command → report current state + usage, and change nothing.

    Treating a missing argument as "off" meant a bare word silently disarmed an
    automation, and since off is stored as an absent key, the config printed
    afterwards showed no trace of it either.
    """
    if state is None:
        cur = _read_conf().get(key)
        state = "off" if not cur else ("on" if cur == "1" else cur)
    print("{} is {}".format(key, state))
    if extra:
        print(extra)
    print(usage)


def _reset():
    if CONF.exists():
        CONF.unlink()
    print("config cleared (defaults restored)")


def _parse_window(arg):
    """Tokens for 600000, 600k, 1M or a bare 100..1000 (thousands), as /autocompact reads them; None if invalid."""
    m = re.fullmatch(r"([0-9]{1,9})([km]?)", arg)
    if not m:
        return None
    n = int(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2)]
    if not m.group(2) and 100 <= n <= 1000:
        n *= 1000
    return n if WINDOW_MIN <= n <= WINDOW_MAX else None


def _load_settings():
    """(settings, None), with a missing file read as {}, or (None, why it can't be used)."""
    try:
        text = SETTINGS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        return None, f"can't be read ({e})"
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"isn't valid JSON ({e})"
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        return None, "isn't a JSON object with an env object"
    return data, None


def _save_settings(data):
    """None once written, or why it couldn't be."""
    try:
        _replace_file(SETTINGS, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as e:
        return f"can't be written ({e})"
    return None


def _subagent_ceiling(window):
    # precompact.py's subagent rule on the same inputs; change both together.
    try:
        pct = min(max(int(os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "100")), 1), 100)
    except ValueError:
        pct = 100
    trigger = window * pct // 100
    return math.ceil(min(trigger * 0.85, trigger - 60000))


def _window_names():
    """{pane id: window name} for the panes tmux knows, or None when it can't be asked."""
    tmux = shlex.split(os.environ.get("COMPACTO_TMUX") or "tmux")
    if not (tmux and shutil.which(tmux[0])):
        return None
    try:
        r = subprocess.run(tmux + ["list-panes", "-a", "-F", "#{pane_id}\t#{window_name}"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    return dict(ln.split("\t", 1) for ln in r.stdout.splitlines() if "\t" in ln)


def _panes_over(limit):
    """(pane, window name, context) for each pane whose statusline export is at or over limit."""
    names = _window_names()
    over = []
    for f in sorted(handoff.signal_dir().glob("*.ctx")):
        try:
            fields = f.read_text(encoding="utf-8").strip().split("\t")
            pane, tokens = fields[0], int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        # A 4th field is a psmux server pid, and those panes aren't in this tmux's list.
        local = names is not None and len(fields) < 4
        if local and pane not in names:
            continue
        if tokens >= limit:
            over.append((pane, names[pane] if local else "", tokens))
    return over


def _report_window(value):
    if not value:
        print(f"bigwindow is off ({WINDOW_VAR} isn't set in ~/.claude/settings.json)")
        return
    try:
        window = int(value)
    except ValueError:
        print(f"bigwindow is {value!r} in ~/.claude/settings.json, which isn't a plain token count, "
              "so Claude Code and this plugin ignore it")
        return
    print(f"bigwindow is {window} ({WINDOW_VAR} in ~/.claude/settings.json; running sessions pick it up too)")
    print("Claude Code compacts on its own about 25-33k below that.")
    ceiling = _subagent_ceiling(window)
    if ceiling <= 0:
        print("That's too small for the plugin to tell a subagent's compaction from the main session's, "
              "so it blocks both.")
        return
    print(f"A subagent's compaction runs natively while its main session is under {ceiling}.")
    over = _panes_over(ceiling)
    if not over:
        print(f"Panes at or over {ceiling}: none")
        return
    print(f"Panes at or over {ceiling}, whose subagents can't compact:")
    for pane, name, tokens in over:
        print(f"  {pane}  {name}  {tokens}" if name else f"  {pane}  {tokens}")


def _bigwindow(arg):
    val = (arg or "").strip().lower()
    window = None
    if val and val != "off":
        window = _parse_window(val)
        if window is None:
            print(f"/cc:bigwindow: {arg!r} isn't a size from 100k to 1M; nothing changed")
            print(WINDOW_USAGE)
            return
    settings, err = _load_settings()
    if not err and val:
        env = settings.get("env", {})
        want = str(window) if window else None
        if env.get(WINDOW_VAR) != want:
            if want:
                settings.setdefault("env", {})[WINDOW_VAR] = want
            else:
                del env[WINDOW_VAR]
                if not env:
                    del settings["env"]
            err = _save_settings(settings)
    if err:
        print(f"/cc:bigwindow: ~/.claude/settings.json {err}; nothing changed")
        return
    _report_window(settings.get("env", {}).get(WINDOW_VAR))
    if val == "off":
        print("Claude Code goes back to /autocompact, autoCompactWindow or the model's default, and the "
              "plugin can't tell a subagent's compaction from the main session's, so it blocks both.")
    elif not val:
        print(WINDOW_USAGE)


HELP = """Precompact slash commands (all under the /cc: plugin namespace)

Absolute token budgets (N = raw tiktoken count):
  /cc:begin N        set head-window           (e.g. /cc:begin 25000  -> head_tokens=25000)
  /cc:end N          set tail-window           (e.g. /cc:end 30000    -> tail_tokens=30000)
  /cc:both N         set both head and tail    (e.g. /cc:both 15000)
  N is a tiktoken count; Claude's own tokenizer runs ~1.5-1.66x higher on
  code/tool-heavy sessions, so N preserves ~1.5-1.66xN tokens of real context
  (set N to ~0.6x of a target real size). Percentage budgets are unaffected.

Percentage budgets (N = percent of total convo tokens, head_pct + tail_pct ≤ 100):
  /cc:begin-pct N    set head as %             (e.g. /cc:begin-pct 10  -> head_pct=10)
  /cc:end-pct N      set tail as %             (e.g. /cc:end-pct 20    -> tail_pct=20)
  /cc:both-pct N     set both                  (e.g. /cc:both-pct 25   -> head_pct=tail_pct=25; N ≤ 50)

Other (the middle is summarized by a model picked automatically from its size):
  /cc:model200k ID   model when the middle fits standard context  (e.g. /cc:model200k sonnet)
  /cc:model1m ID     model when the middle needs 1M context        (e.g. /cc:model1m opus[1m])
  /cc:model ID       alias for /cc:model200k
  /cc:autoresume on|off  after each compact, a daemon types /resume <fork> into your tmux pane (default off)
  /cc:autocompact N [msgs|ctx]  auto-/compact this pane at N tokens; needs the daemon + statusline export (default 300000; off disables)
  /cc:continue MSG|off  after an auto-compacted resume, auto-send MSG (e.g. continue) so the task keeps going (default off)
  /cc:bigwindow N|off  Claude Code's own auto-compact window, every session (e.g. /cc:bigwindow 600k; N = 100k..1M)
  /cc:show           print current config
  /cc:reset          clear config (defaults restored)
  /cc:help           this help

The automation commands with NO argument report what they're set to and change
  nothing -- /cc:autoresume, /cc:autocompact, /cc:continue, /cc:bigwindow.

Note: [1m] models need the long-context beta / usage credits. On Claude Max,
  opus[1m] works; sonnet[1m] needs usage credits; standard models need neither.

Auto-resume: /cc:autoresume on only ARMS the signal. It also requires (1) the
  session to run inside tmux and (2) compacto-resume-daemon.sh running once in
  the background. Off or daemon-not-running -> nothing auto-resumes; the block
  message's /resume line is the manual fallback. Keyed on $TMUX_PANE, so any
  number of parallel sessions each resume their own window.

Autonomous loop (UNATTENDED, no cap): /cc:autocompact N + /cc:continue MSG make a
  pane compact at N tokens, auto-resume, then auto-send MSG -- looping with no
  human in it until you stop the daemon (Ctrl-C). Metric is msgs (the statusline
  number) unless you pass ctx. Needs the statusline context export (see README).
  Watch your usage: it runs until stopped.
  A ! anywhere in a tmux window's name exempts that window from auto-compact
  (re-read every poll; a /compact you type there is still auto-resumed).
  A = instead (no !) hands off first: at min(600000, 2 x N) the daemon types
  /compact-handoff (handoff_command=NAME in the config file), waits for that turn
  to finish, then types /compact. While a /compact-handoff you typed is unfinished,
  /compact is refused once (repeat it within 2 minutes to compact anyway).

Window markers (daemon running, inside tmux): the window option @cc_state is
  ? when a dialog waits for you, $ when Claude finished while you weren't viewing
  that window, & when it's idle but background agents/shells still run. Add
  #{@cc_state} to window-status-format to see it (see README).

Big window: /cc:bigwindow N writes CLAUDE_CODE_AUTO_COMPACT_WINDOW into
  ~/.claude/settings.json, and running sessions pick it up too. A subagent's
  compaction runs natively while its main session is under min(0.85 x N, N - 60000);
  with it off, the plugin blocks those as well. While it's set, Claude Code's own
  /autocompact can't change the window.

Mutex per window (last command wins): /cc:begin clears head_pct; /cc:begin-pct clears head_tokens.
  Mixing modes ACROSS windows is fine (e.g. head_tokens=20000 + tail_pct=15).

Scope: ~/.claude/precompact.conf is GLOBAL. Edits take effect on the NEXT compact
  in any session — current, other running sessions, or future ones. No per-session
  snapshot. Env vars (if set) override the file for that shell.

Defaults (no config, no env): head_tokens=0  tail_tokens=25000  model_200k=sonnet  model_1m=opus[1m]  auto_resume=off  auto_compact_at=300000  resume_continue=off
Config file: ~/.claude/precompact.conf

Precedence per window (head and tail resolved independently):
  TOKENS beats PCT regardless of where each comes from. Within each, env beats file.

  1. env  PRECOMPACT_HEAD_TOKENS / PRECOMPACT_TAIL_TOKENS
  2. file head_tokens / tail_tokens
  3. env  PRECOMPACT_HEAD_PCT    / PRECOMPACT_TAIL_PCT
  4. file head_pct    / tail_pct
  5. PRECOMPACT_WINDOW_TOKENS (legacy symmetric, env or file)
  6. built-in default (head=0, tail=25000)
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
            sys.stdout.write("usage: /cc:begin N  (N = tiktoken count; real context ~1.5-1.66x N. e.g. /cc:begin 30000)\n")
            return
        _set("head_tokens", str(int(arg)))
        _clear("head_pct")
    elif cmd == "end":
        if arg is None:
            sys.stdout.write("usage: /cc:end N  (N = tiktoken count; real context ~1.5-1.66x N. e.g. /cc:end 30000)\n")
            return
        _set("tail_tokens", str(int(arg)))
        _clear("tail_pct")
    elif cmd == "both":
        if arg is None:
            sys.stdout.write("usage: /cc:both N  (N = tiktoken count; real context ~1.5-1.66x N. e.g. /cc:both 15000 sets head+tail to 15000 each)\n")
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
    elif cmd in ("model", "model200k"):
        _set("model_200k", arg if arg is not None else "sonnet")
        _clear("model")
    elif cmd == "model1m":
        _set("model_1m", arg if arg is not None else "opus[1m]")
    elif cmd == "autoresume":
        val = (arg or "").strip().lower()
        if not val:
            _report_state("auto_resume", "usage: /cc:autoresume on|off")
            return
        if val in ("on", "1", "true", "yes"):
            _set("auto_resume", "1")
        elif val in ("off", "0", "false", "no"):
            # Default is off, represented by the key being absent.
            _clear("auto_resume")
        else:
            sys.stdout.write("usage: /cc:autoresume on|off\n")
            return
    elif cmd == "autocompact":
        val = (arg or "").strip().lower()
        if not val:
            _report_state(
                "auto_compact_at",
                "usage: /cc:autocompact N [msgs|ctx] | off   (N = token threshold)",
                extra="auto_compact_metric is {}".format(
                    _read_conf().get("auto_compact_metric") or "msgs"),
                state=_auto_compact_state(_read_conf()),
            )
            return
        # Off is a stored 0: with the key missing the default threshold applies.
        if val in ("off", "0"):
            _set("auto_compact_at", "0")
        else:
            try:
                n = int(val)
            except ValueError:
                sys.stdout.write("usage: /cc:autocompact N [msgs|ctx] | off   (N = token threshold)\n")
                return
            if n <= 0:
                _set("auto_compact_at", "0")
            else:
                _set("auto_compact_at", str(n))
                metric = (sys.argv[3] if len(sys.argv) > 3 else "").strip().lower()
                if metric in ("msgs", "ctx"):
                    _set("auto_compact_metric", metric)
    elif cmd == "continue":
        # Message may be multiple words; "off" disables.
        msg = " ".join(sys.argv[2:]).strip()
        if not msg:
            _report_state("resume_continue",
                          "usage: /cc:continue MSG | off   (e.g. /cc:continue continue)")
            return
        if msg.lower() == "off":
            _clear("resume_continue")
        else:
            _set("resume_continue", msg)
    elif cmd == "bigwindow":
        _bigwindow(arg)
        return
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
        sys.stderr.write("usage: pcconf.py begin|end|both|begin-pct|end-pct|both-pct N | model|model200k|model1m MODEL | autoresume on|off | autocompact N [msgs|ctx] | continue MSG|off | bigwindow N|off | show | reset | help\n")
        sys.exit(1)

    # mutating commands fall through and print the resulting config
    _show()


if __name__ == "__main__":
    main()
