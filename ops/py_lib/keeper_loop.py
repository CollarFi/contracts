from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def resolve_start_block(
    *,
    state_file: Path,
    start_block: int,
    backfill_blocks: int,
    latest_block: Callable[[], int],
) -> int:
    if state_file.is_file() or start_block != 0:
        return int(start_block)
    return max(0, int(latest_block()) - int(backfill_blocks))


def resolve_scan_range(next_block: int, latest_block: int) -> tuple[int, int] | None:
    if latest_block < next_block:
        return None
    return int(next_block), int(latest_block)


def should_advance_cursor(
    *,
    broadcast: bool,
    attempts: int,
    sent: int,
    advance_on_dry_run: bool,
    blocked: bool = False,
) -> bool:
    if blocked:
        return False
    if not broadcast:
        return advance_on_dry_run
    return attempts == sent
