from __future__ import annotations

import json
import time as _time
from pathlib import Path
from typing import Any


class State:
    """持久化运行状态（上次抓取时间、feed 详情缓存等）。"""

    def __init__(self, state_file: Path) -> None:
        self._file = state_file
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            with open(self._file) as f:
                self._data = json.load(f)

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def last_fetch_time(self) -> float:
        return float(self._data.get("last_fetch_time", 0.0))

    @last_fetch_time.setter
    def last_fetch_time(self, value: float) -> None:
        self._data["last_fetch_time"] = value

    @property
    def last_fetched_at(self) -> float:
        """上次实际触发抓取的墙上时间（time.time()），用于控制抓取间隔。"""
        return float(self._data.get("last_fetched_at", 0.0))

    @last_fetched_at.setter
    def last_fetched_at(self, value: float) -> None:
        self._data["last_fetched_at"] = value

    @property
    def last_full_refresh_at(self) -> float:
        """上次全量 stats 刷新的墙上时间。"""
        return float(self._data.get("last_full_refresh_at", 0.0))

    @last_full_refresh_at.setter
    def last_full_refresh_at(self, value: float) -> None:
        self._data["last_full_refresh_at"] = value

    @property
    def feed_store(self) -> dict[str, Any]:
        """feed 详情存储：fid → 序列化的 feed 字典（不含 AI 生成内容）。"""
        return self._data.setdefault("feed_store", {})

    def expire_feeds(self, retention_hours: float) -> int:
        """清除早于 retention_hours 的 feed，返回被清除的条数。"""
        cutoff = _time.time() - retention_hours * 3600
        store = self.feed_store
        to_remove = [fid for fid, f in store.items() if f.get("time", 0) < cutoff]
        for fid in to_remove:
            del store[fid]
        return len(to_remove)
