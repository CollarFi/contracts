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


def build_pending_message_debug_payload(
    *,
    pending_message: dict[str, Any],
    tsa_addr: str,
    fallback_asset_name: str,
    rpc_url: str,
    nonce: int | None = None,
    signature_expiry_sec: int | None = None,
) -> dict[str, Any]:
    resolved_nonce = nonce
    resolved_expiry = signature_expiry_sec
    if resolved_nonce is None or resolved_expiry is None:
        fresh_nonce, fresh_expiry = fresh_action_nonce_and_expiry(rpc_url, tsa_addr, int(pending_message["loanId"]))
        if resolved_nonce is None:
            resolved_nonce = fresh_nonce
        if resolved_expiry is None:
            resolved_expiry = fresh_expiry

    return {
        "amount": to_decimal_str(int(pending_message["amount"]), _erc20_decimals(rpc_url, pending_message["asset"])),
        "asset_name": resolve_asset_name(fallback_asset_name, pending_message["asset"], rpc_url),
        "is_atomic_signing": True,
        "nonce": int(resolved_nonce),
        "signature_expiry_sec": int(resolved_expiry),
        "signer": tsa_addr,
        "subaccount_id": int(pending_message["subaccountId"]),
    }


def post_public_deposit_debug(*, api_url: str, body: dict[str, Any]) -> dict[str, Any]:
    status, out = http_post_json(f"{api_url.rstrip('/')}/public/deposit_debug", body)
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"public/deposit_debug failed ({status}): {json.dumps(out)}")
    return out


def post_public_withdraw_debug(*, api_url: str, body: dict[str, Any]) -> dict[str, Any]:
    status, out = http_post_json(f"{api_url.rstrip('/')}/public/withdraw_debug", body)
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"public/withdraw_debug failed ({status}): {json.dumps(out)}")
    return out


def build_private_action_body(*, debug_payload: dict[str, Any], signature: str) -> dict[str, Any]:
    return {
        "amount": debug_payload["amount"],
        "asset_name": debug_payload["asset_name"],
        "is_atomic_signing": True,
        "subaccount_id": int(debug_payload["subaccount_id"]),
        "nonce": int(debug_payload["nonce"]),
        "signature_expiry_sec": int(debug_payload["signature_expiry_sec"]),
        "signer": debug_payload["signer"],
        "signature": signature,
    }


def sign_private_api_auth(
    *,
    account: str,
    private_key: str,
    timestamp_ms: str | None = None,
) -> tuple[str, str]:
    ts_ms = timestamp_ms or str(int(time.time() * 1000))
    auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    return ts_ms, auth_sig


def post_private_deposit(
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


def post_private_withdraw(
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
    debug_payload = build_pending_message_debug_payload(
        pending_message=pending_message,
        tsa_addr=tsa_addr,
        fallback_asset_name=fallback_asset_name,
        rpc_url=rpc_url,
    )
    nonce = int(debug_payload["nonce"])
    expiry = int(debug_payload["signature_expiry_sec"])

    if action_type == ACTION_DEPOSIT_INTENT:
        debug_json = post_public_deposit_debug(api_url=api_url, body=debug_payload)
        typed_hash = debug_json["result"]["typed_data_hash"]
        action_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)
        ts_ms, auth_sig = sign_private_api_auth(
            account=account,
            private_key=private_key,
        )
        private_resp = post_private_deposit(
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            x_lyra_timestamp=ts_ms,
            x_lyra_signature=auth_sig,
            body=build_private_action_body(debug_payload=debug_payload, signature=action_sig),
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
        debug_json = post_public_withdraw_debug(api_url=api_url, body=debug_payload)
        typed_hash = debug_json["result"]["typed_data_hash"]
        action_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)
        ts_ms, auth_sig = sign_private_api_auth(
            account=account,
            private_key=private_key,
        )
        private_resp = post_private_withdraw(
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            x_lyra_timestamp=ts_ms,
            x_lyra_signature=auth_sig,
            body=build_private_action_body(debug_payload=debug_payload, signature=action_sig),
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
