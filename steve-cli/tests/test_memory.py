"""Pruning memory files, and compacting conversations, through the real agent."""

import json

import httpx

from fake_upstream import Step

from steve_agent.agent import build_agent
from steve_agent.models import SteveConfig


def memory_file(home):
    return home / "agents" / "memories" / "MEMORY.md"


# Memory files


def test_small_memory_gets_no_instruction(start_server, upstream, steve_home):
    server = start_server()
    memory_file(steve_home).write_text("short")
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="hello")
    assert "Memory maintenance" not in upstream.body_text()


def test_oversized_memory_gets_an_instruction_naming_the_file(start_server, upstream, steve_home):
    server = start_server()
    memory_file(steve_home).write_text("x" * 9000)
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="hello")
    body = upstream.body_text()
    assert "Memory maintenance" in body and "/memories/MEMORY.md: 9000 characters" in body and "under 6000" in body


def test_instruction_stops_as_soon_as_the_file_is_pruned(start_server, upstream, steve_home):
    server = start_server()
    memory_file(steve_home).write_text("x" * 9000)
    prune = ("write_file", json.dumps({"file_path": "/memories/MEMORY.md", "content": "pruned"}))
    upstream.steps.extend([Step(text="", tools=[prune]), Step(text="Done.")])
    response = server.client.responses.create(input="hello")

    assert response.output_text == "Done."
    assert memory_file(steve_home).read_text() == "pruned"
    assert "Memory maintenance" in json.dumps(upstream.requests[0]["body"])
    assert "Memory maintenance" not in json.dumps(upstream.requests[1]["body"])


def test_agents_md_is_not_treated_as_memory(start_server, upstream, steve_home):
    server = start_server()
    (steve_home / "agents" / "AGENTS.md").write_text("x" * 9000)
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="hello")
    assert "Memory maintenance" not in upstream.body_text()


def test_limit_is_configurable_and_missing_files_are_fine(tmp_path, upstream):
    home = tmp_path / "home"
    (home / "agents" / "memories").mkdir(parents=True)
    memory_file(home).write_text("x" * 100)
    config = SteveConfig(
        model={"model": "m", "base_url": upstream.base_url, "api_key": "k"}, memory={"max_file_chars": 50}
    )
    agent = build_agent(config, home=home)
    upstream.steps.append(Step(text="Hi."))
    # The REPL's path: a synchronous invoke.
    agent.invoke({"messages": "hello"})
    assert "over the 50 character limit" in upstream.body_text()

    memory_file(home).unlink()
    upstream.steps.append(Step(text="Hi."))
    agent.invoke({"messages": "hello"})
    assert "Memory maintenance" not in upstream.body_text()


# Compaction


def test_tool_is_offered_and_refuses_when_there_is_nothing_to_compact(server, upstream):
    upstream.steps.extend([Step(text="", tools=[("compact_conversation", "{}")]), Step(text="Done.")])
    response = server.client.responses.create(input="hello")
    assert "compact_conversation" in json.dumps(upstream.requests[0]["body"]["tools"])
    tool = next(item for item in response.output if item.type == "mcp_call")
    assert tool.name == "compact_conversation" and tool.status == "completed"
    assert tool.output.startswith("Nothing to compact"), tool.output


def test_compaction_removes_older_text_from_later_prompts(start_server, upstream):
    # About 13k tokens after the first turn: past the tool's 8.5k gate, short of the automatic 17k trigger.
    server = start_server(max_input_tokens=20000)
    upstream.steps.append(Step(text="First answer.", input_tokens=13000))
    first = server.client.responses.create(input="OLD_DETAIL " + "words " * 6000)
    assert "Summarized" not in upstream.body_text(), "automatic summarization should not have run yet"

    upstream.steps.extend(
        [
            Step(text="", tools=[("compact_conversation", "{}")], input_tokens=13000),
            Step(text="Summary of earlier work."),
            Step(text="Compacted."),
        ]
    )
    second = server.client.responses.create(input="please compact", previous_response_id=first.id)
    tool = next(item for item in second.output if item.type == "mcp_call")
    assert tool.name == "compact_conversation" and tool.status == "completed", tool
    assert not tool.output.startswith("Nothing to compact"), tool.output

    upstream.steps.append(Step(text="Later."))
    server.client.responses.create(input="LATER", previous_response_id=second.id)
    body = upstream.body_text()
    assert "OLD_DETAIL" not in body and "Summary of earlier work." in body and "LATER" in body


def test_without_a_context_size_small_conversations_cannot_compact(server, upstream):
    """The default gate is 85k tokens, which is why model.max_input_tokens matters for local models."""
    upstream.steps.extend([Step(text="", tools=[("compact_conversation", "{}")]), Step(text="Done.")])
    response = server.client.responses.create(input="OLD_DETAIL " + "words " * 6000)
    tool = next(item for item in response.output if item.type == "mcp_call")
    assert tool.output.startswith("Nothing to compact"), tool.output


# Context display and manual compaction


def compact(server, response_id):
    response = httpx.post(f"{server.url}/v1/responses/{response_id}/compact", timeout=30)
    return response


def test_responses_report_their_context(start_server, upstream):
    server = start_server(max_input_tokens=262144)
    upstream.steps.append(Step(text="Hi.", input_tokens=5000))
    response = server.client.responses.create(input="hello " * 400)
    context = response.model_dump()["context"]
    parts = {part["label"]: part["tokens"] for part in context["breakdown"]}
    assert context["limit"] == 262144 and context["compacts_at"] == 222822
    assert abs(context["used"] - 5000) < 6, "the total should be what the model reported"
    assert set(parts) == {"System", "Tools", "User", "Assistant", "Reasoning", "Tool results"}
    assert parts["User"] > 0 and parts["System"] > 0 and parts["Tools"] > 0


def test_context_shows_earlier_turns_and_tool_results(server, upstream):
    upstream.steps.extend([Step(reasoning=True, text="", tools=[("ls", '{"path": "/"}')]), Step(text="First answer.", input_tokens=50000)])
    first = server.client.responses.create(input="hello")
    parts = {part["label"]: part["tokens"] for part in first.model_dump()["context"]["breakdown"]}
    assert parts["Tool results"] > 0 and parts["Reasoning"] > 0
    assert first.model_dump()["context"]["limit"] is None and first.model_dump()["context"]["compacts_at"] == 170000


def test_compact_works_below_the_tools_gate_and_needs_no_model_decision(server, upstream):
    """Far under the gate, and the scripted reply is only there for the summary call."""
    previous = None
    for turn in range(8):
        upstream.steps.append(Step(text=f"Answer {turn}."))
        previous = server.client.responses.create(input=f"OLD_{turn} " + "words " * 50, previous_response_id=previous).id
    calls_before = len(upstream.requests)
    upstream.steps.append(Step(text="Summary of earlier work."))

    result = compact(server, previous)

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "completed" and body["previous_response_id"] == previous
    text = body["output"][-1]["content"][0]["text"]
    assert "ummar" in text, text
    assert len(upstream.requests) == calls_before + 1, "only the summary should reach the model"
    assert body["context"]["used"] > 0, "the pie should update without waiting for another message"
    assert "tools" not in upstream.requests[-1]["body"], "the summary call does not offer tools"

    upstream.steps.append(Step(text="Later."))
    later = server.client.responses.create(input="LATER", previous_response_id=body["id"])
    assert later.output_text == "Later."
    assert "OLD_0" not in upstream.body_text() and "Summary of earlier work." in upstream.body_text()
    after = later.model_dump()["context"]
    assert after["used"] < 20000


def test_compact_keeps_only_the_last_exchange_on_a_large_context(start_server, upstream):
    """The library's 10% would be about 26k tokens here, which would leave nothing to summarize."""
    server = start_server(max_input_tokens=262144)
    upstream.steps.append(Step(text="First."))
    first = server.client.responses.create(input="OLD_DETAIL")
    upstream.steps.append(Step(text="Second."))
    second = server.client.responses.create(input="RECENT_DETAIL", previous_response_id=first.id)
    upstream.steps.append(Step(text="Summary of earlier work."))

    result = compact(server, second.id)

    assert result.status_code == 200, result.text
    assert result.json()["output"][-1]["content"][0]["text"].startswith("Conversation compacted"), result.text
    summary_call = json.dumps(upstream.requests[-1]["body"])
    assert "OLD_DETAIL" in summary_call and "RECENT_DETAIL" not in summary_call

    upstream.steps.append(Step(text="Later."))
    server.client.responses.create(input="LATER", previous_response_id=result.json()["id"])
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "OLD_DETAIL" not in prompt and "RECENT_DETAIL" in prompt and "Summary of earlier work." in prompt


def test_the_model_calling_the_tool_keeps_more_than_a_manual_compact(start_server, upstream):
    server = start_server(max_input_tokens=20000)
    upstream.steps.append(Step(text="First.", input_tokens=13000))
    first = server.client.responses.create(input="OLD_DETAIL " + "words " * 6000)
    upstream.steps.append(Step(text="Second.", input_tokens=13000))
    second = server.client.responses.create(input="MIDDLE_DETAIL", previous_response_id=first.id)
    upstream.steps.extend(
        [
            Step(text="", tools=[("compact_conversation", "{}")], input_tokens=13000),
            Step(text="Summary of earlier work."),
            Step(text="Compacted."),
        ]
    )
    third = server.client.responses.create(input="please compact", previous_response_id=second.id)

    upstream.steps.append(Step(text="Later."))
    server.client.responses.create(input="LATER", previous_response_id=third.id)
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "OLD_DETAIL" not in prompt and "MIDDLE_DETAIL" in prompt


def test_compact_explains_when_there_is_nothing_to_do(server, upstream):
    upstream.steps.append(Step(text="Short."))
    first = server.client.responses.create(input="hi")
    result = compact(server, first.id)
    assert result.status_code == 200
    assert result.json()["output"][-1]["content"][0]["text"].startswith("Nothing to compact")
    assert len(upstream.requests) == 1
    # And the conversation can carry on from it.
    upstream.steps.append(Step(text="Still fine."))
    assert server.client.responses.create(input="more", previous_response_id=result.json()["id"]).output_text == "Still fine."


def test_compact_errors(server, upstream):
    assert compact(server, "resp_missing").status_code == 404
