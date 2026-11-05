"""Manual, auto and planning modes, and MCP elicitation, through the real agent."""

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

import httpx
import pytest
from fake_upstream import Step

APPS = {"command": sys.executable, "args": [str(Path(__file__).with_name("fake_mcp_app.py"))]}


def read(path):
    return ("read_file", json.dumps({"file_path": path}))


def write(path, content):
    return ("write_file", json.dumps({"file_path": path, "content": content}))


def post_stream(server, **body):
    return httpx.stream("POST", f"{server.url}/v1/responses", json={"stream": True, **body}, timeout=60)


class Conversation:
    """Streams one run on a thread, answering each question as `answer` says."""

    def __init__(self, server, answer, **body):
        self.events = []
        self.questions = []
        self.server = server
        with post_stream(server, **body) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                self.events.append(event)
                if event["type"] == "response.created":
                    self.response_id = event["response"]["id"]
                if event["type"] in ("steve.approval.requested", "steve.elicitation.requested"):
                    self.questions.append(event)
                    reply = answer(event)
                    if reply is not None:
                        posted = httpx.post(
                            f"{server.url}/v1/responses/{self.response_id}/questions/{event['id']}", json=reply, timeout=10
                        )
                        assert posted.status_code == 200, posted.text

    @property
    def response(self):
        return self.events[-1]["response"]

    def tools(self):
        return [item for item in self.response["output"] if item["type"] == "mcp_call"]


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def write_mcp(home):
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"board": APPS}}))


# Auto


def test_auto_writes_without_asking(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "auto")]), Step(text="Done.")])
    run = Conversation(server, lambda event: pytest.fail("auto should not ask"), input="go", mode="auto")
    assert (folder / "a.txt").read_text() == "auto" and not run.questions


def test_auto_is_the_default(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Done.")])
    assert server.client.responses.create(input="go").status == "completed"
    assert (folder / "a.txt").exists()


# Manual


def test_manual_waits_and_then_writes(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "approved")]), Step(text="Done.")])

    def answer(event):
        assert not (folder / "a.txt").exists(), "nothing happens before the answer"
        return {"approve": True}

    run = Conversation(server, answer, input="go", mode="manual")
    assert run.questions[0]["tool"] == "write_file"
    assert run.questions[0]["arguments"]["content"] == "approved"
    assert (folder / "a.txt").read_text() == "approved"
    assert run.tools()[0]["status"] == "completed"
    assert any(event["type"] == "steve.interaction.resolved" for event in run.events)


def test_manual_denied_changes_nothing_and_tells_the_model_why(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Understood.")])
    run = Conversation(server, lambda event: {"approve": False, "reason": "wrong file"}, input="go", mode="manual")
    assert not (folder / "a.txt").exists()
    assert run.tools()[0]["status"] == "failed"
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "denied" in prompt and "wrong file" in prompt
    assert run.response["status"] == "completed"


def test_manual_does_not_ask_for_reading(start_server, upstream, folder):
    (folder / "a.txt").write_text("WORDS")
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[read("/workspace/a.txt")]), Step(text="Done.")])
    run = Conversation(server, lambda event: pytest.fail("reading should not ask"), input="go", mode="manual")
    assert "WORDS" in run.tools()[0]["output"]


def test_manual_asks_for_each_of_several_calls(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "1"), write("/workspace/b.txt", "2")]), Step(text="Done.")])
    run = Conversation(
        server, lambda event: {"approve": event["arguments"]["file_path"].endswith("a.txt")}, input="go", mode="manual"
    )
    assert len(run.questions) == 2
    assert (folder / "a.txt").exists() and not (folder / "b.txt").exists()


def test_manual_gates_a_subagents_tools_too(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="", tools=[write("/workspace/sub.txt", "from subagent")]), Step(text="Done.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="All done.")])
    seen = []

    def answer(event):
        seen.append(event["tool"])
        assert not (folder / "sub.txt").exists()
        return {"approve": True}

    Conversation(server, answer, input="go", mode="manual")
    assert seen == ["write_file"], "the question reaches the top-level stream"
    assert (folder / "sub.txt").exists()


def test_manual_gates_mcp_tools_unless_they_say_they_are_read_only(start_server, upstream, steve_home):
    write_mcp(steve_home)
    server = start_server()
    upstream.steps.extend(
        [Step(text="", tools=[("board_look_only", "{}"), ("board_change_something", "{}")]), Step(text="Done.")]
    )
    run = Conversation(server, lambda event: {"approve": True}, input="go", mode="manual")
    assert [question["tool"] for question in run.questions] == ["board_change_something"]
    assert [tool["output"] for tool in run.tools()] == ["LOOKED", "CHANGED"]


def test_manual_needs_a_stream(start_server):
    response = httpx.post(f"{start_server().url}/v1/responses", json={"input": "go", "mode": "manual"}, timeout=10)
    assert response.status_code == 400 and response.json()["error"]["code"] == "mode_needs_stream"


def test_stopping_while_waiting_changes_nothing(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Done.")])
    with post_stream(server, input="go", mode="manual") as stream:
        for line in stream.iter_lines():
            if "steve.approval.requested" in line:
                break
    time.sleep(0.5)
    assert not (folder / "a.txt").exists()


def test_answers_are_checked(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Done.")])
    results = []

    def answer(event):
        url = f"{server.url}/v1/responses"
        results.append(httpx.post(f"{url}/resp_nope/questions/{event['id']}", json={"approve": True}, timeout=5).status_code)
        return {"approve": False}

    run = Conversation(server, answer, input="go", mode="manual")
    assert results == [404]
    late = httpx.post(
        f"{server.url}/v1/responses/{run.response_id}/questions/{run.questions[0]['id']}", json={"approve": True}, timeout=5
    )
    assert late.status_code == 404
    assert not (folder / "a.txt").exists()


# Planning


def test_planning_is_not_offered_tools_that_change_things(start_server, upstream, folder, steve_home):
    write_mcp(steve_home)
    server = start_server(workspace=str(folder))
    upstream.steps.append(Step(text="Step 1: look. Step 2: change."))
    run = Conversation(server, lambda event: pytest.fail("planning should not ask"), input="plan it", mode="planning")
    assert "Step 1" in run.response["output"][-1]["content"][0]["text"]
    body = upstream.requests[-1]["body"]
    names = [tool.get("name") for tool in body["tools"]]
    assert "read_file" in names and "board_look_only" in names
    assert "write_file" not in names and "edit_file" not in names and "board_change_something" not in names
    assert "Planning mode" in json.dumps(body)


def test_planning_refuses_a_change_even_if_the_model_tries(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Planned.")])
    run = Conversation(server, lambda event: pytest.fail("planning should not ask"), input="go", mode="planning")
    assert not (folder / "a.txt").exists()
    assert run.tools()[0]["status"] == "failed"
    assert "planning mode" in json.dumps(upstream.requests[-1]["body"])


def test_planning_holds_for_subagents(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="", tools=[write("/workspace/sub.txt", "x")]), Step(text="Done.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Planned.")])
    Conversation(server, lambda event: pytest.fail("planning should not ask"), input="go", mode="planning")
    assert not (folder / "sub.txt").exists()


def test_the_mode_can_change_between_turns(start_server, upstream, folder):
    server = start_server(workspace=str(folder))
    upstream.steps.append(Step(text="The plan."))
    plan = Conversation(server, lambda event: None, input="plan", mode="planning")
    upstream.steps.extend([Step(text="", tools=[write("/workspace/a.txt", "x")]), Step(text="Done.")])
    Conversation(server, lambda event: None, input="do it", mode="auto", previous_response_id=plan.response_id)
    assert (folder / "a.txt").exists()
    assert "Planning mode" not in json.dumps(upstream.requests[-1]["body"]), "a mode lasts for its own turn"


# Elicitation


def test_elicitation_shows_the_prompt_and_choices_and_returns_the_answer(start_server, upstream, steve_home):
    write_mcp(steve_home)
    server = start_server()
    upstream.steps.extend([Step(text="", tools=[("board_pick_colour", "{}")]), Step(text="Done.")])
    run = Conversation(server, lambda event: {"action": "accept", "content": {"value": "green"}}, input="go")
    question = run.questions[0]
    assert question["type"] == "steve.elicitation.requested"
    assert question["server"] == "board" and question["message"] == "Which colour?"
    assert "red" in json.dumps(question["schema"]) and "green" in json.dumps(question["schema"])
    assert run.tools()[0]["output"] == "accept:green"


def test_elicitation_with_custom_input_fields(start_server, upstream, steve_home):
    write_mcp(steve_home)
    server = start_server()
    upstream.steps.extend([Step(text="", tools=[("board_file_ticket", "{}")]), Step(text="Done.")])
    run = Conversation(
        server, lambda event: {"action": "accept", "content": {"title": "Broken", "urgent": True, "count": 3}}, input="go"
    )
    properties = run.questions[0]["schema"]["properties"]
    assert properties["title"]["type"] == "string" and properties["urgent"]["type"] == "boolean"
    assert "Broken" in run.tools()[0]["output"] and "urgent=True" in run.tools()[0]["output"]


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_elicitation_can_be_refused(start_server, upstream, steve_home, action):
    write_mcp(steve_home)
    server = start_server()
    upstream.steps.extend([Step(text="", tools=[("board_pick_colour", "{}")]), Step(text="Done.")])
    run = Conversation(server, lambda event: {"action": action}, input="go")
    assert run.tools()[0]["output"].startswith(f"{action}:")


def test_elicitation_is_declined_when_nobody_can_answer(start_server, upstream, steve_home):
    write_mcp(steve_home)
    server = start_server()
    upstream.steps.extend([Step(text="", tools=[("board_pick_colour", "{}")]), Step(text="Done.")])
    response = server.client.responses.create(input="go")
    assert next(item for item in response.output if item.type == "mcp_call").output.startswith(("cancel:", "decline:"))
