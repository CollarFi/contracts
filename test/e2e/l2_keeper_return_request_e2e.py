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
    ANVIL_ADDR0,
    ANVIL_PK0,
    abi_encode as _abi_encode,
    cast_call,
    cast_send_from,
    cast_send_pk,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_live_deployments as _ensure_live_deployments,
    print_step as _print_step,
    require_code as _require_code,
    resolve_l2_runtime_env as _resolve_l2_runtime_env,
    run,
    set_eth_balance as _set_eth_balance,
    write_env_with_updates as _write_env_with_updates,
)
from loan_flow_helpers import extract_tx_hash, parse_json_or_fallback

app = typer.Typer(add_completion=False)


def _tx_block(rpc: str, tx_hash: str) -> int:
    receipt = json.loads(run(["cast", "receipt", tx_hash, "--rpc-url", rpc, "--json"]))
    block_raw = receipt.get("blockNumber")
    return int(block_raw, 0) if isinstance(block_raw, str) else int(block_raw)


def _run_keeper_command(cmd: list[str]) -> dict:
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"keeper command failed ({proc.returncode}): {proc.stderr.strip()}\n{proc.stdout}")
    return parse_json_or_fallback(proc.stdout)


def _tick_int(payload: dict, key: str) -> int | None:
    tick = payload.get("tick") if isinstance(payload, dict) else None
    if isinstance(tick, dict) and key in tick:
        try:
            return int(tick[key])
        except Exception:
            pass

    raw = payload.get("raw") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        if key == "advancedCursor":
            match_bool = re.search(r'"advancedCursor"\s*:\s*(true|false)', raw, flags=re.I)
            if match_bool:
                return 1 if match_bool.group(1).lower() == "true" else 0
        match = re.search(rf'"{key}"\s*:\s*(\d+)', raw)
        if match:
            return int(match.group(1))
    return None


def _keeper_has_status(payload: dict, loan_id: int, status: str) -> bool:
    handled = payload.get("handled") if isinstance(payload, dict) else None
    if isinstance(handled, list):
        for row in handled:
            if isinstance(row, dict) and str(row.get("loanId")) == str(loan_id) and row.get("status") == status:
                return True

    raw = payload.get("raw") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        has_status = f'"status": "{status}"' in raw or f'"status":"{status}"' in raw
        has_loan = f'"loanId": "{loan_id}"' in raw or f'"loanId":"{loan_id}"' in raw
        if has_status and has_loan:
            return True
    return False


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str) -> None:
    keeper_role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    has = cast_call(l2_rpc, receiver, "hasRole(bytes32,address)(bool)", keeper_role, ANVIL_ADDR0).strip().lower() == "true"
    if has:
        return
    cast_send_pk(l2_rpc, receiver, "grantRole(bytes32,address)", keeper_role, ANVIL_ADDR0)


def _ensure_tsa_signer(l2_rpc: str, tsa: str, receiver: str) -> None:
    if cast_call(l2_rpc, tsa, "isSigner(address)(bool)", receiver).strip().lower() != "true":
        cast_send_pk(l2_rpc, tsa, "setSigner(address,bool)", receiver, "true")

    if cast_call(l2_rpc, tsa, "isSigner(address)(bool)", ANVIL_ADDR0).strip().lower() != "true":
        cast_send_pk(l2_rpc, tsa, "setSigner(address,bool)", ANVIL_ADDR0, "true")


def _inject_return_request(l2_rpc: str, receiver: str, guid: str, loan_id: int, asset: str, recipient: str, subaccount_id: int) -> str:
    endpoint = cast_call(l2_rpc, receiver, "endpoint()(address)").splitlines()[0].strip()
    _set_eth_balance(l2_rpc, endpoint)

    src_eid = int(cast_call(l2_rpc, receiver, "remoteEid()(uint32)").split()[0])
    sender_b32 = cast_call(l2_rpc, receiver, "peers(uint32)(bytes32)", str(src_eid)).splitlines()[0].strip()
    if sender_b32.lower() == "0x" + "00" * 32:
        sender_b32 = "0x" + "00" * 12 + ANVIL_ADDR0[2:]

    message_payload = _abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(1,{loan_id},{asset},0,{recipient},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    tx_raw = cast_send_from(
        l2_rpc,
        endpoint,
        receiver,
        "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
        f"({src_eid},{sender_b32},1)",
        guid,
        message_payload,
        "0x0000000000000000000000000000000000000000",
        "0x",
    )
    return extract_tx_hash(tx_raw)


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
    print("=== collar.fi l2 keeper return-request handling e2e ===")

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

    receiver = l2["l2Receiver"]
    vault = l1["l1Vault"]
    tsa = l2["l2Tsa"]
    _ensure_l2_keeper_role(l2_rpc, receiver)
    _ensure_tsa_signer(l2_rpc, tsa, receiver)

    subaccount_id = int(cast_call(l2_rpc, tsa, "subAccount()(uint256)").split()[0])
    block_now = int(run(["cast", "block-number", "--rpc-url", l2_rpc]).split()[0])
    loan_id = 900_000 + (block_now % 90_000)
    request_guid = "0x" + format(90_000_000 + loan_id, "064x")

    relay_tx = _inject_return_request(l2_rpc, receiver, request_guid, loan_id, sepolia_weth, vault, subaccount_id)
    l2_start_block = _tx_block(l2_rpc, relay_tx)
    _print_step(True, f"Injected ReturnRequest message on L2 receiver (loanId={loan_id})")

    tmpdir = Path(tempfile.mkdtemp(prefix="l2-keeper-return-request-"))
    l2_env = tmpdir / ".env.l2.fork"
    _write_env_with_updates(
        ROOT / ".env.l2.testnet",
        l2_env,
        _resolve_l2_runtime_env(l2_rpc, l2, receiver),
    )

    dry_state = tmpdir / "keeper_l2_dry_state.json"
    dry_cmd = [
        "uv",
        "run",
        "python",
        str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
        str(l2_env),
        "--state-file",
        str(dry_state),
        "--start-block",
        str(l2_start_block),
        "--once",
        "--json",
    ]
    dry_first = _run_keeper_command(dry_cmd)
    dry_attempted = _tick_int(dry_first, "attempted")
    dry_sent = _tick_int(dry_first, "sent")
    dry_advanced = _tick_int(dry_first, "advancedCursor")
    if dry_attempted is None or dry_attempted < 1 or dry_sent != 0 or dry_advanced != 0:
        raise RuntimeError(f"unexpected dry-run keeper result: {json.dumps(dry_first)}")
    dry_second = _run_keeper_command(dry_cmd)
    dry_attempted_2 = _tick_int(dry_second, "attempted")
    if dry_attempted_2 is None or dry_attempted_2 < 1:
        raise RuntimeError(f"dry-run re-run should re-attempt ReturnRequest, got: {json.dumps(dry_second)}")
    _print_step(True, "Verified once-mode dry-run does not advance cursor and re-attempts ReturnRequest")

    broadcast_state = tmpdir / "keeper_l2_broadcast_state.json"
    broadcast_cmd = [
        "uv",
        "run",
        "python",
        str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
        str(l2_env),
        "--state-file",
        str(broadcast_state),
        "--start-block",
        str(l2_start_block),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--json",
    ]
    broadcast_first = _run_keeper_command(broadcast_cmd)
    broadcast_attempted = _tick_int(broadcast_first, "attempted")
    broadcast_sent = _tick_int(broadcast_first, "sent")
    broadcast_advanced = _tick_int(broadcast_first, "advancedCursor")
    if broadcast_attempted != 1 or broadcast_sent != 1 or broadcast_advanced != 1:
        raise RuntimeError(f"unexpected broadcast keeper result: {json.dumps(broadcast_first)}")
    if not _keeper_has_status(broadcast_first, loan_id, "sent"):
        raise RuntimeError(f"expected sent status for loan {loan_id}, got: {json.dumps(broadcast_first)}")

    broadcast_second = _run_keeper_command(broadcast_cmd)
    broadcast_attempted_2 = _tick_int(broadcast_second, "attempted")
    if broadcast_attempted_2 != 0:
        raise RuntimeError(
            f"expected zero attempts after broadcast cursor advance, got: {json.dumps(broadcast_second)}"
        )

    replay_state = tmpdir / "keeper_l2_replay_state.json"
    replay_cmd = [
        "uv",
        "run",
        "python",
        str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
        str(l2_env),
        "--state-file",
        str(replay_state),
        "--start-block",
        str(l2_start_block),
        "--once",
        "--broadcast",
        "--private-key",
        ANVIL_PK0,
        "--json",
    ]
    replay = _run_keeper_command(replay_cmd)
    replay_attempted = _tick_int(replay, "attempted")
    if replay_attempted != 0:
        raise RuntimeError(f"expected handledMessages idempotency replay=0 attempts, got: {json.dumps(replay)}")

    handled = cast_call(l2_rpc, receiver, "handledMessages(bytes32)(bool)", request_guid).strip().lower()
    return_requested = cast_call(l2_rpc, receiver, "returnRequested(uint256)(bool)", str(loan_id)).strip().lower()
    if handled != "true" or return_requested != "true":
        raise RuntimeError(
            f"unexpected ReturnRequest post-state: handled={handled}, returnRequested={return_requested}, guid={request_guid}"
        )
    _print_step(True, "Verified include-return-requests handling and once/broadcast idempotency semantics")

    out = {
        "status": "success",
        "loanId": loan_id,
        "returnRequestGuid": request_guid,
        "l2InjectTx": relay_tx,
        "broadcastHandled": True,
    }
    result_path = tmpdir / "result.json"
    result_path.write_text(json.dumps(out, indent=2))
    _print_step(True, f"Result artifact written: {result_path}")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
