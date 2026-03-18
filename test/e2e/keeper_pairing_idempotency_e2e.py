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
    ANVIL_PK0,
    cast_call,
    ensure_keeper_role as _ensure_keeper_role,
    ensure_l1_sepolia_rpc as _ensure_l1_sepolia_rpc,
    ensure_l2_derive_rpc as _ensure_l2_derive_rpc,
    ensure_liquidity_vault_role as _ensure_liquidity_vault_role,
    ensure_live_deployments as _ensure_live_deployments,
    print_step as _print_step,
    require_code as _require_code,
    resolve_l2_runtime_env as _resolve_l2_runtime_env,
    run,
    write_env_with_updates as _write_env_with_updates,
)
from loan_flow_helpers import (
    accept_mandate_for_pending,
    get_loan,
    get_mandate,
    get_pending,
    inject_deposit_confirmed,
    inject_trade_confirmed,
    parse_json_or_fallback,
    run_fresh_atomic_loan_before_ack,
    run_fresh_atomic_pending_loan,
)

app = typer.Typer(add_completion=False)


def _run_keeper(cmd: list[str]) -> dict:
    out = run(cmd)
    return parse_json_or_fallback(out)


def _step_result(flow: dict, step_name: str) -> dict:
    for step in flow.get("steps", []):
        if step.get("step") == step_name:
            result = step.get("result")
            if isinstance(result, dict):
                return result
            break
    raise RuntimeError(f"missing step result: {step_name}")


def _tx_block(rpc: str, tx_hash: str) -> int:
    receipt = json.loads(run(["cast", "receipt", tx_hash, "--rpc-url", rpc, "--json"]))
    block_raw = receipt.get("blockNumber")
    return int(block_raw, 0) if isinstance(block_raw, str) else int(block_raw)


def _keeper_attempted(payload: dict) -> int | None:
    tick = payload.get("tick") if isinstance(payload, dict) else None
    if isinstance(tick, dict) and "attempted" in tick:
        try:
            return int(tick["attempted"])
        except Exception:
            pass

    raw = payload.get("raw") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        match = re.search(r'"attempted"\s*:\s*(\d+)', raw)
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
    print("=== collar.fi keeper pairing/idempotency e2e ===")

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
    _ensure_keeper_role(l1_rpc, vault)

    # Phase 1: prove the L2 keeper is idempotent after a deposit flow has already
    # been handled once. Replaying from the same block range should not attempt to
    # send a second action for the same handled message.
    initial_atomic_pending_flow = run_fresh_atomic_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    initial_flow = initial_atomic_pending_flow["flow"]
    initial_loan_id = int(initial_atomic_pending_flow["loanId"])
    _print_step(True, f"Created atomic pending loan via fresh flow (loanId={initial_loan_id})")

    l2_keeper_from_initial_flow = _step_result(initial_flow, "l2_keeper_handle_deposit")
    if not _keeper_has_status(l2_keeper_from_initial_flow, initial_loan_id, "sent"):
        raise RuntimeError(
            f"fresh_loan_flow did not produce sent L2 keeper action: {json.dumps(l2_keeper_from_initial_flow)}"
        )
    _print_step(True, "Observed L2 keeper completion from fresh loan flow")

    initial_l1_to_l2_relay = _step_result(initial_flow, "relay_l1_to_l2_exact")
    l2_start_block = _tx_block(l2_rpc, str(initial_l1_to_l2_relay["relayTx"]))

    tmpdir = Path(tempfile.mkdtemp(prefix="keeper-pairing-e2e-"))
    l1_env = tmpdir / "l1.env"
    l2_env = tmpdir / "l2.env"
    l1_state_wait = tmpdir / "l1_wait_state.json"
    l1_state_done = tmpdir / "l1_done_state.json"
    l2_state = tmpdir / "l2_state.json"

    l1_env.write_text((ROOT / ".env.l1.testnet").read_text() + f"\nRPC_URL={l1_rpc}\nL1_VAULT={vault}\nL1_MESSENGER={messenger}\n")
    _write_env_with_updates(
        ROOT / ".env.l2.testnet",
        l2_env,
        _resolve_l2_runtime_env(l2_rpc, l2),
    )

    l2_idempotent = _run_keeper(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l2_keeper_handle_messages.py"),
            str(l2_env),
            "--state-file",
            str(l2_state),
            "--start-block",
            str(l2_start_block),
            "--once",
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--json",
        ]
    )
    attempted_l2 = _keeper_attempted(l2_idempotent)
    if attempted_l2 is None or attempted_l2 != 0:
        raise RuntimeError(f"expected idempotent L2 keeper run with zero attempts, got: {json.dumps(l2_idempotent)}")
    _print_step(True, "Verified L2 keeper idempotent re-run on handled deposit message")

    # Phase 2: exercise L1 keeper pairing semantics. The keeper should wait when
    # only one side of the finalize pair is present, and finalize exactly once
    # once the matching message arrives.
    second_atomic_pre_ack_flow = run_fresh_atomic_loan_before_ack(l1_json, l2_json, l1_rpc, l2_rpc, sepolia_weth)
    paired_loan_id = int(second_atomic_pre_ack_flow["loanId"])
    pending_deposit = get_pending(vault, l1_rpc, paired_loan_id)
    onchain_mandate = get_mandate(vault, l1_rpc, paired_loan_id)
    if onchain_mandate["borrower"] == "0x0000000000000000000000000000000000000000":
        mandate_ctx = accept_mandate_for_pending(l1_rpc, vault, sepolia_weth, paired_loan_id, pending_deposit)
    else:
        mandate_ctx = {
            "borrower": onchain_mandate["borrower"],
            "mandateDeadline": onchain_mandate["deadline"],
            "callStrike": onchain_mandate["minCallStrike"],
            "subaccountId": int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0]),
        }
    _print_step(True, f"Loaded second atomic pre-ACK loan + mandate for L1 keeper pairing (loanId={paired_loan_id})")

    l1_start_block = int(run(["cast", "block-number", "--rpc-url", l1_rpc]).split()[0])

    paired_trade_guid = inject_trade_confirmed(
        l1_rpc,
        messenger,
        paired_loan_id,
        vault,
        int(mandate_ctx["subaccountId"]),
        int(pending_deposit["maturity"]),
        int(pending_deposit["putStrike"]),
        int(mandate_ctx["callStrike"]),
        guid_nonce_base=90_000_000,
    )
    _print_step(True, "Injected trade-confirmed marker before deposit-confirmed message")

    # With only the trade-confirmed side present, keeper should observe the loan
    # but hold off because the deposit-confirmed side has not been seen yet.
    wait_for_pair = _run_keeper(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l1_keeper_handle_messages.py"),
            str(l1_env),
            "--state-file",
            str(l1_state_wait),
            "--start-block",
            str(l1_start_block),
            "--once",
            "--logs-rpc-url",
            l1_rpc,
            "--json",
        ]
    )
    if not _keeper_has_status(wait_for_pair, paired_loan_id, "waiting-pair"):
        raise RuntimeError(f"expected waiting-pair status for loan {paired_loan_id}, got: {json.dumps(wait_for_pair)}")
    _print_step(True, "Observed L1 keeper waiting-pair with only trade-confirmed marker")

    # Once the matching deposit-confirmed message is injected, the pair is complete
    # and the next keeper pass should finalize the loan.
    paired_deposit_guid = "0x" + format(95_000_000 + paired_loan_id, "064x")
    inject_deposit_confirmed(
        l1_rpc,
        messenger,
        paired_loan_id,
        vault,
        int(mandate_ctx["subaccountId"]),
        sepolia_weth,
        int(pending_deposit["collateral"]),
        paired_deposit_guid,
    )
    _print_step(True, "Injected matching deposit-confirmed message to complete pair")

    finalized_pair = _run_keeper(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l1_keeper_handle_messages.py"),
            str(l1_env),
            "--state-file",
            str(l1_state_done),
            "--start-block",
            str(l1_start_block),
            "--once",
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--logs-rpc-url",
            l1_rpc,
            "--json",
        ]
    )
    if not _keeper_has_status(finalized_pair, paired_loan_id, "sent"):
        raise RuntimeError(f"expected finalize completion for loan {paired_loan_id}, got: {json.dumps(finalized_pair)}")
    _print_step(True, "Observed L1 keeper finalize completion after message pairing")

    # Re-running from the persisted keeper state must be a no-op after finalize.
    repeat_finalize_check = _run_keeper(
        [
            "uv",
            "run",
            "python",
            str(ROOT / "ops/management/l1_keeper_handle_messages.py"),
            str(l1_env),
            "--state-file",
            str(l1_state_done),
            "--once",
            "--broadcast",
            "--private-key",
            ANVIL_PK0,
            "--logs-rpc-url",
            l1_rpc,
            "--json",
        ]
    )
    attempted_l1 = _keeper_attempted(repeat_finalize_check)
    if attempted_l1 is None or attempted_l1 != 0:
        raise RuntimeError(
            f"expected idempotent L1 keeper re-run with zero attempts, got: {json.dumps(repeat_finalize_check)}"
        )

    finalized_loan = get_loan(vault, l1_rpc, paired_loan_id)
    if int(finalized_loan["state"]) != 1:
        raise RuntimeError(f"loan not ACTIVE_ZERO_COST after keeper finalize (state={finalized_loan['state']})")
    _print_step(True, "Verified L1 keeper idempotency after finalize completion")

    print("\nResult: SUCCESS")


if __name__ == "__main__":
    app()
