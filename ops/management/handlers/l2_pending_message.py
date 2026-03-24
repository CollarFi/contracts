#!/usr/bin/env python3
from __future__ import annotations

from urllib.parse import urlparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lz_harness.common import cast_call, cast_send  # noqa: E402
from management.l2_common import extract_tx_hash, wallet_address, wallet_sign  # noqa: E402
from management.handlers.l2_derive_client import submit_api_for_pending_message, submit_with_retries  # noqa: E402
from management.handlers.l2_tsa_actions import (  # noqa: E402
    ACTION_DEPOSIT_INTENT,
    ACTION_RETURN_REQUEST,
    action_name,
    abi_encode,
    build_pending_action,
    format_action_tuple,
    parse_pending_message,
    quote_ack_native_fee,
)
from py_lib.keeper_logs import data_int, topic_hex, topic_int  # noqa: E402
from py_lib.keeper_state import save_keeper_state  # noqa: E402


def ensure_api_state(state: dict[str, Any]) -> None:
    if "apiSubmitted" not in state or not isinstance(state.get("apiSubmitted"), dict):
        state["apiSubmitted"] = {}


def is_local_rpc(rpc_url: str) -> bool:
    try:
        parsed = urlparse(rpc_url)
    except Exception:
        return False
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def matching_addr(rpc_url: str, tsa_addr: str, configured_matching: str) -> str:
    if configured_matching:
        return configured_matching
    raw = cast_call(
        rpc_url,
        tsa_addr,
        "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
    )
    return raw.strip().splitlines()[-1].strip()


def _ensure_local_trade_executor(rpc_url: str, matching_addr_value: str, executor_addr: str) -> None:
    if cast_call(rpc_url, matching_addr_value, "tradeExecutors(address)(bool)", executor_addr, allow_fail=True).strip().lower() == "true":
        return

    owner_addr = cast_call(rpc_url, matching_addr_value, "owner()(address)").strip()
    cast_send(
        rpc_url,
        None,
        matching_addr_value,
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
    matching_addr_value: str,
    action: dict[str, Any],
    signer_sig: str,
    account: str,
    private_key: str,
    from_addr: str,
    unlocked: bool,
) -> dict[str, Any]:
    sender_addr = wallet_address(account=account, private_key=private_key) if (account or private_key) else from_addr
    _ensure_local_trade_executor(rpc_url, matching_addr_value, sender_addr)
    _ensure_local_trade_executor(rpc_url, matching_addr_value, atomic_executor_addr)

    action_data = abi_encode(
        "f((uint256,uint256,address,bytes,uint256,address,address))",
        format_action_tuple(action),
    )
    tx_out = cast_send(
        rpc_url,
        account or None,
        atomic_executor_addr,
        "atomicVerifyAndMatch((uint256,uint256,address,bytes,uint256,address,address)[],bytes[],bytes,(bool,bytes)[])",
        f"[{format_action_tuple(action)}]",
        f"[{signer_sig}]",
        action_data,
        "[(true,0x)]",
        private_key=private_key or None,
        from_addr=from_addr or None,
        unlocked=unlocked,
    )
    return {
        "mode": "localAtomic",
        "matchingTx": extract_tx_hash(tx_out),
        "typedDataHash": str(action["typedDataHash"]),
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
    }


def _should_submit_api(action_type: int, submit_deposit_api: bool, submit_withdraw_api: bool) -> bool:
    return (action_type == ACTION_DEPOSIT_INTENT and submit_deposit_api) or (
        action_type == ACTION_RETURN_REQUEST and submit_withdraw_api
    )


def process_message_logs(
    runtime: Any,
    *,
    state: dict[str, Any],
    logs: list[dict[str, Any]],
    handled: list[dict[str, Any]],
    attempts_so_far: int,
) -> tuple[int, int]:
    attempts = 0
    sent = 0

    for log in logs:
        if attempts_so_far + attempts >= runtime.max_per_tick:
            break

        guid = topic_hex(log, 1)
        loan_id = topic_int(log, 2)
        action = data_int(log, default=-1)
        block_raw = log.get("blockNumber", "0x0")
        block_no = int(block_raw, 16) if isinstance(block_raw, str) and block_raw.startswith("0x") else int(block_raw)

        if guid is None or loan_id is None or action not in runtime.allowed_actions:
            continue

        already_handled_raw = cast_call(
            runtime.rpc_url,
            runtime.receiver_addr,
            "handledMessages(bytes32)(bool)",
            guid,
            allow_fail=True,
        )
        already_handled = already_handled_raw.strip().lower() == "true"
        if already_handled:
            continue

        attempts += 1
        item: dict[str, Any] = {
            "guid": guid,
            "loanId": str(loan_id),
            "eventBlock": str(block_no),
            "action": action_name(action),
            "tx": None,
            "status": "dry-run",
        }

        if runtime.broadcast:
            try:
                pending_raw = cast_call(
                    runtime.rpc_url,
                    runtime.receiver_addr,
                    "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
                    guid,
                    allow_fail=True,
                )
                if pending_raw == "N/A":
                    raise RuntimeError("failed to read pending message")
                pending_message = parse_pending_message(pending_raw)

                # Prefer KeeperSigner (eth_account + Web3) for sending txs.
                if getattr(runtime, "signer", None) is None:
                    raise RuntimeError("L2 keeper runtime missing signer for handleMessage in broadcast mode")
                tx_hash = runtime.signer.send_contract_tx(
                    contract_name="CollarTSAReceiver",
                    address=runtime.receiver_addr,
                    fn_name="handleMessage",
                    args=[guid],
                    label=f"L2 keeper handleMessage {guid}",
                )
                item["tx"] = tx_hash

                if action in {ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST}:
                    if runtime.local_atomic_submit:
                        action_data = build_pending_action(
                            action_type=action,
                            pending_message=pending_message,
                            tsa_addr=runtime.tsa_addr,
                            deposit_module=runtime.deposit_module,
                            withdrawal_module=runtime.withdrawal_module,
                            wrapped_deposit_asset=runtime.wrapped_deposit_asset,
                            rpc_url=runtime.rpc_url,
                        )
                        signer_sig = wallet_sign(
                            str(action_data["typedDataHash"]),
                            no_hash=True,
                            account=runtime.account,
                            private_key=runtime.private_key,
                        )
                        item["deriveApi"] = _submit_action_to_local_atomic(
                            rpc_url=runtime.rpc_url,
                            atomic_executor_addr=runtime.atomic_executor_addr,
                            matching_addr_value=runtime.matching_addr,
                            action=action_data,
                            signer_sig=signer_sig,
                            account=runtime.account,
                            private_key=runtime.private_key,
                            from_addr=runtime.sender,
                            unlocked=runtime.unlocked,
                        )
                        if action == ACTION_DEPOSIT_INTENT:
                            fee = quote_ack_native_fee(runtime.rpc_url, runtime.receiver_addr, pending_raw)
                            fee_with_buffer = fee + (fee * runtime.lz_fee_buffer_bps) // 10_000
                            if getattr(runtime, "signer", None) is None:
                                raise RuntimeError(
                                    "L2 keeper runtime missing signer for sendDepositConfirmedAfterExecution in broadcast mode"
                                )
                            ack_hash = runtime.signer.send_contract_tx(
                                contract_name="CollarTSAReceiver",
                                address=runtime.receiver_addr,
                                fn_name="sendDepositConfirmedAfterExecution",
                                args=[int(loan_id)],
                                value_wei=int(fee_with_buffer),
                                label=f"L2 keeper sendDepositConfirmedAfterExecution {loan_id}",
                            )
                            item["depositConfirmedTx"] = ack_hash
                    elif _should_submit_api(action, runtime.submit_deposit_api, runtime.submit_withdraw_api):
                        api_meta, api_attempt = submit_with_retries(
                            lambda: submit_api_for_pending_message(
                                action_type=action,
                                pending_message=pending_message,
                                tsa_addr=runtime.tsa_addr,
                                account=runtime.account,
                                private_key=runtime.private_key,
                                api_url=runtime.api_url,
                                x_lyra_wallet=runtime.derive_wallet,
                                fallback_asset_name=runtime.derive_asset_name,
                                rpc_url=runtime.rpc_url,
                            ),
                            attempts=runtime.api_retry_attempts,
                            initial_delay_seconds=runtime.api_retry_initial_delay_seconds,
                            max_delay_seconds=runtime.api_retry_max_delay_seconds,
                        )
                        item["deriveApiAttempts"] = str(api_attempt)
                        item["deriveApi"] = api_meta

                if item.get("deriveApi") is not None:
                    state["apiSubmitted"][guid] = {
                        "action": action_name(action),
                        "submittedAt": int(time.time()),
                        "deriveApi": item["deriveApi"],
                    }
                    save_keeper_state(runtime.state_file, state)

                item["status"] = "sent"
                sent += 1
            except Exception as exc:
                item["status"] = f"error: {exc}"

        handled.append(item)

    return attempts, sent
