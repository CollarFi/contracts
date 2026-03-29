#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from management.handlers.l2_rfq_common import (
    ZERO_ADDRESS,
    ZERO_BYTES32,
    normalize_rfq_execute_quote,
    sign_and_submit_rfq_execute_quote,
    submit_rfq_trade_confirmation,
)
from management.handlers.l2_rfq_jobs import process_rfq_jobs
from management.handlers.l2_derive_client import submit_with_retries
from py_lib.keeper_state import save_keeper_state


def ensure_rfq_trade_state(state: dict[str, Any]) -> None:
    if "rfqTradeQueue" not in state or not isinstance(state.get("rfqTradeQueue"), list):
        state["rfqTradeQueue"] = []
    if "rfqTradesCompleted" not in state or not isinstance(state.get("rfqTradesCompleted"), dict):
        state["rfqTradesCompleted"] = {}
    if "rfqJobs" not in state or not isinstance(state.get("rfqJobs"), dict):
        state["rfqJobs"] = {}
    if "rfqTrackedLoans" not in state or not isinstance(state.get("rfqTrackedLoans"), dict):
        state["rfqTrackedLoans"] = {}


def trade_queue_key(entry: dict[str, Any]) -> str:
    return f"{int(entry['loanId'])}:{int(entry['takerNonce'])}"


def _normalize_rfq_trade_entry(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"rfq trade entry must be an object, got: {raw!r}")

    def pick_int(*names: str, default: int | None = None) -> int:
        for name in names:
            if name in raw and raw[name] is not None:
                return int(raw[name])
        if default is not None:
            return default
        raise ValueError(f"missing required RFQ trade integer field from {names}")

    def pick_str(*names: str, default: str | None = None) -> str:
        for name in names:
            if name in raw and raw[name] is not None:
                return str(raw[name]).strip()
        if default is not None:
            return default
        raise ValueError(f"missing required RFQ trade string field from {names}")

    entry = {
        "loanId": pick_int("loanId", "loan_id"),
        "takerNonce": pick_int("takerNonce", "taker_nonce"),
        "callStrike": pick_int("callStrike", "call_strike"),
        "putStrike": pick_int("putStrike", "put_strike"),
        "expiry": pick_int("expiry"),
        "asset": pick_str("asset", default=ZERO_ADDRESS),
        "amount": pick_int("amount", default=0),
        "socketMessageId": pick_str("socketMessageId", "socket_message_id", default=ZERO_BYTES32),
        "quoteHash": pick_str("quoteHash", "quote_hash", default=ZERO_BYTES32),
        "realizedC": pick_int("realizedC", "realized_c", default=0),
        "enqueuedAt": pick_int("enqueuedAt", "enqueued_at", default=int(time.time())),
    }

    execute_raw = raw.get("executeQuote")
    if execute_raw is None:
        execute_raw = raw.get("execute_quote")
    if execute_raw is not None:
        entry["executeQuote"] = normalize_rfq_execute_quote(execute_raw)

    return entry


def _load_rfq_trade_entries(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"rfq trade file not found: {path}")

    parsed = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(parsed, dict) and isinstance(parsed.get("rfqTrades"), list):
        raw_entries = parsed["rfqTrades"]
    elif isinstance(parsed, list):
        raw_entries = parsed
    elif isinstance(parsed, dict):
        raw_entries = [parsed]
    else:
        raise ValueError(f"unexpected RFQ trade file payload: {parsed!r}")

    return [_normalize_rfq_trade_entry(entry) for entry in raw_entries]


def enqueue_rfq_trades_from_file(state: dict[str, Any], rfq_trade_file: Path | None) -> dict[str, int]:
    if rfq_trade_file is None:
        return {"added": 0, "skipped": 0}

    entries = _load_rfq_trade_entries(rfq_trade_file)
    existing = {trade_queue_key(entry) for entry in state["rfqTradeQueue"] if isinstance(entry, dict)}
    completed = {str(key) for key in state["rfqTradesCompleted"].keys()}

    added = 0
    skipped = 0
    for entry in entries:
        key = trade_queue_key(entry)
        if key in existing or key in completed:
            skipped += 1
            continue
        state["rfqTradeQueue"].append(entry)
        existing.add(key)
        added += 1
    return {"added": added, "skipped": skipped}


def _process_manual_rfq_trade_queue(
    runtime: Any,
    *,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    attempts_so_far: int,
) -> tuple[int, int]:
    attempts = 0
    sent = 0

    while attempts_so_far + attempts < runtime.max_per_tick and state["rfqTradeQueue"]:
        trade = state["rfqTradeQueue"][0]
        key = trade_queue_key(trade)
        attempts += 1

        item: dict[str, Any] = {
            "action": "RfqExecuteAndConfirm" if trade.get("executeQuote") else "RfqPostFillTradeConfirm",
            "loanId": str(trade["loanId"]),
            "takerNonce": str(trade["takerNonce"]),
            "queueKey": key,
            "status": "dry-run",
        }

        try:
            execute_state = trade.get("executeState") if isinstance(trade.get("executeState"), dict) else None
            if trade.get("executeQuote"):
                if execute_state is None:
                    execute_meta, api_attempt = submit_with_retries(
                        lambda: sign_and_submit_rfq_execute_quote(
                            rpc_url=runtime.rpc_url,
                            tsa_addr=runtime.tsa_addr,
                            rfq_module=runtime.rfq_module,
                            trade=trade,
                            account=runtime.account,
                            private_key=runtime.private_key,
                            api_url=runtime.api_url,
                            x_lyra_wallet=runtime.derive_wallet,
                            broadcast=runtime.broadcast,
                        ),
                        attempts=runtime.api_retry_attempts,
                        initial_delay_seconds=runtime.api_retry_initial_delay_seconds,
                        max_delay_seconds=runtime.api_retry_max_delay_seconds,
                    )
                    item["deriveApiAttempts"] = str(api_attempt)
                    item["deriveApi"] = execute_meta
                    if runtime.broadcast:
                        trade["executeState"] = {
                            "completedAt": int(time.time()),
                            "deriveApi": execute_meta,
                        }
                        save_keeper_state(runtime.state_file, state)
                else:
                    item["deriveApi"] = execute_state.get("deriveApi")

            trade_result = submit_rfq_trade_confirmation(
                rpc_url=runtime.rpc_url,
                receiver_addr=runtime.receiver_addr,
                trade=trade,
                lz_fee_buffer_bps=runtime.lz_fee_buffer_bps,
                broadcast=runtime.broadcast,
                account=runtime.account,
                private_key=runtime.private_key,
                from_addr=runtime.sender,
                unlocked=runtime.unlocked,
            )
            item.update(trade_result)

            if runtime.broadcast:
                state["rfqTradesCompleted"][key] = {
                    "completedAt": int(time.time()),
                    "loanId": int(trade["loanId"]),
                    "takerNonce": int(trade["takerNonce"]),
                    "tradeConfirmedTx": item.get("tradeConfirmedTx"),
                }
                if item.get("deriveApi") is not None:
                    state["rfqTradesCompleted"][key]["deriveApi"] = item["deriveApi"]
                state["rfqTradeQueue"].pop(0)
                save_keeper_state(runtime.state_file, state)
                item["status"] = "sent"
                sent += 1
        except Exception as exc:
            item["status"] = f"error: {exc}"

        handled.append(item)

    return attempts, sent


def process_rfq_trade_queue(
    runtime: Any,
    *,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    attempts_so_far: int,
) -> tuple[int, int]:
    auto_attempts, auto_sent = process_rfq_jobs(
        runtime,
        state=state,
        handled=handled,
        attempts_so_far=attempts_so_far,
    )
    manual_attempts, manual_sent = _process_manual_rfq_trade_queue(
        runtime,
        state=state,
        handled=handled,
        attempts_so_far=attempts_so_far + auto_attempts,
    )
    return auto_attempts + manual_attempts, auto_sent + manual_sent
