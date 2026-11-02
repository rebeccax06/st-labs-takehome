"""Small environment-based configuration for the database foundation."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str = "example-corp"
    source_config_path: str = str(Path(__file__).resolve().parents[1] / "examples/sample_sources.json")
    database_url: str = "sqlite:///cmdb.db"
    sqlite_busy_timeout_ms: int = Field(default=3000, ge=0, le=60000)

    openrouter_api_key: SecretStr | None = None
    openrouter_token_path: str = str(Path(__file__).resolve().parents[1] / "token.txt")
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_timeout_seconds: float = Field(default=30, gt=0, le=120)
    openrouter_max_tokens: int = Field(default=4096, ge=256, le=16384)

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for name in cls.model_fields:
            value = os.getenv(f"CMDB_{name.upper()}")
            if value is not None:
                values[name] = value
        if "openrouter_api_key" not in values and os.getenv("OPENROUTER_API_KEY"):
            values["openrouter_api_key"] = os.environ["OPENROUTER_API_KEY"]
        return cls.model_validate(values)
