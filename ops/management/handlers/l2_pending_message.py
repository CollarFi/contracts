#!/usr/bin/env python3
from __future__ import annotations

from urllib.parse import urlparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lz_harness.common import cast_call, cast_send, run  # noqa: E402
from management.l2_common import extract_tx_hash, wallet_address, wallet_sign  # noqa: E402
from management.handlers.l2_derive_client import submit_api_for_pending_message, submit_with_retries  # noqa: E402
from management.handlers.l2_tsa_actions import (  # noqa: E402
    ACTION_DEPOSIT_INTENT,
    ACTION_RETURN_REQUEST,
    action_name,
    abi_encode,
    build_pending_action,
    format_action_tuple,
    parse_uint,
    parse_pending_message,
    quote_ack_native_fee,
)
from py_lib.keeper_logs import data_int, topic_hex, topic_int  # noqa: E402
from py_lib.keeper_state import save_keeper_state  # noqa: E402


def ensure_api_state(state: dict[str, Any]) -> None:
    if "apiSubmitted" not in state or not isinstance(state.get("apiSubmitted"), dict):
        state["apiSubmitted"] = {}
    if "messageTxs" not in state or not isinstance(state.get("messageTxs"), dict):
        state["messageTxs"] = {}


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
    signer: Any,
    account: str,
    private_key: str,
    from_addr: str,
    unlocked: bool,
) -> dict[str, Any]:
    sender_addr = wallet_address(account=account, private_key=private_key) if (account or private_key) else from_addr
    _ensure_local_trade_executor(rpc_url, matching_addr_value, sender_addr)
    _ensure_local_trade_executor(rpc_url, matching_addr_value, atomic_executor_addr)

    if signer is None:
        raise RuntimeError("L2 keeper runtime missing signer for local atomic submit")

    # Encode the atomicVerifyAndMatch call data and send via KeeperSigner so
    # nonces stay consistent with other keeper txs.
    action_data = abi_encode(
        "f((uint256,uint256,address,bytes,uint256,address,address))",
        format_action_tuple(action),
    )
    call_data = run(
        [
            "cast",
            "calldata",
            "atomicVerifyAndMatch((uint256,uint256,address,bytes,uint256,address,address)[],bytes[],bytes,(bool,bytes)[])",
            f"[{format_action_tuple(action)}]",
            f"[{signer_sig}]",
            action_data,
            "[(true,0x)]",
        ]
    )
    tx_hash = signer.send_tx(
        to=atomic_executor_addr,
        data=bytes.fromhex(call_data[2:]),
        value_wei=0,
        label="L2 keeper localAtomic atomicVerifyAndMatch",
    )
    return {
        "mode": "localAtomic",
        "matchingTx": tx_hash,
        "typedDataHash": str(action["typedDataHash"]),
        "subaccountId": str(action["subaccountId"]),
        "nonce": str(action["nonce"]),
        "expiry": str(action["expiry"]),
    }


def _send_deposit_confirmed_after_execution(runtime: Any, *, loan_id: int, value_wei: int) -> str:
    if getattr(runtime, "signer", None) is None:
        raise RuntimeError("L2 keeper runtime missing signer for sendDepositConfirmedAfterExecution in broadcast mode")

    call_data = run(
        [
            "cast",
            "calldata",
            "sendDepositConfirmedAfterExecution(uint256)",
            str(int(loan_id)),
        ]
    )
    return runtime.signer.send_tx(
        to=runtime.receiver_addr,
        data=bytes.fromhex(call_data[2:]),
        value_wei=int(value_wei),
        label=f"L2 keeper sendDepositConfirmedAfterExecution {loan_id}",
    )


def _collateral_return_already_sent(runtime: Any, *, loan_id: int) -> bool:
    raw = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "collateralReturnedSent(uint256)(bool)",
        str(int(loan_id)),
        allow_fail=True,
    )
    return raw.strip().lower() == "true"


def _quote_collateral_return_native_fee(runtime: Any, *, loan_id: int, pending_message: dict[str, Any]) -> int:
    vault_recipient = cast_call(runtime.rpc_url, runtime.receiver_addr, "vaultRecipient()(address)").strip()
    wrapped_underlying = cast_call(
        runtime.rpc_url,
        runtime.wrapped_deposit_asset,
        "wrappedAsset()(address)",
        allow_fail=True,
    ).strip().split()[0]
    bridge_fee = int(
        parse_uint(cast_call(
            runtime.rpc_url,
            runtime.tsa_addr,
            "estimateBridgeFees(address,address,uint256)(uint256)",
            wrapped_underlying,
            vault_recipient,
            str(int(pending_message["amount"])),
        ))
    )
    options = cast_call(runtime.rpc_url, runtime.receiver_addr, "defaultOptions()(bytes)").strip()
    subaccount_id = cast_call(runtime.rpc_url, runtime.tsa_addr, "subAccount()(uint256)").strip().split()[0]
    message_tuple = (
        f"(4,{int(loan_id)},{pending_message['asset']},{int(pending_message['amount'])},{vault_recipient},"
        f"{subaccount_id},0x{'0' * 64},0,0x{'0' * 64},0,0x)"
    )
    quote_raw = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
        message_tuple,
        options,
    ).strip()
    lz_fee = parse_uint(quote_raw.split(",", 1)[0].lstrip("(").strip()) if quote_raw.startswith("(") else parse_uint(quote_raw)
    lz_fee_with_buffer = lz_fee + (lz_fee * runtime.lz_fee_buffer_bps) // 10_000
    return bridge_fee + lz_fee_with_buffer


def _bridge_pending_return_and_notify(
    runtime: Any,
    *,
    loan_id: int,
    pending_message: dict[str, Any],
    value_wei: int,
) -> str:
    if getattr(runtime, "signer", None) is None:
        raise RuntimeError("L2 keeper runtime missing signer for bridgePendingReturnAndNotify in broadcast mode")

    call_data = run(
        [
            "cast",
            "calldata",
            "bridgePendingReturnAndNotify(uint256,address,uint256)",
            str(int(loan_id)),
            str(pending_message["asset"]),
            str(int(pending_message["amount"])),
        ]
    )
    return runtime.signer.send_tx(
        to=runtime.receiver_addr,
        data=bytes.fromhex(call_data[2:]),
        value_wei=int(value_wei),
        label=f"L2 keeper bridgePendingReturnAndNotify {loan_id}",
    )


def _should_submit_api(action_type: int, submit_deposit_api: bool, submit_withdraw_api: bool) -> bool:
    return (action_type == ACTION_DEPOSIT_INTENT and submit_deposit_api) or (
        action_type == ACTION_RETURN_REQUEST and submit_withdraw_api
    )


def _requires_follow_up(runtime: Any, action_type: int) -> bool:
    if action_type not in {ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST}:
        return False
    if runtime.local_atomic_submit:
        return True
    return _should_submit_api(action_type, runtime.submit_deposit_api, runtime.submit_withdraw_api)


def _action_already_executed(runtime: Any, *, action_type: int, loan_id: int) -> bool:
    if action_type == ACTION_DEPOSIT_INTENT:
        raw = cast_call(runtime.rpc_url, runtime.tsa_addr, "depositExecuted(uint256)(bool)", str(int(loan_id)), allow_fail=True)
        return raw.strip().lower() == "true"
    if action_type == ACTION_RETURN_REQUEST:
        raw = cast_call(runtime.rpc_url, runtime.tsa_addr, "withdrawExecuted(uint256)(bool)", str(int(loan_id)), allow_fail=True)
        return raw.strip().lower() == "true"
    return False


def _deposit_already_confirmed(runtime: Any, *, loan_id: int) -> bool:
    raw = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "depositConfirmed(uint256)(bool)",
        str(int(loan_id)),
        allow_fail=True,
    )
    return raw.strip().lower() == "true"


def _deposit_confirm_blocked_by_return(runtime: Any, *, loan_id: int) -> bool:
    return_completed = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "returnCompleted(uint256)(bool)",
        str(int(loan_id)),
        allow_fail=True,
    )
    if return_completed.strip().lower() == "true":
        return True

    collateral_returned_sent = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "collateralReturnedSent(uint256)(bool)",
        str(int(loan_id)),
        allow_fail=True,
    )
    return collateral_returned_sent.strip().lower() == "true"


def _load_pending_message(runtime: Any, guid: str) -> tuple[str, dict[str, Any]]:
    pending_raw = cast_call(
        runtime.rpc_url,
        runtime.receiver_addr,
        "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
        guid,
        allow_fail=True,
    )
    if pending_raw == "N/A":
        raise RuntimeError("failed to read pending message")
    return pending_raw, parse_pending_message(pending_raw)


def _submit_follow_up(
    runtime: Any,
    *,
    action_type: int,
    loan_id: int,
    pending_raw: str,
    pending_message: dict[str, Any],
) -> dict[str, Any]:
    item_updates: dict[str, Any] = {}

    if action_type not in {ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST}:
        return item_updates

    if runtime.local_atomic_submit:
        action_data = build_pending_action(
            action_type=action_type,
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
        item_updates["deriveApi"] = _submit_action_to_local_atomic(
            rpc_url=runtime.rpc_url,
            atomic_executor_addr=runtime.atomic_executor_addr,
            matching_addr_value=runtime.matching_addr,
            action=action_data,
            signer_sig=signer_sig,
            signer=runtime.signer,
            account=runtime.account,
            private_key=runtime.private_key,
            from_addr=runtime.sender,
            unlocked=runtime.unlocked,
        )
        if action_type == ACTION_DEPOSIT_INTENT:
            if _deposit_confirm_blocked_by_return(runtime, loan_id=loan_id):
                item_updates["depositConfirmedSkipped"] = "return-completed"
            else:
                fee = quote_ack_native_fee(runtime.rpc_url, runtime.receiver_addr, pending_raw)
                fee_with_buffer = fee + (fee * runtime.lz_fee_buffer_bps) // 10_000
                ack_hash = _send_deposit_confirmed_after_execution(
                    runtime,
                    loan_id=loan_id,
                    value_wei=int(fee_with_buffer),
                )
                item_updates["depositConfirmedTx"] = ack_hash
        elif action_type == ACTION_RETURN_REQUEST and not _collateral_return_already_sent(runtime, loan_id=loan_id):
            fee_with_buffer = _quote_collateral_return_native_fee(runtime, loan_id=loan_id, pending_message=pending_message)
            bridge_hash = _bridge_pending_return_and_notify(
                runtime,
                loan_id=loan_id,
                pending_message=pending_message,
                value_wei=int(fee_with_buffer),
            )
            item_updates["collateralReturnedTx"] = bridge_hash
        return item_updates

    if _should_submit_api(action_type, runtime.submit_deposit_api, runtime.submit_withdraw_api):
        if _action_already_executed(runtime, action_type=action_type, loan_id=loan_id):
            item_updates["deriveApi"] = {
                "status": "alreadyExecutedOnchain",
                "loanId": str(loan_id),
            }
            if action_type == ACTION_DEPOSIT_INTENT and not _deposit_already_confirmed(runtime, loan_id=loan_id):
                if _deposit_confirm_blocked_by_return(runtime, loan_id=loan_id):
                    item_updates["depositConfirmedSkipped"] = "return-completed"
                else:
                    fee = quote_ack_native_fee(runtime.rpc_url, runtime.receiver_addr, pending_raw)
                    fee_with_buffer = fee + (fee * runtime.lz_fee_buffer_bps) // 10_000
                    ack_hash = _send_deposit_confirmed_after_execution(
                        runtime,
                        loan_id=loan_id,
                        value_wei=int(fee_with_buffer),
                    )
                    item_updates["depositConfirmedTx"] = ack_hash
            elif action_type == ACTION_RETURN_REQUEST and not _collateral_return_already_sent(runtime, loan_id=loan_id):
                fee_with_buffer = _quote_collateral_return_native_fee(runtime, loan_id=loan_id, pending_message=pending_message)
                bridge_hash = _bridge_pending_return_and_notify(
                    runtime,
                    loan_id=loan_id,
                    pending_message=pending_message,
                    value_wei=int(fee_with_buffer),
                )
                item_updates["collateralReturnedTx"] = bridge_hash
            return item_updates

        api_meta, api_attempt = submit_with_retries(
            lambda: submit_api_for_pending_message(
                action_type=action_type,
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
        item_updates["deriveApiAttempts"] = str(api_attempt)
        item_updates["deriveApi"] = api_meta
        if action_type == ACTION_RETURN_REQUEST and not _collateral_return_already_sent(runtime, loan_id=loan_id):
            fee_with_buffer = _quote_collateral_return_native_fee(runtime, loan_id=loan_id, pending_message=pending_message)
            bridge_hash = _bridge_pending_return_and_notify(
                runtime,
                loan_id=loan_id,
                pending_message=pending_message,
                value_wei=int(fee_with_buffer),
            )
            item_updates["collateralReturnedTx"] = bridge_hash

    return item_updates


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
        follow_up_required = _requires_follow_up(runtime, action)
        already_submitted = guid in state["apiSubmitted"]

        if (
            already_handled
            and runtime.local_atomic_submit
            and not already_submitted
            and _action_already_executed(runtime, action_type=action, loan_id=loan_id)
        ):
            if action != ACTION_DEPOSIT_INTENT or _deposit_already_confirmed(runtime, loan_id=loan_id):
                continue

        if already_handled and not (runtime.broadcast and follow_up_required and not already_submitted):
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
                pending_raw, pending_message = _load_pending_message(runtime, guid)

                if already_handled and runtime.local_atomic_submit:
                    raise RuntimeError(
                        "message already handled but local atomic follow-up was not recorded; refusing to re-submit local atomic action"
                    )

                if not already_handled:
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

                item.update(
                    _submit_follow_up(
                        runtime,
                        action_type=action,
                        loan_id=loan_id,
                        pending_raw=pending_raw,
                        pending_message=pending_message,
                    )
                )

                if item.get("deriveApi") is not None:
                    state["apiSubmitted"][guid] = {
                        "action": action_name(action),
                        "submittedAt": int(time.time()),
                        "deriveApi": item["deriveApi"],
                    }
                tx_record = {
                    "action": action_name(action),
                    "loanId": str(loan_id),
                    "completedAt": int(time.time()),
                }
                for key in ("tx", "depositConfirmedTx", "collateralReturnedTx"):
                    if item.get(key):
                        tx_record[key] = item[key]
                derive_api = item.get("deriveApi")
                if isinstance(derive_api, dict):
                    for key in ("matchingTx", "apiId"):
                        if derive_api.get(key):
                            tx_record[key] = derive_api[key]
                if len(tx_record) > 3:
                    state["messageTxs"][guid] = tx_record
                save_keeper_state(runtime.state_file, state)

                item["status"] = "sent"
                sent += 1
            except Exception as exc:
                item["status"] = f"error: {exc}"

        handled.append(item)

    return attempts, sent
