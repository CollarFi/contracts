from __future__ import annotations

import json
from typing import Any, Iterable

from .runtime import run


def _to_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return default
    stripped = value.strip()
    if not stripped or stripped == "0x":
        return default
    return int(stripped, 16) if stripped.startswith("0x") else int(stripped)


def log_order_key(log: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        _to_int(log.get("blockNumber")),
        _to_int(log.get("transactionIndex")),
        _to_int(log.get("logIndex")),
        str(log.get("transactionHash", "")),
    )


def order_logs(logs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(logs, key=log_order_key)


def get_message_received_logs(rpc_url: str, receiver_addr: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    out = run(
        [
            "cast",
            "logs",
            "MessageReceived(bytes32,uint8,uint256)",
            "--address",
            receiver_addr,
            "--from-block",
            str(from_block),
            "--to-block",
            str(to_block),
            "--rpc-url",
            rpc_url,
            "--json",
        ]
    )
    parsed = json.loads(out)
    if not isinstance(parsed, list):
        raise RuntimeError(f"unexpected cast logs output: {out}")
    return order_logs(parsed)


def topic_hex(log: dict[str, Any], index: int) -> str | None:
    topics = log.get("topics", [])
    if not isinstance(topics, list) or len(topics) <= index:
        return None
    value = topics[index]
    return value if isinstance(value, str) and value else None


def topic_int(log: dict[str, Any], index: int) -> int | None:
    raw = topic_hex(log, index)
    if raw is None:
        return None
    return _to_int(raw, default=0)


def data_int(log: dict[str, Any], *, default: int = 0) -> int:
    return _to_int(log.get("data"), default=default)
