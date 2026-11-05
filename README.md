# Steve Agent

A research and file assistant that runs on your machine and talks to a model you choose. There are two parts:

- **The CLI** (`steve-agent`) is the agent. It has a terminal chat, and can serve the OpenAI Responses API.
- **The desktop app** (Steve) is a chat window for the agent. It needs the CLI installed.

You need a model server that speaks the OpenAI Responses API, along with its address, model name and API key.

## Install the CLI

The CLI needs [uv](https://docs.astral.sh/uv/getting-started/installation/), which also fetches Python 3.13 or later if you don't have it.

```sh
uv tool install https://github.com/Qinbeans/steve-agent/releases/download/cli-v0.1.0/steve_agent-0.1.0-py3-none-any.whl
```

For a newer version, take the wheel's address from the [releases page](https://github.com/Qinbeans/steve-agent/releases).

Then set it up:

```sh
steve-agent            # asks for the model name, base URL and API key, then starts a chat; /quit leaves it
steve-agent web setup  # downloads the browser the agent reads the web with, about 120 MB
```

If you would rather use a browser you already have, skip `web setup` and choose one in the app's Settings, or set `web.browser` in `~/.config/steve/config.yaml`. It takes `chrome`, `msedge`, or the path of a Chromium-based browser. Steve never uses your profile or logins.

If `steve-agent` isn't found, run `uv tool update-shell` and open a new terminal.

### Linux: let the agent run commands

The agent runs commands in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox, and without it the agent has no command tool.

```sh
sudo dnf install bubblewrap   # Fedora
sudo apt install bubblewrap   # Debian and Ubuntu
```

Windows and macOS don't have bubblewrap, so the agent can't run commands there. Everything else works the same.

## Install the desktop app

Install the CLI first.

On Fedora or RHEL, `dnf` can install straight from the release:

```sh
sudo dnf install https://github.com/Qinbeans/steve-agent/releases/download/desktop-v0.1.0/Steve-0.1.0-1.x86_64.rpm
```

On other systems, download the package for your system from the [latest `desktop-` release](https://github.com/Qinbeans/steve-agent/releases).

| System | File | How to install |
|---|---|---|
| Debian, Ubuntu | `Steve_<version>_amd64.deb` | `sudo apt install ./Steve_<version>_amd64.deb` |
| Windows | `Steve_<version>_x64-setup.exe` | Run the installer |
| macOS (Intel and Apple silicon) | `Steve_<version>_universal.dmg` | Open it and drag Steve to Applications |

The Windows and macOS builds aren't code signed, and haven't been tried on real machines yet, so please [open an issue](https://github.com/Qinbeans/steve-agent/issues) if something goes wrong.

- **Windows:** if SmartScreen stops the installer, choose **More info**, then **Run anyway**.
- **macOS:** if Steve won't open or is reported as damaged, run this once:

  ```sh
  xattr -dr com.apple.quarantine /Applications/Steve.app
  ```

## Update

Install the new wheel over the old one, using its address from the releases page:

```sh
uv tool install --force <address of the new wheel>
```

To update the app, install the new package the same way as the first one. Your settings and conversations are kept in `~/.config/steve`.

## Troubleshooting

- **The app says it can't find `steve-agent`:** it looks on your `PATH` and in `~/.local/bin`. Check that `steve-agent --help` works in a terminal. If the command is somewhere else, set `STEVE_AGENT_BIN` to its full path.
- **The app asks you to run `steve-agent`:** there is no config yet. Run it once in a terminal, as above.
- **The web tools say the browser could not start:** run `steve-agent web setup`, or choose another browser in Settings.

## More

- [CLI documentation](steve-cli/README.md): configuration, the server API and how the agent behaves.
- [Desktop app documentation](steve-desktop/README.md): building from source and how the app works.
