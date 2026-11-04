from pydantic import BaseModel, Field


class SteveModelConfig(BaseModel):
    api_key: str
    model: str
    base_url: str
    use_responses_api: bool = True


class SteveServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str | None = None
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "[::1]"]
    max_conversations: int = 256


class SteveConfig(BaseModel):
    model: SteveModelConfig
    server: SteveServerConfig = Field(default_factory=SteveServerConfig)
