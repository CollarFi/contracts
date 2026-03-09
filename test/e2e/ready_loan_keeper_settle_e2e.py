#!/usr/bin/env python3
from __future__ import annotations

import json
import re
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

app = typer.Typer(add_completion=False)

from common import (
    ANVIL_ADDR0,
    ANVIL_PK0,
    BORROWER_PK,
    abi_encode as _abi_encode,
    borrower_address as _borrower_address,
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
    run,
    run_fresh_loan_flow as _run_fresh_loan_flow,
    seed_l1_liquidity_vault as _seed_l1_liquidity_vault,
    set_time as _set_time,
    sign_no_prefix as _sign_no_prefix,
)
from loan_flow_helpers import get_mandate


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def _expect_revert(fn, err_hint: str) -> None:
    try:
        fn()
    except Exception as exc:
        msg = str(exc)
        if err_hint and err_hint not in msg and "custom error" not in msg.lower():
            raise RuntimeError(f"unexpected revert reason while expecting {err_hint}: {msg}")
        _print_step(True, f"Observed expected revert: {err_hint}")
        return
    raise RuntimeError(f"expected revert not observed: {err_hint}")


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
    print("=== collar.fi keeper ready-loan settle e2e ===")

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
        _print_step(True, "Detected stale deployment artifacts/runtime; ran deployment_e2e to refresh")

    vault = l1["l1Vault"]
    messenger = l1["l1Messenger"]
    l1_liquidity_vault = l1["l1LiquidityVault"]
    _print_step(True, f"Loaded deployments: L1 vault={vault} L2 receiver={l2.get('l2Receiver')}")

    _ensure_liquidity_vault_role(l1_rpc, vault)
    _ensure_keeper_role(l1_rpc, vault)

    _print_step(True, "Using ready-loan fallback defaults (grace=3 days, keeperPenalty=500bps)")

    flow = _run_fresh_loan_flow(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    if not flow.get("ok"):
        raise RuntimeError("fresh_loan_flow failed")
    verify = next((s.get("result") for s in flow.get("steps", []) if s.get("step") == "verify_expected_state"), None)
    if not isinstance(verify, dict):
        raise RuntimeError("fresh_loan_flow verify result missing")
    loan_id = int(verify["loanId"])
    deposit_guid = verify["l2ToL1Guid"]
    _print_step(True, f"Created pending loan via fresh flow (loanId={loan_id})")

    borrower = _borrower_address()
    pending_raw = cast_call(
        l1_rpc,
        vault,
        "pendingDeposits(uint256)((address,address,uint256,uint256,uint256,uint256))",
        str(loan_id),
    )
    m = re.search(
        r"\((0x[a-fA-F0-9]{40}),\s*(0x[a-fA-F0-9]{40}),\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?\)",
        pending_raw,
        flags=re.S,
    )
    if not m:
        raise RuntimeError(f"failed to parse pending: {pending_raw}")
    p_borrower, p_asset, p_collateral, p_maturity, p_put, p_borrow = m.groups()
    if p_borrower.lower() != borrower.lower() or p_asset.lower() != sepolia_weth.lower():
        raise RuntimeError("pending deposit does not match expected borrower/asset")
    liquidity_seed = max(int(p_borrow) * 2, 10_000 * 10**6)
    _seed_l1_liquidity_vault(l1_rpc, sepolia_usdc, l1_liquidity_vault, liquidity_seed)
    _print_step(True, f"Seeded L1 liquidity vault (amount={liquidity_seed})")

    block_latest = json.loads(run(["cast", "block", "latest", "--rpc-url", l1_rpc, "--json"]))
    ts_raw = block_latest.get("timestamp")
    now_ts = int(ts_raw, 0) if isinstance(ts_raw, str) else int(ts_raw)
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((int(p_borrow) * apr) // 10**18) * (int(p_maturity) - now_ts) // year
    mandate = get_mandate(vault, l1_rpc, loan_id)
    subaccount_id = cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0]
    if mandate["borrower"] == "0x0000000000000000000000000000000000000000":
        rfq_expiry = now_ts + 3600
        mandate_deadline = now_ts + 1800
        call_strike = int(p_put) + 1
        rfq_tuple = (
            f"({loan_id},{sepolia_weth},{p_collateral},{p_maturity},{p_put},{call_strike},"
            f"{p_borrow},0,{rfq_expiry},{borrower},0)"
        )
        rfq_hash = cast_call(
            l1_rpc,
            vault,
            "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
            rfq_tuple,
        ).splitlines()[0].strip()
        rfq_sig = _sign_no_prefix(rfq_hash, ANVIL_PK0)

        lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
        default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
        max_roll_ltv = int(cast_call(l1_rpc, vault, "maxRollLtv()(uint256)").split()[0])
        strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", sepolia_weth).split()[0])
        mandate_data = _abi_encode(
            "f(address,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64)",
            borrower,
            str(call_strike),
            p_put,
            "0",
            str(fixed_interest),
            str(max_roll_ltv),
            str(strike_scale),
            p_maturity,
            str(mandate_deadline),
        )
        quote_msg = (
            f"(6,{loan_id},{sepolia_weth},{p_borrow},{vault},{subaccount_id},"
            f"0x{'00'*32},0,0x{'00'*32},0,{mandate_data})"
        )
        lz_fee = int(
            re.search(
                r"\d+",
                cast_call(
                    l1_rpc,
                    lz_messenger,
                    "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
                    quote_msg,
                    default_opts,
                ),
            ).group(0)
        )

        cast_send_pk(
            l1_rpc,
            vault,
            "acceptMandate(uint256,(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)",
            str(loan_id),
            rfq_tuple,
            rfq_sig,
            str(mandate_deadline),
            private_key=BORROWER_PK,
            value=str(lz_fee),
        )
        _print_step(True, "Accepted mandate on L1")
    else:
        call_strike = int(mandate["minCallStrike"])
        _print_step(True, "Loaded atomic mandate on L1")

    trade_data = _abi_encode("f(uint256,uint256,uint64,int256)", str(call_strike), str(p_put), str(p_maturity), "0")
    trade_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(5,{loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{trade_data})",
    )
    trade_guid = "0x" + format(10_000_000 + loan_id, "064x")
    _inject_lz_message(l1_rpc, messenger, trade_guid, trade_msg)

    # Fresh-flow ACK can carry L2 asset; normalize to L1 collateral asset for finalize checks.
    deposit_confirm_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(3,{loan_id},{sepolia_weth},{p_collateral},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    _inject_lz_message(l1_rpc, messenger, deposit_guid, deposit_confirm_msg)

    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_guid)
    _print_step(True, "Finalized loan to ACTIVE_ZERO_COST")

    maturity = int(p_maturity)
    _set_time(l1_rpc, maturity + 1)
    collat_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(4,{loan_id},{sepolia_weth},{p_collateral},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    collat_guid = "0x" + format(20_000_000 + loan_id, "064x")
    _inject_lz_message(l1_rpc, messenger, collat_guid, collat_msg)

    cast_send_pk(l1_rpc, vault, "settleLoan(uint256,uint8,bytes32)", str(loan_id), "1", collat_guid)
    _print_step(True, "Settled neutral loan and moved to READY_FOR_VARIABLE")
    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(p_collateral))

    block_after_ready = json.loads(run(["cast", "block", "latest", "--rpc-url", l1_rpc, "--json"]))
    ready_ts_raw = block_after_ready.get("timestamp")
    ready_ts = int(ready_ts_raw, 0) if isinstance(ready_ts_raw, str) else int(ready_ts_raw)
    _set_time(l1_rpc, ready_ts + 3 * 24 * 3600 + 2)

    total_due = int(p_borrow) + fixed_interest
    _ensure_token_balance(l1_rpc, sepolia_usdc, borrower, total_due)
    cast_send_pk(l1_rpc, sepolia_usdc, "approve(address,uint256)", vault, str(total_due), private_key=BORROWER_PK)
    _expect_revert(
        lambda: cast_send_pk(
            l1_rpc,
            vault,
            "settleReadyLoanByRepay(uint256)(uint256,uint256,uint256)",
            str(loan_id),
            private_key=BORROWER_PK,
        ),
        "CV_Unauthorized",
    )

    penalty_bps = 500
    strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", sepolia_weth).split()[0])
    put_strike = int(p_put)
    collateral_amount = int(p_collateral)
    base_seize = _ceil_div(total_due * strike_scale, put_strike)
    keeper_seize = _ceil_div(base_seize * (10_000 + penalty_bps), 10_000)
    keeper_seize = min(keeper_seize, collateral_amount)
    borrower_remainder = collateral_amount - keeper_seize

    _ensure_token_balance(l1_rpc, sepolia_usdc, ANVIL_ADDR0, total_due)
    cast_send_pk(l1_rpc, sepolia_usdc, "approve(address,uint256)", vault, str(total_due), private_key=ANVIL_PK0)

    keeper_usdc_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", ANVIL_ADDR0).split()[0])
    keeper_weth_before = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", ANVIL_ADDR0).split()[0])
    borrower_weth_before = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])
    lv_usdc_before = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", l1_liquidity_vault).split()[0])

    cast_send_pk(
        l1_rpc,
        vault,
        "settleReadyLoanByRepay(uint256)(uint256,uint256,uint256)",
        str(loan_id),
        private_key=ANVIL_PK0,
    )

    keeper_usdc_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", ANVIL_ADDR0).split()[0])
    keeper_weth_after = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", ANVIL_ADDR0).split()[0])
    borrower_weth_after = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])
    lv_usdc_after = int(cast_call(l1_rpc, sepolia_usdc, "balanceOf(address)(uint256)", l1_liquidity_vault).split()[0])

    if keeper_usdc_before - keeper_usdc_after != total_due:
        raise RuntimeError(
            f"keeper USDC spend mismatch: expected {total_due}, got {keeper_usdc_before - keeper_usdc_after}"
        )
    if lv_usdc_after - lv_usdc_before != total_due:
        raise RuntimeError(
            f"liquidity vault USDC receive mismatch: expected {total_due}, got {lv_usdc_after - lv_usdc_before}"
        )
    if keeper_weth_after - keeper_weth_before != keeper_seize:
        raise RuntimeError(
            f"keeper collateral seize mismatch: expected {keeper_seize}, got {keeper_weth_after - keeper_weth_before}"
        )
    if borrower_weth_after - borrower_weth_before != borrower_remainder:
        raise RuntimeError(
            f"borrower collateral remainder mismatch: expected {borrower_remainder}, got {borrower_weth_after - borrower_weth_before}"
        )

    loan_raw = cast_call(
        l1_rpc,
        vault,
        "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)",
        str(loan_id),
    )
    loan_lines = [ln.strip() for ln in loan_raw.splitlines() if ln.strip()]
    loan_state = int(loan_lines[8].split()[0]) if len(loan_lines) > 8 else -1
    if loan_state != 4:
        raise RuntimeError(f"loan not CLOSED after keeper settle (state={loan_state}): {loan_raw}")

    _print_step(True, "Keeper settled READY_FOR_VARIABLE loan by repaying and seizing deterministic collateral")

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "collateralGuid": collat_guid,
        "totalDue": total_due,
        "keeperSeize": keeper_seize,
        "borrowerRemainder": borrower_remainder,
    }
    p = Path(tempfile.mkdtemp(prefix="ready-loan-keeper-settle-")) / "result.json"
    p.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {p}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
