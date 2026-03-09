#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
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
    BORROWER_PK,
    abi_encode as _abi_encode,
    borrower_address as _borrower_address,
    cast_call,
    cast_send_pk,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_liquidity_vault_role as _ensure_liquidity_vault_role,
    ensure_live_deployments as _ensure_live_deployments,
    ensure_token_balance as _ensure_token_balance,
    inject_lz_message as _inject_lz_message,
    print_step as _print_step,
    require_code as _require_code,
    set_time as _set_time,
)
from loan_flow_helpers import finalize_fresh_atomic_loan_to_active_zero_cost, get_loan

app = typer.Typer(add_completion=False)


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
    print("=== collar.fi ready-for-variable borrower self-close e2e ===")

    _ensure_l1_sepolia_rpc(l1_rpc)
    _ensure_l2_derive_rpc(l2_rpc)
    _print_step(True, "RPC topology verified (L1=11155111, L2=901)")

    _require_code(l1_rpc, sepolia_weth, "Sepolia WETH collateral")
    _require_code(l1_rpc, sepolia_usdc, "Sepolia USDC debt")

    l1_path = ROOT / l1_json
    l2_path = ROOT / l2_json
    l1, _, redeployed = _ensure_live_deployments(
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
    messenger = l1["l1Messenger"]
    liquidity_vault = l1["l1LiquidityVault"]

    _ensure_liquidity_vault_role(l1_rpc, vault)

    finalized = finalize_fresh_atomic_loan_to_active_zero_cost(
        l1_json, l2_json, l1_rpc, l2_rpc, vault, messenger, sepolia_weth
    )
    loan_id = int(finalized["loanId"])
    pending = finalized["pending"]
    loan = finalized["loan"]
    subaccount_id = int(finalized["mandate"]["subaccountId"])
    _print_step(True, f"Created ACTIVE_ZERO_COST loan via deterministic finalize path (loanId={loan_id})")

    _set_time(l1_rpc, int(loan["maturity"]) + 1)
    collateral_guid = "0x" + format(81_000_000 + loan_id, "064x")
    collateral_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(4,{loan_id},{sepolia_weth},{pending['collateral']},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    _inject_lz_message(l1_rpc, messenger, collateral_guid, collateral_msg)
    cast_send_pk(l1_rpc, vault, "settleLoan(uint256,uint8,bytes32)", str(loan_id), "1", collateral_guid)
    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(pending["collateral"]))
    _print_step(True, "Moved loan to READY_FOR_VARIABLE using deterministic collateral-returned message")

    total_due = int(loan["principal"]) + int(loan["interestOwed"])
    borrower = _borrower_address()

    _ensure_token_balance(l1_rpc, sepolia_usdc, borrower, total_due)
    cast_send_pk(l1_rpc, sepolia_usdc, "approve(address,uint256)", vault, str(total_due), private_key=BORROWER_PK)

    borrower_usdc_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", borrower).split()[0])
    borrower_weth_before = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])
    liquidity_usdc_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])

    cast_send_pk(
        l1_rpc,
        vault,
        "settleReadyLoanByRepay(uint256)(uint256,uint256,uint256)",
        str(loan_id),
        private_key=BORROWER_PK,
    )

    borrower_usdc_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", borrower).split()[0])
    borrower_weth_after = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])
    liquidity_usdc_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])

    if borrower_usdc_before - borrower_usdc_after != total_due:
        raise RuntimeError(
            f"borrower USDC spend mismatch: expected {total_due}, got {borrower_usdc_before - borrower_usdc_after}"
        )
    if liquidity_usdc_after - liquidity_usdc_before != total_due:
        raise RuntimeError(
            f"liquidity vault USDC receive mismatch: expected {total_due}, got {liquidity_usdc_after - liquidity_usdc_before}"
        )
    if borrower_weth_after - borrower_weth_before != int(pending["collateral"]):
        raise RuntimeError(
            "borrower collateral return mismatch: "
            f"expected {pending['collateral']}, got {borrower_weth_after - borrower_weth_before}"
        )

    closed = get_loan(vault, l1_rpc, loan_id)
    if int(closed["state"]) != 4:
        raise RuntimeError(f"loan not CLOSED after borrower self-close (state={closed['state']})")
    _print_step(True, "Borrower self-close settled READY_FOR_VARIABLE loan during grace period")

    out = {
        "status": "success",
        "loanId": loan_id,
        "collateralGuid": collateral_guid,
        "totalDue": total_due,
        "borrowerCollateral": int(pending["collateral"]),
    }
    path = Path(tempfile.mkdtemp(prefix="ready-loan-borrower-self-close-e2e-")) / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
