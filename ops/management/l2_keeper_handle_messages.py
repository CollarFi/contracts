#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run  # noqa: E402
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
ACTION_SIGNED_TOPIC0 = "0x41cf207ce16a9affd2802d7565d332072d6ac8caca70010be9203c8b0840e6fe"
MESSAGE_HANDLED_TOPIC0 = "0x342468323d5aa8f601250bb7a841742c9e0d5c72a898da183e73a18803936428"
MAX_LOAN_ID_FOR_NONCE_SUFFIX = 999_999


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


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_receiver_addr(env: dict[str, str]) -> str:
    if env.get("L2_RECEIVER"):
        return str(env["L2_RECEIVER"])
    output_json = env.get("OUTPUT_JSON") or _default_output_json(must(env, "RPC_URL"), "l2")
    return _read_addr_from_output(output_json, "l2Receiver")


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
    if not m:
        raise ValueError(f"failed to parse pendingMessages tuple: {raw}")
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


def _extract_tx_hash(cast_send_output: str) -> str:
    # cast send formats vary by version:
    # - "Transaction: 0x..."
    # - key/value lines with "transactionHash      0x..."
    # - JSON with "transactionHash":"0x..."
    for pattern in (
        r'"transactionHash"\s*:\s*"(0x[a-fA-F0-9]{64})"',
        r"\btransactionHash\b\s*[:=]?\s*(0x[a-fA-F0-9]{64})",
        r"\bTransaction:\s*(0x[a-fA-F0-9]{64})",
    ):
        m = re.search(pattern, cast_send_output, flags=re.IGNORECASE)
        if m:
            return m.group(1)

    raise ValueError(f"failed to extract tx hash from cast output: {cast_send_output}")


def _get_receipt(rpc_url: str, tx_hash: str) -> dict[str, Any]:
    raw = run(["cast", "rpc", "eth_getTransactionReceipt", tx_hash, "--rpc-url", rpc_url])
    payload = json.loads(raw)
    if isinstance(payload, dict) and "result" in payload:
        return payload.get("result") or {}
    if isinstance(payload, dict) and "transactionHash" in payload:
        return payload
    return {}


def _get_block_timestamp(rpc_url: str, block_number: int) -> int:
    out = run(["cast", "block", str(block_number), "--rpc-url", rpc_url, "--json"])
    payload = json.loads(out)
    ts = payload.get("timestamp", 0)
    if isinstance(ts, str):
        return int(ts, 16) if ts.startswith("0x") else int(ts)
    return int(ts)


def _find_message_handled_log_for_guid(rpc_url: str, receiver_addr: str, guid: str) -> dict[str, Any]:
    out = run(
        [
            "cast",
            "logs",
            "MessageHandled(bytes32,uint8,uint256)",
            "--address",
            receiver_addr,
            "--from-block",
            "0",
            "--to-block",
            "latest",
            "--rpc-url",
            rpc_url,
            "--json",
        ]
    )
    logs = json.loads(out)
    target = guid.lower()
    matched = [l for l in logs if (l.get("topics") or [None, None])[1].lower() == target]
    if not matched:
        raise RuntimeError(f"MessageHandled log not found for guid={guid}")
    return matched[-1]


def _tsa_signature_expiry_window(rpc_url: str, tsa_addr: str) -> tuple[int, int]:
    raw = cast_call(
        rpc_url,
        tsa_addr,
        "getCollarTSAParams()((uint256,uint256,uint256,uint256,int256,uint256,uint256,uint256))",
    )

    # cast may annotate large integers with scientific notation hints, e.g.
    #   86400 [8.64e4]
    # Strip those hints, then parse first two tuple fields.
    cleaned = re.sub(r"\s*\[[^\]]+\]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    m = re.match(r"^\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,", cleaned)
    if not m:
        raise RuntimeError(f"failed to parse signature expiry window from TSA params: {raw}")
    return int(m.group(1)), int(m.group(2))


def _latest_block_timestamp(rpc_url: str) -> int:
    out = run(["cast", "block", "latest", "--rpc-url", rpc_url, "--json"])
    payload = json.loads(out)
    ts = payload.get("timestamp", 0)
    if isinstance(ts, str):
        return int(ts, 16) if ts.startswith("0x") else int(ts)
    return int(ts)


def _derive_reissue_nonce(chain_now: int, loan_id: int) -> int:
    if loan_id > MAX_LOAN_ID_FOR_NONCE_SUFFIX:
        raise RuntimeError(f"loanId too large for nonce suffix (max={MAX_LOAN_ID_FOR_NONCE_SUFFIX}, got={loan_id})")

    # Mirror receiver nonce convention:
    # nonce = timestamp_sec || loanId(6 digits)
    return (chain_now * 1_000_000) + loan_id


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


def _reissue_action_signature(
    *,
    rpc_url: str,
    tsa_addr: str,
    action: dict[str, Any],
    loan_id: int,
    account: str,
    private_key: str,
    from_addr: str,
    unlocked: bool,
) -> tuple[str, dict[str, Any], int]:
    min_sig, max_sig = _tsa_signature_expiry_window(rpc_url, tsa_addr)
    chain_now = _latest_block_timestamp(rpc_url)
    # Keep enough freshness margin beyond API window while respecting TSA bounds.
    cushion = max(30, min(300, min_sig // 5 if min_sig > 0 else 30))
    target_expiry = chain_now + min_sig + cushion
    upper_bound = chain_now + max_sig - 1
    if target_expiry > upper_bound:
        target_expiry = upper_bound
    if target_expiry <= chain_now + min_sig:
        raise RuntimeError(
            "cannot reissue action with valid expiry window: "
            f"chain_now={chain_now} min={min_sig} max={max_sig}"
        )

    refreshed = dict(action)
    refreshed["nonce"] = _derive_reissue_nonce(chain_now, loan_id)
    refreshed["expiry"] = target_expiry

    tx_out = cast_send(
        rpc_url,
        account or None,
        tsa_addr,
        "signActionData((uint256,uint256,address,bytes,uint256,address,address),bytes)",
        _format_action_tuple(refreshed),
        "0x",
        private_key=private_key or None,
        from_addr=from_addr or None,
        unlocked=unlocked,
    )
    tx_hash = _extract_tx_hash(tx_out)
    receipt = _get_receipt(rpc_url, tx_hash)
    signed = _decode_action_signed_from_receipt(receipt, tsa_addr)
    block_no_raw = receipt.get("blockNumber", "0x0")
    block_no = int(block_no_raw, 16) if isinstance(block_no_raw, str) else int(block_no_raw)
    signed_at = _get_block_timestamp(rpc_url, block_no)
    return tx_hash, signed, signed_at


def _http_post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.5.0",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            status = int(resp.status)
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return status, data
    except HTTPError as e:
        try:
            data = json.loads((e.read() or b"{}").decode("utf-8"))
        except Exception:
            data = {"error": {"message": str(e)}}
        return int(e.code), data
    except URLError as e:
        raise RuntimeError(f"http post failed: {url}: {e}") from e


def _is_retryable_signature_sync_error_text(err_text: str) -> bool:
    t = err_text.lower()
    return "14014" in t or "signature invalid" in t


def _decode_action_signed_from_receipt(receipt: dict[str, Any], tsa_addr: str) -> dict[str, Any]:
    tsa = tsa_addr.lower()
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        if str(log.get("address", "")).lower() != tsa:
            continue
        if str(topics[0]).lower() != ACTION_SIGNED_TOPIC0:
            continue

        data_hex = str(log.get("data", "0x"))
        if not data_hex.startswith("0x"):
            continue
        decoded_raw = run(
            [
                "cast",
                "decode-abi",
                "--json",
                "f()((uint256,uint256,address,bytes,uint256,address,address))",
                data_hex,
            ]
        )
        decoded = json.loads(decoded_raw)[0]
        return {
            "eventSigner": "0x" + str(topics[1])[-40:],
            "typedDataHash": str(topics[2]),
            "subaccountId": int(decoded[0]),
            "nonce": int(decoded[1]),
            "module": str(decoded[2]),
            "data": str(decoded[3]),
            "expiry": int(decoded[4]),
            "owner": str(decoded[5]),
            "signer": str(decoded[6]),
        }

    raise RuntimeError("ActionSigned event not found in handleMessage receipt")


def _decode_deposit_module_data(data_hex: str) -> dict[str, Any]:
    if not data_hex.startswith("0x"):
        raise ValueError(f"invalid data hex: {data_hex}")
    decoded_raw = run(["cast", "decode-abi", "--json", "f()(uint256,address,address)", data_hex])
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


def _wallet_sign(message: str, *, no_hash: bool, account: str, private_key: str) -> str:
    cmd = ["cast", "wallet", "sign"]
    if no_hash:
        cmd.append("--no-hash")
    if private_key:
        cmd += ["--private-key", private_key]
    elif account:
        cmd += ["--account", account]
    else:
        raise ValueError("wallet signing requires account or private key")
    cmd.append(message)
    return run(cmd)


def _wallet_address(*, account: str, private_key: str) -> str:
    if private_key:
        return run(["cast", "wallet", "address", "--private-key", private_key]).strip()
    if account:
        return run(["cast", "wallet", "address", "--account", account]).strip()
    raise ValueError("wallet address resolution requires account or private key")


def _assert_tsa_signer(rpc_url: str, tsa_addr: str, wallet_addr: str) -> None:
    raw = cast_call(rpc_url, tsa_addr, "isSigner(address)(bool)", wallet_addr, allow_fail=True)
    if raw.strip().lower() != "true":
        raise ValueError(
            f"wallet {wallet_addr} is not a TSA signer for {tsa_addr}; "
            "Derive API signature will be rejected"
        )


def _post_private_deposit(
    *,
    api_url: str,
    x_lyra_wallet: str,
    x_lyra_timestamp: str,
    x_lyra_signature: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "body": body,
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.5.0",
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": x_lyra_timestamp,
            "X-LyraSignature": x_lyra_signature,
        },
    }
    # Build raw request with headers via urllib for no extra deps.
    data = json.dumps(body).encode("utf-8")
    req = Request(
        f"{api_url.rstrip('/')}/private/deposit",
        data=data,
        headers=payload["headers"],
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            status = int(resp.status)
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        status = int(e.code)
        try:
            out = json.loads((e.read() or b"{}").decode("utf-8"))
        except Exception:
            out = {"error": {"message": str(e)}}
    except URLError as e:
        raise RuntimeError(f"private/deposit http error: {e}") from e

    if status >= 400 or out.get("error"):
        raise RuntimeError(f"private/deposit failed ({status}): {json.dumps(out)}")
    return out


def _erc20_decimals(rpc_url: str, asset: str) -> int:
    raw = cast_call(rpc_url, asset, "decimals()(uint8)")
    return _parse_uint(raw)


def _erc20_symbol(rpc_url: str, asset: str) -> str:
    raw = cast_call(rpc_url, asset, "symbol()(string)")
    return raw.strip()


def _resolve_asset_name(fallback_asset_name: str, asset_addr: str, rpc_url: str) -> str:
    if fallback_asset_name:
        return fallback_asset_name
    symbol = _erc20_symbol(rpc_url, asset_addr).upper()
    if symbol == "WETH":
        return "ETH"
    if symbol == "WBTC":
        return "BTC"
    return symbol


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

    debug_status, debug_json = _http_post_json(f"{api_url.rstrip('/')}/public/deposit_debug", debug_payload)
    if debug_status >= 400 or debug_json.get("error"):
        raise RuntimeError(f"public/deposit_debug failed ({debug_status}): {json.dumps(debug_json)}")

    typed_hash = debug_json["result"]["typed_data_hash"]
    if typed_hash.lower() != str(action["typedDataHash"]).lower():
        raise RuntimeError(
            "typed hash mismatch between ActionSigned and deposit_debug "
            f"(onchain={action['typedDataHash']}, debug={typed_hash})"
        )

    signed_raw = cast_call(rpc_url, tsa_addr, "signedData(bytes32)(bool)", typed_hash)
    if signed_raw.strip().lower() != "true":
        raise RuntimeError(f"TSA signedData({typed_hash}) returned false")

    ts_ms = str(int(time.time() * 1000))
    auth_sig = _wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    deposit_sig = _wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)

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


def _submit_deposit_to_derive_api(
    *,
    rpc_url: str,
    receipt_tx_hash: str,
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
) -> dict[str, Any]:
    receipt = _get_receipt(rpc_url, receipt_tx_hash)
    action = _decode_action_signed_from_receipt(receipt, tsa_addr)
    return _submit_deposit_to_derive_api_from_action(
        rpc_url=rpc_url,
        action=action,
        tsa_addr=tsa_addr,
        account=account,
        private_key=private_key,
        api_url=api_url,
        x_lyra_wallet=x_lyra_wallet,
        fallback_asset_name=fallback_asset_name,
    )


def _decode_withdraw_module_data(data_hex: str) -> dict[str, Any]:
    if not data_hex.startswith("0x"):
        raise ValueError(f"invalid data hex: {data_hex}")
    decoded_raw = run(["cast", "decode-abi", "--json", "f()(address,uint256)", data_hex])
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
    data = json.dumps(body).encode("utf-8")
    req = Request(
        f"{api_url.rstrip('/')}/private/withdraw",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.5.0",
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": x_lyra_timestamp,
            "X-LyraSignature": x_lyra_signature,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            status = int(resp.status)
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except HTTPError as e:
        status = int(e.code)
        try:
            out = json.loads((e.read() or b"{}").decode("utf-8"))
        except Exception:
            out = {"error": {"message": str(e)}}
    except URLError as e:
        raise RuntimeError(f"private/withdraw http error: {e}") from e

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

    debug_status, debug_json = _http_post_json(f"{api_url.rstrip('/')}/public/withdraw_debug", debug_payload)
    if debug_status >= 400 or debug_json.get("error"):
        raise RuntimeError(f"public/withdraw_debug failed ({debug_status}): {json.dumps(debug_json)}")

    typed_hash = debug_json["result"]["typed_data_hash"]
    if typed_hash.lower() != str(action["typedDataHash"]).lower():
        raise RuntimeError(
            "typed hash mismatch between ActionSigned and withdraw_debug "
            f"(onchain={action['typedDataHash']}, debug={typed_hash})"
        )

    signed_raw = cast_call(rpc_url, tsa_addr, "signedData(bytes32)(bool)", typed_hash)
    if signed_raw.strip().lower() != "true":
        raise RuntimeError(f"TSA signedData({typed_hash}) returned false")

    ts_ms = str(int(time.time() * 1000))
    auth_sig = _wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    withdraw_sig = _wallet_sign(typed_hash, no_hash=True, account=account, private_key=private_key)

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


def _submit_withdraw_to_derive_api(
    *,
    rpc_url: str,
    receipt_tx_hash: str,
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
) -> dict[str, Any]:
    receipt = _get_receipt(rpc_url, receipt_tx_hash)
    action = _decode_action_signed_from_receipt(receipt, tsa_addr)
    return _submit_withdraw_to_derive_api_from_action(
        rpc_url=rpc_url,
        action=action,
        tsa_addr=tsa_addr,
        account=account,
        private_key=private_key,
        api_url=api_url,
        x_lyra_wallet=x_lyra_wallet,
        fallback_asset_name=fallback_asset_name,
    )


def _ensure_api_state(state: dict[str, Any]) -> None:
    if "apiSubmitted" not in state or not isinstance(state.get("apiSubmitted"), dict):
        state["apiSubmitted"] = {}


def _action_meta_from_chain(
    *,
    rpc_url: str,
    receiver_addr: str,
    tsa_addr: str,
    guid: str,
) -> tuple[dict[str, Any], int, str]:
    handled_log = _find_message_handled_log_for_guid(rpc_url, receiver_addr, guid)
    tx_hash = str(handled_log.get("transactionHash"))
    if not tx_hash:
        raise RuntimeError(f"missing tx hash for MessageHandled guid={guid}")

    receipt = _get_receipt(rpc_url, tx_hash)
    action = _decode_action_signed_from_receipt(receipt, tsa_addr)

    block_no_raw = receipt.get("blockNumber", "0x0")
    block_no = int(block_no_raw, 16) if isinstance(block_no_raw, str) else int(block_no_raw)
    signed_at = _get_block_timestamp(rpc_url, block_no)
    return action, signed_at, tx_hash


def _submit_api_for_action(
    *,
    action_type: int,
    rpc_url: str,
    action: dict[str, Any],
    tsa_addr: str,
    account: str,
    private_key: str,
    api_url: str,
    x_lyra_wallet: str,
    fallback_asset_name: str,
) -> dict[str, Any]:
    if action_type == ACTION_DEPOSIT_INTENT:
        return _submit_deposit_to_derive_api_from_action(
            rpc_url=rpc_url,
            action=action,
            tsa_addr=tsa_addr,
            account=account,
            private_key=private_key,
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            fallback_asset_name=fallback_asset_name,
        )
    if action_type == ACTION_RETURN_REQUEST:
        return _submit_withdraw_to_derive_api_from_action(
            rpc_url=rpc_url,
            action=action,
            tsa_addr=tsa_addr,
            account=account,
            private_key=private_key,
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            fallback_asset_name=fallback_asset_name,
        )
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
    api_signature_max_age_seconds: int = typer.Option(
        300,
        "--api-signature-max-age-seconds",
        min=1,
        help="If signed action is older than this, re-sign action before submitting Derive API request.",
    ),
    post_reissue_api_retry_attempts: int = typer.Option(
        6,
        "--post-reissue-api-retry-attempts",
        min=1,
        help="Retries for Derive API submit after onchain reissue when signature invalid is returned.",
    ),
    post_reissue_api_retry_initial_delay_seconds: float = typer.Option(
        2.0,
        "--post-reissue-api-retry-initial-delay-seconds",
        min=0.0,
        help="Initial retry delay (seconds) for post-reissue Derive API retries.",
    ),
    post_reissue_api_retry_max_delay_seconds: float = typer.Option(
        20.0,
        "--post-reissue-api-retry-max-delay-seconds",
        min=0.0,
        help="Maximum retry delay (seconds) for post-reissue Derive API retries.",
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

    eff_api_url = (derive_api_url or env.get("DERIVE_API_URL") or "https://api-demo.lyra.finance").strip()
    eff_asset_name = (derive_asset_name or env.get("DERIVE_ASSET_NAME") or "ETH").strip()
    eff_derive_wallet = (derive_wallet or env.get("DERIVE_WALLET") or tsa_addr).strip()
    deposit_intents = not no_deposit_intents
    return_requests = not no_return_requests
    submit_deposit_api = broadcast and (not no_submit_deposit_api)
    submit_withdraw_api = broadcast and (not no_submit_withdraw_api)

    if (submit_deposit_api or submit_withdraw_api) and not (account or pk):
        raise ValueError(
            "API submission requires ACCOUNT or --private-key "
            "(or disable via --no-submit-deposit-api/--no-submit-withdraw-api)"
        )
    if submit_deposit_api or submit_withdraw_api:
        signer_wallet = _wallet_address(account=account, private_key=pk)
        _assert_tsa_signer(rpc_url, tsa_addr, signer_wallet)

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
                    # 1) Ensure action is signed onchain (either fresh handleMessage or already handled lookup/reissue)
                    action_data: dict[str, Any] | None = None
                    signed_at: int | None = None

                    if not already_handled:
                        value_wei = None
                        pending_raw = cast_call(
                            rpc_url,
                            receiver_addr,
                            "pendingMessages(bytes32)((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
                            guid,
                            allow_fail=True,
                        )
                        if action == ACTION_DEPOSIT_INTENT:
                            if pending_raw == "N/A":
                                raise RuntimeError("failed to read pendingMessages for fee quote")
                            fee = _quote_ack_native_fee(rpc_url, receiver_addr, pending_raw)
                            fee_with_buffer = fee + (fee * lz_fee_buffer_bps) // 10_000
                            value_wei = str(fee_with_buffer)
                            item["quotedAckNativeFee"] = str(fee)
                            item["valueWei"] = value_wei

                        tx = cast_send(
                            rpc_url,
                            account or None,
                            receiver_addr,
                            "handleMessage(bytes32)",
                            guid,
                            value_wei=value_wei,
                            private_key=pk or None,
                            from_addr=sender or None,
                            unlocked=use_unlocked,
                        )
                        tx_hash = _extract_tx_hash(tx)
                        item["tx"] = tx_hash

                        if _should_submit_api(action, submit_deposit_api, submit_withdraw_api):
                            receipt = _get_receipt(rpc_url, tx_hash)
                            action_data = _decode_action_signed_from_receipt(receipt, tsa_addr)
                            block_no_raw = receipt.get("blockNumber", "0x0")
                            block_no = int(block_no_raw, 16) if isinstance(block_no_raw, str) else int(block_no_raw)
                            signed_at = _get_block_timestamp(rpc_url, block_no)
                    # 2) Optional API submit path for deposit/withdraw
                    if _should_submit_api(action, submit_deposit_api, submit_withdraw_api):
                        if action_data is None or signed_at is None:
                            raise RuntimeError("missing fresh ActionSigned metadata for API submission")

                        age_sec = int(time.time()) - int(signed_at)
                        item["signedAgeSec"] = str(age_sec)

                        if age_sec > api_signature_max_age_seconds:
                            reissue_tx, action_data, signed_at = _reissue_action_signature(
                                rpc_url=rpc_url,
                                tsa_addr=tsa_addr,
                                action=action_data,
                                loan_id=loan_id,
                                account=account,
                                private_key=pk,
                                from_addr=sender,
                                unlocked=use_unlocked,
                            )
                            item["reissuedTx"] = reissue_tx
                            item["reissuedNonce"] = str(action_data["nonce"])
                            item["signedAgeSec"] = "0"

                        retry_attempts = post_reissue_api_retry_attempts if item.get("reissuedTx") else 1
                        retry_delay = post_reissue_api_retry_initial_delay_seconds
                        api_meta: dict[str, Any] | None = None
                        for api_attempt in range(1, retry_attempts + 1):
                            try:
                                api_meta = _submit_api_for_action(
                                    action_type=action,
                                    rpc_url=rpc_url,
                                    action=action_data,
                                    tsa_addr=tsa_addr,
                                    account=account,
                                    private_key=pk,
                                    api_url=eff_api_url,
                                    x_lyra_wallet=eff_derive_wallet,
                                    fallback_asset_name=eff_asset_name,
                                )
                                item["deriveApiAttempts"] = str(api_attempt)
                                break
                            except Exception as exc:
                                if (
                                    api_attempt >= retry_attempts
                                    or not _is_retryable_signature_sync_error_text(str(exc))
                                ):
                                    raise
                                item["deriveApiAttempts"] = str(api_attempt)
                                time.sleep(retry_delay)
                                retry_delay = min(
                                    max(retry_delay * 1.7, 0.0),
                                    post_reissue_api_retry_max_delay_seconds,
                                )

                        if api_meta is None:
                            raise RuntimeError("derive API submit failed after retries")

                        item["deriveApi"] = api_meta
                        state["apiSubmitted"][guid] = {
                            "action": _action_name(action),
                            "submittedAt": int(time.time()),
                            "deriveApi": api_meta,
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
