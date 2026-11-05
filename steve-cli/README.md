# Steve Agent

`steve-agent` starts the REPL, working in the current directory. `steve-agent serve [--host HOST] [--port PORT] [--workspace DIR]` serves the agent over the OpenAI Responses API, so any client built on the `openai` SDK can use it. From a checkout, prefix either with `uv run`. With no config yet, both first prompt for the upstream model, base URL and API key.

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
- `GET /v1/mcp`, each MCP server's status, and `POST /v1/mcp/apps/page`, `/resource` and `/call` for hosts showing MCP Apps
- `POST /v1/responses/{id}/compact`, which summarizes older messages and returns a new response to continue from
- `GET /v1/models` and `GET /v1/models/steve-agent`
- `GET /health`, which needs no API key

Options go in `~/.config/steve/config.yaml`, next to the model settings. All of these are optional:

```yaml
model:
  max_input_tokens: 32000   # your model's context size, for compaction; see below
memory:
  max_file_chars: 8000      # the agent prunes a MEMORY.md above this
server:
  host: 127.0.0.1
  port: 8000
  api_key: a-long-random-string   # required to bind a non-loopback host
  allowed_hosts: [localhost, 127.0.0.1, "[::1]"]
  max_conversations: 256
  max_conversation_mb: 64   # prune above this; see below
  response_retries: 1       # 0 turns retries off
  workspace: /path/to/dir   # the directory the agent works in; --workspace overrides it
  max_asset_mb: 25          # largest file a request can attach
sandbox:                    # the desktop app's Settings changes these and web
  enabled: true
  network: false
  timeout_seconds: 120      # the longest a command can run
web:
  enabled: true
  allowed_hosts: []         # private addresses it may reach: a name, *.example.com, an address or 10.0.0.0/8
  obey_robots: true
  browser: null             # chrome, msedge or a path; empty uses the one `steve-agent web setup` downloads
  timeout_seconds: 30       # a page's limit
  search_url: null          # a page containing {query}, which lets web_crawl search
```

### Supported

- Honoured: `input` (text, `input_image`, `input_file` with inline `file_data`, and replayed assistant messages and `mcp_call` items), `stream`, `instructions`, `metadata`, `previous_response_id`, `store`. `model` is only echoed, as the agent is fixed.
- Rejected with a 400: `tools`, a forced `tool_choice`, JSON `text.format`, `conversation`, `prompt`, `background: true`, `input_file` by `file_id` or `file_url`, and `function_call` or `item_reference` input items.
- Ignored: everything else, such as `temperature` and `max_output_tokens`.
- Output: text, reasoning, refusals and `mcp_call` items. `use_responses_api: false` also works with a chat-completions upstream, but `usage` is then usually null.

### Security defaults

- It binds to loopback, and refuses to start on a non-loopback host without `api_key`, because the agent can write files. With `api_key` set, all of `/v1` needs it.
- Requests are checked against `allowed_hosts`, against DNS rebinding. Add the name or address clients connect through if it is not a default one, including other loopback addresses such as `127.0.0.2`. `serve` warns when the bound address is not listed.
- There is no CORS and there are no docs routes. Upstream error details stay in the server log.
- A first run creates `config.yaml` as `0600` in a `0700` directory. Tighten an older file yourself: `chmod 600 ~/.config/steve/config.yaml`.
- There is one API key and no separation between clients. Any client can read what the agent's file tools can reach, including files the agent wrote for another conversation.

### Behaviour to know about

- **The agent works in one directory.** It is the root of its file tools: `/notes.md` is `notes.md` in that directory, and the real path and `/workspace` also work. `.agents` inside it is its project directory, and they cannot leave it. Steve's own memory and skills are at `/.steve`, and `/memories`, `/skills`, `/agents` and `/local` still work unless the directory has a real folder of that name, which wins. Listing or searching `/` covers the directory and the conversation's `/artifacts`, not Steve's own files. The terminal chat uses the directory it was started in. `serve` never does: it uses `--workspace` or `server.workspace`, and with neither the agent has no working directory, only its own memory under `~/.config/steve/agents`.
- **Commands run in a sandbox.** With bubblewrap and a working directory, the agent gets an `execute` tool. A command can change only that directory, sees the rest of the system read-only, has an empty home and no inherited environment, and its whole process tree dies on a timeout or on Stop. There is no fallback: without bubblewrap the tool is not offered, nor when the directory contains your home or `~/.config/steve`. `sandbox.network` is off by default. Turned on, commands can also reach this machine's services and your local network.
- **The web is read in a real browser.** `web_fetch` reads one page and `web_crawl` follows links and saves pages under `/artifacts/crawl`, for `read_file` and `grep`. Run `steve-agent web setup` once to download the browser (about 120 MB), or set `web.browser` to `chrome`, `msedge` or the path of a Chromium-based browser. Each call uses a fresh context, never your profile. All browser traffic goes through a local proxy that refuses private addresses, except what is in `web.allowed_hosts`. `web.obey_robots` is on by default and applies to both tools. There is no search engine: `web.search_url` can name a page containing `{query}`, but search pages tend to refuse bots or disallow them in `robots.txt`.
- **The agent is told today's date,** on every model call and in subagents, with a note that being past its training data is normal.
- **Modes cover these too.** Manual asks before a command or a web read, auto does not, and planning offers the web tools but not commands.
- **Files sent with a message are saved for the agent to read.** An `input_file` is stored at `/artifacts/uploads/...` in the conversation's state, and the message tells the agent its path. So it works with any model, forks and goes with the conversation, counts towards `max_conversation_mb`, and does not appear in `input_items`. An `input_image` still goes straight to the model.
- **PDFs reach the model as text, and images as a user message.** `read_file` extracts a PDF's text with `pypdf`, so PDFs work on any model; a scanned PDF reports that it has no text. An image follows the tool result in a user message, because llama.cpp accepts only text in tool output, so the model needs vision. If the model rejects a file just read, or the upstream answers 5xx or takes over 3 minutes, the agent gets a notice in its place and the conversation carries on.
- **Each response reports its context.** An extra `context` field gives `used`, `limit` (`model.max_input_tokens`, or null), `compacts_at` and a `breakdown` by category, for the last model call. The total is what the model reported where it reports usage, and the split is an estimate scaled to match.
- **Compacting on request needs no model decision.** `POST /v1/responses/{id}/compact` skips the tool's size check and keeps only the last exchange, and the only model call is the one writing the summary. A conversation of one exchange reports that there is nothing to compact.
- **Tools run on the server.** They appear as `mcp_call` output items with `server_label: "steve"`. Client-defined `tools` are rejected with a 400.
- **Conversations are in memory.** A restart loses them, and an old `previous_response_id` then returns 404 `previous_response_not_found`. Run one worker only.
- **Memory is capped in two ways.** Least recently used conversations are evicted beyond `max_conversations`, with all their responses. A conversation with a run in flight is never evicted, so that cap can be exceeded until the run ends.
- **A conversation is pruned above `max_conversation_mb`.** This is an estimate of its size in server memory. It frees checkpoints nothing needs first, such as finished subagents' and dead branches'. Then the replay events of older responses, whose `?stream=true` then returns only the terminal event, which still holds the whole response. Then the oldest responses, which then return 404. Its message history is never removed, because later turns depend on it, so a conversation that stays over the cap gets a 400 `conversation_too_large` on its next turn. It can still be read, and new conversations are unaffected. Pruning waits until no run is in flight in that conversation. This is about server memory: the agent's `MEMORY.md` is not touched.
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
- **Failed runs are retried once, from where they stopped.** Set `server.response_retries` to change that, or 0 to turn it off. A retry resumes from the last checkpoint, so tools that finished are not run again and their files are not written twice. A tool that was interrupted partway through runs again. A failed subagent runs again as the same child response, from its start. Requests the upstream refuses, such as a 400 or 401, fail at once. So does `incomplete`, which is not a failure. This is on top of the SDK's own retries of each HTTP request.
- **Partial output from a failed attempt is dropped.** It is left out of the final response. A streaming client may have seen its deltas already, and sees that item end with status `incomplete`. Treat the terminal event's `output` as the result.
- **Clients should not retry.** Every error response carries `x-should-retry: false`, because a new request would run the agent from the start and write files again.
- **Continuing after a failed turn can resend a large input.** If the first attempt at an oversized input fails upstream and retries run out, continuing from that failed response sends the whole input to the model on every later turn.
- **llama.cpp upstreams are supported.** Missing stream indexes are filled in before LangChain parses the stream. Their `reasoning_text` events reach clients as `response.reasoning_summary_text` events, because LangChain only keeps summaries, and go back to the upstream in its own format on later turns.
- **The agent prunes its own memory files.** When a `MEMORY.md` is over `memory.max_file_chars` (default 8000), the agent is told to rewrite it to about three quarters of that before doing anything else, keeping what you explicitly asked it to remember. The size is checked before each model call, so this stops once the file is small enough. The agent decides what to drop, it can be aggressive, and there is no backup. `AGENTS.md` is not touched.
- **Long conversations are compacted.** The agent summarizes older messages automatically at 85% of `model.max_input_tokens`, and can do so earlier with its `compact_conversation` tool, from about 42%. Without that option the thresholds are 170k and 85k tokens, which can be past a local model's context, so set it for those. Summarized messages stay in the server's memory, so this shortens prompts, not `max_conversation_mb` use.
- **A chat-completions stream must send `finish_reason`.** Without one, the turn counts as cut off and the response fails with `upstream_stream_truncated`. `length` and `content_filter` give `incomplete`. A refusal from such an upstream does not reach the client, because `ChatOpenAI` drops it while streaming.

## Modes and questions

A request's extra `mode` field sets what the agent may do. It is enforced for the agent and its subagents, and lasts for that request.

- `auto`, the default: tools run without asking.
- `manual`: a tool that changes something waits for approval. Needs `stream: true`. Read-only tools don't ask, including MCP tools annotated `readOnlyHint`.
- `planning`: tools that change things are not offered, and are refused if tried. The agent is told to finish with a plan.

While a question is open the stream carries `steve.approval.requested` (`id`, `tool`, `arguments`) or `steve.elicitation.requested` (`id`, `server`, `mode`, `message`, and `schema` or `url`), then `steve.interaction.resolved`. Answer with `POST /v1/responses/{id}/questions/{question_id}`: `{"approve": true}` or `{"approve": false, "reason": "..."}`, and `{"action": "accept", "content": {...}}`, `decline` or `cancel` for an elicitation.

MCP elicitation works in any mode. With nobody to answer, such as a non-streaming request or the terminal chat, it is cancelled or declined. Unanswered questions end after 15 minutes. An MCP App's own tool calls are not gated by a mode.

## Saved conversations

The server saves each finished turn in `~/.config/steve/conversations.db`, which only you can read, so conversations survive a restart. Set `server.save_conversations: false` to keep them in memory only, as requests with `store: false` always are.

- `GET /v1/conversations` lists the current working folder's, newest first. `GET /v1/conversations/{id}` gives one's turns, and `DELETE` removes it from disk for good, including from SQLite's log. A response's `conversation.id` says which it belongs to.
- **They belong to a working folder.** The agent reaches that folder as `/` or by its real path. Serving another folder shows that folder's.
- **`server.max_conversations` limits what is in memory.** A conversation past it is loaded back when asked for. Nothing limits how many are on disk, and each is held to `server.max_conversation_mb`.
- **What is saved is what continuing needs**, with attached files, not subagents' checkpoints. A saved response replays as its final event alone.
- A run in flight when the server stops is lost. If the database cannot be opened, the server says so and carries on without saving.

## MCP servers

Servers are listed in `~/.config/steve/mcp.json`, and work in the terminal chat and the server. Keep the file private, as it can hold tokens:

```json
{
  "mcpServers": {
    "local": {"command": "uv", "args": ["run", "server.py"], "env": {"API_KEY": "..."}},
    "bearer": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}},
    "oauth": {"url": "https://example.com/mcp", "auth": "oauth"}
  }
}
```

- **OAuth:** run `steve-agent mcp login NAME`. It opens your browser, registers itself with the server, and keeps tokens in `~/.config/steve/mcp-auth/`. They refresh by themselves.
- **Tools** are named `server_tool`, and the agent and its subagents can use them without asking, so only add servers you trust.
- **Connections are made at startup.** A server that fails, or needs signing in, is reported in `GET /v1/mcp` and the agent starts without it. Restart to pick up changes. Each tool call opens its own session, so a server cannot keep state between calls in a session.
- **MCP Apps:** the client declares the `io.modelcontextprotocol/ui` extension. Tools with `_meta.ui.visibility` lacking `model` are hidden from the model. A tool with a page gets an extra `app` field on its `mcp_call` item, and `app_result` once it finishes. `/v1/mcp/apps/call` only runs a tool on its app's own server that is visible to apps. The terminal chat has no way to show them.
- Only tools are supported: a server's prompts and resources are not offered to the agent, and resources are only read for apps.
