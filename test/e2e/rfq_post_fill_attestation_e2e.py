#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent

import sys

sys.path.insert(0, str(THIS_DIR))
from defaults import (  # noqa: E402
    L1_ANVIL_PORT,
    L1_ARTIFACT_JSON,
    L1_COLLATERAL_ASSET,
    L1_DEBT_ASSET,
    L1_WETH_SOCKET_CONNECTOR,
    L1_WETH_SOCKET_VAULT,
    L2_ANVIL_PORT,
    L2_ARTIFACT_JSON,
)
from common import (  # noqa: E402
    ANVIL_ADDR0,
    ANVIL_PK0,
    cast_call,
    cast_send_pk,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_live_deployments as _ensure_live_deployments,
    print_step as _print_step,
    relay_exact_lz_packet as _relay_exact_lz_packet,
    require_code as _require_code,
    resolve_l2_runtime_env as _resolve_l2_runtime_env,
    run,
    write_env_with_updates as _write_env_with_updates,
)
from loan_flow_helpers import (  # noqa: E402
    accept_mandate_for_pending,
    get_loan,
    get_mandate,
    get_pending,
    inject_deposit_confirmed,
    parse_json_or_fallback,
    run_fresh_atomic_pending_loan,
)

app = typer.Typer(add_completion=False)

BASE_MODULE_USED_NONCES_SLOT = 2
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + ("00" * 32)


def _run_keeper_command(cmd: list[str]) -> dict:
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"keeper command failed ({proc.returncode}): {proc.stderr.strip()}\n{proc.stdout}")
    return parse_json_or_fallback(proc.stdout)


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


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l2_rpc, receiver, "hasRole(bytes32,address)(bool)", keeper_role, ANVIL_ADDR0).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l2_rpc, receiver, "grantRole(bytes32,address)", keeper_role, ANVIL_ADDR0)


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
    print("=== collar.fi rfq post-fill attestation e2e ===")

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
    receiver = l2["l2Receiver"]
    tsa = l2["l2Tsa"]
    _ensure_l2_keeper_role(l2_rpc, receiver)

    fresh = run_fresh_atomic_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    loan_id = int(fresh["loanId"])
    deposit_guid = fresh["depositGuid"]
    pending = get_pending(vault, l1_rpc, loan_id)
    mandate = get_mandate(vault, l1_rpc, loan_id)
    if mandate["borrower"] == ZERO_ADDRESS:
        mandate_ctx = accept_mandate_for_pending(l1_rpc, vault, sepolia_weth, loan_id, pending)
        call_strike = int(mandate_ctx["callStrike"])
    else:
        call_strike = int(mandate["minCallStrike"])
    _print_step(True, f"Loaded pending loan + mandate (loanId={loan_id})")

    taker_nonce = 1_000_000 + loan_id
    _, module_slot = _simulate_rfq_nonce_used(l2_rpc, tsa, taker_nonce)

    runtime_updates = _resolve_l2_runtime_env(l2_rpc, l2, receiver)
    tmpdir = Path(tempfile.mkdtemp(prefix="rfq-post-fill-attestation-e2e-"))
    l2_env = _write_env_with_updates(ROOT / ".env.l2.testnet", tmpdir / ".env.l2.fork", runtime_updates)
    keeper_out = _run_keeper_command(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l2_confirm_rfq_trade.py"),
            str(l2_env),
            "--receiver",
            receiver,
            "--loan-id",
            str(loan_id),
            "--taker-nonce",
            str(taker_nonce),
            "--call-strike",
            str(call_strike),
            "--put-strike",
            str(pending["putStrike"]),
            "--expiry",
            str(pending["maturity"]),
            "--asset",
            ZERO_ADDRESS,
            "--amount",
            "0",
            "--socket-message-id",
            ZERO_BYTES32,
            "--quote-hash",
            ZERO_BYTES32,
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--json",
        ]
    )
    _print_step(True, f"Confirmed RFQ trade on L2 via keeper command (moduleSlot={module_slot})")

    trade_packet = _relay_exact_lz_packet(l2_rpc, l1_rpc, keeper_out["sendTradeConfirmedTx"])
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    inject_deposit_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        sepolia_weth,
        int(pending["collateral"]),
        deposit_guid,
    )
    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_packet["guid"])
    loan = get_loan(vault, l1_rpc, loan_id)
    if loan["borrower"].lower() == ZERO_ADDRESS:
        raise RuntimeError("finalizeLoan did not persist borrower")
    if loan["principal"] != int(pending["borrowAmount"]):
        raise RuntimeError(f"principal mismatch after finalizeLoan: expected {pending['borrowAmount']}, got {loan['principal']}")
    _print_step(True, "Finalized loan from relayed L2 TradeConfirmed packet")

    out = {
        "status": "success",
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_packet["guid"],
        "recordTradeExecutedTx": keeper_out["recordTradeExecutedTx"],
        "sendTradeConfirmedTx": keeper_out["sendTradeConfirmedTx"],
    }
    path = tmpdir / "result.json"
    path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
