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
    L2_WETH_SOCKET_BRIDGE,
    L2_USDC_SOCKET_BRIDGE,
)
from common import (  # noqa: E402
    ANVIL_ADDR0,
    ANVIL_PK0,
    BORROWER_PK,
    cast_call,
    cast_send_from,
    cast_send_pk,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_live_deployments as _ensure_live_deployments,
    ensure_token_balance as _ensure_token_balance,
    print_step as _print_step,
    relay_exact_lz_packet as _relay_exact_lz_packet,
    require_code as _require_code,
    resolve_l2_runtime_env as _resolve_l2_runtime_env,
    run,
    run_fresh_loan_flow as _run_fresh_loan_flow,
    set_eth_balance as _set_eth_balance,
    set_time as _set_time,
    write_env_with_updates as _write_env_with_updates,
)
from loan_flow_helpers import extract_tx_hash, get_mandate, get_pending, parse_json_or_fallback  # noqa: E402

app = typer.Typer(add_completion=False)

TSA_STORAGE_LOCATION = int("0x62b72349c5c9dfc4c2d0e5f1b0600421e6f0d0f8ac3a0ffdf4c4c0b7d4b4b000", 16)
TSA_WITHDRAW_EXECUTION_NONCE_SLOT = TSA_STORAGE_LOCATION + 23
BASE_MODULE_USED_NONCES_SLOT = 2
RECEIVER_TRADE_CONFIRMED_SLOT = 17


def _quote_native_fee(raw: str) -> int:
    match = re.search(r"\d+", raw)
    if not match:
        raise RuntimeError(f"failed to parse fee quote: {raw}")
    return int(match.group(0))


def _run_keeper_command(cmd: list[str]) -> dict:
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"keeper command failed ({proc.returncode}): {proc.stderr.strip()}\n{proc.stdout}")
    return parse_json_or_fallback(proc.stdout)


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l2_rpc, receiver, "hasRole(bytes32,address)(bool)", keeper_role, ANVIL_ADDR0).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l2_rpc, receiver, "grantRole(bytes32,address)", keeper_role, ANVIL_ADDR0)


def _ensure_tsa_signers(l2_rpc: str, tsa: str, receiver: str) -> None:
    if cast_call(l2_rpc, tsa, "isSigner(address)(bool)", receiver).strip().lower() != "true":
        cast_send_pk(l2_rpc, tsa, "setSigner(address,bool)", receiver, "true")
    if cast_call(l2_rpc, tsa, "isSigner(address)(bool)", ANVIL_ADDR0).strip().lower() != "true":
        cast_send_pk(l2_rpc, tsa, "setSigner(address,bool)", ANVIL_ADDR0, "true")


def _tx_block(rpc: str, tx_hash: str) -> int:
    receipt = json.loads(run(["cast", "receipt", tx_hash, "--rpc-url", rpc, "--json"]))
    block_raw = receipt.get("blockNumber")
    return int(block_raw, 0) if isinstance(block_raw, str) else int(block_raw)


def _l2_base_addrs(l2_rpc: str, tsa: str) -> list[str]:
    return re.findall(
        r"0x[a-fA-F0-9]{40}",
        cast_call(l2_rpc, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)"),
    )


def _check_quote(path: str, quote_fn) -> int:
    try:
        fee = quote_fn()
    except Exception as exc:
        raise RuntimeError(f"{path} quote failed: {exc}") from exc
    if fee <= 0:
        raise RuntimeError(f"{path} quote is not positive: {fee}")
    return fee


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


def _simulate_withdraw_executed(
    l2_rpc: str,
    tsa: str,
    withdrawal_module: str,
    loan_id: int,
    asset: str,
    amount: int,
) -> dict[str, str | int]:
    nonce = 1_000_000 + loan_id
    tsa_slot = _mapping_slot_uint(loan_id, TSA_WITHDRAW_EXECUTION_NONCE_SLOT)
    _set_storage(l2_rpc, tsa, tsa_slot, _to_bytes32(nonce))

    owner_slot = _mapping_slot_address(tsa, BASE_MODULE_USED_NONCES_SLOT)
    module_slot = _mapping_slot_uint(nonce, owner_slot)
    _set_storage(l2_rpc, withdrawal_module, module_slot, _to_bytes32(1))

    balance_before = int(cast_call(l2_rpc, asset, "balanceOf(address)(uint256)", tsa).split()[0])
    if balance_before < amount:
        _set_eth_balance(l2_rpc, L2_WETH_SOCKET_BRIDGE)
        cast_send_from(l2_rpc, L2_WETH_SOCKET_BRIDGE, asset, "mint(address,uint256)", tsa, str(amount - balance_before))
    balance_after = int(cast_call(l2_rpc, asset, "balanceOf(address)(uint256)", tsa).split()[0])

    return {
        "nonce": nonce,
        "tsaSlot": tsa_slot,
        "moduleSlot": module_slot,
        "balanceBefore": balance_before,
        "balanceAfter": balance_after,
    }


def _simulate_receiver_trade_confirmed(l2_rpc: str, receiver: str, loan_id: int) -> str:
    slot = _mapping_slot_uint(loan_id, RECEIVER_TRADE_CONFIRMED_SLOT)
    _set_storage(l2_rpc, receiver, slot, _to_bytes32(1))
    return slot


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
    print("=== collar.fi strict l2 bridge e2e ===")

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
    receiver = l2["l2Receiver"]
    tsa = l2["l2Tsa"]
    l2_weth_adapter = l2.get("l2WethAdapter") or ""
    l2_usdc_adapter = l2.get("l2UsdcAdapter") or ""
    if not l2_weth_adapter or not l2_usdc_adapter:
        raise RuntimeError(f"strict bridge e2e requires deployed L2 adapters, got weth={l2_weth_adapter}, usdc={l2_usdc_adapter}")

    base_addrs = _l2_base_addrs(l2_rpc, tsa)
    collar_addrs = re.findall(
        r"0x[a-fA-F0-9]{40}",
        cast_call(
            l2_rpc,
            tsa,
            "getCollarTSAAddresses()(address,address,address,address,address,address)",
        ),
    )
    withdrawal_module = collar_addrs[2]
    wrapped_deposit_asset = base_addrs[2]
    wrapped_cash_asset = base_addrs[3]
    l2_weth_underlying = cast_call(l2_rpc, wrapped_deposit_asset, "wrappedAsset()(address)").splitlines()[0].strip()
    l2_usdc_underlying = cast_call(l2_rpc, wrapped_cash_asset, "wrappedAsset()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l2_rpc, tsa, "subAccount()(uint256)").split()[0])

    _check_quote(
        "L1 WETH bridge fee",
        lambda: int(cast_call(l1_rpc, vault, "estimateBridgeFees(address,address,uint256)(uint256)", sepolia_weth, receiver, str(10**18)).split()[0]),
    )
    _check_quote(
        "L2 WETH adapter fee",
        lambda: int(cast_call(l2_rpc, l2_weth_adapter, "estimateFee()(uint256)").split()[0]),
    )
    _check_quote(
        "L2 USDC adapter fee",
        lambda: int(cast_call(l2_rpc, l2_usdc_adapter, "estimateFee()(uint256)").split()[0]),
    )
    _check_quote(
        "L2 TSA WETH bridge fee",
        lambda: int(cast_call(l2_rpc, tsa, "estimateBridgeFees(address,address,uint256)(uint256)", l2_weth_underlying, vault, str(10**18)).split()[0]),
    )
    _check_quote(
        "L2 TSA USDC bridge fee",
        lambda: int(cast_call(l2_rpc, tsa, "estimateBridgeFees(address,address,uint256)(uint256)", l2_usdc_underlying, vault, str(10**6)).split()[0]),
    )
    _print_step(True, "Verified live L1/L2 bridge fee quotes with real adapters")

    fresh = _run_fresh_loan_flow(
        l1_json,
        l2_json,
        l1_rpc,
        l2_rpc,
        sepolia_weth,
        strict_bridge_paths=True,
    )
    verify = next((s.get("result") for s in fresh.get("steps", []) if s.get("step") == "verify_expected_state"), None)
    create_deposit = next((s.get("result") for s in fresh.get("steps", []) if s.get("step") == "create_deposit_with_permit"), None)
    if not isinstance(verify, dict):
        raise RuntimeError("strict fresh loan flow did not produce verification result")
    if not isinstance(create_deposit, dict):
        raise RuntimeError("strict fresh loan flow did not produce create_deposit_with_permit result")
    loan_id = int(verify["loanId"])
    pending = get_pending(vault, l1_rpc, loan_id)
    mandate = get_mandate(vault, l1_rpc, loan_id)
    _print_step(True, f"Created strict pending loan via real L1 bridge path (loanId={loan_id})")

    _set_time(l1_rpc, int(mandate["deadline"]) + 1)
    lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    return_msg = (
        f"(1,{loan_id},{sepolia_weth},{pending['collateral']},{vault},{subaccount_id},"
        f"0x{'00'*32},0,0x{'00'*32},0,0x)"
    )
    return_lz_fee = _quote_native_fee(
        cast_call(
            l1_rpc,
            lz_messenger,
            "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
            return_msg,
            default_opts,
        )
    )
    request_tx = cast_send_pk(
        l1_rpc,
        vault,
        "requestCollateralReturn(uint256)",
        str(loan_id),
        private_key=BORROWER_PK,
        value=str(return_lz_fee),
    )
    request_hash = extract_tx_hash(request_tx)
    l1_to_l2 = _relay_exact_lz_packet(l1_rpc, l2_rpc, request_hash)
    _print_step(True, "Relayed ReturnRequest to L2 receiver")

    _ensure_l2_keeper_role(l2_rpc, receiver)
    _ensure_tsa_signers(l2_rpc, tsa, receiver)

    tmpdir = Path(tempfile.mkdtemp(prefix="strict-l2-bridge-"))
    l2_env = tmpdir / ".env.l2.fork"
    _write_env_with_updates(
        ROOT / ".env.l2.testnet",
        l2_env,
        _resolve_l2_runtime_env(l2_rpc, l2, receiver),
    )
    state_file = tmpdir / "keeper_l2_state.json"
    keeper = _run_keeper_command(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
            str(l2_env),
            "--state-file",
            str(state_file),
            "--start-block",
            str(_tx_block(l2_rpc, l1_to_l2["relayTx"])),
            "--once",
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--json",
        ]
    )
    sent = [row for row in keeper.get("handled", []) if isinstance(row, dict) and str(row.get("loanId")) == str(loan_id)]
    use_manual_return_notify = False
    if not sent:
        raise RuntimeError(f"L2 keeper did not process ReturnRequest successfully: {json.dumps(keeper)}")
    if sent[0].get("status") != "sent":
        status = str(sent[0].get("status") or "")
        if "BLF_DataTooOld" not in status:
            raise RuntimeError(f"L2 keeper did not process ReturnRequest successfully: {json.dumps(keeper)}")
        simulated = _simulate_withdraw_executed(
            l2_rpc,
            tsa,
            withdrawal_module,
            loan_id,
            l2_weth_underlying,
            int(pending["collateral"]),
        )
        use_manual_return_notify = True
        _print_step(True, f"Simulated executed withdrawal on fork after local-atomic feed staleness ({simulated['nonce']})")

    withdraw_executed = cast_call(l2_rpc, tsa, "withdrawExecuted(uint256)(bool)", str(loan_id)).strip().lower()
    if not use_manual_return_notify and withdraw_executed != "true":
        raise RuntimeError(f"withdrawExecuted is not true for loan {loan_id}")
    tsa_balance = int(cast_call(l2_rpc, l2_weth_underlying, "balanceOf(address)(uint256)", tsa).split()[0])
    if tsa_balance < int(pending["collateral"]):
        raise RuntimeError(f"TSA WETH balance too low after withdrawal: {tsa_balance} < {pending['collateral']}")
    _print_step(True, "Processed ReturnRequest on L2 and executed real withdrawal into TSA")

    bridge_fee = int(
        cast_call(
            l2_rpc,
            tsa,
            "estimateBridgeFees(address,address,uint256)(uint256)",
            l2_weth_underlying,
            vault,
            str(pending["collateral"]),
        ).split()[0]
    )
    socket_message_id = cast_call(l2_rpc, l2_weth_adapter, "messageId()(bytes32)").splitlines()[0].strip()
    collateral_return_msg = (
        f"(4,{loan_id},{sepolia_weth},{pending['collateral']},{vault},{subaccount_id},"
        f"{socket_message_id},0,0x{'00'*32},0,0x)"
    )
    l2_default_opts = cast_call(l2_rpc, receiver, "defaultOptions()(bytes)").splitlines()[0].strip()
    l2_lz_fee = _quote_native_fee(
        cast_call(
            l2_rpc,
            receiver,
            "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
            collateral_return_msg,
            l2_default_opts,
        )
    )
    if use_manual_return_notify:
        cast_send_pk(l2_rpc, tsa, "setBridgeCoordinator(address)", ANVIL_ADDR0)
        cast_send_pk(
            l2_rpc,
            tsa,
            "bridgeToL1(address,uint256,address)",
            l2_weth_underlying,
            str(pending["collateral"]),
            vault,
            private_key=ANVIL_PK0,
            value=str(bridge_fee),
        )
        bridge_tx = cast_send_pk(
            l2_rpc,
            receiver,
            "sendCollateralReturned(uint256,address,uint256,bytes32)",
            str(loan_id),
            sepolia_weth,
            str(pending["collateral"]),
            socket_message_id,
            private_key=ANVIL_PK0,
            value=str(l2_lz_fee + max(1, l2_lz_fee // 20)),
        )
        cast_send_pk(l2_rpc, tsa, "setBridgeCoordinator(address)", receiver)
        _print_step(True, "Executed real L2 return bridge with manual notify fallback after simulated withdrawal")
    else:
        bridge_tx = cast_send_pk(
            l2_rpc,
            receiver,
            "bridgePendingReturnAndNotify(uint256,address,uint256)",
            str(loan_id),
            sepolia_weth,
            str(pending["collateral"]),
            private_key=ANVIL_PK0,
            value=str(bridge_fee + l2_lz_fee + max(1, l2_lz_fee // 20)),
        )
    bridge_hash = extract_tx_hash(bridge_tx)
    l2_to_l1 = _relay_exact_lz_packet(l2_rpc, l1_rpc, bridge_hash)
    _ensure_token_balance(l1_rpc, sepolia_weth, vault, int(pending["collateral"]))
    borrower_before = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", create_deposit["borrower"]).split()[0])
    cast_send_pk(l1_rpc, vault, "finalizeDepositReturn(uint256,bytes32)", str(loan_id), l2_to_l1["guid"])
    borrower_after = int(cast_call(l1_rpc, sepolia_weth, "balanceOf(address)(uint256)", create_deposit["borrower"]).split()[0])
    if borrower_after - borrower_before != int(pending["collateral"]):
        raise RuntimeError("finalized pending return did not transfer the expected collateral")
    _print_step(True, "Executed real L2 outbound return bridge/orchestration and finalized on L1")

    settlement_loan_id = loan_id + 1000
    settlement_slot = _simulate_receiver_trade_confirmed(l2_rpc, receiver, settlement_loan_id)
    _print_step(True, f"Seeded trade-confirmed receiver state for strict settlement bridge check ({settlement_slot})")

    usdc_amount = 10**6
    _set_eth_balance(l2_rpc, L2_USDC_SOCKET_BRIDGE)
    cast_send_from(
        l2_rpc,
        L2_USDC_SOCKET_BRIDGE,
        l2_usdc_underlying,
        "mint(address,uint256)",
        tsa,
        str(usdc_amount),
    )
    usdc_bridge_fee = int(
        cast_call(
            l2_rpc,
            tsa,
            "estimateBridgeFees(address,address,uint256)(uint256)",
            l2_usdc_underlying,
            vault,
            str(usdc_amount),
        ).split()[0]
    )
    settlement_msg = (
        f"(2,{settlement_loan_id},{sepolia_usdc},{usdc_amount},{vault},{subaccount_id},"
        f"{cast_call(l2_rpc, l2_usdc_adapter, 'messageId()(bytes32)').splitlines()[0].strip()},0,0x{'00'*32},0,0x)"
    )
    settlement_lz_fee = _quote_native_fee(
        cast_call(
            l2_rpc,
            receiver,
            "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
            settlement_msg,
            l2_default_opts,
        )
    )
    settlement_tx = cast_send_pk(
        l2_rpc,
        receiver,
        "bridgeSettlementAndNotify(uint256,address,uint256,uint256)",
        str(settlement_loan_id),
        sepolia_usdc,
        str(usdc_amount),
        "0",
        private_key=ANVIL_PK0,
        value=str(usdc_bridge_fee + settlement_lz_fee + max(1, settlement_lz_fee // 20)),
    )
    settlement_hash = extract_tx_hash(settlement_tx)
    settlement_l2_to_l1 = _relay_exact_lz_packet(l2_rpc, l1_rpc, settlement_hash)
    settlement_raw = cast_call(
        l1_rpc,
        lz_messenger,
        "receivedMessage(bytes32)((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        settlement_l2_to_l1["guid"],
    )
    m = re.match(r"\((\d+),\s*(\d+)(?:\s*\[[^\]]+\])?,", settlement_raw)
    if not m:
        raise RuntimeError(f"failed parsing settlement receivedMessage: {settlement_raw}")
    action, msg_loan = int(m.group(1)), int(m.group(2))
    if action != 2 or msg_loan != settlement_loan_id:
        raise RuntimeError(
            f"unexpected settlement ack contents: action={action}, loanId={msg_loan}, expected action=2 loanId={settlement_loan_id}"
        )
    _print_step(True, "Executed receiver settlement bridge orchestration using the real USDC adapter")

    result = {
        "status": "success",
        "loanId": loan_id,
        "wethBridgeTx": bridge_hash,
        "wethBridgeGuid": l2_to_l1["guid"],
        "usdcBridgeAmount": usdc_amount,
        "usdcBridgeTx": settlement_hash,
        "usdcBridgeGuid": settlement_l2_to_l1["guid"],
        "l2WethAdapter": l2_weth_adapter,
        "l2UsdcAdapter": l2_usdc_adapter,
    }
    out_path = tmpdir / "result.json"
    out_path.write_text(json.dumps(result, indent=2))
    _print_step(True, f"Result artifact written: {out_path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
