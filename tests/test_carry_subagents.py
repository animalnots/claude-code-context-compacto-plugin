import os
import shutil

import pytest

import rewrite_transcript


def session_with_subagents(tmp_path):
    parent = tmp_path / "s1.jsonl"
    parent.write_text("{}\n")
    sub = tmp_path / "s1" / "subagents"
    (sub / "team").mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text('{"uuid": "x"}\n')
    (sub / "agent-a1.meta.json").write_text('{"agentType": "reader"}')
    (sub / "team" / "agent-a2.jsonl").write_text('{"uuid": "y"}\n')
    return parent, sub


def test_the_forks_folder_links_the_parents_subagent_transcripts_and_copies_the_rest(tmp_path):
    parent, sub = session_with_subagents(tmp_path)
    assert rewrite_transcript.carry_subagents(str(parent), "f1") == 3
    out = tmp_path / "f1" / "subagents"
    assert os.path.samefile(out / "agent-a1.jsonl", sub / "agent-a1.jsonl")
    assert os.path.samefile(out / "team" / "agent-a2.jsonl", sub / "team" / "agent-a2.jsonl")
    assert (out / "agent-a1.meta.json").read_text() == '{"agentType": "reader"}'
    assert not os.path.samefile(out / "agent-a1.meta.json", sub / "agent-a1.meta.json")


def test_files_already_in_the_forks_folder_are_kept(tmp_path):
    parent, _ = session_with_subagents(tmp_path)
    out = tmp_path / "f1" / "subagents"
    out.mkdir(parents=True)
    (out / "agent-a1.jsonl").write_text("mine\n")
    assert rewrite_transcript.carry_subagents(str(parent), "f1") == 2
    assert (out / "agent-a1.jsonl").read_text() == "mine\n"


def test_a_session_without_subagents_carries_nothing(tmp_path):
    parent = tmp_path / "s1.jsonl"
    parent.write_text("{}\n")
    (tmp_path / "s1" / "subagents").mkdir(parents=True)
    assert rewrite_transcript.carry_subagents(str(parent), "f1") == 0
    assert not (tmp_path / "f1").exists()


def test_where_links_fail_the_transcripts_are_copied(tmp_path, monkeypatch):
    parent, sub = session_with_subagents(tmp_path)

    def cannot_link(src, dst):
        raise OSError("Invalid cross-device link")
    monkeypatch.setattr(os, "link", cannot_link)
    assert rewrite_transcript.carry_subagents(str(parent), "f1") == 3
    out = tmp_path / "f1" / "subagents"
    assert (out / "agent-a1.jsonl").read_text() == '{"uuid": "x"}\n'
    assert not os.path.samefile(out / "agent-a1.jsonl", sub / "agent-a1.jsonl")


def test_a_file_that_cannot_be_carried_does_not_stop_the_rest(tmp_path, monkeypatch):
    parent, sub = session_with_subagents(tmp_path)
    copy2 = shutil.copy2

    def cannot_copy_meta(src, dst, **kw):
        if src.endswith(".meta.json"):
            raise PermissionError(13, "Permission denied")
        return copy2(src, dst, **kw)
    monkeypatch.setattr(shutil, "copy2", cannot_copy_meta)
    with pytest.raises(OSError, match=r"1 of 3 files failed: agent-a1\.meta\.json \(Permission denied\)"):
        rewrite_transcript.carry_subagents(str(parent), "f1")
    out = tmp_path / "f1" / "subagents"
    assert os.path.samefile(out / "agent-a1.jsonl", sub / "agent-a1.jsonl")
    assert os.path.samefile(out / "team" / "agent-a2.jsonl", sub / "team" / "agent-a2.jsonl")


@pytest.mark.parametrize("n, more", [(3, False), (4, True)])
def test_a_carry_error_names_at_most_three_files(tmp_path, monkeypatch, n, more):
    parent = tmp_path / "s1.jsonl"
    parent.write_text("{}\n")
    sub = tmp_path / "s1" / "subagents"
    sub.mkdir(parents=True)
    for i in range(n):
        (sub / f"agent-a{i}.meta.json").write_text("{}")

    def cannot_copy(src, dst, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(shutil, "copy2", cannot_copy)
    with pytest.raises(OSError) as e:
        rewrite_transcript.carry_subagents(str(parent), "f1")
    assert str(e.value).startswith(f"{n} of {n} files failed: ")
    assert str(e.value).count("Permission denied") == 3
    assert str(e.value).endswith(", …") == more
