import datetime
import json
import re
import subprocess
import sys

import pytest

from conftest import HOOKS

import rewrite_transcript
import slim_fork

SESSION = "e40df81b-1df5-4a0e-9893-751013356ca8"
BIG = "x " * 20000
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo" * 100}}
REFUSED_AT = "2026-11-11T14:31:05.365Z"


def epoch(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def entry(kind, uid, parent, content, ts="2026-11-11T12:00:00.000Z", **kw):
    e = {"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": uid, "timestamp": ts, "sessionId": SESSION,
         "cwd": "/w", "version": "2.1.322", "gitBranch": "main", "userType": "external",
         "message": {"role": kind, "content": content}}
    if kind == "assistant":
        e["message"]["model"] = kw.pop("model", "claude-opus-5-5")
    e.update(kw)
    return e


def refused(uid, parent, ts=REFUSED_AT):
    return entry("assistant", uid, parent, [{"type": "text", "text": "Prompt is too long"}], ts=ts,
                 isApiErrorMessage=True, model="<synthetic>", error="invalid_request")


def handoff_prompt(parent):
    return [entry("user", "h1", parent, "<command-message>compact-handoff</command-message>\n"
                                        "<command-name>/compact-handoff</command-name>", promptId="p-h"),
            entry("user", "h2", "h1", [{"type": "text", "text": "Base directory for this skill: /s"}], promptId="p-h"),
            {"type": "attachment", "uuid": "at2", "parentUuid": "h2", "sessionId": SESSION,
             "attachment": {"type": "hook_success", "content": "refused prompt's hook"}},
            refused("e2", "h2")]


def conversation(last_tool_result=True):
    rows = [
        {"type": "custom-title", "customTitle": "curi — compact 11-11 12:26", "sessionId": SESSION},
        entry("user", "u1", None, "build the pill"),
        entry("assistant", "a1", "u1", [{"type": "thinking", "thinking": "", "signature": "sig" * 500}]),
        entry("assistant", "a2", "a1", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        entry("user", "r1", "a2", [{"type": "tool_result", "tool_use_id": "t1",
                                    "content": [{"type": "text", "text": BIG}, IMAGE]}], toolUseResult={"stdout": BIG}),
        {"type": "attachment", "uuid": "at1", "parentUuid": "r1", "sessionId": SESSION,
         "attachment": {"type": "edited_text_file", "content": "kept"}},
        entry("assistant", "a3", "r1", [{"type": "thinking", "thinking": "", "signature": "s"},
                                        {"type": "text", "text": "looking at it"}]),
    ]
    if last_tool_result:
        rows += [entry("assistant", "a4", "a3", [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/w/x"}}]),
                 entry("user", "r2", "a4", [{"type": "tool_result", "tool_use_id": "t2", "content": "short"}]),
                 refused("e1", "r2", ts="2026-11-11T14:31:03.039Z")] + handoff_prompt("e1")
    else:
        rows += [entry("user", "u2", "a3", "and the other one", promptId="p-2"),
                 refused("e1", "u2", ts="2026-11-11T14:31:03.039Z")] + handoff_prompt("e1")
    return rows


def write(tmp_path, rows, name=SESSION):
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def fork_rows(tmp_path, new_id):
    return [json.loads(ln) for ln in (tmp_path / f"{new_id}.jsonl").read_text().splitlines()]


def messages(rows):
    return [r for r in rows if r.get("type") in ("user", "assistant")]


def texts(rows):
    return json.dumps(messages(rows))


@pytest.fixture
def stuck(tmp_path):
    return write(tmp_path, conversation())


def test_the_slim_copy_keeps_every_message_and_tool_call_without_thinking(tmp_path, stuck):
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(stuck)))
    blocks = [[b["type"] for b in m["message"]["content"]] if isinstance(m["message"]["content"], list) else ["str"]
              for m in messages(rows)]
    assert blocks == [["str"], ["tool_use"], ["tool_result"], ["text"], ["tool_use"], ["tool_result"], ["text"]]
    assert '"thinking"' not in texts(rows)


def test_long_tool_outputs_are_capped_and_images_left_out(tmp_path, stuck):
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(stuck)))
    result = messages(rows)[2]
    content = result["message"]["content"][0]["content"]
    assert "[truncated" in content[0]["text"] and len(content[0]["text"]) < len(BIG) / 3
    assert content[1] == {"type": "text", "text": rewrite_transcript.IMAGE_PLACEHOLDER}
    assert "toolUseResult" not in result and "iVBOR" not in texts(rows)


def test_the_refused_requests_and_the_prompts_they_carried_are_left_out(tmp_path, stuck):
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(stuck)))
    assert "Prompt is too long" not in texts(rows) and "compact-handoff" not in texts(rows)
    kinds = [r["attachment"]["type"] for r in rows if r.get("type") == "attachment"]
    assert kinds == ["edited_text_file"]


def test_a_closing_reply_follows_the_last_tool_result(tmp_path, stuck):
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(stuck)))
    *_, result, closing = messages(rows)
    assert closing["type"] == "assistant" and closing["parentUuid"] == result["uuid"]
    assert closing["message"]["stop_reason"] == "end_turn" and closing["message"]["model"] == "claude-opus-5-5"
    assert "context limit" in closing["message"]["content"][0]["text"]


def test_a_copy_that_ends_on_a_reply_gets_no_closing_one(tmp_path):
    path = write(tmp_path, conversation(last_tool_result=False))
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(path)))
    last = messages(rows)[-1]
    assert last["message"]["content"] == [{"type": "text", "text": "looking at it"}]
    assert "and the other one" not in texts(rows)


def test_a_reply_cut_off_before_its_tool_result_is_left_out(tmp_path):
    rows = conversation()
    rows = rows[:rows.index(next(r for r in rows if r.get("uuid") == "r2"))] + [refused("e1", "a4")]
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(write(tmp_path, rows))))
    assert messages(rows)[-1]["message"]["content"] == [{"type": "text", "text": "looking at it"}]


def test_the_copy_is_a_session_of_its_own_whose_chain_reaches_every_message(tmp_path, stuck):
    new_id = rewrite_transcript.slim(str(stuck))
    rows = fork_rows(tmp_path, new_id)
    source = {r.get("uuid") for r in conversation()} - {None}
    assert not source & {r.get("uuid") for r in rows}
    assert {r["sessionId"] for r in rows if "sessionId" in r} == {new_id}
    by_uuid = {r["uuid"]: r for r in rows if r.get("uuid")}
    walked, cur = 0, messages(rows)[-1]
    while cur is not None:
        walked += 1
        cur = by_uuid.get(cur.get("parentUuid"))
    assert walked == len(messages(rows))


def test_the_copy_is_titled_as_a_slim_copy_and_a_later_compaction_does_not_stack_titles(tmp_path, stuck):
    rows = fork_rows(tmp_path, rewrite_transcript.slim(str(stuck)))
    title = [r["customTitle"] for r in rows if r.get("type") == "custom-title"]
    assert len(title) == 1 and re.fullmatch(r"curi — slim \d{2}-\d{2} \d{2}:\d{2}", title[0])
    assert rewrite_transcript._source_title(rows) == "curi"


def test_a_session_with_no_request_the_api_took_is_refused(tmp_path):
    path = write(tmp_path, [entry("user", "u1", None, "hi"), refused("e1", "u1")])
    with pytest.raises(ValueError):
        rewrite_transcript.slim(str(path))


@pytest.mark.parametrize("since,expected", [(epoch("2026-11-11T14:31:04.000Z"), True), (epoch(REFUSED_AT) + 1, False)])
def test_a_handoff_refused_at_the_limit_is_seen_only_from_its_start(stuck, since, expected):
    assert slim_fork.at_limit(str(stuck), since) is expected


def test_a_session_whose_last_reply_went_through_is_not_at_the_limit(tmp_path):
    rows = conversation()
    rows.append(entry("assistant", "ok", "e2", [{"type": "text", "text": "done"}], ts="2026-11-11T14:40:00.000Z"))
    assert not slim_fork.at_limit(str(write(tmp_path, rows)), 0)


def test_a_subagents_error_does_not_count_and_a_long_tail_is_read_through(tmp_path):
    rows = conversation()
    rows = rows[:rows.index(next(r for r in rows if r.get("uuid") == "e1"))]
    rows += [entry("assistant", "ok", "r2", [{"type": "text", "text": "done"}], ts="2026-11-11T14:30:00.000Z"),
             dict(refused("e9", "ok"), isSidechain=True), {"type": "api-request-blob", "blob": "y" * 5000}]
    path = write(tmp_path, rows)
    def main_reply(e):
        return e.get("type") == "assistant" and not e.get("isSidechain")
    assert rewrite_transcript.last_entry(str(path), main_reply, tail_bytes=1000)["uuid"] == "ok"
    assert not slim_fork.at_limit(str(path), 0)


def run_cli(*args):
    return subprocess.run([sys.executable, str(HOOKS / "slim_fork.py"), *map(str, args)],
                          capture_output=True, text=True, timeout=60)


def test_the_cli_checks_the_limit_and_writes_a_copy_with_the_subagents(tmp_path, stuck):
    sub = tmp_path / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text("{}\n")
    assert run_cli("at-limit", stuck, int(epoch("2026-11-11T14:31:00.000Z"))).returncode == 0
    assert run_cli("at-limit", stuck, int(epoch(REFUSED_AT)) + 5).returncode == 1
    r = run_cli("make", stuck)
    new_id = r.stdout.strip()
    assert r.returncode == 0 and re.fullmatch(r"[0-9a-f-]{36}", new_id)
    assert (tmp_path / f"{new_id}.jsonl").exists()
    assert (tmp_path / new_id / "subagents" / "agent-a1.jsonl").read_text() == "{}\n"


@pytest.mark.parametrize("args", [(), ("make",), ("at-limit", "x"), ("other", "x")])
def test_the_cli_rejects_bad_arguments(args):
    assert run_cli(*args).returncode == 2


def test_the_cli_reports_a_copy_it_cannot_make(tmp_path):
    r = run_cli("make", write(tmp_path, [entry("user", "u1", None, "hi"), refused("e1", "u1")]))
    assert r.returncode == 1 and r.stdout == "" and "slim_fork" in r.stderr
