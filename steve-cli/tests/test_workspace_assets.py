"""The agent's working directory, and files sent with a message, through the real agent and SDK."""

import base64
import json
import re
import subprocess
import sys
import zlib
from collections import deque
from pathlib import Path

import openai
import pytest
from fake_upstream import Step

from steve_agent.mcp import McpTools


def read(path):
    return ("read_file", json.dumps({"file_path": path}))


def write(path, content):
    return ("write_file", json.dumps({"file_path": path, "content": content}))


def tool_results(response):
    return [item for item in response.output if item.type == "mcp_call"]


# Working directory


def test_agent_reads_and_writes_in_the_workspace(start_server, upstream, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("WORKSPACE_NOTE")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend(
        [Step(text="", tools=[read("/workspace/notes.md"), write("/workspace/out.md", "made it")]), Step(text="Done.")]
    )
    response = server.client.responses.create(input="go")
    results = tool_results(response)
    assert all(tool.status == "completed" for tool in results), results
    assert "WORKSPACE_NOTE" in results[0].output
    assert (workspace / "out.md").read_text() == "made it"
    assert str(workspace) in upstream.body_text(), "the agent should be told where it is working"


def test_the_real_path_of_the_workspace_works_too(start_server, upstream, tmp_path):
    """The agent is told the real path, so it tries it. Before this, it failed and went looking."""
    workspace = tmp_path / "project"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "notes.md").write_text("REAL_PATH_NOTE")
    (workspace / "sub" / "deep.md").write_text("DEEP_NOTE")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend(
        [
            Step(
                text="",
                tools=[
                    ("ls", json.dumps({"path": str(workspace)})),
                    read(f"{workspace}/notes.md"),
                    read(f"{workspace}/sub/deep.md"),
                    write(f"{workspace}/out.md", "via the real path"),
                ],
            ),
            Step(text="Done."),
        ]
    )
    response = server.client.responses.create(input="go")
    calls = tool_results(response)
    assert [call.status for call in calls] == ["completed"] * 4, [call.error for call in calls]
    assert "notes.md" in calls[0].output
    assert "REAL_PATH_NOTE" in calls[1].output and "DEEP_NOTE" in calls[2].output
    assert (workspace / "out.md").read_text() == "via the real path"


def test_the_real_path_reaches_nothing_outside_the_workspace(start_server, upstream, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("OUTSIDE_SECRET")
    (tmp_path / "project-sibling").mkdir()
    (tmp_path / "project-sibling" / "secret.txt").write_text("SIBLING_SECRET")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend(
        [
            Step(
                text="",
                tools=[
                    read(f"{workspace}/../secret.txt"),
                    read(f"{tmp_path}/secret.txt"),
                    read(f"{workspace}-sibling/secret.txt"),
                    write(f"{workspace}/../planted.txt", "x"),
                ],
            ),
            Step(text="Done."),
        ]
    )
    server.client.responses.create(input="go")
    body = upstream.body_text()
    assert "OUTSIDE_SECRET" not in body and "SIBLING_SECRET" not in body
    assert not (tmp_path / "planted.txt").exists()


def test_a_plain_path_lands_in_the_working_folder(start_server, upstream, tmp_path, steve_home):
    """It used to land in Steve's own folder, where the person never saw it."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    server = start_server(workspace=str(workspace))
    upstream.steps.extend([Step(text="", tools=[write("/notes.md", "plain"), write("/sub/deep.md", "deep")]), Step(text="Done.")])
    response = server.client.responses.create(input="go")
    assert all(tool.status == "completed" for tool in tool_results(response))
    assert (workspace / "notes.md").read_text() == "plain"
    assert (workspace / "sub/deep.md").read_text() == "deep"
    assert not (steve_home / "agents/notes.md").exists()


def test_memory_and_skills_stay_in_stevess_own_folder(start_server, upstream, tmp_path, steve_home):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (steve_home / "agents/AGENTS.md").write_text("GLOBAL_INSTRUCTIONS")
    (steve_home / "agents/memories/MEMORY.md").write_text("GLOBAL_MEMORY")
    skill = steve_home / "agents/skills/my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: GLOBAL_SKILL\n---\nBody\n")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend([Step(text="", tools=[write("/.steve/memories/MEMORY.md", "updated")]), Step(text="Done.")])
    response = server.client.responses.create(input="go")
    assert all(tool.status == "completed" for tool in tool_results(response))
    body = upstream.body_text()
    assert "GLOBAL_INSTRUCTIONS" in body and "GLOBAL_MEMORY" in body and "GLOBAL_SKILL" in body
    assert (steve_home / "agents/memories/MEMORY.md").read_text() == "updated"
    assert not (workspace / ".steve").exists()


def test_a_project_folder_is_not_hidden_by_stevess_names(start_server, upstream, tmp_path, steve_home):
    """A project's own skills, memories or agents folder must not be swapped for Steve's."""
    workspace = tmp_path / "project"
    for name in ("skills", "memories", "agents", "local", "workspace"):
        (workspace / name).mkdir(parents=True)
    server = start_server(workspace=str(workspace))
    tools = [write(f"/{name}/x.py", name) for name in ("skills", "memories", "agents", "local", "workspace")]
    upstream.steps.extend([Step(text="", tools=tools), Step(text="Done.")])
    response = server.client.responses.create(input="go")
    assert all(tool.status == "completed" for tool in tool_results(response))
    for name in ("skills", "memories", "agents", "local", "workspace"):
        assert (workspace / name / "x.py").read_text() == name
    assert not (steve_home / "agents/skills/x.py").exists()


def test_the_older_names_still_work_for_steves_files(start_server, upstream, tmp_path, steve_home):
    workspace = tmp_path / "project"
    workspace.mkdir()
    server = start_server(workspace=str(workspace))
    upstream.steps.extend([Step(text="", tools=[write("/memories/MEMORY.md", "kept")]), Step(text="Done.")])
    server.client.responses.create(input="go")
    assert (steve_home / "agents/memories/MEMORY.md").read_text() == "kept"
    assert not (workspace / "memories").exists()


def test_searching_the_root_finds_each_project_file_once(start_server, upstream, tmp_path, steve_home):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("NEEDLE here")
    (steve_home / "agents/memories/MEMORY.md").write_text("NEEDLE in memory")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend(
        [
            Step(
                text="",
                tools=[
                    ("grep", json.dumps({"pattern": "NEEDLE", "path": "/", "output_mode": "content"})),
                    ("glob", json.dumps({"pattern": "**/*.md"})),
                    ("glob", json.dumps({"pattern": "/workspace/*.md"})),
                    ("ls", json.dumps({"path": "/"})),
                ],
            ),
            Step(text="Done."),
        ]
    )
    grep, glob, named, ls = tool_results(server.client.responses.create(input="go"))
    assert grep.output.count("NEEDLE") == 1, grep.output
    assert "NEEDLE here" in grep.output
    assert glob.output.count("notes.md") == 1, glob.output
    assert "MEMORY.md" not in glob.output
    assert "/workspace/notes.md" in named.output, "naming an alias in a pattern still has to work"
    assert "notes.md" in ls.output
    for hidden in ("/workspace", "/.steve", "/local", str(workspace)):
        assert hidden not in ls.output, ls.output


def test_the_agent_is_told_that_root_is_the_folder(start_server, upstream, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    server = start_server(workspace=str(workspace))
    upstream.steps.append(Step(text="Hi."))
    server.client.responses.create(input="go")
    body = upstream.body_text()
    assert f"/notes.md is {workspace}/notes.md" in body
    assert "create and edit files" in body


def test_the_agent_and_its_subagents_are_told_the_date(start_server, upstream, monkeypatch):
    monkeypatch.setattr("steve_agent.server.middleware._today", lambda: "Friday, March 3, 2031")
    server = start_server()
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="Sub done.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="All done.")])
    assert server.client.responses.create(input="go").status == "completed"
    assert len(upstream.requests) == 3
    for request in upstream.requests:
        assert "Today is Friday, March 3, 2031" in json.dumps(request["body"])


def test_the_date_is_not_fixed_when_the_server_starts(start_server, upstream, monkeypatch):
    server = start_server()
    for day in ("Monday, March 3, 2031", "Tuesday, March 4, 2031"):
        monkeypatch.setattr("steve_agent.server.middleware._today", lambda day=day: day)
        upstream.steps.append(Step(text="Hi."))
        server.client.responses.create(input="go")
        assert f"Today is {day}" in json.dumps(upstream.requests[-1]["body"])


def test_the_date_is_a_real_one():
    from datetime import datetime

    from steve_agent.server.middleware import _today

    assert datetime.strptime(_today(), "%A, %B %d, %Y").date() == datetime.now().astimezone().date()


def test_agent_cannot_escape_the_workspace(start_server, upstream, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("OUTSIDE_SECRET")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend(
        [Step(text="", tools=[read("/workspace/../secret.txt"), write("/workspace/../escaped.txt", "x")]), Step(text="Done.")]
    )
    response = server.client.responses.create(input="go")
    assert "OUTSIDE_SECRET" not in json.dumps([tool.model_dump() for tool in tool_results(response)])
    assert not (tmp_path / "escaped.txt").exists()


def test_without_a_workspace_the_server_has_no_local_routes(server, upstream, tmp_path, monkeypatch):
    """A server must not pick up the directory it happens to run in."""
    upstream.steps.extend([Step(text="", tools=[read("/workspace/here.md")]), Step(text="Done.")])
    response = server.client.responses.create(input="go")
    assert "/workspace" not in upstream.requests[0]["body"]["input"][0]["content"][0]["text"]
    assert "HERE" not in json.dumps([tool.model_dump() for tool in tool_results(response)])


def test_serve_ignores_the_directory_it_was_started_in(tmp_path, monkeypatch):
    from steve_agent import __main__ as cli

    captured = {}

    class Stop(Exception):
        pass

    def fake_build(config, home=None, **kwargs):
        captured["workspace"] = config.server.workspace
        raise Stop

    monkeypatch.setattr("steve_agent.server.app.build_server_agent", fake_build)
    monkeypatch.setattr(cli, "load_mcp", lambda home: McpTools([], []))
    monkeypatch.setattr(
        cli, "get_config", lambda: __import__("steve_agent.models", fromlist=["SteveConfig"]).SteveConfig(
            model={"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
        )
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Stop):
        cli.serve("127.0.0.1", 0, None)
    assert captured["workspace"] is None

    (tmp_path / "chosen").mkdir()
    with pytest.raises(Stop):
        cli.serve("127.0.0.1", 0, "chosen")
    assert captured["workspace"] == str(tmp_path / "chosen")


def test_serve_refuses_a_workspace_that_is_not_a_directory(tmp_path, monkeypatch):
    from steve_agent import __main__ as cli
    from steve_agent.models import SteveConfig

    monkeypatch.setattr(cli, "get_config", lambda: SteveConfig(model={"model": "m", "base_url": "u", "api_key": "k"}))
    with pytest.raises(SystemExit, match="not a directory"):
        cli.serve("127.0.0.1", 0, str(tmp_path / "missing"))


def test_repl_works_in_the_current_directory(tmp_path, monkeypatch, fake_model):
    from steve_agent import __main__ as cli
    from steve_agent.models import SteveConfig

    seen = {}
    monkeypatch.setattr(cli, "get_config", lambda: SteveConfig(model={"model": "m", "base_url": "u", "api_key": "k"}))
    monkeypatch.setattr(cli, "build_agent", lambda config, workspace=None, tools=(): seen.update(workspace=workspace))
    monkeypatch.setattr(cli, "load_mcp", lambda home: McpTools([], []))
    monkeypatch.setattr(cli, "run", lambda agent: None)
    monkeypatch.chdir(tmp_path)
    cli.main()
    assert seen["workspace"] == Path.cwd()


# Assets


def attach(name, data: bytes, media="text/plain", text="Look at this."):
    url = f"data:{media};base64," + base64.b64encode(data).decode()
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": text},
                {"type": "input_file", "filename": name, "file_data": url},
            ],
        }
    ]


@pytest.fixture
def fixed_upload_dir(monkeypatch):
    class Fixed:
        hex = "abcdef0123456789"

    monkeypatch.setattr("steve_agent.server.inputs.uuid4", lambda: Fixed)
    return "/artifacts/uploads/abcdef01"


def test_agent_is_told_about_a_file_and_can_read_it(server, upstream, fixed_upload_dir):
    path = f"{fixed_upload_dir}/notes.txt"
    upstream.steps.extend([Step(text="", tools=[read(path)]), Step(text="Done.")])
    response = server.client.responses.create(input=attach("notes.txt", b"ASSET_CONTENT"))

    first = json.dumps(upstream.requests[0]["body"]["input"])
    assert f"at {path}" in first and "text/plain" in first and "Look at this." in first
    assert "ASSET_CONTENT" not in first, "the file should be read, not pasted into the prompt"
    assert "ASSET_CONTENT" in tool_results(response)[0].output


def test_file_is_still_there_on_a_later_turn_and_is_gone_with_the_conversation(server, upstream, fixed_upload_dir):
    path = f"{fixed_upload_dir}/notes.txt"
    upstream.steps.append(Step(text="Got it."))
    first = server.client.responses.create(input=attach("notes.txt", b"ASSET_CONTENT"))
    upstream.steps.extend([Step(text="", tools=[read(path)]), Step(text="Done.")])
    second = server.client.responses.create(input="read it again", previous_response_id=first.id)
    assert "ASSET_CONTENT" in tool_results(second)[0].output

    upstream.steps.extend([Step(text="", tools=[read(path)]), Step(text="Done.")])
    other = server.client.responses.create(input="another conversation")
    assert "ASSET_CONTENT" not in json.dumps([tool.model_dump() for tool in tool_results(other)])


def test_binary_file_round_trips_as_a_file_block(server, upstream, fixed_upload_dir):
    path = f"{fixed_upload_dir}/chart.png"
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    upstream.steps.extend([Step(text="", tools=[read(path)]), Step(text="Done.")])
    response = server.client.responses.create(input=attach("chart.png", png, "image/png"))
    assert tool_results(response)[0].status == "completed"


def test_retry_keeps_the_file(start_server, upstream, fixed_upload_dir):
    client = start_server(response_retries=1).client
    path = f"{fixed_upload_dir}/notes.txt"
    upstream.steps.extend(
        [Step(http_error=(500, "x"), no_sdk_retry=True), Step(text="", tools=[read(path)]), Step(text="Done.")]
    )
    response = client.responses.create(input=attach("notes.txt", b"ASSET_CONTENT"))
    assert "ASSET_CONTENT" in tool_results(response)[0].output


def test_file_name_cannot_escape_its_directory(server, upstream):
    upstream.steps.append(Step(text="Ok."))
    server.client.responses.create(input=attach("../../etc/pass wd;.txt", b"x"))
    body = upstream.body_text()
    assert "pass wd_.txt" in body and "/../" not in body.replace("\\\\", "")


def test_input_items_do_not_keep_a_second_copy(server, upstream):
    upstream.steps.append(Step(text="Ok."))
    response = server.client.responses.create(input=attach("notes.txt", b"ASSET_CONTENT"))
    stored = server.app.state.store.input_items(response.id)
    assert base64.b64encode(b"ASSET_CONTENT").decode() not in json.dumps(stored)
    assert "notes.txt" in json.dumps(stored)


def test_bad_uploads_get_clear_errors(start_server, upstream):
    client = start_server(max_asset_mb=0.001).client
    with pytest.raises(openai.APIStatusError) as too_big:
        client.responses.create(input=attach("big.bin", b"x" * 5000))
    assert too_big.value.status_code == 413 and too_big.value.code == "asset_too_large"

    def part(**fields):
        return [{"role": "user", "content": [{"type": "input_file", **fields}]}]

    for bad in (part(file_id="file_1"), part(file_url="https://example.com/a"), part(filename="a", file_data="not base64!")):
        with pytest.raises(openai.BadRequestError):
            client.responses.create(input=bad)
    assert not upstream.requests


def test_subagent_can_read_an_uploaded_file(server, upstream, fixed_upload_dir):
    from collections import deque

    path = f"{fixed_upload_dir}/notes.txt"
    upstream.routes["ALPHA"] = deque([Step(text="", tools=[read(path)]), Step(text="Subagent finished.")])
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Parent done.")])
    response = server.client.responses.create(input=attach("notes.txt", b"ASSET_CONTENT"))
    child_id = next(tool for tool in tool_results(response) if tool.name == "task").child_response_id
    child = server.client.responses.retrieve(child_id)
    assert "ASSET_CONTENT" in next(i for i in child.output if i.type == "mcp_call").output


# PDFs and images read with read_file


def small_pdf(text=None):
    """A real PDF with a compressed stream, so its text cannot be found in the raw bytes."""
    stream = zlib.compress(f"BT /F1 18 Tf 40 700 Td ({text}) Tj ET".encode() if text else b"")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def workspace_with(tmp_path, name, data):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / name).write_bytes(data)
    return workspace


def mcp_call(response):
    return next(item for item in response.output if item.type == "mcp_call")


def test_a_pdf_reaches_the_model_as_text(start_server, upstream, tmp_path):
    server = start_server(workspace=str(workspace_with(tmp_path, "brief.pdf", small_pdf("LAUNCH 7731"))))
    upstream.steps.extend([Step(text="", tools=[read("/workspace/brief.pdf")]), Step(text="7731.")])
    response = server.client.responses.create(input="code?")
    assert "LAUNCH 7731" in json.dumps(upstream.requests[-1]["body"])
    assert "JVBERi" not in json.dumps(upstream.requests[-1]["body"]), "the raw file should not be sent"
    assert "LAUNCH 7731" in mcp_call(response).output


def test_an_uploaded_pdf_reaches_the_model_as_text(start_server, upstream):
    server = start_server()
    data = base64.b64encode(small_pdf("UPLOADED 4417")).decode()
    upstream.steps.append(Step(text="Reading."))
    first = server.client.responses.create(
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "code?"},
            {"type": "input_file", "filename": "brief.pdf", "file_data": f"data:application/pdf;base64,{data}"},
        ]}]
    )
    path = re.search(r"/artifacts/uploads/\w+/brief\.pdf", upstream.body_text()).group()
    upstream.steps.extend([Step(text="", tools=[read(path)]), Step(text="4417.")])
    server.client.responses.create(input="read it", previous_response_id=first.id)
    assert "UPLOADED 4417" in json.dumps(upstream.requests[-1]["body"])


def test_a_subagent_gets_pdfs_as_text_too(start_server, upstream, tmp_path):
    server = start_server(workspace=str(workspace_with(tmp_path, "brief.pdf", small_pdf("SUBAGENT 2288"))))
    task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
    upstream.routes["ALPHA"] = deque([Step(text="", tools=[read("/workspace/brief.pdf")]), Step(text="2288.")])
    upstream.steps.extend([Step(text="", tools=[task]), Step(text="Done.")])
    server.client.responses.create(input="go")
    bodies = [json.dumps(request["body"]) for request in upstream.requests]
    assert any("SUBAGENT 2288" in body for body in bodies), "the subagent should have been given the text"
    assert not any("JVBERi" in body for body in bodies)


def test_a_pdf_without_text_or_a_damaged_one_says_so(start_server, upstream, tmp_path):
    workspace = workspace_with(tmp_path, "scan.pdf", small_pdf())
    (workspace / "bad.pdf").write_bytes(b"%PDF-1.4 not really \xff\xfe")
    server = start_server(workspace=str(workspace))
    upstream.steps.extend([Step(text="", tools=[read("/workspace/scan.pdf"), read("/workspace/bad.pdf")]), Step(text="Sorry.")])
    response = server.client.responses.create(input="read")
    outputs = [item.output for item in response.output if item.type == "mcp_call"]
    assert "no text to extract" in outputs[0]
    assert "Could not read" in outputs[1] or "no text to extract" in outputs[1]


def test_an_image_goes_in_a_user_message_after_the_tool_result(start_server, upstream, tmp_path):
    server = start_server(workspace=str(workspace_with(tmp_path, "red.png", PNG)))
    upstream.steps.extend([Step(text="", tools=[read("/workspace/red.png")]), Step(text="Red.")])
    response = server.client.responses.create(input="colour?")

    items = upstream.requests[-1]["body"]["input"]
    output = next(index for index, item in enumerate(items) if item.get("type") == "function_call_output")
    assert isinstance(items[output]["output"], str), "llama.cpp only accepts text in tool output"
    assert items[output + 1]["role"] == "user"
    assert items[output + 1]["content"][0]["type"] == "input_image"
    assert response.output_text == "Red."
    assert "iVBOR" not in mcp_call(response).output, "the file should not reach the app's tool row"

    upstream.steps.append(Step(text="Still red."))
    later = server.client.responses.create(input="sure?", previous_response_id=response.id)
    assert later.output_text == "Still red."
    assert "input_image" in json.dumps(upstream.requests[-1]["body"]), "the model can still see it later"


def test_a_server_error_on_an_image_drops_it_for_good(start_server, upstream, tmp_path):
    server = start_server(workspace=str(workspace_with(tmp_path, "red.png", PNG)))
    upstream.steps.extend(
        [
            Step(text="", tools=[read("/workspace/red.png")]),
            Step(http_error=(502, "Bad gateway")),  # were this retried, the image would get the next step's answer
            Step(text="I could not see it."),
        ]
    )
    response = server.client.responses.create(input="colour?")
    assert response.status == "completed" and response.output_text == "I could not see it."
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "input_image" not in prompt and "Unsupported content" in prompt

    upstream.steps.append(Step(text="Carrying on."))
    later = server.client.responses.create(input="next", previous_response_id=response.id)
    assert later.output_text == "Carrying on."
    assert "input_image" not in json.dumps(upstream.requests[-1]["body"])


def test_an_image_the_model_never_answers_is_dropped_too(start_server, upstream, tmp_path, monkeypatch):
    monkeypatch.setattr("steve_agent.server.middleware.MEDIA_CALL_SECONDS", 0.5)
    server = start_server(workspace=str(workspace_with(tmp_path, "red.png", PNG)))
    upstream.steps.extend(
        [
            Step(text="", tools=[read("/workspace/red.png")]),
            Step(text="partial", hold=True),
            Step(text="I could not see it."),
        ]
    )
    response = server.client.responses.create(input="colour?")
    assert response.status == "completed" and "I could not see it." in response.output_text
    prompt = json.dumps(upstream.requests[-1]["body"])
    assert "input_image" not in prompt and "Unsupported content" in prompt


def test_a_server_error_without_a_new_file_still_fails(start_server, upstream):
    server = start_server()
    upstream.steps.append(Step(http_error=(502, "Bad gateway"), no_sdk_retry=True))
    with pytest.raises(openai.InternalServerError):
        server.client.responses.create(input="hello")
