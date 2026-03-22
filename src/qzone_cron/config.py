from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuthConfig(BaseModel):
    uin: int


class StorageConfig(BaseModel):
    data_dir: str = "~/.local/share/qzone-cron"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def cookie_file(self) -> Path:
        return self.data_path / "cookies.json"

    @property
    def state_file(self) -> Path:
        return self.data_path / "state.json"


class FetchConfig(BaseModel):
    max_pages: int = 10
    time_window_hours: float = 24.0
    fetch_interval_minutes: int = 5
    """Crontab 每次执行时，若距上次实际抓取不足此分钟数则跳过抓取，仅执行插件维护任务。"""


class Config(BaseModel):
    auth: AuthConfig
    storage: StorageConfig = Field(default_factory=StorageConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    plugins: dict[str, Any] = Field(default_factory=dict)


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
