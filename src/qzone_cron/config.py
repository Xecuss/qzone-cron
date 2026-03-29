from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AuthConfig(BaseModel):
    uin: int
    auto_relogin: bool = False
    """检测到登录失效时自动触发重新登录流程，并将二维码发送至 Telegram（需配置 [telegram]）。"""


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
    stats_refresh_interval_minutes: int = 60
    """全量 stats 刷新间隔（分钟）：定期重拉近期说说以更新点赞/评论数。"""
    stats_refresh_window_hours: float = 6.0
    """全量 stats 刷新时向前看多久（小时）。"""
    feed_retention_hours: float = 48.0
    """feed_store 中 feed 详情的保留时长（小时），必须 >= stats_refresh_window_hours。"""

    @model_validator(mode="after")
    def _check_retention(self) -> "FetchConfig":
        if self.feed_retention_hours < self.stats_refresh_window_hours:
            raise ValueError(
                f"feed_retention_hours ({self.feed_retention_hours}) "
                f"必须 >= stats_refresh_window_hours ({self.stats_refresh_window_hours})"
            )
        return self


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


class OpenAIConfig(BaseModel):
    """顶层全局大模型配置，各插件未单独配置时自动回退至此。"""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"

    def as_dict(self) -> dict[str, Any]:
        """返回可直接传给插件的字典，仅包含非空字段。"""
        d: dict[str, Any] = {}
        if self.api_key:
            d["api_key"] = self.api_key
        if self.base_url:
            d["base_url"] = self.base_url
        if self.model:
            d["model"] = self.model
        return d


class Config(BaseModel):
    auth: AuthConfig
    storage: StorageConfig = Field(default_factory=StorageConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    plugins: dict[str, Any] = Field(default_factory=dict)


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
