# Steve Desktop

A desktop app for Steve Agent, built with Tauri, SvelteKit and shadcn-svelte. It runs the `steve-agent` CLI as a background server and talks to it over the Responses API, so the CLI needs to be installed and configured.

## Run it

```sh
cd steve-cli && uv sync          # once, and run `uv run steve-agent` once if you have no config yet
cd ../steve-desktop && pnpm install
pnpm tauri dev
```

The app looks for the CLI in this order: `STEVE_AGENT_BIN`, `PATH`, `~/.local/bin` (where `uv tool install ./steve-cli` puts it), and in a debug build `../steve-cli/.venv`. A release build does not bundle the CLI, so install it first.

`pnpm dev` runs the UI alone in a browser with a scripted mock, for working on the interface.

## Build it

```sh
pnpm tauri build
```

This writes packages for the system it runs on to `src-tauri/target/release/bundle/`: an `.rpm` and a `.deb` on Linux, an installer on Windows, and an `.app` and a `.dmg` on macOS. There is deliberately no AppImage, which would ship its own copies of WebKitGTK and GTK. Each format uses the system's web view instead.

`.github/workflows/build.yml` builds all three systems. On every push to main, `release.yml` runs it, and makes a release for an app whose version differs from its latest tag: `desktop-v1.2.3` with these packages, and `cli-v1.2.3` with the CLI's wheel. To release the app, change its version in `tauri.conf.json`, `package.json` and `Cargo.toml`, which have to agree. For the CLI, change it in `pyproject.toml`. Windows and macOS builds are not code signed, so those systems warn before opening them.

The packages don't include the CLI, so whoever runs one also needs it, and the browser for the web tools:

```sh
uv tool install ./steve-cli
steve-agent web setup      # or choose your own browser in Settings
```

Commands need `bubblewrap`, which is Linux only. On Windows and macOS the agent has no command tool.

## How it works

- `src-tauri/src/backend.rs` starts `steve-agent serve` on a free loopback port, waits for `/health`, and stops the server when the app exits. On Linux the kernel also stops it if the app crashes.
- All traffic goes through Rust and reaches the UI over Tauri channels. The server has no CORS by design, and this keeps `server.api_key` out of the webview.
- `src/lib/chat/turn.ts` folds stream events into what the UI shows. The terminal event's output replaces what was streamed, so partial output from a retried attempt disappears.
- The folder selector is a dropdown under the prompt box, listing recent folders with an "Other…" option that opens the folder picker. The agent starts with no folder until one is chosen. The choice and recent folders are saved in the app's config directory, and the folder is passed as `steve-agent serve --workspace`.
- Files can be attached with the paperclip or dropped on the window. Rust reads them and sends them inline, so the webview never holds their contents.
- The pie in the header shows the last prompt's token breakdown. Clicking it compacts the conversation.
- The window stays hidden until its page has loaded, or for 5 seconds at most. A splash in `src/app.html` shows from the first paint until the agent is ready or has failed.
- The look is liquid glass, in `src/routes/layout.css`: frosted panels over a liquid gradient, with the conversation directly on the gradient. The mascot (`static/splash.png`) appears only on the startup splash. Without blur support, or with reduced transparency requested, panels are solid.
- The menu bar is native, and its items reach the UI as `menu` events. The sidebar can be dragged wider or narrower, or moved with the arrow keys when focused, and remembers its width. Double-clicking its edge resets it.
- Reasoning shows while the model thinks and folds away when it finishes.
- The MCP menu under the prompt box lists servers with their status, adds one (a local command, or a URL with a bearer token or OAuth), signs in and removes. It edits `~/.config/steve/mcp.json`, which is private to you, and restarts the CLI. Tokens, headers, arguments and environment variables never return to the webview.
- The skills menu lists your skills and the working folder's, copies a skill's folder into `~/.config/steve/agents/skills` and removes one. Skills need no restart.
- A tool with an MCP App shows the app's page below its row. The page is served from its own `mcp-app` scheme, in an iframe with only `allow-scripts`, so it has an opaque origin with no access to this window, its storage or the app's commands. Its Content-Security-Policy comes from what its resource declares, and can never include this computer or an IP address.
- An app's `ui/message` goes in the composer rather than being sent. `src/lib/chat/mcp-host.ts` is the host's side of the protocol.
- The mode selector under the prompt box sets what the next message may do: manual asks before anything changes, auto does not ask, and planning only offers tools that change nothing. It starts on manual and is remembered. The CLI enforces it, for subagents too.
- When a run needs an answer, an approval card or an MCP elicitation form appears in the conversation: the server's prompt, then its choices and inputs. The tool call waits for up to 15 minutes.
- Agent messages can be copied, read aloud with the system's voices, rated and retried. User messages can be retried, edited and copied. Retrying or editing drops that exchange and later ones, asking first if there are later ones, and the server forks from the exchange before. Ratings are appended to `~/.config/steve/feedback.jsonl` with the prompt and reply, and go nowhere else.
- Agent and user messages both render as Markdown. Output is sanitized before rendering, and the CSP blocks remote content. Links open in your browser.

## Limits

- **Settings rewrites `config.yaml`.** Other values are kept, but comments in the file are not. Saving restarts the agent.
- **Commands are Linux only,** as they need bubblewrap. Elsewhere the agent does not get the tool.

- Conversations are saved by the CLI, per working folder, and come back with the app. Ratings and the paths of attached files are not saved with them, so a saved message with files cannot be retried or edited.
- Changing the working folder restarts the CLI, which stops a reply in progress, so the app asks first if there is one. Each folder has its own conversations.
- Attachments are sent as files the agent reads with its tools. PDFs reach the model as text, and images need a model with vision.
- MCP Apps are inline only, and camera, microphone, location and clipboard are not granted. Apps get no `localStorage`.
- Adding, removing or signing in to an MCP server restarts the CLI, which stops a reply in progress. The dialog says so.
- Signing in cannot be cancelled from the app. If you close the browser window, it gives up after 5 minutes.
- No in-app setup: with no config, the app asks you to run `steve-agent` once in a terminal.

## Tests

```sh
pnpm test                         # event reducer
pnpm check                        # types
(cd src-tauri && cargo test)      # SSE parser, and the bridge against the real CLI with a fake model
pnpm tauri build --debug --no-bundle && pnpm e2e
```

`pnpm e2e` drives the real app over WebDriver, with a throwaway `HOME` and a fake model. It needs `cargo install tauri-driver` and `WebKitWebDriver`. The window opens on your desktop unless you run it on another display, for example:

```sh
weston --backend=headless --socket=steve-e2e --no-config &
env -u DISPLAY WAYLAND_DISPLAY=steve-e2e GDK_BACKEND=wayland pnpm e2e
```
