from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    """持久化运行状态（上次抓取时间）。"""

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
