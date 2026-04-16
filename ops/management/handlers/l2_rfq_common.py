#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lz_harness.common import cast_call, cast_send, run  # noqa: E402
from management.l2_common import extract_tx_hash, http_post_json, wallet_sign  # noqa: E402
from management.handlers.l2_derive_client import decimal_1e18_to_int, normalize_decimal_str  # noqa: E402
from management.handlers.l2_tsa_actions import (  # noqa: E402
    ZERO_ADDRESS,
    ZERO_BYTES32,
    abi_encode,
    format_action_tuple,
    fresh_action_expiry,
    quote_trade_confirm_native_fee,
)


def rfq_inverse_direction(direction: str) -> str:
    normalized = direction.strip().lower()
    if normalized == "buy":
        return "sell"
    if normalized == "sell":
        return "buy"
    raise ValueError(f"invalid RFQ direction: {direction}")


def rfq_signed_amount(direction: str, global_direction: str, amount: str) -> int:
    normalized_direction = direction.strip().lower()
    if normalized_direction not in {"buy", "sell"}:
        raise ValueError(f"invalid RFQ leg direction: {direction}")
    normalized_global = global_direction.strip().lower()
    if normalized_global not in {"buy", "sell"}:
        raise ValueError(f"invalid RFQ global direction: {global_direction}")
    leg_sign = 1 if normalized_direction == "buy" else -1
    quote_sign = 1 if normalized_global == "buy" else -1
    return decimal_1e18_to_int(amount) * leg_sign * quote_sign


def format_trade_tuple(asset_address: str, sub_id: int, price: str, amount: int) -> str:
    return f"({asset_address},{sub_id},{decimal_1e18_to_int(price)},{amount})"


def build_rfq_trade_array_literal(legs: list[dict[str, Any]], global_direction: str) -> str:
    tuples = [
        format_trade_tuple(
            str(leg["assetAddress"]),
            int(leg["subId"]),
            str(leg["price"]),
            rfq_signed_amount(str(leg["direction"]), global_direction, str(leg["amount"])),
        )
        for leg in legs
    ]
    return "[" + ",".join(tuples) + "]"


def build_rfq_sign_payloads(loan_id: int, execute_quote: dict[str, Any]) -> tuple[str, str]:
    maker_trades = build_rfq_trade_array_literal(execute_quote["legs"], rfq_inverse_direction(str(execute_quote["direction"])))
    maker_trades_data = abi_encode("f((address,uint256,uint256,int256)[])", maker_trades)
    extra_data = abi_encode("f(uint256,bytes)", loan_id, maker_trades_data)
    order_hash = run(["cast", "keccak", maker_trades_data]).splitlines()[0].strip()
    return maker_trades_data, abi_encode("f(bytes32,uint256)", order_hash, int(execute_quote["maxFee1e18"]))


def normalize_rfq_execute_leg(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"RFQ execute leg must be an object, got: {raw!r}")

    def pick_str(*names: str) -> str:
        for name in names:
            if raw.get(name) is not None:
                return str(raw[name]).strip()
        raise ValueError(f"missing required RFQ execute leg field from {names}")

    def pick_int(*names: str) -> int:
        for name in names:
            if raw.get(name) is not None:
                return int(raw[name])
        raise ValueError(f"missing required RFQ execute leg field from {names}")

    return {
        "instrumentName": pick_str("instrumentName", "instrument_name"),
        "direction": pick_str("direction"),
        "assetAddress": pick_str("assetAddress", "asset_address"),
        "subId": pick_int("subId", "sub_id"),
        "price": normalize_decimal_str(pick_str("price")),
        "amount": normalize_decimal_str(pick_str("amount")),
    }


def normalize_rfq_execute_quote(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"RFQ execute payload must be an object, got: {raw!r}")

    def pick_str(*names: str, default: str | None = None) -> str:
        for name in names:
            if raw.get(name) is not None:
                return str(raw[name]).strip()
        if default is not None:
            return default
        raise ValueError(f"missing required RFQ execute string field from {names}")

    def pick_int(*names: str, default: int | None = None) -> int:
        for name in names:
            if raw.get(name) is not None:
                return int(raw[name])
        if default is not None:
            return default
        raise ValueError(f"missing required RFQ execute integer field from {names}")

    legs_raw = raw.get("legs")
    if not isinstance(legs_raw, list) or not legs_raw:
        raise ValueError("RFQ execute payload requires a non-empty `legs` array")

    max_fee = normalize_decimal_str(pick_str("maxFee", "max_fee"))
    return {
        "rfqId": pick_str("rfqId", "rfq_id"),
        "quoteId": pick_str("quoteId", "quote_id"),
        "subaccountId": pick_int("subaccountId", "subaccount_id"),
        "direction": pick_str("direction"),
        "maxFee": max_fee,
        "maxFee1e18": decimal_1e18_to_int(max_fee),
        "label": pick_str("label", default=""),
        "legs": [normalize_rfq_execute_leg(entry) for entry in legs_raw],
    }


def build_rfq_execute_action(
    *,
    rpc_url: str,
    tsa_addr: str,
    rfq_module: str,
    trade: dict[str, Any],
) -> dict[str, Any]:
    execute_quote = trade.get("executeQuote")
    if not isinstance(execute_quote, dict):
        raise ValueError("RFQ trade entry missing executeQuote payload")

    expiry = fresh_action_expiry(rpc_url, tsa_addr)
    _, action_data = build_rfq_sign_payloads(int(trade["loanId"]), execute_quote)

    action = {
        "subaccountId": int(execute_quote["subaccountId"]),
        "nonce": int(trade["takerNonce"]),
        "module": rfq_module,
        "data": action_data,
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


def sign_and_submit_rfq_execute_quote(
    *,
    rpc_url: str,
    tsa_addr: str,
    rfq_module: str,
    trade: dict[str, Any],
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    broadcast: bool,
) -> dict[str, Any]:
    execute_quote = trade.get("executeQuote")
    if not isinstance(execute_quote, dict):
        raise ValueError("RFQ trade entry missing executeQuote payload")
    if broadcast and not (account or private_key):
        raise ValueError("RFQ execute_quote submission requires ACCOUNT or --private-key")

    action = build_rfq_execute_action(rpc_url=rpc_url, tsa_addr=tsa_addr, rfq_module=rfq_module, trade=trade)
    maker_trades_data, _ = build_rfq_sign_payloads(int(trade["loanId"]), execute_quote)
    signer_sig = wallet_sign(str(action["typedDataHash"]), no_hash=True, account=account, private_key=private_key)

    out: dict[str, Any] = {
        "typedDataHash": str(action["typedDataHash"]),
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
        "rfqId": str(execute_quote["rfqId"]),
        "quoteId": str(execute_quote["quoteId"]),
    }
    if not broadcast:
        return out

    permit_tx = cast_send(
        rpc_url,
        account or None,
        tsa_addr,
        "signActionViaPermit((uint256,uint256,address,bytes,uint256,address,address),bytes,bytes)",
        format_action_tuple(action),
        abi_encode("f(uint256,bytes)", int(trade["loanId"]), maker_trades_data),
        signer_sig,
        private_key=private_key or None,
    )

    ts_ms = str(int(time.time() * 1000))
    auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    status, private_resp = http_post_json(
        f"{api_url.rstrip('/')}/private/execute_quote",
        {
            "subaccount_id": int(execute_quote["subaccountId"]),
            "nonce": int(action["nonce"]),
            "signer": tsa_addr,
            "signature_expiry_sec": int(action["expiry"]),
            "signature": signer_sig,
            "direction": str(execute_quote["direction"]),
            "legs": [
                {
                    "instrument_name": str(leg["instrumentName"]),
                    "direction": str(leg["direction"]),
                    "price": str(leg["price"]),
                    "amount": str(leg["amount"]),
                }
                for leg in execute_quote["legs"]
            ],
            "max_fee": str(execute_quote["maxFee"]),
            "label": str(execute_quote["label"]),
            "rfq_id": str(execute_quote["rfqId"]),
            "quote_id": str(execute_quote["quoteId"]),
        },
        headers={
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": ts_ms,
            "X-LyraSignature": auth_sig,
        },
    )
    if status >= 400 or private_resp.get("error"):
        raise RuntimeError(f"private/execute_quote failed ({status}): {json.dumps(private_resp)}")

    out["permitSignerSig"] = signer_sig
    out["signActionViaPermitTx"] = extract_tx_hash(permit_tx)
    out["apiResult"] = private_resp.get("result")
    out["apiId"] = private_resp.get("id")
    return out


def submit_rfq_trade_confirmation(
    *,
    rpc_url: str,
    receiver_addr: str,
    trade: dict[str, Any],
    lz_fee_buffer_bps: int,
    broadcast: bool,
    account: str,
    private_key: str,
    from_addr: str,
    unlocked: bool,
) -> dict[str, Any]:
    quoted_fee = quote_trade_confirm_native_fee(
        rpc_url=rpc_url,
        receiver_addr=receiver_addr,
        asset=str(trade["asset"]),
        amount=int(trade["amount"]),
        socket_message_id=str(trade["socketMessageId"]),
        quote_hash=str(trade["quoteHash"]),
        taker_nonce=int(trade["takerNonce"]),
        call_strike=int(trade["callStrike"]),
        put_strike=int(trade["putStrike"]),
        expiry=int(trade["expiry"]),
        loan_id=int(trade["loanId"]),
        realized_c=int(trade["realizedC"]),
    )
    fee_with_buffer = quoted_fee + (quoted_fee * lz_fee_buffer_bps) // 10_000

    out: dict[str, Any] = {
        "quotedLzFee": str(quoted_fee),
        "quotedLzFeeWithBuffer": str(fee_with_buffer),
    }
    if not broadcast:
        return out

    record_tx = cast_send(
        rpc_url,
        account or None,
        receiver_addr,
        "recordTradeExecuted(uint256,uint256)",
        str(trade["loanId"]),
        str(trade["takerNonce"]),
        private_key=private_key or None,
        from_addr=from_addr or None,
        unlocked=unlocked,
    )
    confirm_tx = cast_send(
        rpc_url,
        account or None,
        receiver_addr,
        "sendTradeConfirmed((uint256,address,uint256,bytes32,bytes32,uint256,uint256,uint256,uint64,int256))",
        (
            f"({trade['loanId']},{trade['asset']},{trade['amount']},{trade['socketMessageId']},{trade['quoteHash']},"
            f"{trade['takerNonce']},{trade['callStrike']},{trade['putStrike']},{trade['expiry']},{trade['realizedC']})"
        ),
        value_wei=str(fee_with_buffer),
        private_key=private_key or None,
        from_addr=from_addr or None,
        unlocked=unlocked,
    )
    out["recordTradeExecutedTx"] = extract_tx_hash(record_tx)
    out["tradeConfirmedTx"] = extract_tx_hash(confirm_tx)
    out["sendTradeConfirmedTx"] = out["tradeConfirmedTx"]
    return out

