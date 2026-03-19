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
    relay_exact_lz_packet as _relay_exact_lz_packet,
    print_step as _print_step,
    require_code as _require_code,
    run,
    set_time as _set_time,
    extract_tx_hash as _extract_tx_hash,
)
from loan_flow_helpers import get_mandate, get_pending, run_fresh_atomic_pending_loan

app = typer.Typer(add_completion=False)

BASE_MODULE_USED_NONCES_SLOT = 2


def _to_bytes32(value: int) -> str:
    return f"0x{value:064x}"


def _keccak_hex(data_hex: str) -> str:
    return run(["cast", "keccak", data_hex]).splitlines()[0].strip()


def _mapping_slot_uint(key: int, slot: int) -> str:
    encoded = run(["cast", "abi-encode", "f(uint256,uint256)", str(key), str(slot)]).strip()
    return _keccak_hex(encoded)


def _mapping_slot_address(address_key: str, slot: int) -> int:
    encoded = run(["cast", "abi-encode", "f(address,uint256)", address_key, str(slot)]).strip()
    return int(_keccak_hex(encoded), 16)


def _set_storage(rpc: str, contract: str, slot: str, value: str) -> None:
    run(["cast", "rpc", "anvil_setStorageAt", contract, slot, value, "--rpc-url", rpc])


def _simulate_rfq_nonce_used(l2_rpc: str, tsa: str, taker_nonce: int) -> tuple[str, str]:
    raw = cast_call(
        l2_rpc,
        tsa,
        "getCollarTSAAddresses()(address,address,address,address,address,address)",
    )
    addrs = re.findall(r"0x[a-fA-F0-9]{40}", raw)
    if len(addrs) < 5:
        raise RuntimeError(f"failed to parse TSA collar addrs: {raw}")
    rfq_module = addrs[4]
    owner_slot = _mapping_slot_address(tsa, BASE_MODULE_USED_NONCES_SLOT)
    module_slot = _mapping_slot_uint(taker_nonce, owner_slot)
    _set_storage(l2_rpc, rfq_module, module_slot, _to_bytes32(1))
    return rfq_module, module_slot


def _expect_revert(fn, err_hint: str) -> None:
    try:
        fn()
    except Exception as exc:
        _print_step(True, f"Observed expected revert: {err_hint} ({str(exc).splitlines()[0][:120]})")
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
    print("=== collar.fi return-before-trade protocol-state e2e ===")

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
    messenger = l1["l1Messenger"]
    _ensure_liquidity_vault_role(l1_rpc, vault)
    _print_step(True, f"Loaded deployments: vault={vault}")

    fresh = run_fresh_atomic_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    loan_id = int(fresh["loanId"])
    deposit_guid = fresh["depositGuid"]
    pending = get_pending(vault, l1_rpc, loan_id)
    borrower = _borrower_address()

    if pending["borrower"].lower() != borrower.lower() or pending["asset"].lower() != sepolia_weth.lower():
        raise RuntimeError("pending deposit does not match expected borrower/asset")

    mandate = get_mandate(vault, l1_rpc, loan_id)
    if mandate["borrower"].lower() != borrower.lower():
        raise RuntimeError("atomic origination did not persist the expected mandate")
    _print_step(True, f"Loaded atomic mandate for pending loan (loanId={loan_id})")

    _expect_revert(
        lambda: cast_send_pk(
            l1_rpc,
            vault,
            "requestCollateralReturn(uint256)",
            str(loan_id),
            private_key=BORROWER_PK,
        ),
        "requestCollateralReturn before mandate deadline",
    )

    _set_time(l1_rpc, int(mandate["deadline"]) + 1)

    lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    return_msg = (
        f"(1,{loan_id},{sepolia_weth},{pending['collateral']},{vault},{subaccount_id},"
        f"0x{'00'*32},0,0x{'00'*32},0,0x)"
    )
    return_lz_fee = int(
        re.search(
            r"\d+",
            cast_call(
                l1_rpc,
                lz_messenger,
                "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
                return_msg,
                default_opts,
            ),
        ).group(0)
    )
    cast_send_pk(
        l1_rpc,
        vault,
        "requestCollateralReturn(uint256)",
        str(loan_id),
        private_key=BORROWER_PK,
        value=str(return_lz_fee),
    )
    _print_step(True, "Requested collateral return after mandate deadline")

    collateral_return_guid = "0x" + format(30_000_000 + loan_id, "064x")
    collateral_return_msg = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(4,{loan_id},{sepolia_weth},{pending['collateral']},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    _inject_lz_message(l1_rpc, messenger, collateral_return_guid, collateral_return_msg)

    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(pending["collateral"]))
    borrower_weth_before = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])

    cast_send_pk(l1_rpc, vault, "finalizeDepositReturn(uint256,bytes32)", str(loan_id), collateral_return_guid)
    borrower_weth_after = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", borrower).split()[0])
    if borrower_weth_after - borrower_weth_before != int(pending["collateral"]):
        raise RuntimeError(
            "borrower collateral return mismatch: "
            f"expected {pending['collateral']}, got {borrower_weth_after - borrower_weth_before}"
        )

    pending_after = cast_call(
        l1_rpc,
        vault,
        "pendingDeposits(uint256)((address,address,uint256,uint256,uint256,uint256))",
        str(loan_id),
    )
    if "0x0000000000000000000000000000000000000000" not in pending_after:
        raise RuntimeError(f"pending deposit not cleared after finalizeDepositReturn: {pending_after}")
    _print_step(True, "Finalized returned collateral and cleared pending deposit")

    random_trade_guid = "0x" + format(40_000_000 + loan_id, "064x")
    _expect_revert(
        lambda: cast_send_pk(
            l1_rpc,
            vault,
            "finalizeLoan(uint256,bytes32,bytes32)",
            str(loan_id),
            deposit_guid,
            random_trade_guid,
        ),
        "finalizeLoan after collateral return",
    )

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "mandateDeadline": mandate["deadline"],
        "collateralReturnGuid": collateral_return_guid,
    }
    path = Path(tempfile.mkdtemp(prefix="return-before-trade-e2e-")) / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    fresh_blocked = run_fresh_atomic_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    blocked_loan_id = int(fresh_blocked["loanId"])
    blocked_pending = get_pending(vault, l1_rpc, blocked_loan_id)
    blocked_mandate = get_mandate(vault, l1_rpc, blocked_loan_id)

    receiver = l2["l2Receiver"]
    tsa = l2["l2Tsa"]
    taker_nonce = 1_000_000 + blocked_loan_id
    _, rfq_slot = _simulate_rfq_nonce_used(l2_rpc, tsa, taker_nonce)
    cast_send_pk(l2_rpc, receiver, "recordTradeExecuted(uint256,uint256)", str(blocked_loan_id), str(taker_nonce))
    _print_step(True, f"Recorded RFQ execution before L1 trade confirmation (loanId={blocked_loan_id})")

    _set_time(l1_rpc, int(blocked_mandate["deadline"]) + 1)
    blocked_lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    blocked_subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    blocked_default_opts = cast_call(l1_rpc, blocked_lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    blocked_return_msg = (
        f"(1,{blocked_loan_id},{sepolia_weth},{blocked_pending['collateral']},{vault},{blocked_subaccount_id},"
        f"0x{'00'*32},0,0x{'00'*32},0,0x)"
    )
    blocked_return_lz_fee = int(
        re.search(
            r"\d+",
            cast_call(
                l1_rpc,
                blocked_lz_messenger,
                "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
                blocked_return_msg,
                blocked_default_opts,
            ),
        ).group(0)
    )
    blocked_return_tx = cast_send_pk(
        l1_rpc,
        vault,
        "requestCollateralReturn(uint256)",
        str(blocked_loan_id),
        private_key=BORROWER_PK,
        value=str(blocked_return_lz_fee),
    )
    blocked_request_hash = _extract_tx_hash(blocked_return_tx)
    relayed = _relay_exact_lz_packet(l1_rpc, l2_rpc, blocked_request_hash)
    _expect_revert(
        lambda: cast_send_pk(l2_rpc, receiver, "handleMessage(bytes32)", relayed["guid"]),
        "custom error",
    )
    _print_step(True, f"Blocked return request after recorded RFQ execution (slot={rfq_slot})")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
