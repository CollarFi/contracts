from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CURSOR_KEY = "nextBlock"


def load_keeper_state(path: Path, start_block: int, *, cursor_key: str = DEFAULT_CURSOR_KEY) -> dict[str, Any]:
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object in {path}")
        return data
    return {cursor_key: int(start_block)}


def save_keeper_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_keeper_cursor(state: dict[str, Any], start_block: int, *, cursor_key: str = DEFAULT_CURSOR_KEY) -> int:
    return int(state.get(cursor_key, start_block))


def write_keeper_cursor(state: dict[str, Any], next_block: int, *, cursor_key: str = DEFAULT_CURSOR_KEY) -> int:
    state[cursor_key] = int(next_block)
    return int(next_block)
