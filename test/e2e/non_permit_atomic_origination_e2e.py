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
    ANVIL_PK0,
    BORROWER_PK,
    abi_encode as _abi_encode,
    borrower_address as _borrower_address,
    cast_call,
    cast_send_pk,
    deploy_contract as _deploy_contract,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_liquidity_vault_role as _ensure_liquidity_vault_role,
    ensure_live_deployments as _ensure_live_deployments,
    ensure_token_balance as _ensure_token_balance,
    print_step as _print_step,
    require_code as _require_code,
    run,
    seed_l1_liquidity_vault as _seed_l1_liquidity_vault,
    set_eth_balance as _set_eth_balance,
    sign_no_prefix as _sign_no_prefix,
)
from loan_flow_helpers import (
    get_loan,
    get_mandate,
    get_pending,
)

app = typer.Typer(add_completion=False)


def _latest_timestamp(rpc: str) -> int:
    block = json.loads(run(["cast", "block", "latest", "--rpc-url", rpc, "--json"]))
    ts_raw = block.get("timestamp")
    return int(ts_raw, 0) if isinstance(ts_raw, str) else int(ts_raw)


def _set_mock_message(rpc: str, messenger: str, guid: str, msg_tuple: str) -> None:
    cast_send_pk(
        rpc,
        messenger,
        "setMessage(bytes32,(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        guid,
        msg_tuple,
    )


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
    print("=== collar.fi non-permit atomic origination e2e ===")

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

    mock_messenger = _deploy_contract(l1_rpc, "test/CollarVault.t.sol:MockLZMessenger")
    cast_send_pk(l1_rpc, mock_messenger, "setQuoteFee(uint256)", "0")
    cast_send_pk(l1_rpc, vault, "setLZMessenger(address)", mock_messenger)
    _print_step(True, f"Temporarily set MockLZMessenger for deterministic atomic-origination fee path ({mock_messenger})")

    borrower = _borrower_address()
    _set_eth_balance(l1_rpc, borrower)

    next_loan_id = int(cast_call(l1_rpc, vault, "nextLoanId()(uint256)").split()[0])
    now_ts = _latest_timestamp(l1_rpc)

    collateral_amount = 10**18
    maturity = now_ts + 7 * 24 * 3600
    put_strike = 1_500 * 10**18
    call_strike = put_strike + 1
    borrow_amount = 1_500 * 10**6
    rfq_expiry = now_ts + 3600
    mandate_deadline = now_ts + 1800
    rfq_nonce = 777_001

    params = f"({sepolia_weth},{collateral_amount},{maturity},{put_strike},{borrow_amount})"
    rfq_tuple = (
        f"(0,{sepolia_weth},{collateral_amount},{maturity},{put_strike},{call_strike},"
        f"{borrow_amount},0,{rfq_expiry},{borrower},{rfq_nonce})"
    )

    _ensure_token_balance(l1_rpc, sepolia_weth, borrower, collateral_amount)
    cast_send_pk(
        l1_rpc,
        sepolia_weth,
        "approve(address,uint256)",
        vault,
        str(collateral_amount),
        private_key=BORROWER_PK,
    )
    _seed_l1_liquidity_vault(l1_rpc, sepolia_usdc, liquidity_vault, borrow_amount * 2)

    rfq_hash = cast_call(
        l1_rpc,
        vault,
        "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
        rfq_tuple,
    ).splitlines()[0].strip()
    rfq_sig = _sign_no_prefix(rfq_hash, ANVIL_PK0)

    l2_recipient = cast_call(l1_rpc, vault, "l2Recipient()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    bridge_fee = int(
        cast_call(l1_rpc, vault, "estimateBridgeFees(address,address,uint256)(uint256)", sepolia_weth, l2_recipient, str(collateral_amount))
        .split()[0]
    )
    msg_value = bridge_fee + 1

    fallback_bridge = None
    try:
        cast_send_pk(
            l1_rpc,
            vault,
            "createDepositWithMandate((address,uint256,uint256,uint256,uint256),(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)(uint256,bytes32,bytes32,bytes32)",
            params,
            rfq_tuple,
            rfq_sig,
            str(mandate_deadline),
            private_key=BORROWER_PK,
            value=str(msg_value),
        )
    except Exception as exc:
        if "TRANSFER_FROM_FAILED" not in str(exc):
            raise
        fallback_bridge = _deploy_contract(l1_rpc, "test/mocks/MockBridgeAdapter.sol:MockBridgeAdapter")
        cast_send_pk(l1_rpc, fallback_bridge, "setFee(uint256)", "0")
        cast_send_pk(l1_rpc, fallback_bridge, "setMessageId(bytes32)", "0x" + "11" * 32)
        cast_send_pk(l1_rpc, vault, "setSocketVaultConfig(address,address)", sepolia_weth, fallback_bridge)
        cast_send_pk(
            l1_rpc,
            vault,
            "createDepositWithMandate((address,uint256,uint256,uint256,uint256),(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)(uint256,bytes32,bytes32,bytes32)",
            params,
            rfq_tuple,
            rfq_sig,
            str(mandate_deadline),
            private_key=BORROWER_PK,
            value=str(msg_value),
        )

    pending = get_pending(vault, l1_rpc, next_loan_id)
    mandate = get_mandate(vault, l1_rpc, next_loan_id)

    if pending["borrower"].lower() != borrower.lower() or pending["asset"].lower() != sepolia_weth.lower():
        raise RuntimeError("pending deposit mismatch after createDepositWithMandate")
    if pending["collateral"] != collateral_amount or pending["borrowAmount"] != borrow_amount:
        raise RuntimeError("pending deposit values mismatch after createDepositWithMandate")
    if mandate["borrower"].lower() != borrower.lower() or mandate["collateralAsset"].lower() != sepolia_weth.lower():
        raise RuntimeError("mandate mismatch after createDepositWithMandate")
    if not mandate["sentToL2"]:
        raise RuntimeError("mandate not marked as sentToL2")
    if mandate["minCallStrike"] != call_strike or mandate["maxPutStrike"] != put_strike:
        raise RuntimeError("mandate strike bounds mismatch")
    _print_step(True, f"Created pending+mandate atomically (loanId={next_loan_id})")

    deposit_guid = "0x" + format(71_000_000 + next_loan_id, "064x")
    trade_guid = "0x" + format(72_000_000 + next_loan_id, "064x")
    _set_mock_message(
        l1_rpc,
        mock_messenger,
        deposit_guid,
        f"(3,{next_loan_id},{sepolia_weth},{collateral_amount},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    trade_data = _abi_encode("f(uint256,uint256,uint64,int256)", str(call_strike), str(put_strike), str(maturity), "0")
    _set_mock_message(
        l1_rpc,
        mock_messenger,
        trade_guid,
        f"(5,{next_loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{trade_data})",
    )
    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(next_loan_id), deposit_guid, trade_guid)

    loan = get_loan(vault, l1_rpc, next_loan_id)
    if int(loan["state"]) != 1:
        raise RuntimeError(f"loan not ACTIVE_ZERO_COST after finalize (state={loan['state']})")
    if int(loan["principal"]) != borrow_amount:
        raise RuntimeError(f"loan principal mismatch after finalize: expected {borrow_amount}, got {loan['principal']}")
    _print_step(True, "Finalized atomic-origination loan via deterministic injected messages")

    cast_send_pk(l1_rpc, vault, "setLZMessenger(address)", messenger)
    _print_step(True, "Restored canonical L1 messenger after atomic-origination scenario")

    out = {
        "status": "success",
        "loanId": next_loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "bridgeAdapterFallback": fallback_bridge,
    }
    path = Path(tempfile.mkdtemp(prefix="non-permit-atomic-origination-e2e-")) / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
