# Steve Agent

`steve-agent` starts the REPL. `steve-agent serve [--host HOST] [--port PORT]` serves the agent over the OpenAI Responses API, so any client built on the `openai` SDK can use it. From a checkout, prefix either with `uv run`. With no config yet, both first prompt for the upstream model, base URL and API key.

## Server

Point a client at `http://127.0.0.1:8000/v1`. `api_key` is `server.api_key`, or any string when that is unset:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
first = client.responses.create(model="steve-agent", input="What is in my notes?")
print(first.output_text)
next_turn = client.responses.create(model="steve-agent", input="Shorter.", previous_response_id=first.id)
```

Routes:

- `POST /v1/responses`, streaming or not
- `GET` and `DELETE /v1/responses/{id}`
- `GET /v1/responses/{id}/input_items`
- `GET /v1/models` and `GET /v1/models/steve-agent`
- `GET /health`, which needs no API key

Options go under `server` in `~/.config/steve/config.yaml`. All are optional:

```yaml
server:
  host: 127.0.0.1
  port: 8000
  api_key: a-long-random-string   # required to bind a non-loopback host
  allowed_hosts: [localhost, 127.0.0.1, "[::1]"]
  max_conversations: 256
```

### Supported

- Honoured: `input` (text, `input_image`, and replayed assistant messages and `mcp_call` items), `stream`, `instructions`, `metadata`, `previous_response_id`, `store`. `model` is only echoed, as the agent is fixed.
- Rejected with a 400: `tools`, a forced `tool_choice`, JSON `text.format`, `conversation`, `prompt`, `background: true`, `input_file`, and `function_call` or `item_reference` input items.
- Ignored: everything else, such as `temperature` and `max_output_tokens`.
- Output: text, reasoning summaries, refusals and `mcp_call` items. `use_responses_api: false` also works with a chat-completions upstream, but `usage` is then usually null.

### Security defaults

- It binds to loopback, and refuses to start on a non-loopback host without `api_key`, because the agent can write files. With `api_key` set, all of `/v1` needs it.
- Requests are checked against `allowed_hosts`, against DNS rebinding. Add the name or address clients connect through if it is not a default one, including other loopback addresses such as `127.0.0.2`. `serve` warns when the bound address is not listed.
- There is no CORS and there are no docs routes. Upstream error details stay in the server log.
- A first run creates `config.yaml` as `0600` in a `0700` directory. Tighten an older file yourself: `chmod 600 ~/.config/steve/config.yaml`.
- There is one API key and no separation between clients. Any client can read what the agent's file tools can reach, including files the agent wrote for another conversation.

### Behaviour to know about

- **Tools run on the server.** They appear as `mcp_call` output items with `server_label: "steve"`. Client-defined `tools` are rejected with a 400.
- **Conversations are in memory.** A restart loses them, and an old `previous_response_id` then returns 404 `previous_response_not_found`. Run one worker only.
- **The cap counts conversations, not memory.** Least recently used conversations are evicted beyond `max_conversations`, with all their responses. A conversation with a run in flight is never evicted, so the cap can be exceeded until that run ends. One long conversation is never evicted and its memory keeps growing, as does that of a `store: false` turn continuing a stored one, until the whole conversation is deleted or evicted.
- **Offloaded content stays with its conversation.** Oversized input, summarized history and large tool results are kept in memory under `/artifacts`, not on disk. Each fork sees its own, and they go when the conversation does. Anything the agent writes with its file tools is a real file, shared and kept.
- **Responses can be read while they run.** `GET /v1/responses/{id}` returns the current snapshot, and adding `?stream=true` (with an optional `starting_after`) replays the events and follows the run. Watching never affects the run. `input_items` works during the run too, while `DELETE` returns 400 `response_in_progress`. Once a run ends, only stored responses remain: `store: false` and cancelled runs then return 404. Events are kept in memory with the response.
- **Subagents can be watched as responses of their own.** Each `task` tool item carries a `child_response_id` from its `output_item.added` event onwards; read it as `item.child_response_id` in the SDK. That id works with the same `GET` calls, during and after the run. Children are read-only: `previous_response_id` and `DELETE` on one return 400, and `input_items` is empty (the task description is in the parent's tool item). Deleting or evicting the parent removes its children. When the parent is cancelled or fails, its unfinished children end with `response.failed`. A task call that never reaches a subagent, such as one naming an unknown type, ends as an empty child, and its reason is in the parent's tool output.

  ```python
  for event in client.responses.create(model="steve-agent", input="Research this.", stream=True):
      if event.type == "response.output_item.added" and event.item.type == "mcp_call" and event.item.name == "task":
          for child_event in client.responses.retrieve(event.item.child_response_id, stream=True):
              print(child_event.type)
  ```
- **`input_items` lists one request's input.** It does not include earlier turns of the conversation. An item whose `id` repeats an earlier one in the request gets a new `id`.
- **Instructions last one request.** `instructions` and system or developer input messages apply to the current request only. They do not reach subagents.
- **Disconnecting cancels the run**, in both streaming and non-streaming modes, and a cancelled run is not stored. A file write already running in a worker thread may still finish after the cancel.
- **Errors are not retried.** Every error response carries `x-should-retry: false`, because a retried run would write files again.
- **Continuing after a failed turn can resend a large input.** If the first attempt at an oversized input fails upstream, continuing from that failed response sends the whole input to the model on every later turn.
- **llama.cpp upstreams are supported, without visible reasoning.** Missing stream indexes are filled in before LangChain parses the stream. LangChain ignores `reasoning_text` events, so this reasoning is neither shown to clients nor replayed on later turns. Answers are not affected.
- **A chat-completions stream must send `finish_reason`.** Without one, the turn counts as cut off and the response fails with `upstream_stream_truncated`. `length` and `content_filter` give `incomplete`. A refusal from such an upstream does not reach the client, because `ChatOpenAI` drops it while streaming.
