"""Fake model for the end-to-end test: scripted replies, using the CLI test suite's fake upstream."""

import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "steve-cli" / "tests"))
from fake_upstream import FakeUpstream, Step  # noqa: E402

upstream = FakeUpstream()
task = ("task", json.dumps({"description": "ALPHA job", "subagent_type": "general-purpose"}))
upstream.routes["ALPHA"] = deque([Step(reasoning=True, text="The subagent says **PONG**.")])
write_file = lambda name: ("write_file", json.dumps({"file_path": f"/workspace/{name}", "content": "written"}))
upstream.routes["WRITE ALLOWED"] = deque([Step(text="", tools=[write_file("allowed.txt")]), Step(text="Wrote it.")])
upstream.routes["WRITE DENIED"] = deque([Step(text="", tools=[write_file("denied.txt")]), Step(text="I will not.")])
upstream.routes["PLAN IT"] = deque([Step(text="", tools=[write_file("planned.txt")]), Step(text="Step 1: write the file.")])
upstream.routes["RUN COMMAND"] = deque([Step(text="", tools=[("execute", json.dumps({"command": "echo ran > from-command.txt"}))]), Step(text="Ran it.")])
upstream.routes["ASK COLOUR"] = deque([Step(text="", tools=[("board_pick_colour", "{}")]), Step(text="Picked.")])
upstream.routes["ASK TICKET"] = deque([Step(text="", tools=[("board_file_ticket", "{}")]), Step(text="Filed.")])
upstream.routes["ASK NAME"] = deque([Step(text="", tools=[("board_pick_name", "{}")]), Step(text="Named.")])
upstream.routes["OPEN BOARD"] = deque([Step(text="", tools=[("board_show_board", json.dumps({"title": "plan"}))]), Step(text="Opened the board.")])
upstream.steps.extend(
    [
        # First message: use a tool, delegate, then answer in Markdown.
        Step(
            reasoning=True,
            text="",
            tools=[("write_file", json.dumps({"file_path": "/workspace/from-agent.txt", "content": "written by the agent"}))],
        ),
        Step(reasoning=True, text="", tools=[task]),
        Step(reasoning=True, text="## Result\n\nParent **done**.\n\n<img src=x onerror=\"document.title='pwned'\">"),
        # Second message.
        Step(reasoning=True, text="I remember that."),
        # Third message: fails once, and the server's automatic retry recovers.
        Step(http_error=(500, "upstream exploded"), no_sdk_retry=True),
        Step(reasoning=True, text="Recovered after a retry."),
        # Fourth message: keeps going until the user presses stop.
        Step(reasoning=True, text="This goes on and on", hold=True),
    ]
)
upstream.llama_like = True
print(upstream.base_url, flush=True)
# Runs until the test closes stdin or kills this process.
sys.stdin.read()
