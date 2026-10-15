import json
import os
import pathlib
import stat
import subprocess

import pytest

from conftest import run_hook

VAR = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
S1 = "11111111-aaaa-4bbb-8ccc-000000000001"
P1 = "aaaaaaaa-0000-4000-8000-000000000001"


@pytest.fixture
def settings(env):
    # No such tmux server, so pane names come back unknown and no .ctx file is skipped.
    env["COMPACTO_TMUX"] = f"tmux -L cct-none-{os.getpid()}"
    return pathlib.Path(env["HOME"]) / ".claude" / "settings.json"


def bigwindow(env, *args):
    r = run_hook("pcconf.py", "", env, ["bigwindow", *args])
    assert r.returncode == 0, r.stderr
    return r.stdout


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def ctx(env, pane, tokens):
    signals = pathlib.Path(env["COMPACTO_SIGNAL_DIR"])
    signals.mkdir(parents=True, exist_ok=True)
    (signals / f"{pane}.ctx").write_text(f"%{pane}\t{tokens}\t{tokens - 37700}\n")


def test_sets_the_variable_and_keeps_the_rest_of_the_file(env, settings):
    write(settings, {"model": "opus", "env": {"FOO": "1"}, "hooks": {"Stop": []}})
    out = bigwindow(env, "600k")
    data = json.loads(settings.read_text())
    assert data == {"model": "opus", "env": {"FOO": "1", VAR: "600000"}, "hooks": {"Stop": []}}
    assert list(data) == ["model", "env", "hooks"]
    assert "bigwindow is 600000" in out


@pytest.mark.parametrize("arg, tokens", [("600000", "600000"), ("600k", "600000"), ("600K", "600000"),
                                         ("1M", "1000000"), ("1m", "1000000"), ("600", "600000"),
                                         ("100", "100000"), ("1000", "1000000"), ("100k", "100000")])
def test_reads_sizes_the_way_autocompact_does(env, settings, arg, tokens):
    bigwindow(env, arg)
    assert json.loads(settings.read_text())["env"][VAR] == tokens


@pytest.mark.parametrize("arg", ["99999", "99k", "1001k", "2M", "1001", "50", "0", "-600k", "abc",
                                 "6e5", "1.5M", "600kk", "６００k", pytest.param("1" + "0" * 5000, id="5001-digits")])
def test_a_size_outside_100k_to_1m_changes_nothing(env, settings, arg):
    write(settings, {"env": {VAR: "800000"}})
    before = settings.read_bytes()
    out = bigwindow(env, arg)
    assert settings.read_bytes() == before
    assert "usage: /cc:bigwindow" in out


def test_setting_the_value_it_already_has_leaves_the_file_untouched(env, settings):
    settings.write_text('{\n    "env": {"%s": "600000"}\n}\n' % VAR)
    before = settings.read_bytes()
    bigwindow(env, "600k")
    assert settings.read_bytes() == before


def test_off_removes_the_variable_and_an_env_left_empty(env, settings):
    write(settings, {"model": "opus", "env": {VAR: "600000"}})
    out = bigwindow(env, "off")
    assert json.loads(settings.read_text()) == {"model": "opus"}
    assert "bigwindow is off" in out


def test_off_keeps_other_env_entries(env, settings):
    write(settings, {"env": {"FOO": "1", VAR: "600000"}})
    bigwindow(env, "off")
    assert json.loads(settings.read_text()) == {"env": {"FOO": "1"}}


def test_off_without_a_settings_file_creates_none(env, settings):
    bigwindow(env, "off")
    assert not settings.exists()


@pytest.mark.parametrize("data, shown", [({"env": {VAR: "600000"}}, "bigwindow is 600000"),
                                         ({"model": "opus"}, "bigwindow is off")])
def test_no_argument_reports_and_changes_nothing(env, settings, data, shown):
    write(settings, data)
    before = settings.read_bytes()
    out = bigwindow(env)
    assert settings.read_bytes() == before
    assert shown in out and "usage: /cc:bigwindow" in out


def test_creates_the_settings_file_when_there_is_none(env, settings):
    bigwindow(env, "700k")
    assert json.loads(settings.read_text()) == {"env": {VAR: "700000"}}


@pytest.mark.parametrize("text", ["{ not json", "", "[]", '"text"', '{"env": "x"}', '{"env": [1]}'])
def test_a_settings_file_that_is_not_a_json_object_is_never_overwritten(env, settings, text):
    settings.write_text(text)
    out = bigwindow(env, "600k")
    assert settings.read_text() == text
    assert "nothing changed" in out


def test_a_settings_file_it_cannot_write_is_reported_and_left_as_it_was(env, settings):
    write(settings, {"model": "opus"})
    settings.parent.chmod(0o500)
    try:
        out = bigwindow(env, "600k")
    finally:
        settings.parent.chmod(0o700)
    assert "nothing changed" in out
    assert json.loads(settings.read_text()) == {"model": "opus"}


def test_a_symlinked_settings_file_stays_a_symlink(env, settings, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    write(real, {"model": "opus"})
    settings.symlink_to(real)
    bigwindow(env, "600k")
    assert settings.is_symlink()
    assert json.loads(real.read_text())["env"][VAR] == "600000"


def test_the_file_keeps_its_permissions(env, settings):
    write(settings, {})
    settings.chmod(0o600)
    bigwindow(env, "600k")
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_reports_the_subagent_cutoff_and_the_panes_at_or_over_it(env, settings):
    ctx(env, 5, 510000)
    ctx(env, 6, 509999)
    out = bigwindow(env, "600k")
    assert "under 510000" in out
    assert "%5" in out and "%6" not in out


def test_the_cutoff_follows_the_pct_override(env, settings):
    env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "50"
    assert "under 240000" in bigwindow(env, "600k")


@pytest.mark.parametrize("parent, passes", [(509999, True), (510000, False)])
def test_the_printed_cutoff_is_where_the_hook_stops_passing_subagents(env, settings, tmp_path, parent, passes):
    assert "under 510000" in bigwindow(env, "600k")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "uuid": "a1", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": parent, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 1}}}) + "\n")
    env.update({VAR: "600000", "CLAUDE_PROJECT_DIR": str(tmp_path / "proj")})
    r = run_hook("precompact.py", {"hook_event_name": "PreCompact", "session_id": S1, "prompt_id": P1,
                                   "transcript_path": str(transcript), "trigger": "auto",
                                   "custom_instructions": None}, env)
    assert r.returncode == 0, r.stderr
    assert (r.stdout.strip() == "") is passes


def test_names_each_pane_and_skips_panes_that_are_gone(env, settings):
    cmd = ["tmux", "-L", f"cct-bigwindow-{os.getpid()}"]
    subprocess.run(cmd + ["new-session", "-d", "-s", "t", "-n", "big!"], check=True)
    try:
        pane = subprocess.run(cmd + ["display-message", "-p", "-t", "t", "#{pane_id}"],
                              capture_output=True, text=True, check=True).stdout.strip()
        env["COMPACTO_TMUX"] = " ".join(cmd)
        ctx(env, pane.lstrip("%"), 700000)
        ctx(env, 99999, 700000)
        out = bigwindow(env, "600k")
        assert f"{pane}  big!  700000" in out
        assert "%99999" not in out
    finally:
        subprocess.run(cmd + ["kill-server"], capture_output=True)
