#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run  # noqa: E402
from l2_common import (  # noqa: E402
    assert_tsa_signer,
    derive_reissue_nonce,
    extract_tx_hash,
    http_post_json,
    is_retryable_signature_sync_error_text,
    latest_block_timestamp,
    tsa_signature_expiry_window,
    wallet_address,
    wallet_sign,
)
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l2_env_path  # noqa: E402

app = typer.Typer(add_completion=False)

# CollarLZMessages.Action enum
ACTION_DEPOSIT_INTENT = 0
ACTION_RETURN_REQUEST = 1
ACTION_SETTLEMENT_REPORT = 2
ACTION_DEPOSIT_CONFIRMED = 3
ACTION_COLLATERAL_RETURNED = 4
ACTION_TRADE_CONFIRMED = 5
ACTION_MANDATE_CREATED = 6
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _resolve_env_path(env_profile: str, l2_env_file: Path) -> Path:
    profile = env_profile.strip().lower()
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        return ROOT_DIR / f".env.l2.{profile}"
    return l2_env_file


def _read_addr_from_output(path_value: str, key: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"deployment output not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    val = addrs.get(key)
    if not val:
        raise ValueError(f"missing {key} in deployment output: {path}")
    return str(val)


def _extract_addresses(raw: str, expected: int, label: str) -> list[str]:
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", raw)
    if len(addrs) < expected:
        raise RuntimeError(f"failed to parse {label}: {raw}")
    return addrs[:expected]


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_receiver_addr(env: dict[str, str]) -> str:
    if env.get("L2_RECEIVER"):
        return str(env["L2_RECEIVER"])
    output_json = env.get("OUTPUT_JSON") or _default_output_json(must(env, "RPC_URL"), "l2")
    return _read_addr_from_output(output_json, "l2Receiver")


def _resolve_atomic_executor_addr(env: dict[str, str], rpc_url: str) -> str:
    configured = (env.get("ATOMIC_EXECUTOR") or "").strip()
    if configured:
        return configured
    output_json = env.get("OUTPUT_JSON") or _default_output_json(rpc_url, "l2")
    try:
        return _read_addr_from_output(output_json, "l2AtomicExecutor")
    except Exception:
        return ""


def _resolve_local_atomic_config(
    rpc_url: str,
    tsa_addr: str,
    deposit_module: str,
    withdrawal_module: str,
    wrapped_deposit_asset: str,
) -> tuple[str, str, str]:
    resolved_deposit = deposit_module.strip()
    resolved_withdrawal = withdrawal_module.strip()
    resolved_wrapped = wrapped_deposit_asset.strip()

    if not (resolved_deposit and resolved_withdrawal):
        collar_addrs = _extract_addresses(
            cast_call(
                rpc_url,
                tsa_addr,
                "getCollarTSAAddresses()(address,address,address,address,address,address)",
            ),
            6,
            "getCollarTSAAddresses",
        )
        if not resolved_deposit:
            resolved_deposit = collar_addrs[1]
        if not resolved_withdrawal:
            resolved_withdrawal = collar_addrs[2]

    if not resolved_wrapped:
        base_addrs = _extract_addresses(
            cast_call(
                rpc_url,
                tsa_addr,
                "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
            ),
            7,
            "getBaseTSAAddresses",
        )
        resolved_wrapped = base_addrs[2]

    return resolved_deposit, resolved_withdrawal, resolved_wrapped


def _block_number(rpc_url: str) -> int:
    return int(run(["cast", "block-number", "--rpc-url", rpc_url]))


def _get_logs(rpc_url: str, receiver_addr: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
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
    return parsed


def _action_name(action: int) -> str:
    return {
        0: "DepositIntent",
        1: "ReturnRequest",
        2: "SettlementReport",
        3: "DepositConfirmed",
        4: "CollateralReturned",
        5: "TradeConfirmed",
        6: "MandateCreated",
    }.get(action, f"Unknown({action})")

def _parse_uint(raw: str) -> int:
    token = raw.strip().split()[0]
    return int(token)


def _quote_ack_native_fee(rpc_url: str, receiver_addr: str, pending_raw: str) -> int:
    msg = _parse_pending_message(pending_raw)
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
        f"0x{'0'*64},"
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
    # cast may return either tuple line "(native,lz)" or first-line number.
    if cleaned.startswith("("):
        first = cleaned.split(",", 1)[0].lstrip("(").strip()
        return _parse_uint(first)
    return _parse_uint(cleaned)



def _parse_pending_message(raw: str) -> dict[str, Any]:
    s = re.sub(r"\s*\[[^\]]+\]", "", raw.strip())
    m = re.match(
        r"^\((\d+),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]*)\)$",
        s,
    )
    if m:
        return {
            "action": int(m.group(1)),
            "loanId": int(m.group(2)),
            "asset": m.group(3),
            "amount": int(m.group(4)),
            "recipient": m.group(5),
            "subaccountId": int(m.group(6)),
            "socketMessageId": m.group(7),
            "secondaryAmount": int(m.group(8)),
            "quoteHash": m.group(9),
            "takerNonce": int(m.group(10)),
            "data": m.group(11),
        }

    lines = [line.strip() for line in s.splitlines() if line.strip()]
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


def _abi_encode(signature: str, *args: Any) -> str:
    return run(["cast", "abi-encode", signature, *[str(arg) for arg in args]]).strip()


def _fresh_action_nonce_and_expiry(rpc_url: str, tsa_addr: str, loan_id: int) -> tuple[int, int]:
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
    return derive_reissue_nonce(chain_now, loan_id), expiry


def _build_pending_action(
    *,
    action_type: int,
    pending_message: dict[str, Any],
    tsa_addr: str,
    deposit_module: str,
    withdrawal_module: str,
    wrapped_deposit_asset: str,
    rpc_url: str,
) -> dict[str, Any]:
    nonce, expiry = _fresh_action_nonce_and_expiry(rpc_url, tsa_addr, int(pending_message["loanId"]))
    if action_type == ACTION_DEPOSIT_INTENT:
        data = _abi_encode(
            "f(uint256,address,address)",
            int(pending_message["amount"]),
            wrapped_deposit_asset,
            ZERO_ADDRESS,
        )
        module = deposit_module
    elif action_type == ACTION_RETURN_REQUEST:
        data = _abi_encode(
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
    typed_hash = cast_call(
        rpc_url,
        tsa_addr,
        "getActionTypedDataHash((uint256,uint256,address,bytes,uint256,address,address))(bytes32)",
        _format_action_tuple(action),
    ).strip()
    action["typedDataHash"] = typed_hash
    return action


def _format_action_tuple(action: dict[str, Any]) -> str:
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


def _decode_deposit_module_data(data_hex: str) -> dict[str, Any]:
    if not data_hex.startswith("0x"):
        raise ValueError(f"invalid data hex: {data_hex}")
    decoded_raw = run(["cast", "decode-abi", "--json", "f(uint256,address,address)", data_hex])
    amount, asset, manager = json.loads(decoded_raw)
    return {
        "amount": int(amount),
        "asset": str(asset),
        "manager": str(manager),
    }


def _to_decimal_str(amount: int, decimals: int) -> str:
    getcontext().prec = 80
    q = Decimal(amount) / (Decimal(10) ** Decimal(decimals))
    s = format(q.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


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


def _erc20_decimals(rpc_url: str, asset: str) -> int:
    raw = cast_call(rpc_url, _metadata_asset(rpc_url, asset), "decimals()(uint8)")
    return _parse_uint(raw)


def _erc20_symbol(rpc_url: str, asset: str) -> str:
    raw = cast_call(rpc_url, _metadata_asset(rpc_url, asset), "symbol()(string)")
    return raw.strip()


def _metadata_asset(rpc_url: str, asset: str) -> str:
    raw = cast_call(rpc_url, asset, "wrappedAsset()(address)", allow_fail=True).strip().split()[0]
    if raw.startswith("0x") and raw.lower() != ZERO_ADDRESS:
        return raw
    return asset


def _resolve_asset_name(fallback_asset_name: str, asset_addr: str, rpc_url: str) -> str:
    if fallback_asset_name:
        return fallback_asset_name
    symbol = _erc20_symbol(rpc_url, asset_addr).upper()
    if symbol == "WETH":
        return "ETH"
    if symbol == "WBTC":
        return "BTC"
    return symbol


def _is_local_rpc(rpc_url: str) -> bool:
    try:
        parsed = urlparse(rpc_url)
    except Exception:
        return False
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def _matching_addr(rpc_url: str, tsa_addr: str, configured_matching: str) -> str:
    if configured_matching:
        return configured_matching
    raw = cast_call(
        rpc_url,
        tsa_addr,
        "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
    )
    return raw.strip().splitlines()[-1].strip()


def _ensure_local_trade_executor(rpc_url: str, matching_addr: str, executor_addr: str) -> None:
    if cast_call(rpc_url, matching_addr, "tradeExecutors(address)(bool)", executor_addr, allow_fail=True).strip().lower() == "true":
        return

    owner_addr = cast_call(rpc_url, matching_addr, "owner()(address)").strip()
    cast_send(
        rpc_url,
        None,
        matching_addr,
        "setTradeExecutor(address,bool)",
        executor_addr,
        "true",
        from_addr=owner_addr,
        unlocked=True,
    )


def _submit_action_to_local_atomic(
    *,
    rpc_url: str,
    atomic_executor_addr: str,
    matching_addr: str,
    action: dict[str, Any],
    signer_sig: str,
    account: str,
    private_key: str,
    from_addr: str,
    unlocked: bool,
) -> dict[str, Any]:
    sender_addr = wallet_address(account=account, private_key=private_key) if (account or private_key) else from_addr
    _ensure_local_trade_executor(rpc_url, matching_addr, sender_addr)
    _ensure_local_trade_executor(rpc_url, matching_addr, atomic_executor_addr)

    action_data = _abi_encode(
        "f((uint256,uint256,address,bytes,uint256,address,address))",
        _format_action_tuple(action),
    )
    tx_out = cast_send(
        rpc_url,
        account or None,
        atomic_executor_addr,
        "atomicVerifyAndMatch((uint256,uint256,address,bytes,uint256,address,address)[],bytes[],bytes,(bool,bytes)[])",
        f"[{_format_action_tuple(action)}]",
        f"[{signer_sig}]",
        action_data,
        "[(true,0x)]",
        private_key=private_key or None,
        from_addr=from_addr or None,
        unlocked=unlocked,
    )
    tx_hash = extract_tx_hash(tx_out)
    return {
        "mode": "localAtomic",
        "matchingTx": tx_hash,
        "typedDataHash": str(action["typedDataHash"]),
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
    }


def _submit_deposit_to_derive_api_from_action(
    *,
    rpc_url: str,
    action: dict[str, Any],
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
) -> dict[str, Any]:
    dep = _decode_deposit_module_data(action["data"])
    decimals = _erc20_decimals(rpc_url, dep["asset"])
    amount_str = _to_decimal_str(dep["amount"], decimals)
    asset_name = _resolve_asset_name(fallback_asset_name, dep["asset"], rpc_url)

    debug_payload = {
        "amount": amount_str,
        "asset_name": asset_name,
        "is_atomic_signing": True,
        "nonce": action["nonce"],
        "signature_expiry_sec": action["expiry"],
        "signer": tsa_addr,
        "subaccount_id": action["subaccountId"],
    }

    debug_status, debug_json = http_post_json(f"{api_url.rstrip('/')}/public/deposit_debug", debug_payload)
    if debug_status >= 400 or debug_json.get("error"):
        raise RuntimeError(f"public/deposit_debug failed ({debug_status}): {json.dumps(debug_json)}")

    typed_hash = debug_json["result"]["typed_data_hash"]
    if typed_hash.lower() != str(action["typedDataHash"]).lower():
        raise RuntimeError(
            "typed hash mismatch between local action and deposit_debug "
            f"(onchain={action['typedDataHash']}, debug={typed_hash})"
        )

    ts_ms = str(int(time.time() * 1000))
    auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    deposit_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)

    private_body = {
        "amount": amount_str,
        "asset_name": asset_name,
        "subaccount_id": action["subaccountId"],
        "nonce": action["nonce"],
        "signature_expiry_sec": action["expiry"],
        "signer": tsa_addr,
        "signature": deposit_sig,
    }

    private_resp = _post_private_deposit(
        api_url=api_url,
        x_lyra_wallet=x_lyra_wallet,
        x_lyra_timestamp=ts_ms,
        x_lyra_signature=auth_sig,
        body=private_body,
    )

    return {
        "typedDataHash": typed_hash,
        "actionHash": debug_json["result"].get("action_hash"),
        "encodedDataHash": debug_json["result"].get("encoded_data_hashed"),
        "amount": amount_str,
        "assetName": asset_name,
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
        "apiResult": private_resp.get("result"),
        "apiId": private_resp.get("id"),
    }


def _decode_withdraw_module_data(data_hex: str) -> dict[str, Any]:
    if not data_hex.startswith("0x"):
        raise ValueError(f"invalid data hex: {data_hex}")
    decoded_raw = run(["cast", "decode-abi", "--json", "f(address,uint256)", data_hex])
    asset, amount = json.loads(decoded_raw)
    return {
        "asset": str(asset),
        "amount": int(amount),
    }


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


def _submit_withdraw_to_derive_api_from_action(
    *,
    rpc_url: str,
    action: dict[str, Any],
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
) -> dict[str, Any]:
    wd = _decode_withdraw_module_data(action["data"])
    decimals = _erc20_decimals(rpc_url, wd["asset"])
    amount_str = _to_decimal_str(wd["amount"], decimals)
    asset_name = _resolve_asset_name(fallback_asset_name, wd["asset"], rpc_url)

    debug_payload = {
        "amount": amount_str,
        "asset_name": asset_name,
        "is_atomic_signing": True,
        "nonce": action["nonce"],
        "signature_expiry_sec": action["expiry"],
        "signer": tsa_addr,
        "subaccount_id": action["subaccountId"],
    }

    debug_status, debug_json = http_post_json(f"{api_url.rstrip('/')}/public/withdraw_debug", debug_payload)
    if debug_status >= 400 or debug_json.get("error"):
        raise RuntimeError(f"public/withdraw_debug failed ({debug_status}): {json.dumps(debug_json)}")

    typed_hash = debug_json["result"]["typed_data_hash"]
    if typed_hash.lower() != str(action["typedDataHash"]).lower():
        raise RuntimeError(
            "typed hash mismatch between local action and withdraw_debug "
            f"(onchain={action['typedDataHash']}, debug={typed_hash})"
        )

    ts_ms = str(int(time.time() * 1000))
    auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    withdraw_sig = wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)

    private_body = {
        "amount": amount_str,
        "asset_name": asset_name,
        "subaccount_id": action["subaccountId"],
        "nonce": action["nonce"],
        "signature_expiry_sec": action["expiry"],
        "signer": tsa_addr,
        "signature": withdraw_sig,
    }

    private_resp = _post_private_withdraw(
        api_url=api_url,
        x_lyra_wallet=x_lyra_wallet,
        x_lyra_timestamp=ts_ms,
        x_lyra_signature=auth_sig,
        body=private_body,
    )

    return {
        "typedDataHash": typed_hash,
        "actionHash": debug_json["result"].get("action_hash"),
        "encodedDataHash": debug_json["result"].get("encoded_data_hashed"),
        "amount": amount_str,
        "assetName": asset_name,
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
        "apiResult": private_resp.get("result"),
        "apiId": private_resp.get("id"),
    }


def _ensure_api_state(state: dict[str, Any]) -> None:
    if "apiSubmitted" not in state or not isinstance(state.get("apiSubmitted"), dict):
        state["apiSubmitted"] = {}


def _submit_api_for_pending_message(
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
    nonce, expiry = _fresh_action_nonce_and_expiry(rpc_url, tsa_addr, int(pending_message["loanId"]))
    debug_payload = {
        "amount": _to_decimal_str(int(pending_message["amount"]), _erc20_decimals(rpc_url, pending_message["asset"])),
        "asset_name": _resolve_asset_name(fallback_asset_name, pending_message["asset"], rpc_url),
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


def _should_submit_api(action_type: int, submit_deposit_api: bool, submit_withdraw_api: bool) -> bool:
    return (action_type == ACTION_DEPOSIT_INTENT and submit_deposit_api) or (
        action_type == ACTION_RETURN_REQUEST and submit_withdraw_api
    )


def _load_state(path: Path, start_block: int) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"nextBlock": start_block}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    start_block: int = typer.Option(0, "--start-block", help="Start block when state file doesn't exist"),
    state_file: Path = typer.Option(ROOT_DIR / "deployments" / "keeper_l2_state.json", "--state-file"),
    poll_seconds: int = typer.Option(5, "--poll-seconds", min=1),
    once: bool = typer.Option(False, "--once", help="Run one polling tick and exit"),
    max_per_tick: int = typer.Option(10, "--max-per-tick", min=1),
    no_deposit_intents: bool = typer.Option(
        False,
        "--no-deposit-intents",
        help="Disable handling of DepositIntent messages.",
    ),
    no_return_requests: bool = typer.Option(
        False,
        "--no-return-requests",
        help="Disable handling of ReturnRequest messages.",
    ),
    broadcast: bool = typer.Option(False, help="Send onchain transactions (default: dry-run)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
    lz_fee_buffer_bps: int = typer.Option(500, "--lz-fee-buffer-bps", min=0, help="Buffer over quoted LZ native fee (bps)."),
    no_submit_deposit_api: bool = typer.Option(
        False,
        "--no-submit-deposit-api",
        help="Disable Derive private/deposit submission after handling DepositIntent.",
    ),
    no_submit_withdraw_api: bool = typer.Option(
        False,
        "--no-submit-withdraw-api",
        help="Disable Derive private/withdraw submission after handling ReturnRequest.",
    ),
    api_retry_attempts: int = typer.Option(
        6,
        "--api-retry-attempts",
        min=1,
        help="Retries for Derive API submit when signature propagation errors are returned.",
    ),
    api_retry_initial_delay_seconds: float = typer.Option(
        2.0,
        "--api-retry-initial-delay-seconds",
        min=0.0,
        help="Initial retry delay in seconds for Derive API retries.",
    ),
    api_retry_max_delay_seconds: float = typer.Option(
        20.0,
        "--api-retry-max-delay-seconds",
        min=0.0,
        help="Maximum retry delay in seconds for Derive API retries.",
    ),
    derive_api_url: str = typer.Option(
        "",
        "--derive-api-url",
        help="Derive API base URL (default: DERIVE_API_URL env or https://api-demo.lyra.finance)",
    ),
    derive_wallet: str = typer.Option(
        "",
        "--derive-wallet",
        help="X-LyraWallet header override (default: TSA address)",
    ),
    derive_asset_name: str = typer.Option(
        "",
        "--derive-asset-name",
        help="Asset name for private/public deposit payloads (default: DERIVE_ASSET_NAME env or ETH)",
    ),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")
    pk = private_key or env.get("PRIVATE_KEY", "")
    sender = from_addr or env.get("FROM", "")
    use_unlocked = unlocked or (str(env.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})
    receiver_addr = receiver or resolve_addr(env, "L2_RECEIVER", "l2Receiver", "l2")
    if broadcast and not account and not pk and not (use_unlocked and sender):
        raise ValueError("missing auth for --broadcast: provide ACCOUNT, or --private-key, or --unlocked --from")

    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)").strip()
    matching_addr = _matching_addr(rpc_url, tsa_addr, env.get("MATCHING", "").strip())
    atomic_executor_addr = _resolve_atomic_executor_addr(env, rpc_url)
    deposit_module = (env.get("DEPOSIT_MODULE") or "").strip()
    withdrawal_module = (env.get("WITHDRAWAL_MODULE") or "").strip()
    wrapped_deposit_asset = (env.get("WRAPPED_DEPOSIT_ASSET") or "").strip()

    eff_api_url = (derive_api_url or env.get("DERIVE_API_URL") or "https://api-demo.lyra.finance").strip()
    eff_asset_name = (derive_asset_name or env.get("DERIVE_ASSET_NAME") or "ETH").strip()
    eff_derive_wallet = (derive_wallet or env.get("DERIVE_WALLET") or tsa_addr).strip()
    local_atomic_submit = _is_local_rpc(rpc_url)
    deposit_intents = not no_deposit_intents
    return_requests = not no_return_requests
    submit_deposit_api = broadcast and (not no_submit_deposit_api)
    submit_withdraw_api = broadcast and (not no_submit_withdraw_api)

    if (submit_deposit_api or submit_withdraw_api) and not (account or pk):
        raise ValueError(
            "API submission requires ACCOUNT or --private-key "
            "(or disable via --no-submit-deposit-api/--no-submit-withdraw-api)"
        )
    if broadcast and local_atomic_submit and not (account or pk):
        raise ValueError("local atomic submission requires ACCOUNT or --private-key for typed-data signing")
    if (submit_deposit_api or submit_withdraw_api) and not local_atomic_submit:
        signer_wallet = wallet_address(account=account, private_key=pk)
        assert_tsa_signer(rpc_url, tsa_addr, signer_wallet)
    if broadcast and local_atomic_submit:
        deposit_module, withdrawal_module, wrapped_deposit_asset = _resolve_local_atomic_config(
            rpc_url,
            tsa_addr,
            deposit_module,
            withdrawal_module,
            wrapped_deposit_asset,
        )
        if not atomic_executor_addr:
            raise ValueError("local atomic submission requires ATOMIC_EXECUTOR in env")
        for key, value in (
            ("DEPOSIT_MODULE", deposit_module),
            ("WITHDRAWAL_MODULE", withdrawal_module),
            ("WRAPPED_DEPOSIT_ASSET", wrapped_deposit_asset),
        ):
            if not value:
                raise ValueError(f"local atomic submission requires {key} in env")

    state = _load_state(state_file, start_block)
    _ensure_api_state(state)
    next_block = int(state.get("nextBlock", start_block))

    allowed_actions: set[int] = set()
    if deposit_intents:
        allowed_actions.add(ACTION_DEPOSIT_INTENT)
    if return_requests:
        allowed_actions.add(ACTION_RETURN_REQUEST)
    if not allowed_actions:
        raise ValueError("no actions enabled; use --deposit-intents and/or --return-requests")

    handled: list[dict[str, Any]] = []

    def tick() -> dict[str, Any]:
        nonlocal next_block
        latest = _block_number(rpc_url)
        if latest < next_block:
            return {"fromBlock": next_block, "toBlock": latest, "logs": 0, "attempted": 0, "sent": 0}

        scan_from = next_block
        logs = _get_logs(rpc_url, receiver_addr, scan_from, latest)

        attempts = 0
        sent = 0
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            block_raw = log.get("blockNumber", "0x0")
            block_no = int(block_raw, 16) if isinstance(block_raw, str) and block_raw.startswith("0x") else int(block_raw)
            guid = topics[1]
            loan_id = int(topics[2], 16)
            data = log.get("data", "0x")
            action = int(data, 16) if data not in {"0x", ""} else -1

            if action not in allowed_actions:
                continue

            already_handled_raw = cast_call(
                rpc_url,
                receiver_addr,
                "handledMessages(bytes32)(bool)",
                guid,
                allow_fail=True,
            )
            already_handled = already_handled_raw.strip().lower() == "true"

            # Do not trigger API submission for already-processed messages.
            if already_handled:
                continue

            attempts += 1
            item = {
                "guid": guid,
                "loanId": str(loan_id),
                "eventBlock": str(block_no),
                "action": _action_name(action),
                "tx": None,
                "status": "dry-run",
            }

            if broadcast:
                try:
                    pending_raw = cast_call(
                        rpc_url,
                        receiver_addr,
                        "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
                        guid,
                        allow_fail=True,
                    )
                    if pending_raw == "N/A":
                        raise RuntimeError("failed to read pending message")
                    pending_message = _parse_pending_message(pending_raw)

                    tx = cast_send(
                        rpc_url,
                        account or None,
                        receiver_addr,
                        "handleMessage(bytes32)",
                        guid,
                        private_key=pk or None,
                        from_addr=sender or None,
                        unlocked=use_unlocked,
                    )
                    tx_hash = extract_tx_hash(tx)
                    item["tx"] = tx_hash

                    if action in {ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST}:
                        if local_atomic_submit:
                            action_data = _build_pending_action(
                                action_type=action,
                                pending_message=pending_message,
                                tsa_addr=tsa_addr,
                                deposit_module=deposit_module,
                                withdrawal_module=withdrawal_module,
                                wrapped_deposit_asset=wrapped_deposit_asset,
                                rpc_url=rpc_url,
                            )
                            signer_sig = wallet_sign(
                                str(action_data["typedDataHash"]),
                                no_hash=True,
                                account=account,
                                private_key=pk,
                            )
                            api_meta = _submit_action_to_local_atomic(
                                rpc_url=rpc_url,
                                atomic_executor_addr=atomic_executor_addr,
                                matching_addr=matching_addr,
                                action=action_data,
                                signer_sig=signer_sig,
                                account=account,
                                private_key=pk,
                                from_addr=sender,
                                unlocked=use_unlocked,
                            )
                            item["deriveApi"] = api_meta
                            if action == ACTION_DEPOSIT_INTENT:
                                fee = _quote_ack_native_fee(rpc_url, receiver_addr, pending_raw)
                                fee_with_buffer = fee + (fee * lz_fee_buffer_bps) // 10_000
                                ack_tx = cast_send(
                                    rpc_url,
                                    account or None,
                                    receiver_addr,
                                    "sendDepositConfirmedAfterExecution(uint256)",
                                    str(loan_id),
                                    value_wei=str(fee_with_buffer),
                                    private_key=pk or None,
                                    from_addr=sender or None,
                                    unlocked=use_unlocked,
                                )
                                item["depositConfirmedTx"] = extract_tx_hash(ack_tx)
                        elif _should_submit_api(action, submit_deposit_api, submit_withdraw_api):
                            retry_delay = api_retry_initial_delay_seconds
                            api_meta: dict[str, Any] | None = None
                            for api_attempt in range(1, api_retry_attempts + 1):
                                try:
                                    api_meta = _submit_api_for_pending_message(
                                        action_type=action,
                                        pending_message=pending_message,
                                        tsa_addr=tsa_addr,
                                        account=account,
                                        private_key=pk,
                                        api_url=eff_api_url,
                                        x_lyra_wallet=eff_derive_wallet,
                                        fallback_asset_name=eff_asset_name,
                                        rpc_url=rpc_url,
                                    )
                                    item["deriveApiAttempts"] = str(api_attempt)
                                    break
                                except Exception as exc:
                                    if (
                                        api_attempt >= api_retry_attempts
                                        or not is_retryable_signature_sync_error_text(str(exc))
                                    ):
                                        raise
                                    item["deriveApiAttempts"] = str(api_attempt)
                                    time.sleep(retry_delay)
                                    retry_delay = min(
                                        max(retry_delay * 1.7, 0.0),
                                        api_retry_max_delay_seconds,
                                    )
                            if api_meta is None:
                                raise RuntimeError("derive API submit failed after retries")
                            item["deriveApi"] = api_meta

                    if item.get("deriveApi") is not None:
                        state["apiSubmitted"][guid] = {
                            "action": _action_name(action),
                            "submittedAt": int(time.time()),
                            "deriveApi": item["deriveApi"],
                        }
                        _save_state(state_file, state)

                    item["status"] = "sent"
                    sent += 1
                except Exception as exc:
                    item["status"] = f"error: {exc}"
            handled.append(item)

            if attempts >= max_per_tick:
                break

        # Advance cursor only when safe:
        # - dry-run: never advance (no onchain effects)
        # - broadcast: advance only if all attempted txs were sent successfully
        advanced = False
        if broadcast and attempts == sent:
            next_block = latest + 1
            state["nextBlock"] = next_block
            _save_state(state_file, state)
            advanced = True

        return {
            "fromBlock": scan_from,
            "toBlock": latest,
            "logs": len(logs),
            "attempted": attempts,
            "sent": sent,
            "advancedCursor": advanced,
            "nextBlock": next_block,
        }

    if once:
        result = tick()
        out = {
            "mode": "broadcast" if broadcast else "dry-run",
            "receiver": receiver_addr,
            "stateFile": str(state_file),
            "tick": result,
            "handled": handled,
        }
        if json_out:
            print(json.dumps(out, indent=2))
        else:
            print(out)
        return

    print(
        f"[bold]L2 keeper loop[/bold] mode={'broadcast' if broadcast else 'dry-run'} "
        f"receiver={receiver_addr} poll={poll_seconds}s"
    )
    while True:
        try:
            result = tick()
            if result["attempted"]:
                print(
                    f"[cyan][tick][/cyan] blocks {result['fromBlock']}..{result['toBlock']} "
                    f"logs={result['logs']} attempted={result['attempted']} sent={result['sent']} "
                    f"advanced={result['advancedCursor']}"
                )
                for item in handled[-result["attempted"] :]:
                    print(
                        f"  - {item['action']} loan={item['loanId']} guid={item['guid']} "
                        f"block={item.get('eventBlock', '?')} -> {item['status']}"
                    )
        except Exception as exc:
            print(f"[red][error][/red] {exc}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    app()
