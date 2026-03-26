#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich import print

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must  # noqa: E402
from management.handlers.l2_derive_client import (  # noqa: E402
    build_pending_message_debug_payload,
    build_private_action_body,
    post_private_deposit,
    post_public_deposit_debug,
    sign_private_api_auth,
)
from management.handlers.l2_tsa_actions import ACTION_DEPOSIT_INTENT, parse_pending_message  # noqa: E402
from management.l2_common import assert_tsa_signer, wallet_address, wallet_sign  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l2_env_path  # noqa: E402

app = typer.Typer(add_completion=False)


def _load_pending_message_by_guid(rpc_url: str, receiver_addr: str, guid: str) -> tuple[str, dict[str, Any]]:
    pending_raw = cast_call(
        rpc_url,
        receiver_addr,
        "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
        guid,
        allow_fail=True,
    )
    if pending_raw == "N/A":
        raise RuntimeError(f"failed to read pending message for guid={guid}")
    pending_message = parse_pending_message(pending_raw)
    if int(pending_message["action"]) != ACTION_DEPOSIT_INTENT:
        raise ValueError(
            f"pending message for guid={guid} is action={pending_message['action']}, expected {ACTION_DEPOSIT_INTENT}"
        )
    return pending_raw, pending_message


def _build_manual_pending_message(
    *,
    loan_id: int,
    asset: str,
    amount: int,
    subaccount_id: int,
) -> dict[str, Any]:
    if loan_id <= 0:
        raise ValueError("manual mode requires --loan-id")
    if not asset:
        raise ValueError("manual mode requires --asset")
    if amount <= 0:
        raise ValueError("manual mode requires --amount in base units")
    if subaccount_id < 0:
        raise ValueError("manual mode requires --subaccount-id")
    return {
        "action": ACTION_DEPOSIT_INTENT,
        "loanId": int(loan_id),
        "asset": asset,
        "amount": int(amount),
        "recipient": "0x0000000000000000000000000000000000000000",
        "subaccountId": int(subaccount_id),
        "socketMessageId": "0x" + ("00" * 32),
        "secondaryAmount": 0,
        "quoteHash": "0x" + ("00" * 32),
        "takerNonce": 0,
        "data": "0x",
    }


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    guid: str = typer.Option("", "--guid", help="Pending message guid from the keeper log."),
    receiver: str = typer.Option(
        "",
        "--receiver",
        help="L2 receiver address override. Defaults to L2_RECEIVER env or deployment output.",
    ),
    tsa: str = typer.Option("", "--tsa", help="TSA signer address override. Defaults to receiver.tsa()."),
    loan_id: int = typer.Option(0, "--loan-id", help="Manual mode only."),
    asset: str = typer.Option("", "--asset", help="Manual mode only. ERC20 asset address."),
    amount: int = typer.Option(0, "--amount", help="Manual mode only. Raw token amount in base units."),
    subaccount_id: int = typer.Option(-1, "--subaccount-id", help="Manual mode only."),
    nonce: int = typer.Option(0, "--nonce", help="Override nonce sent to Derive."),
    signature_expiry_sec: int = typer.Option(0, "--signature-expiry-sec", help="Override signature expiry."),
    derive_api_url: str = typer.Option(
        "",
        "--derive-api-url",
        help="Derive API base URL (default: DERIVE_API_URL env or https://api-demo.lyra.finance)",
    ),
    derive_wallet: str = typer.Option(
        "",
        "--derive-wallet",
        help="X-LyraWallet header override (default: DERIVE_WALLET env or TSA address).",
    ),
    derive_asset_name: str = typer.Option(
        "",
        "--derive-asset-name",
        help="Asset name override for the Derive payload (default: DERIVE_ASSET_NAME env or token symbol).",
    ),
    account: str = typer.Option("", "--account", help="Foundry keystore account override."),
    private_key: str = typer.Option("", "--private-key", help="Raw private key override."),
    timestamp_ms: str = typer.Option("", "--timestamp-ms", help="Override X-LyraTimestamp for reproducibility."),
    submit: bool = typer.Option(
        True,
        "--submit/--debug-only",
        help="Issue private/deposit after deposit_debug. Use --debug-only to skip the real submit.",
    ),
    skip_signer_check: bool = typer.Option(
        False,
        "--skip-signer-check",
        help="Skip onchain isSigner() validation for the configured API signer.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account_name = account or env.get("ACCOUNT", "")
    pk = private_key or env.get("PRIVATE_KEY", "")
    if not (account_name or pk):
        raise ValueError("provide ACCOUNT in env, or pass --account, or pass --private-key")

    receiver_addr = receiver or resolve_addr(env, "L2_RECEIVER", "l2Receiver", "l2")
    tsa_addr = (tsa or cast_call(rpc_url, receiver_addr, "tsa()(address)").strip()).strip()
    api_url = (derive_api_url or env.get("DERIVE_API_URL") or "https://api-demo.lyra.finance").strip()
    fallback_asset_name = (derive_asset_name or env.get("DERIVE_ASSET_NAME") or "").strip()
    x_lyra_wallet = (derive_wallet or env.get("DERIVE_WALLET") or tsa_addr).strip()
    signer_wallet = wallet_address(account=account_name, private_key=pk)

    if not skip_signer_check:
        assert_tsa_signer(rpc_url, tsa_addr, signer_wallet)

    pending_raw = ""
    if guid:
        pending_raw, pending_message = _load_pending_message_by_guid(rpc_url, receiver_addr, guid)
    else:
        pending_message = _build_manual_pending_message(
            loan_id=loan_id,
            asset=asset,
            amount=amount,
            subaccount_id=subaccount_id,
        )

    debug_payload = build_pending_message_debug_payload(
        pending_message=pending_message,
        tsa_addr=tsa_addr,
        fallback_asset_name=fallback_asset_name,
        rpc_url=rpc_url,
        nonce=(nonce or None),
        signature_expiry_sec=(signature_expiry_sec or None),
    )
    debug_response = post_public_deposit_debug(api_url=api_url, body=debug_payload)
    typed_hash = str(debug_response["result"]["typed_data_hash"])
    action_signature = wallet_sign(typed_hash, no_hash=True, account=account_name, private_key=pk)

    x_lyra_timestamp, x_lyra_signature = sign_private_api_auth(
        account=account_name,
        private_key=pk,
        timestamp_ms=(timestamp_ms or None),
    )
    private_body = build_private_action_body(debug_payload=debug_payload, signature=action_signature)

    private_response: dict[str, Any] | None = None
    private_request_url = f"{api_url.rstrip('/')}/private/deposit"
    if submit:
        print("[cyan][info][/cyan] sending private/deposit request:")
        print(
            json.dumps(
                {
                    "url": private_request_url,
                    "headers": {
                        "X-LyraWallet": x_lyra_wallet,
                        "X-LyraTimestamp": x_lyra_timestamp,
                        "X-LyraSignature": x_lyra_signature,
                    },
                    "body": private_body,
                },
                indent=2,
            )
        )
        private_response = post_private_deposit(
            api_url=api_url,
            x_lyra_wallet=x_lyra_wallet,
            x_lyra_timestamp=x_lyra_timestamp,
            x_lyra_signature=x_lyra_signature,
            body=private_body,
        )

    result = {
        "envFile": str(l2_env_file),
        "rpcUrl": rpc_url,
        "apiUrl": api_url,
        "receiver": receiver_addr,
        "tsa": tsa_addr,
        "guid": guid or None,
        "pendingRaw": pending_raw or None,
        "pendingMessage": pending_message,
        "xLyraWallet": x_lyra_wallet,
        "signerWallet": signer_wallet,
        "debugPayload": debug_payload,
        "debugResponse": debug_response,
        "typedDataHash": typed_hash,
        "actionSignature": action_signature,
        "privateHeaders": {
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": x_lyra_timestamp,
            "X-LyraSignature": x_lyra_signature,
        },
        "privateUrl": private_request_url,
        "privateBody": private_body,
        "privateResponse": private_response,
    }

    if json_out:
        print(json.dumps(result, indent=2))
        return

    print(f"[cyan][info][/cyan] env: {l2_env_file}")
    print(f"[cyan][info][/cyan] rpc_url: {rpc_url}")
    print(f"[cyan][info][/cyan] api_url: {api_url}")
    print(f"[cyan][info][/cyan] receiver: {receiver_addr}")
    print(f"[cyan][info][/cyan] tsa: {tsa_addr}")
    print(f"[cyan][info][/cyan] signer_wallet: {signer_wallet}")
    print(f"[cyan][info][/cyan] x_lyra_wallet: {x_lyra_wallet}")
    if guid:
        print(f"[cyan][info][/cyan] guid: {guid}")
        print(f"[cyan][info][/cyan] pending_raw: {pending_raw}")

    print("[cyan][info][/cyan] deposit_debug payload:")
    print(json.dumps(debug_payload, indent=2))
    print("[cyan][info][/cyan] deposit_debug response:")
    print(json.dumps(debug_response, indent=2))
    print(f"[cyan][info][/cyan] typed_data_hash: {typed_hash}")
    print(f"[cyan][info][/cyan] action_signature: {action_signature}")

    print("[cyan][info][/cyan] private/deposit request:")
    print(
        json.dumps(
            {
                "url": private_request_url,
                "headers": result["privateHeaders"],
                "body": private_body,
            },
            indent=2,
        )
    )
    if private_response is None:
        print("[yellow][dry-run][/yellow] skipped private/deposit because --debug-only was used")
    else:
        print("[green][ok][/green] private/deposit response:")
        print(json.dumps(private_response, indent=2))


if __name__ == "__main__":
    app()
