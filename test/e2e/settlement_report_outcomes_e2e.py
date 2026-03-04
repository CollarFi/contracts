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
    abi_encode as _abi_encode,
    cast_call,
    cast_send_pk,
    ensure_keeper_role as _ensure_keeper_role,
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
from loan_flow_helpers import (
    accept_mandate_for_pending,
    get_loan,
    get_pending,
    inject_deposit_confirmed,
    inject_trade_confirmed,
    run_fresh_pending_loan,
)

app = typer.Typer(add_completion=False)


def _finalize_to_active_zero_cost(
    l1_json: Path,
    l2_json: Path,
    l1_rpc: str,
    l2_rpc: str,
    vault: str,
    messenger: str,
    collateral_asset: str,
) -> dict:
    fresh = run_fresh_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, collateral_asset)
    loan_id = int(fresh["loanId"])
    deposit_guid = fresh["depositGuid"]
    pending = get_pending(vault, l1_rpc, loan_id)
    mandate_ctx = accept_mandate_for_pending(l1_rpc, vault, collateral_asset, loan_id, pending)

    subaccount_id = int(mandate_ctx["subaccountId"])
    trade_guid = inject_trade_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        int(pending["maturity"]),
        int(pending["putStrike"]),
        int(mandate_ctx["callStrike"]),
    )
    inject_deposit_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        collateral_asset,
        int(pending["collateral"]),
        deposit_guid,
    )
    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_guid)
    loan = get_loan(vault, l1_rpc, loan_id)
    if int(loan["state"]) != 1:
        raise RuntimeError(f"loan not ACTIVE_ZERO_COST after finalize (state={loan['state']})")
    return {
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "loan": loan,
    }


def _inject_settlement_report(
    l1_rpc: str,
    messenger: str,
    loan_id: int,
    usdc: str,
    amount: int,
    vault: str,
    guid_nonce_base: int,
) -> str:
    guid = "0x" + format(guid_nonce_base + loan_id, "064x")
    payload = (
        f"(2,{loan_id},{usdc},{amount},{vault},0,"
        f"0x{'00'*32},0,0x{'00'*32},0,0x)"
    )
    encoded_message = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        payload,
    )
    _inject_lz_message(l1_rpc, messenger, guid, encoded_message)
    return guid


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
    print("=== collar.fi settlement report outcomes e2e ===")

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
    _ensure_keeper_role(l1_rpc, vault)

    treasury = cast_call(l1_rpc, vault, "treasury()(address)").splitlines()[0].strip()
    treasury_bps = int(cast_call(l1_rpc, vault, "treasuryBps()(uint256)").split()[0])

    put_flow = _finalize_to_active_zero_cost(l1_json, l2_json, l1_rpc, l2_rpc, vault, messenger, sepolia_weth)
    put_loan_id = int(put_flow["loanId"])
    put_loan = put_flow["loan"]
    _set_time(l1_rpc, int(put_loan["maturity"]) + 1)

    put_total_due = int(put_loan["principal"]) + int(put_loan["interestOwed"])
    put_excess = 3_210_987
    put_settlement = put_total_due + put_excess
    _ensure_token_balance(l1_rpc, sepolia_usdc, vault, put_settlement)

    lv_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])
    treasury_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", treasury).split()[0])
    borrower_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", put_loan["borrower"]).split()[0])

    put_guid = _inject_settlement_report(l1_rpc, messenger, put_loan_id, sepolia_usdc, put_settlement, vault, 50_000_000)
    cast_send_pk(l1_rpc, vault, "settleLoan(uint256,uint8,bytes32)", str(put_loan_id), "0", put_guid)

    lv_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])
    treasury_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", treasury).split()[0])
    borrower_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", put_loan["borrower"]).split()[0])

    expected_treasury_cut = (put_excess * treasury_bps) // 10_000
    expected_vault_cut = put_excess - expected_treasury_cut
    expected_lv_gain = put_total_due + expected_vault_cut

    if lv_after - lv_before != expected_lv_gain:
        raise RuntimeError(f"PutITM liquidity vault gain mismatch: expected {expected_lv_gain}, got {lv_after - lv_before}")
    if treasury_after - treasury_before != expected_treasury_cut:
        raise RuntimeError(
            f"PutITM treasury gain mismatch: expected {expected_treasury_cut}, got {treasury_after - treasury_before}"
        )
    if borrower_after - borrower_before != 0:
        raise RuntimeError(f"PutITM borrower should not receive excess, got {borrower_after - borrower_before}")
    if get_loan(vault, l1_rpc, put_loan_id)["state"] != 4:
        raise RuntimeError("PutITM loan is not CLOSED")
    _print_step(True, "PutITM accounting checks passed")

    call_flow = _finalize_to_active_zero_cost(l1_json, l2_json, l1_rpc, l2_rpc, vault, messenger, sepolia_weth)
    call_loan_id = int(call_flow["loanId"])
    call_loan = call_flow["loan"]
    _set_time(l1_rpc, int(call_loan["maturity"]) + 1)

    call_total_due = int(call_loan["principal"]) + int(call_loan["interestOwed"])
    call_excess = 4_567_891
    call_settlement = call_total_due + call_excess
    _ensure_token_balance(l1_rpc, sepolia_usdc, vault, call_settlement)

    lv_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])
    treasury_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", treasury).split()[0])
    borrower_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", call_loan["borrower"]).split()[0])

    call_guid = _inject_settlement_report(l1_rpc, messenger, call_loan_id, sepolia_usdc, call_settlement, vault, 60_000_000)
    cast_send_pk(l1_rpc, vault, "settleLoan(uint256,uint8,bytes32)", str(call_loan_id), "2", call_guid)

    lv_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", liquidity_vault).split()[0])
    treasury_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", treasury).split()[0])
    borrower_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", call_loan["borrower"]).split()[0])

    if lv_after - lv_before != call_total_due:
        raise RuntimeError(f"CallITM liquidity vault gain mismatch: expected {call_total_due}, got {lv_after - lv_before}")
    if treasury_after - treasury_before != 0:
        raise RuntimeError(f"CallITM treasury should not receive excess, got {treasury_after - treasury_before}")
    if borrower_after - borrower_before != call_excess:
        raise RuntimeError(
            f"CallITM borrower excess mismatch: expected {call_excess}, got {borrower_after - borrower_before}"
        )
    if get_loan(vault, l1_rpc, call_loan_id)["state"] != 4:
        raise RuntimeError("CallITM loan is not CLOSED")
    _print_step(True, "CallITM accounting checks passed")

    out = {
        "status": "success",
        "putItm": {
            "loanId": put_loan_id,
            "guid": put_guid,
            "settlementAmount": put_settlement,
            "excess": put_excess,
            "treasuryCut": expected_treasury_cut,
        },
        "callItm": {
            "loanId": call_loan_id,
            "guid": call_guid,
            "settlementAmount": call_settlement,
            "excess": call_excess,
        },
    }
    path = Path(tempfile.mkdtemp(prefix="settlement-report-outcomes-e2e-")) / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
