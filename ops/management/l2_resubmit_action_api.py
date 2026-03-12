#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run  # noqa: E402
from l2_common import (  # noqa: E402
    assert_tsa_signer,
    derive_reissue_nonce,
    extract_tx_hash,
    get_receipt,
    http_post_json,
    is_retryable_signature_sync_error,
    latest_block_timestamp,
    tsa_signature_expiry_window,
    wallet_address,
    wallet_sign,
)
from py_lib.envs import resolve_l2_env_path  # noqa: E402

app = typer.Typer(add_completion=False)

ACTION_SIGNED_TOPIC0 = "0x41cf207ce16a9affd2802d7565d332072d6ac8caca70010be9203c8b0840e6fe"
MESSAGE_HANDLED_TOPIC0 = "0x342468323d5aa8f601250bb7a841742c9e0d5c72a898da183e73a18803936428"


@dataclass
class SignedAction:
    event_signer: str
    typed_data_hash: str
    subaccount_id: int
    nonce: int
    module: str
    data: str
    expiry: int
    owner: str
    signer: str


@dataclass
class HandledMessageMeta:
    guid: str
    loan_id: int


def _decode_action_log(log: dict[str, Any]) -> SignedAction:
    topics = log.get("topics") or []
    if len(topics) < 3:
        raise RuntimeError("invalid ActionSigned log: expected >=3 topics")

    data_hex = str(log.get("data", "0x"))
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
    return SignedAction(
        event_signer="0x" + str(topics[1])[-40:],
        typed_data_hash=str(topics[2]),
        subaccount_id=int(decoded[0]),
        nonce=int(decoded[1]),
        module=str(decoded[2]),
        data=str(decoded[3]),
        expiry=int(decoded[4]),
        owner=str(decoded[5]),
        signer=str(decoded[6]),
    )


def _find_action_signed_logs(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if not topics:
            continue
        if str(topics[0]).lower() == ACTION_SIGNED_TOPIC0:
            out.append(log)
    return out


def _find_message_handled_meta(receipt: dict[str, Any]) -> HandledMessageMeta | None:
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        if str(topics[0]).lower() != MESSAGE_HANDLED_TOPIC0:
            continue
        return HandledMessageMeta(guid=str(topics[1]), loan_id=int(str(topics[2]), 16))
    return None


def _format_action_tuple_with_nonce(action: SignedAction, expiry: int, nonce: int) -> str:
    return (
        "("
        f"{action.subaccount_id},"
        f"{nonce},"
        f"{action.module},"
        f"{action.data},"
        f"{expiry},"
        f"{action.owner},"
        f"{action.signer}"
        ")"
    )


def _compute_fresh_expiry(rpc_url: str, tsa_addr: str) -> int:
    min_sig, max_sig = tsa_signature_expiry_window(rpc_url, tsa_addr)
    chain_now = latest_block_timestamp(rpc_url)

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
    return target_expiry


def _decode_deposit_module_data(data_hex: str) -> tuple[str, int]:
    decoded_raw = run(["cast", "decode-abi", "--json", "f()(uint256,address,address)", data_hex])
    amount, asset, _manager = json.loads(decoded_raw)
    return str(asset), int(amount)


def _decode_withdraw_module_data(data_hex: str) -> tuple[str, int]:
    decoded_raw = run(["cast", "decode-abi", "--json", "f()(address,uint256)", data_hex])
    asset, amount = json.loads(decoded_raw)
    return str(asset), int(amount)


def _erc20_decimals_safe(rpc_url: str, asset: str, fallback_decimals: int) -> int:
    try:
        raw = cast_call(rpc_url, asset, "decimals()(uint8)")
        return int(raw.strip().split()[0])
    except Exception:
        return fallback_decimals


def _erc20_symbol_safe(rpc_url: str, asset: str) -> str:
    try:
        return cast_call(rpc_url, asset, "symbol()(string)").strip()
    except Exception:
        return ""


def _resolve_asset_name(env_asset_name: str, symbol: str) -> str:
    if env_asset_name:
        return env_asset_name
    sym = symbol.upper().strip()
    if sym == "WETH":
        return "ETH"
    if sym == "WBTC":
        return "BTC"
    return sym or "ETH"


def _to_decimal_str(amount: int, decimals: int) -> str:
    getcontext().prec = 80
    q = Decimal(amount) / (Decimal(10) ** Decimal(decimals))
    s = format(q.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _build_api_payload(
    *,
    action_kind: str,
    action: SignedAction,
    rpc_url: str,
    env_asset_name: str,
    fallback_decimals: int,
) -> tuple[dict[str, Any], str, str]:
    if action_kind == "deposit":
        asset, raw_amount = _decode_deposit_module_data(action.data)
    elif action_kind == "withdraw":
        asset, raw_amount = _decode_withdraw_module_data(action.data)
    else:
        raise RuntimeError(f"unsupported action kind: {action_kind}")

    decimals = _erc20_decimals_safe(rpc_url, asset, fallback_decimals)
    symbol = _erc20_symbol_safe(rpc_url, asset)
    amount_str = _to_decimal_str(raw_amount, decimals)
    asset_name = _resolve_asset_name(env_asset_name, symbol)

    debug_payload = {
        "amount": amount_str,
        "asset_name": asset_name,
        "is_atomic_signing": True,
        "nonce": action.nonce,
        "signature_expiry_sec": action.expiry,
        "signer": action.signer,
        "subaccount_id": action.subaccount_id,
    }
    return debug_payload, amount_str, asset_name


def _infer_action_kind(module_addr: str, deposit_module: str, withdrawal_module: str) -> str:
    mod = module_addr.lower()
    if deposit_module and mod == deposit_module.lower():
        return "deposit"
    if withdrawal_module and mod == withdrawal_module.lower():
        return "withdraw"
    raise RuntimeError(
        "unsupported module for API submit. "
        f"module={module_addr}, DEPOSIT_MODULE={deposit_module or '<unset>'}, "
        f"WITHDRAWAL_MODULE={withdrawal_module or '<unset>'}"
    )


@app.command()
def main(
    tx_hash: str = typer.Argument(..., help="Tx hash that handled LZ message and emitted ActionSigned."),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    action_index: int = typer.Option(0, "--action-index", help="ActionSigned log index when tx has multiple."),
    loan_id: int = typer.Option(
        0,
        "--loan-id",
        help="Override loanId used for nonce derivation when MessageHandled log is absent.",
    ),
    post_reissue_api_retry_attempts: int = typer.Option(
        6,
        "--post-reissue-api-retry-attempts",
        min=1,
        help="Retries for Derive API calls after onchain reissue when signature invalid error is returned.",
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
    derive_api_url: str = typer.Option("", "--derive-api-url", help="Derive API base URL."),
    derive_wallet: str = typer.Option("", "--derive-wallet", help="X-LyraWallet override."),
    derive_asset_name: str = typer.Option("", "--derive-asset-name", help="Asset name override (e.g. ETH)."),
    derive_asset_decimals: int = typer.Option(18, "--derive-asset-decimals", min=0, help="Fallback decimals if token call fails."),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")
    private_key = env.get("PRIVATE_KEY", "")
    sender = env.get("FROM", "")
    use_unlocked = str(env.get("UNLOCKED", "")).lower() in {"1", "true", "yes"}

    if not account and not private_key and not (use_unlocked and sender):
        raise ValueError("missing auth: need ACCOUNT, PRIVATE_KEY, or UNLOCKED=true + FROM")
    if not account and not private_key:
        raise ValueError("API signatures require ACCOUNT or PRIVATE_KEY")
    wallet_addr = wallet_address(account=account, private_key=private_key)

    eff_api_url = (derive_api_url or env.get("DERIVE_API_URL") or "https://api-demo.lyra.finance").strip()

    receipt = get_receipt(rpc_url, tx_hash)
    if not receipt:
        raise RuntimeError(f"transaction receipt not found for tx={tx_hash}")

    action_logs = _find_action_signed_logs(receipt)
    if not action_logs:
        raise RuntimeError(f"ActionSigned event not found in tx={tx_hash}")
    if action_index < 0 or action_index >= len(action_logs):
        raise RuntimeError(f"invalid --action-index {action_index}; tx has {len(action_logs)} ActionSigned log(s)")

    original_action = _decode_action_log(action_logs[action_index])
    tsa_addr = action_logs[action_index].get("address", "")
    if not tsa_addr:
        raise RuntimeError("failed to infer TSA address from ActionSigned log")
    assert_tsa_signer(rpc_url, tsa_addr, wallet_addr)

    action_kind = _infer_action_kind(
        original_action.module,
        env.get("DEPOSIT_MODULE", ""),
        env.get("WITHDRAWAL_MODULE", ""),
    )

    handled_meta = _find_message_handled_meta(receipt)
    nonce_loan_id: int
    if loan_id > 0:
        nonce_loan_id = loan_id
    elif handled_meta is not None:
        nonce_loan_id = handled_meta.loan_id
    else:
        raise RuntimeError(
            "loanId not found in tx logs (MessageHandled missing). "
            "Provide --loan-id to derive nonce for re-signing."
        )

    new_expiry = _compute_fresh_expiry(rpc_url, tsa_addr)
    new_nonce = derive_reissue_nonce(latest_block_timestamp(rpc_url), nonce_loan_id)

    reissue_out = cast_send(
        rpc_url,
        account or None,
        tsa_addr,
        "signActionData((uint256,uint256,address,bytes,uint256,address,address),bytes)",
        _format_action_tuple_with_nonce(original_action, new_expiry, new_nonce),
        "0x",
        private_key=private_key or None,
        from_addr=sender or None,
        unlocked=use_unlocked,
    )
    reissue_tx = extract_tx_hash(reissue_out)

    reissue_receipt = get_receipt(rpc_url, reissue_tx)
    reissue_logs = _find_action_signed_logs(reissue_receipt)
    if not reissue_logs:
        raise RuntimeError(f"ActionSigned not found in reissue tx={reissue_tx}")

    refreshed_action = _decode_action_log(reissue_logs[-1])

    debug_payload, amount_str, asset_name = _build_api_payload(
        action_kind=action_kind,
        action=refreshed_action,
        rpc_url=rpc_url,
        env_asset_name=(derive_asset_name or env.get("DERIVE_ASSET_NAME") or "").strip(),
        fallback_decimals=derive_asset_decimals,
    )

    debug_endpoint = f"{eff_api_url.rstrip('/')}/public/{action_kind}_debug"
    debug_status, debug_json = http_post_json(debug_endpoint, debug_payload)
    if debug_status >= 400 or debug_json.get("error"):
        raise RuntimeError(f"{debug_endpoint} failed ({debug_status}): {json.dumps(debug_json)}")

    debug_hash = str(debug_json["result"]["typed_data_hash"])
    if debug_hash.lower() != refreshed_action.typed_data_hash.lower():
        raise RuntimeError(
            "typed hash mismatch between onchain ActionSigned and Derive debug "
            f"(onchain={refreshed_action.typed_data_hash}, debug={debug_hash})"
        )

    signed_raw = cast_call(rpc_url, tsa_addr, "signedData(bytes32)(bool)", debug_hash)
    if signed_raw.strip().lower() != "true":
        raise RuntimeError(f"TSA signedData({debug_hash}) returned false")

    ts_ms = str(int(time.time() * 1000))
    auth_sig = wallet_sign(ts_ms, no_hash=False, account=account, private_key=private_key)
    action_sig = wallet_sign(debug_hash, no_hash=True, account=account, private_key=private_key)

    private_payload = {
        "amount": amount_str,
        "asset_name": asset_name,
        "subaccount_id": refreshed_action.subaccount_id,
        "nonce": refreshed_action.nonce,
        "signature_expiry_sec": refreshed_action.expiry,
        "signer": refreshed_action.signer,
        "signature": action_sig,
    }

    x_lyra_wallet = (derive_wallet or env.get("DERIVE_WALLET") or tsa_addr).strip()
    private_endpoint = f"{eff_api_url.rstrip('/')}/private/{action_kind}"
    delay_seconds = post_reissue_api_retry_initial_delay_seconds
    private_status = 0
    private_json: dict[str, Any] = {}
    private_attempts_used = 0
    for attempt in range(1, post_reissue_api_retry_attempts + 1):
        private_attempts_used = attempt
        private_status, private_json = http_post_json(
            private_endpoint,
            private_payload,
            headers={
                "X-LyraWallet": x_lyra_wallet,
                "X-LyraTimestamp": ts_ms,
                "X-LyraSignature": auth_sig,
            },
        )
        if private_status < 400 and not private_json.get("error"):
            break

        retryable = is_retryable_signature_sync_error(private_status, private_json)
        if not retryable or attempt >= post_reissue_api_retry_attempts:
            break

        time.sleep(delay_seconds)
        delay_seconds = min(max(delay_seconds * 1.7, 0.0), post_reissue_api_retry_max_delay_seconds)

    if private_status >= 400 or private_json.get("error"):
        raise RuntimeError(f"{private_endpoint} failed ({private_status}): {json.dumps(private_json)}")

    out = {
        "inputTx": tx_hash,
        "actionKind": action_kind,
        "tsa": tsa_addr,
        "oldTypedDataHash": original_action.typed_data_hash,
        "newTypedDataHash": refreshed_action.typed_data_hash,
        "oldExpiry": str(original_action.expiry),
        "newExpiry": str(refreshed_action.expiry),
        "oldNonce": str(original_action.nonce),
        "newNonce": str(refreshed_action.nonce),
        "nonceLoanId": str(nonce_loan_id),
        "nonce": str(refreshed_action.nonce),
        "subaccountId": str(refreshed_action.subaccount_id),
        "amount": amount_str,
        "assetName": asset_name,
        "reissueTx": reissue_tx,
        "debugEndpoint": debug_endpoint,
        "privateEndpoint": private_endpoint,
        "debugResult": debug_json.get("result"),
        "privateAttempts": private_attempts_used,
        "privateResult": private_json.get("result"),
        "privateId": private_json.get("id"),
    }
    typer.echo(json.dumps(out, indent=2))


if __name__ == "__main__":
    app()
