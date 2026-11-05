"""Saved conversations, across a real restart of the server."""

import json
import stat
import time

import httpx
import pytest
from fake_upstream import Step


def get(server, path=""):
    return httpx.get(f"{server.url}/v1/conversations{path}", timeout=30)


@pytest.fixture
def start(start_server):
    def _start(**options):
        return start_server(save_conversations=True, **options)

    return _start


def stop(server):
    server.uvicorn.should_exit = True


def chat(server, upstream, text, previous=None, answer="Answer."):
    upstream.steps.append(Step(text=answer))
    return server.client.responses.create(input=text, previous_response_id=previous)


def test_a_conversation_survives_a_restart_and_can_continue(start, upstream):
    first = start()
    one = chat(first, upstream, "REMEMBER_ME please")
    two = chat(first, upstream, "second question", one.id, "Second answer.")
    assert two.model_dump()["conversation"]["id"] == one.model_dump()["conversation"]["id"]

    second = start()
    listed = get(second).json()["data"]
    assert [entry["title"] for entry in listed] == ["REMEMBER_ME please"]
    assert listed[0]["head_response_id"] == two.id

    upstream.steps.append(Step(text="Third answer."))
    third = second.client.responses.create(input="third question", previous_response_id=two.id)
    assert third.output_text == "Third answer."
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "REMEMBER_ME" in prompt and "second question" in prompt and "Second answer." in prompt, "the history comes back"

    third_server = start()
    turns = get(third_server, f"/{listed[0]['id']}").json()["turns"]
    assert [turn["response"]["id"] for turn in turns] == [one.id, two.id, third.id]
    assert turns[2]["input_items"][0]["content"][0]["text"] == "third question"
    assert turns[2]["response"]["output"][-1]["content"][0]["text"] == "Third answer."


def test_a_saved_response_can_be_read_after_a_restart(start, upstream):
    first = start()
    response = chat(first, upstream, "hello", answer="Saved words.")
    second = start()
    assert second.client.responses.retrieve(response.id).output_text == "Saved words."
    assert second.client.responses.input_items.list(response.id).data[0].content[0].text == "hello"
    events = list(second.client.responses.retrieve(response.id, stream=True))
    assert events[-1].type == "response.completed" and events[-1].response.output_text == "Saved words."


def test_a_retry_forks_and_only_the_new_line_comes_back(start, upstream):
    server = start()
    one = chat(server, upstream, "first")
    chat(server, upstream, "ABANDONED", one.id)
    retried = chat(server, upstream, "KEPT", one.id, "Kept answer.")

    restarted = start()
    entry = get(restarted).json()["data"][0]
    assert entry["head_response_id"] == retried.id
    texts = [turn["input_items"][0]["content"][0]["text"] for turn in get(restarted, f"/{entry['id']}").json()["turns"]]
    assert texts == ["first", "KEPT"]

    upstream.steps.append(Step(text="More."))
    restarted.client.responses.create(input="more", previous_response_id=retried.id)
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "KEPT" in prompt and "ABANDONED" not in prompt


def test_deleting_a_dropped_response_takes_it_off_disk(start, upstream, steve_home):
    server = start()
    one = chat(server, upstream, "first")
    dropped = chat(server, upstream, "DROPPED_WORDS", one.id, "DROPPED_ANSWER")
    server.client.responses.delete(dropped.id)
    chat(server, upstream, "kept", one.id)
    restarted = start()
    entry = get(restarted).json()["data"][0]
    assert [turn["input_items"][0]["content"][0]["text"] for turn in get(restarted, f"/{entry['id']}").json()["turns"]] == ["first", "kept"]
    with pytest.raises(Exception):
        restarted.client.responses.retrieve(dropped.id)


def test_compaction_and_a_failure_come_back_as_they_were(start, upstream):
    server = start()
    previous = None
    for turn in range(4):
        previous = chat(server, upstream, f"TURN_{turn}", previous).id
    upstream.steps.append(Step(text="Summary."))
    compacted = httpx.post(f"{server.url}/v1/responses/{previous}/compact", timeout=30).json()
    upstream.steps.append(Step(end="failed", text="partial"))
    events = list(server.client.responses.create(input="this fails", previous_response_id=compacted["id"], stream=True))
    failed = events[-1].response
    assert failed.status == "failed"

    restarted = start()
    entry = get(restarted).json()["data"][0]
    turns = get(restarted, f"/{entry['id']}").json()["turns"]
    assert turns[4]["compaction"] is True and turns[3]["compaction"] is False
    assert turns[5]["response"]["status"] == "failed" and turns[5]["response"]["error"]["code"]
    assert restarted.client.responses.retrieve(failed.id).status == "failed"

    upstream.steps.append(Step(text="Later."))
    restarted.client.responses.create(input="LATER", previous_response_id=compacted["id"])
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "TURN_0" not in prompt and "Summary." in prompt and "LATER" in prompt


def test_a_subagent_can_be_watched_after_a_restart(start, upstream):
    from collections import deque

    server = start()
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="The subagent's words.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Done.")])
    response = server.client.responses.create(input="delegate")
    child_id = next(item for item in response.output if item.type == "mcp_call").model_dump()["child_response_id"]

    restarted = start()
    assert restarted.client.responses.retrieve(child_id).output_text == "The subagent's words."
    entry = get(restarted).json()["data"][0]
    assert len(get(restarted, f"/{entry['id']}").json()["turns"]) == 1, "a subagent's response is not a turn"
    with pytest.raises(Exception):
        restarted.client.responses.create(input="x", previous_response_id=child_id)


def test_an_uploaded_file_comes_back_with_its_conversation(start, upstream):
    import base64

    server = start()
    data = base64.b64encode(b"FILE_WORDS").decode()
    upstream.steps.append(Step(text="Got it."))
    one = server.client.responses.create(
        input=[{"role": "user", "content": [{"type": "input_file", "filename": "notes.txt", "file_data": f"data:text/plain;base64,{data}"}]}]
    )
    path = __import__("re").search(r"/artifacts/uploads/\w+/notes\.txt", upstream.body_text()).group()

    restarted = start()
    assert get(restarted).json()["data"][0]["title"] == "notes.txt"
    upstream.steps.extend([Step(text="", tools=[("read_file", json.dumps({"file_path": path}))]), Step(text="Read.")])
    restarted.client.responses.create(input="read it", previous_response_id=one.id)
    assert "FILE_WORDS" in upstream.body_text()


def test_deleting_is_for_good(start, upstream, steve_home):
    server = start()
    keep = chat(server, upstream, "KEEP")
    gone = chat(server, upstream, "GONE_SECRET")
    gone_id = gone.model_dump()["conversation"]["id"]
    assert httpx.delete(f"{server.url}/v1/conversations/{gone_id}", timeout=10).status_code == 200
    assert get(server, f"/{gone_id}").status_code == 404
    for path in steve_home.glob("conversations.db*"):
        assert b"GONE_SECRET" not in path.read_bytes(), f"{path.name} still holds deleted words"
    assert httpx.delete(f"{server.url}/v1/conversations/{gone_id}", timeout=10).status_code == 404

    restarted = start()
    assert [entry["title"] for entry in get(restarted).json()["data"]] == ["KEEP"]
    assert restarted.client.responses.retrieve(keep.id).id == keep.id
    with pytest.raises(Exception):
        restarted.client.responses.retrieve(gone.id)


def test_deleting_the_last_response_removes_the_conversation(start, upstream):
    server = start()
    response = chat(server, upstream, "only one")
    server.client.responses.delete(response.id)
    assert get(start()).json()["data"] == []


def test_conversations_belong_to_their_working_folder(start, upstream, tmp_path):
    first_folder = tmp_path / "first"
    second_folder = tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    chat(start(workspace=str(first_folder)), upstream, "IN_FIRST")
    chat(start(workspace=str(second_folder)), upstream, "IN_SECOND")
    chat(start(), upstream, "IN_NONE")
    assert [e["title"] for e in get(start(workspace=str(first_folder))).json()["data"]] == ["IN_FIRST"]
    assert [e["title"] for e in get(start(workspace=str(second_folder))).json()["data"]] == ["IN_SECOND"]
    assert [e["title"] for e in get(start()).json()["data"]] == ["IN_NONE"]


def test_a_saved_conversation_is_only_evicted_from_memory(start, upstream):
    server = start(max_conversations=1)
    one = chat(server, upstream, "FIRST")
    chat(server, upstream, "SECOND")
    assert server.client.responses.retrieve(one.id).id == one.id, "it comes back from disk"
    assert {entry["title"] for entry in get(server).json()["data"]} == {"FIRST", "SECOND"}


def test_the_database_is_private(start, upstream, steve_home):
    chat(start(), upstream, "secret words")
    for path in steve_home.glob("conversations.db*"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


def test_a_store_false_request_is_not_saved(start, upstream, steve_home):
    server = start()
    upstream.steps.append(Step(text="Not kept."))
    server.client.responses.create(input="UNSAVED", store=False)
    assert get(server).json()["data"] == []
    assert b"UNSAVED" not in (steve_home / "conversations.db").read_bytes()


def test_a_cancelled_run_is_not_saved(start, upstream):
    server = start()
    upstream.steps.append(Step(text="Going", hold=True))
    stream = server.client.responses.create(input="CANCELLED", stream=True)
    for event in stream:
        if event.type == "response.output_text.delta":
            break
    stream.close()
    time.sleep(0.5)  # for the disconnect to cancel the run
    assert get(server).json()["data"] == []


def test_a_broken_database_costs_saving_and_not_the_agent(start, upstream, steve_home, capsys):
    (steve_home / "conversations.db").write_bytes(b"this is not a database at all, sorry" * 100)
    server = start()
    assert "will not be saved" in capsys.readouterr().err
    assert chat(server, upstream, "still works").output_text == "Answer."
    assert get(server).json()["data"] == []


def test_a_database_from_a_newer_version_is_left_alone(start, upstream, steve_home, capsys):
    import sqlite3

    db = sqlite3.connect(steve_home / "conversations.db")
    db.execute("PRAGMA user_version=99")
    db.close()
    server = start()
    assert "newer version" in capsys.readouterr().err
    assert chat(server, upstream, "still works").output_text == "Answer."


def test_no_saving_when_turned_off(start_server, upstream, steve_home):
    server = start_server(save_conversations=False)
    chat(server, upstream, "hello")
    assert not (steve_home / "conversations.db").exists()
    assert get(server).json()["data"] == []
