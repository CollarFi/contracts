#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lz_harness.common import cast_call, run  # noqa: E402
from management.l2_common import derive_reissue_nonce, latest_block_timestamp, tsa_signature_expiry_window  # noqa: E402

# CollarLZMessages.Action enum
ACTION_DEPOSIT_INTENT = 0
ACTION_RETURN_REQUEST = 1
ACTION_SETTLEMENT_REPORT = 2
ACTION_DEPOSIT_CONFIRMED = 3
ACTION_COLLATERAL_RETURNED = 4
ACTION_TRADE_CONFIRMED = 5
ACTION_MANDATE_CREATED = 6

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)


def action_name(action: int) -> str:
    return {
        ACTION_DEPOSIT_INTENT: "DepositIntent",
        ACTION_RETURN_REQUEST: "ReturnRequest",
        ACTION_SETTLEMENT_REPORT: "SettlementReport",
        ACTION_DEPOSIT_CONFIRMED: "DepositConfirmed",
        ACTION_COLLATERAL_RETURNED: "CollateralReturned",
        ACTION_TRADE_CONFIRMED: "TradeConfirmed",
        ACTION_MANDATE_CREATED: "MandateCreated",
    }.get(action, f"Unknown({action})")


def parse_uint(raw: str) -> int:
    token = raw.strip().split()[0]
    return int(token)


def parse_pending_message(raw: str) -> dict[str, Any]:
    stripped = re.sub(r"\s*\[[^\]]+\]", "", raw.strip())
    match = re.match(
        r"^\((\d+),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]*)\)$",
        stripped,
    )
    if match:
        return {
            "action": int(match.group(1)),
            "loanId": int(match.group(2)),
            "asset": match.group(3),
            "amount": int(match.group(4)),
            "recipient": match.group(5),
            "subaccountId": int(match.group(6)),
            "socketMessageId": match.group(7),
            "secondaryAmount": int(match.group(8)),
            "quoteHash": match.group(9),
            "takerNonce": int(match.group(10)),
            "data": match.group(11),
        }

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) == 11:
        return {
            "action": int(lines[0]),
            "loanId": int(lines[1]),
            "asset": lines[2],
            "amount": int(lines[3]),
            "recipient": lines[4],
            "subaccountId": int(lines[5]),
            "socketMessageId": lines[6],
            "secondaryAmount": int(lines[7]),
            "quoteHash": lines[8],
            "takerNonce": int(lines[9]),
            "data": lines[10],
        }

    raise ValueError(f"failed to parse pendingMessages tuple: {raw}")


def abi_encode(signature: str, *args: Any) -> str:
    return run(["cast", "abi-encode", signature, *[str(arg) for arg in args]]).strip()


def quote_ack_native_fee(rpc_url: str, receiver_addr: str, pending_raw: str) -> int:
    msg = parse_pending_message(pending_raw)
    options = cast_call(rpc_url, receiver_addr, "defaultOptions()(bytes)")
    message_tuple = (
        f"({ACTION_DEPOSIT_CONFIRMED},"
        f"{msg['loanId']},"
        f"{msg['asset']},"
        f"{msg['amount']},"
        f"{msg['recipient']},"
        f"{msg['subaccountId']},"
        f"{msg['socketMessageId']},"
        f"0,"
        f"0x{'0' * 64},"
        f"0,"
        f"0x)"
    )
    quote_raw = cast_call(
        rpc_url,
        receiver_addr,
        "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
        message_tuple,
        options,
    )
    cleaned = quote_raw.strip()
    if cleaned.startswith("("):
        first = cleaned.split(",", 1)[0].lstrip("(").strip()
        return parse_uint(first)
    return parse_uint(cleaned)


def quote_trade_confirm_native_fee(
    *,
    rpc_url: str,
    receiver_addr: str,
    asset: str,
    amount: int,
    socket_message_id: str,
    quote_hash: str,
    taker_nonce: int,
    call_strike: int,
    put_strike: int,
    expiry: int,
    loan_id: int,
    realized_c: int,
) -> int:
    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)").strip()
    vault_recipient = cast_call(rpc_url, receiver_addr, "vaultRecipient()(address)").strip()
    subaccount_id = parse_uint(cast_call(rpc_url, tsa_addr, "subAccount()(uint256)"))
    options = cast_call(rpc_url, receiver_addr, "defaultOptions()(bytes)")
    payload = abi_encode(
        "f(uint256,uint256,uint64,int256)",
        call_strike,
        put_strike,
        expiry,
        realized_c,
    )
    message_tuple = (
        f"({ACTION_TRADE_CONFIRMED},"
        f"{loan_id},"
        f"{asset},"
        f"{amount},"
        f"{vault_recipient},"
        f"{subaccount_id},"
        f"{socket_message_id},"
        f"0,"
        f"{quote_hash},"
        f"{taker_nonce},"
        f"{payload})"
    )
    quote_raw = cast_call(
        rpc_url,
        receiver_addr,
        "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
        message_tuple,
        options,
    ).strip()
    if quote_raw.startswith("("):
        return parse_uint(quote_raw.split(",", 1)[0].lstrip("(").strip())
    return parse_uint(quote_raw)


def fresh_action_expiry(rpc_url: str, tsa_addr: str) -> int:
    min_sig, max_sig = tsa_signature_expiry_window(rpc_url, tsa_addr)
    chain_now = latest_block_timestamp(rpc_url)
    cushion = max(30, min(300, min_sig // 5 if min_sig > 0 else 30))
    expiry = chain_now + min_sig + cushion
    upper_bound = chain_now + max_sig - 1
    if expiry > upper_bound:
        expiry = upper_bound
    if expiry <= chain_now + min_sig:
        raise RuntimeError(
            "cannot derive valid action expiry window: "
            f"chain_now={chain_now} min={min_sig} max={max_sig}"
        )
    return expiry


def fresh_action_nonce_and_expiry(rpc_url: str, tsa_addr: str, loan_id: int) -> tuple[int, int]:
    expiry = fresh_action_expiry(rpc_url, tsa_addr)
    chain_now = latest_block_timestamp(rpc_url)
    return derive_reissue_nonce(chain_now, loan_id), expiry


def format_action_tuple(action: dict[str, Any]) -> str:
    return (
        "("
        f"{action['subaccountId']},"
        f"{action['nonce']},"
        f"{action['module']},"
        f"{action['data']},"
        f"{action['expiry']},"
        f"{action['owner']},"
        f"{action['signer']}"
        ")"
    )


def build_pending_action(
    *,
    action_type: int,
    pending_message: dict[str, Any],
    tsa_addr: str,
    deposit_module: str,
    withdrawal_module: str,
    wrapped_deposit_asset: str,
    rpc_url: str,
) -> dict[str, Any]:
    nonce, expiry = fresh_action_nonce_and_expiry(rpc_url, tsa_addr, int(pending_message["loanId"]))
    if action_type == ACTION_DEPOSIT_INTENT:
        data = abi_encode(
            "f(uint256,address,address)",
            int(pending_message["amount"]),
            wrapped_deposit_asset,
            ZERO_ADDRESS,
        )
        module = deposit_module
    elif action_type == ACTION_RETURN_REQUEST:
        data = abi_encode(
            "f(address,uint256)",
            wrapped_deposit_asset,
            int(pending_message["amount"]),
        )
        module = withdrawal_module
    else:
        raise RuntimeError(f"unsupported pending action type {action_type}")

    action = {
        "subaccountId": int(pending_message["subaccountId"]),
        "nonce": nonce,
        "module": module,
        "data": data,
        "expiry": expiry,
        "owner": tsa_addr,
        "signer": tsa_addr,
    }
    action["typedDataHash"] = cast_call(
        rpc_url,
        tsa_addr,
        "getActionTypedDataHash((uint256,uint256,address,bytes,uint256,address,address))(bytes32)",
        format_action_tuple(action),
    ).strip()
    return action
