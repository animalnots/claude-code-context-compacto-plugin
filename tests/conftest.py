import json
import os
import pathlib
import subprocess
import sys

import pytest

HOOKS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "context-compacto" / "hooks"
sys.path.insert(0, str(HOOKS))


@pytest.fixture
def env(tmp_path):
    """Hook environment aimed at temp dirs: never the real signal dir or precompact.conf."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("COMPACTO_", "PRECOMPACT_", "TMUX", "CLAUDE_"))}
    e["HOME"] = str(tmp_path / "home")
    e["COMPACTO_SIGNAL_DIR"] = str(tmp_path / "signals")
    e["TMUX_PANE"] = "%7"
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    return e


def run_hook(script, payload, env, args=()):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOKS / script), *args], input=raw,
                          capture_output=True, text=True, env=env, timeout=60)
