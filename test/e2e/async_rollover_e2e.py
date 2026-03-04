#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent

import sys

sys.path.insert(0, str(THIS_DIR))
from defaults import (
    L1_ANVIL_PORT,
    L1_ARTIFACT_JSON,
    L1_COLLATERAL_ASSET,
    L1_DEBT_ASSET,
    L1_WETH_SOCKET_CONNECTOR,
    L1_WETH_SOCKET_VAULT,
    L2_ANVIL_PORT,
    L2_ARTIFACT_JSON,
)

from common import (
    ANVIL_PK0,
    BORROWER_PK,
    abi_encode as _abi_encode,
    cast_call,
    cast_send_pk,
    ensure_keeper_role as _ensure_keeper_role,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_liquidity_vault_role as _ensure_liquidity_vault_role,
    ensure_live_deployments as _ensure_live_deployments,
    inject_lz_message as _inject_lz_message,
    print_step as _print_step,
    require_code as _require_code,
    run,
    sign_no_prefix as _sign_no_prefix,
)
from loan_flow_helpers import extract_tx_hash, finalize_fresh_loan_to_active_zero_cost, get_loan, latest_timestamp

app = typer.Typer(add_completion=False)


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    already = cast_call(
        l2_rpc,
        receiver,
        "hasRole(bytes32,address)(bool)",
        keeper_role,
        "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
    ).strip().lower() == "true"
    if already:
        return
    cast_send_pk(
        l2_rpc,
        receiver,
        "grantRole(bytes32,address)",
        keeper_role,
        "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        private_key=ANVIL_PK0,
    )


def _parse_l2_store_rollover(raw: str) -> tuple[bool, str]:
    cleaned = re.sub(r"\s*\[[^\]]+\]", "", raw).strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    parts = [part.strip() for part in cleaned.split(",")]
    if len(parts) < 14:
        raise RuntimeError(f"unexpected loan-store tuple output: {raw}")
    pending = parts[12].lower() == "true"
    mandate_hash = parts[13]
    return pending, mandate_hash


@app.command()
def main(
    l1_json: Path = typer.Option(L1_ARTIFACT_JSON),
    l2_json: Path = typer.Option(L2_ARTIFACT_JSON),
    l1_rpc: str = typer.Option(f"http://127.0.0.1:{L1_ANVIL_PORT}"),
    l2_rpc: str = typer.Option(f"http://127.0.0.1:{L2_ANVIL_PORT}"),
    sepolia_usdc: str = typer.Option(L1_DEBT_ASSET),
    sepolia_weth: str = typer.Option(L1_COLLATERAL_ASSET),
    auto_redeploy: bool = typer.Option(True, "--auto-redeploy/--no-auto-redeploy"),
):
    print("=== collar.fi async rollover e2e ===")

    _ensure_l1_sepolia_rpc(l1_rpc)
    _ensure_l2_derive_rpc(l2_rpc)
    _print_step(True, "RPC topology verified (L1=11155111, L2=901)")

    _require_code(l1_rpc, sepolia_weth, "Sepolia WETH collateral")
    _require_code(l1_rpc, sepolia_usdc, "Sepolia USDC debt")

    l1_path = ROOT / l1_json
    l2_path = ROOT / l2_json
    l1, l2, redeployed = _ensure_live_deployments(
        l1_path,
        l2_path,
        l1_rpc,
        l2_rpc,
        sepolia_usdc,
        sepolia_weth,
        auto_redeploy,
        L1_WETH_SOCKET_VAULT,
        L1_WETH_SOCKET_CONNECTOR,
    )
    if redeployed:
        _print_step(True, "Detected stale deployments/runtime and refreshed via deployment_e2e")

    vault = l1["l1Vault"]
    l1_messenger = l1["l1Messenger"]
    l2_receiver = l2["l2Receiver"]

    _ensure_liquidity_vault_role(l1_rpc, vault)
    _ensure_keeper_role(l1_rpc, vault)
    _ensure_l2_keeper_role(l2_rpc, l2_receiver)

    active = finalize_fresh_loan_to_active_zero_cost(l1_json, l2_json, l1_rpc, l2_rpc, vault, l1_messenger, sepolia_weth)
    loan_id = int(active["loanId"])
    loan = active["loan"]
    _print_step(True, f"Created ACTIVE_ZERO_COST loan for rollover (loanId={loan_id})")

    mock_messenger = json.loads(
        run(
            [
                "forge",
                "create",
                "test/CollarVault.t.sol:MockLZMessenger",
                "--rpc-url",
                l1_rpc,
                "--private-key",
                ANVIL_PK0,
                "--broadcast",
                "--json",
            ]
        )
    )["deployedTo"]
    cast_send_pk(l1_rpc, vault, "setLZMessenger(address)", mock_messenger, private_key=ANVIL_PK0)
    _print_step(True, f"Switched vault messenger to deterministic mock ({mock_messenger})")

    now_ts = latest_timestamp(l1_rpc)
    borrower = str(loan["borrower"])
    old_call = int(loan["callStrike"])
    old_put = int(loan["putStrike"])
    old_maturity = int(loan["maturity"])
    principal = int(loan["principal"])
    new_maturity = old_maturity + 7 * 24 * 3600
    deadline = now_ts + 24 * 3600
    min_call_strike = old_call + 1
    max_put_strike = old_put + 1_000_000
    min_net_interest = 100_000_000_000
    nonce = 880_000 + loan_id

    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((principal * apr) // 10**18) * (new_maturity - now_ts) // year
    max_roll_ltv = int(cast_call(l1_rpc, vault, "maxRollLtv()(uint256)").split()[0])
    strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", sepolia_weth).split()[0])

    mandate_tuple = (
        f"({borrower},{loan_id},{new_maturity},{min_call_strike},{max_put_strike},"
        f"{min_net_interest},{deadline},{nonce})"
    )
    mandate_hash = cast_call(
        l1_rpc,
        vault,
        "hashRolloverMandate((address,uint256,uint64,uint256,uint256,uint256,uint64,uint256))(bytes32)",
        mandate_tuple,
    ).splitlines()[0].strip()
    mandate_sig = _sign_no_prefix(mandate_hash, BORROWER_PK)

    rollover_data = _abi_encode(
        "f(bytes32,address,uint64,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint256)",
        mandate_hash,
        borrower,
        str(new_maturity),
        str(min_call_strike),
        str(max_put_strike),
        str(min_net_interest),
        str(fixed_interest),
        str(max_roll_ltv),
        str(strike_scale),
        str(deadline),
        str(nonce),
    )

    execute_raw = cast_send_pk(
        l1_rpc,
        vault,
        "executeRollover(uint256,(address,uint256,uint64,uint256,uint256,uint256,uint64,uint256),bytes,uint256,uint256)",
        str(loan_id),
        mandate_tuple,
        mandate_sig,
        str(min_call_strike),
        str(max_put_strike),
        private_key=ANVIL_PK0,
    )
    execute_tx = extract_tx_hash(execute_raw)
    _print_step(True, f"Executed rollover request on L1 (tx={execute_tx})")
    intent_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(7,{loan_id},{sepolia_weth},{principal},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{rollover_data})",
    )
    intent_guid = "0x" + format(70_000_000 + loan_id, "064x")
    _inject_lz_message(l2_rpc, l2_receiver, intent_guid, intent_msg)
    cast_send_pk(l2_rpc, l2_receiver, "handleMessage(bytes32)", intent_guid, private_key=ANVIL_PK0)

    loan_store = cast_call(l2_rpc, l2_receiver, "loanStore()(address)").splitlines()[0].strip()
    store_raw = cast_call(
        l2_rpc,
        loan_store,
        "getLoan(uint256)((address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64,address,uint256,bool,bytes32,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64,bool))",
        str(loan_id),
    )
    rollover_pending, stored_hash = _parse_l2_store_rollover(store_raw)
    if not rollover_pending or stored_hash.lower() != mandate_hash.lower():
        raise RuntimeError(
            f"L2 rollover intent not recorded correctly (pending={rollover_pending}, hash={stored_hash}, expected={mandate_hash})"
        )
    _print_step(True, "Handled rollover intent on L2 and recorded pending rollover state")

    confirm_call = min_call_strike - 1
    confirm_put = max_put_strike + 1
    confirm_interest_apr = 10**16
    realized_c = -10_000_000
    confirm_data = _abi_encode(
        "f(bytes32,address,uint256,uint256,uint256,uint64,int256)",
        mandate_hash,
        borrower,
        str(confirm_call),
        str(confirm_put),
        str(confirm_interest_apr),
        str(new_maturity),
        str(realized_c),
    )
    confirm_guid = "0x" + format(80_000_000 + loan_id, "064x")
    cast_send_pk(
        l1_rpc,
        mock_messenger,
        "setMessage(bytes32,(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        confirm_guid,
        f"(8,{loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{confirm_data})",
        private_key=ANVIL_PK0,
    )

    finalize_raw = cast_send_pk(l1_rpc, vault, "finalizeRollover(uint256,bytes32)", str(loan_id), confirm_guid)
    finalize_tx = extract_tx_hash(finalize_raw)
    _print_step(True, f"Finalized rollover with anomaly payload (tx={finalize_tx})")

    finalized = get_loan(vault, l1_rpc, loan_id)
    if int(finalized["callStrike"]) != confirm_call:
        raise RuntimeError(f"call strike mismatch after finalize: expected {confirm_call}, got {finalized['callStrike']}")
    if int(finalized["putStrike"]) != confirm_put:
        raise RuntimeError(f"put strike mismatch after finalize: expected {confirm_put}, got {finalized['putStrike']}")
    if int(finalized["maturity"]) != new_maturity:
        raise RuntimeError(f"maturity mismatch after finalize: expected {new_maturity}, got {finalized['maturity']}")
    if int(finalized["interestApr"]) != confirm_interest_apr:
        raise RuntimeError(
            f"interest APR mismatch after finalize: expected {confirm_interest_apr}, got {finalized['interestApr']}"
        )

    consumed = cast_call(l1_rpc, vault, "lzMessageConsumed(bytes32)(bool)", confirm_guid).strip().lower() == "true"
    if not consumed:
        raise RuntimeError("rollover confirmation guid not marked consumed")

    receipt = json.loads(run(["cast", "receipt", finalize_tx, "--rpc-url", l1_rpc, "--json"]))
    block_number_raw = receipt.get("blockNumber")
    block_number = int(block_number_raw, 0) if isinstance(block_number_raw, str) else int(block_number_raw)

    logs = json.loads(
        run(
            [
                "cast",
                "logs",
                "RolloverFinalizeAnomaly(uint256,bytes32,uint256,uint256,uint256,uint256,int256)",
                "--address",
                vault,
                "--from-block",
                str(block_number),
                "--to-block",
                str(block_number),
                "--rpc-url",
                l1_rpc,
                "--json",
            ]
        )
    )
    loan_topic = "0x" + int(loan_id).to_bytes(32, "big", signed=False).hex()
    found = any(
        isinstance(item, dict)
        and isinstance(item.get("topics"), list)
        and len(item["topics"]) >= 3
        and str(item["topics"][1]).lower() == loan_topic.lower()
        and str(item["topics"][2]).lower() == confirm_guid.lower()
        for item in logs
    )
    if not found:
        raise RuntimeError("expected RolloverFinalizeAnomaly event not found")
    _print_step(True, "Observed anomaly signal and non-bricking finalize state transition")

    cast_send_pk(l1_rpc, vault, "finalizeRollover(uint256,bytes32)", str(loan_id), confirm_guid)
    _print_step(True, "Verified finalizeRollover idempotency on duplicate confirmation guid")

    cast_send_pk(l1_rpc, vault, "setLZMessenger(address)", l1_messenger, private_key=ANVIL_PK0)
    _print_step(True, "Restored original L1 messenger after rollover test")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
