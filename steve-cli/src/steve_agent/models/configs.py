from pydantic import BaseModel, Field


class SteveModelConfig(BaseModel):
    api_key: str
    model: str
    base_url: str
    use_responses_api: bool = True
    # The model's context size. Automatic summarization then starts at 85% of it, instead of at 170k tokens.
    max_input_tokens: int | None = Field(default=None, gt=0)


class SteveServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str | None = None
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]
    max_conversations: int = 256
    # Above this a conversation is pruned, and it stops accepting turns if pruning cannot get it under.
    max_conversation_mb: float = Field(default=64, gt=0)
    # Times a failed run is retried from its last checkpoint. Tools that finished are not run again.
    response_retries: int = Field(default=1, ge=0)
    # The directory the agent works in. `serve` never falls back to where it was started.
    workspace: str | None = None
    # Keep conversations in ~/.config/steve/conversations.db, so that they survive a restart.
    save_conversations: bool = True
    # Largest asset a request can attach.
    max_asset_mb: float = Field(default=25, gt=0)


class SteveMemoryConfig(BaseModel):
    # Above this size, the agent is told to prune a MEMORY.md file.
    max_file_chars: int = Field(default=8000, gt=0)


class SteveSandboxConfig(BaseModel):
    # Commands need bubblewrap and a working folder. Without them the agent has no command tool at all.
    enabled: bool = True
    # Off, commands reach nothing, including this machine's own services.
    network: bool = False
    # The longest a command can run. The agent can ask for less, not more.
    timeout_seconds: int = Field(default=120, gt=0)


class SteveWebConfig(BaseModel):
    enabled: bool = True
    # What may be reached despite being private: a name, *.example.com, an address, or 10.0.0.0/8.
    allowed_hosts: list[str] = []
    # Applies to both web tools, so that fetching page by page is not a way around it.
    obey_robots: bool = True
    # chrome, msedge or the path of a Chromium-based browser. Empty uses the one `web setup` installs.
    browser: str | None = None
    # A page's limit.
    timeout_seconds: int = Field(default=30, gt=0)
    # An address containing {query}, which lets web_crawl search. No default, as search pages refuse bots
    # or disallow them in robots.txt.
    search_url: str | None = None


class SteveConfig(BaseModel):
    model: SteveModelConfig
    memory: SteveMemoryConfig = Field(default_factory=SteveMemoryConfig)
    server: SteveServerConfig = Field(default_factory=SteveServerConfig)
    sandbox: SteveSandboxConfig = Field(default_factory=SteveSandboxConfig)
    web: SteveWebConfig = Field(default_factory=SteveWebConfig)
