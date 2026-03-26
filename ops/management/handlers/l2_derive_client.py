#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import sys
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Callable, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lz_harness.common import cast_call  # noqa: E402
from management.l2_common import http_post_json, is_retryable_signature_sync_error_text, wallet_sign  # noqa: E402
from management.handlers.l2_tsa_actions import (  # noqa: E402
    ACTION_DEPOSIT_INTENT,
    ACTION_RETURN_REQUEST,
    ZERO_ADDRESS,
    fresh_action_nonce_and_expiry,
    parse_uint,
)

T = TypeVar("T")


def to_decimal_str(amount: int, decimals: int) -> str:
    getcontext().prec = 80
    quantized = Decimal(amount) / (Decimal(10) ** Decimal(decimals))
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_decimal_str(raw: Any) -> str:
    getcontext().prec = 80
    dec = Decimal(str(raw).strip())
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_1e18_to_int(raw: Any) -> int:
    getcontext().prec = 80
    return int(Decimal(str(raw).strip()) * (Decimal(10) ** 18))


def _metadata_asset(rpc_url: str, asset: str) -> str:
    raw = cast_call(rpc_url, asset, "wrappedAsset()(address)", allow_fail=True).strip().split()[0]
    if raw.startswith("0x") and raw.lower() != ZERO_ADDRESS:
        return raw
    return asset


def _erc20_decimals(rpc_url: str, asset: str) -> int:
    raw = cast_call(rpc_url, _metadata_asset(rpc_url, asset), "decimals()(uint8)")
    return parse_uint(raw)


def _erc20_symbol(rpc_url: str, asset: str) -> str:
    raw = cast_call(rpc_url, _metadata_asset(rpc_url, asset), "symbol()(string)")
    return raw.strip()


def resolve_asset_name(fallback_asset_name: str, asset_addr: str, rpc_url: str) -> str:
    if fallback_asset_name:
        return fallback_asset_name
    symbol = _erc20_symbol(rpc_url, asset_addr).upper()
    if symbol == "WETH":
        return "ETH"
    if symbol == "WBTC":
        return "BTC"
    return symbol


def _post_private_deposit(
    *,
    api_url: str,
    x_lyra_wallet: str,
    x_lyra_timestamp: str,
    x_lyra_signature: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    status, out = http_post_json(
        f"{api_url.rstrip('/')}/private/deposit",
        body,
        headers={
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": x_lyra_timestamp,
            "X-LyraSignature": x_lyra_signature,
        },
    )
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"private/deposit failed ({status}): {json.dumps(out)}")
    return out


def _post_private_withdraw(
    *,
    api_url: str,
    x_lyra_wallet: str,
    x_lyra_timestamp: str,
    x_lyra_signature: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    status, out = http_post_json(
        f"{api_url.rstrip('/')}/private/withdraw",
        body,
        headers={
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": x_lyra_timestamp,
            "X-LyraSignature": x_lyra_signature,
        },
    )
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"private/withdraw failed ({status}): {json.dumps(out)}")
    return out


def submit_with_retries(
    submitter: Callable[[], T],
    *,
    attempts: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> tuple[T, int]:
    retry_delay = initial_delay_seconds
    for attempt in range(1, attempts + 1):
        try:
            return submitter(), attempt
        except Exception as exc:
            if attempt >= attempts or not is_retryable_signature_sync_error_text(str(exc)):
                raise
            time.sleep(retry_delay)
            retry_delay = min(max(retry_delay * 1.7, 0.0), max_delay_seconds)
    raise RuntimeError("retry loop exhausted unexpectedly")


def submit_api_for_pending_message(
    *,
    action_type: int,
    pending_message: dict[str, Any],
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
    rpc_url: str,
) -> dict[str, Any]:
    nonce, expiry = fresh_action_nonce_and_expiry(rpc_url, tsa_addr, int(pending_message["loanId"]))
    debug_payload = {
        "amount": to_decimal_str(int(pending_message["amount"]), _erc20_decimals(rpc_url, pending_message["asset"])),
        "asset_name": resolve_asset_name(fallback_asset_name, pending_message["asset"], rpc_url),
        "is_atomic_signing": True,
        "nonce": nonce,
        "signature_expiry_sec": expiry,
        "signer": tsa_addr,
        "subaccount_id": int(pending_message["subaccountId"]),
    }

    if action_type == ACTION_DEPOSIT_INTENT:
        debug_status, debug_json = http_post_json(f"{api_url.rstrip('/')}/public/deposit_debug", debug_payload)
        if debug_status >= 400 or debug_json.get("error"):
            raise RuntimeError(f"public/deposit_debug failed ({debug_status}): {json.dumps(debug_json)}")
        typed_hash = debug_json["result"]["typed_data_hash"]
        action_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)
        ts_ms = str(int(time.time() * 1000))
        auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
        private_resp = _post_private_deposit(
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            x_lyra_timestamp=ts_ms,
            x_lyra_signature=auth_sig,
            body={
                "amount": debug_payload["amount"],
                "asset_name": debug_payload["asset_name"],
                "is_atomic_signing": True,
                "subaccount_id": debug_payload["subaccount_id"],
                "nonce": nonce,
                "signature_expiry_sec": expiry,
                "signer": tsa_addr,
                "signature": action_sig,
            },
        )
        return {
            "typedDataHash": typed_hash,
            "amount": debug_payload["amount"],
            "assetName": debug_payload["asset_name"],
            "subaccountId": str(debug_payload["subaccount_id"]),
            "nonce": str(nonce),
            "expiry": str(expiry),
            "apiResult": private_resp.get("result"),
            "apiId": private_resp.get("id"),
        }

    if action_type == ACTION_RETURN_REQUEST:
        debug_status, debug_json = http_post_json(f"{api_url.rstrip('/')}/public/withdraw_debug", debug_payload)
        if debug_status >= 400 or debug_json.get("error"):
            raise RuntimeError(f"public/withdraw_debug failed ({debug_status}): {json.dumps(debug_json)}")
        typed_hash = debug_json["result"]["typed_data_hash"]
        action_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)
        ts_ms = str(int(time.time() * 1000))
        auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
        private_resp = _post_private_withdraw(
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            x_lyra_timestamp=ts_ms,
            x_lyra_signature=auth_sig,
            body={
                "amount": debug_payload["amount"],
                "asset_name": debug_payload["asset_name"],
                "is_atomic_signing": True,
                "subaccount_id": debug_payload["subaccount_id"],
                "nonce": nonce,
                "signature_expiry_sec": expiry,
                "signer": tsa_addr,
                "signature": action_sig,
            },
        )
        return {
            "typedDataHash": typed_hash,
            "amount": debug_payload["amount"],
            "assetName": debug_payload["asset_name"],
            "subaccountId": str(debug_payload["subaccount_id"]),
            "nonce": str(nonce),
            "expiry": str(expiry),
            "apiResult": private_resp.get("result"),
            "apiId": private_resp.get("id"),
        }

    raise RuntimeError(f"API submit unsupported for action type {action_type}")
