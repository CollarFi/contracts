#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l1_l2_env_paths  # noqa: E402
from py_lib.keeper_logs import data_int, get_message_received_logs, topic_hex, topic_int  # noqa: E402
from py_lib.keeper_loop import resolve_scan_range, resolve_start_block, should_advance_cursor  # noqa: E402
from py_lib.keeper_signer import KeeperSigner  # noqa: E402
from py_lib.keeper_state import load_keeper_state, read_keeper_cursor, save_keeper_state, write_keeper_cursor  # noqa: E402
from py_lib.runtime import run  # noqa: E402

app = typer.Typer(add_completion=False)

# CollarLZMessages.Action enum
ACTION_DEPOSIT_CONFIRMED = 3
ACTION_COLLATERAL_RETURNED = 4
ACTION_TRADE_CONFIRMED = 5
HANDLED_ACTIONS = {ACTION_DEPOSIT_CONFIRMED, ACTION_COLLATERAL_RETURNED, ACTION_TRADE_CONFIRMED}


@dataclass(frozen=True)
class L1KeeperRuntime:
    rpc_url: str
    logs_url: str
    messenger_addr: str
    vault_addr: str
    state_file: Path
    max_per_tick: int
    broadcast: bool
    signer: KeeperSigner | None = None


def ensure_l1_keeper_state(state: dict[str, Any]) -> None:
    if "finalizedLoans" not in state or not isinstance(state.get("finalizedLoans"), dict):
        state["finalizedLoans"] = {}
    if "returnedDeposits" not in state or not isinstance(state.get("returnedDeposits"), dict):
        state["returnedDeposits"] = {}


def _block_number(rpc_url: str) -> int:
    return int(run(["cast", "block-number", "--rpc-url", rpc_url]))


def _action_name(action: int) -> str:
    return {
        3: "DepositConfirmed",
        4: "CollateralReturned",
        5: "TradeConfirmed",
    }.get(action, f"Unknown({action})")


def _guid_consumed(rpc_url: str, vault_addr: str, guid: str) -> bool:
    raw = cast_call(rpc_url, vault_addr, "lzMessageConsumed(bytes32)(bool)", guid, allow_fail=True)
    return raw.strip().lower() == "true"


def _has_pending_deposit(rpc_url: str, vault_addr: str, loan_id: int) -> bool:
    raw = cast_call(
        rpc_url,
        vault_addr,
        "pendingDeposits(uint256)(address,address,uint256,uint256,uint256,uint256)",
        str(loan_id),
        allow_fail=True,
    )
    if raw == "N/A":
        return False
    borrower = raw.splitlines()[0].strip().lower()
    return borrower != "0x0000000000000000000000000000000000000000"


def _has_mandate(rpc_url: str, vault_addr: str, loan_id: int) -> bool:
    raw = cast_call(
        rpc_url,
        vault_addr,
        "mandates(uint256)(address,address,uint256,uint64,uint64,uint256,uint256,uint256,uint256,bool)",
        str(loan_id),
        allow_fail=True,
    )
    if raw == "N/A":
        return False
    borrower = raw.splitlines()[0].strip().lower()
    return borrower != "0x0000000000000000000000000000000000000000"


def _return_requested(rpc_url: str, vault_addr: str, loan_id: int) -> bool:
    raw = cast_call(
        rpc_url,
        vault_addr,
        "returnRequested(uint256)(bool)",
        str(loan_id),
        allow_fail=True,
    )
    return raw.strip().lower() == "true"


def _latest_unconsumed_pairs(logs: list[dict[str, Any]], *, rpc_url: str, vault_addr: str) -> dict[int, dict[int, str]]:
    by_loan: dict[int, dict[int, str]] = {}
    for log in logs:
        guid = topic_hex(log, 1)
        loan_id = topic_int(log, 2)
        action = data_int(log, default=-1)
        if guid is None or loan_id is None or action not in HANDLED_ACTIONS:
            continue
        if _guid_consumed(rpc_url, vault_addr, guid):
            continue
        by_loan.setdefault(loan_id, {})[action] = guid
    return by_loan


def run_keeper_tick(
    runtime: L1KeeperRuntime,
    *,
    state: dict[str, Any],
    next_block: int,
    handled: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    ensure_l1_keeper_state(state)
    latest = _block_number(runtime.rpc_url)
    scan_range = resolve_scan_range(next_block, latest)
    if scan_range is None:
        return {"fromBlock": next_block, "toBlock": latest, "logs": 0, "attempted": 0, "sent": 0}, next_block

    scan_from, scan_to = scan_range
    logs = get_message_received_logs(runtime.logs_url, runtime.messenger_addr, scan_from, scan_to)
    by_loan = _latest_unconsumed_pairs(logs, rpc_url=runtime.rpc_url, vault_addr=runtime.vault_addr)

    attempts = 0
    sent = 0

    for loan_id in sorted(by_loan.keys()):
        pair = by_loan[loan_id]
        deposit_guid = pair.get(ACTION_DEPOSIT_CONFIRMED)
        returned_guid = pair.get(ACTION_COLLATERAL_RETURNED)
        trade_guid = pair.get(ACTION_TRADE_CONFIRMED)

        has_pending = _has_pending_deposit(runtime.rpc_url, runtime.vault_addr, loan_id)
        has_mandate = _has_mandate(runtime.rpc_url, runtime.vault_addr, loan_id)
        return_requested = _return_requested(runtime.rpc_url, runtime.vault_addr, loan_id)

        if returned_guid:
            if not has_pending or not return_requested:
                handled.append(
                    {
                        "loanId": str(loan_id),
                        "action": "finalizeDepositReturn",
                        "collateralReturnedGuid": returned_guid,
                        "status": "blocked-state",
                        "hasPendingDeposit": has_pending,
                        "returnRequested": return_requested,
                    }
                )
                continue

            attempts += 1
            item = {
                "loanId": str(loan_id),
                "action": "finalizeDepositReturn",
                "collateralReturnedGuid": returned_guid,
                "tx": None,
                "status": "dry-run",
            }

            if runtime.broadcast:
                if runtime.signer is None:
                    item["status"] = "error: missing signer for broadcast mode"
                else:
                    try:
                        tx_hash = runtime.signer.send_contract_tx(
                            contract_name="CollarVault",
                            address=runtime.vault_addr,
                            fn_name="finalizeDepositReturn",
                            args=[int(loan_id), returned_guid],
                            label="L1 keeper finalizeDepositReturn",
                        )
                        item["tx"] = tx_hash
                        state["returnedDeposits"][str(loan_id)] = {
                            "completedAt": int(time.time()),
                            "collateralReturnedGuid": returned_guid,
                            "tx": tx_hash,
                        }
                        save_keeper_state(runtime.state_file, state)
                        item["status"] = "sent"
                        sent += 1
                    except Exception as exc:
                        item["status"] = f"error: {exc}"

            handled.append(item)
            if attempts >= runtime.max_per_tick:
                break
            continue

        if not deposit_guid or not trade_guid:
            handled.append(
                {
                    "loanId": str(loan_id),
                    "status": "waiting-pair",
                    "depositGuid": deposit_guid,
                    "tradeGuid": trade_guid,
                    "hasPendingDeposit": has_pending,
                    "hasMandate": has_mandate,
                }
            )
            continue

        if not has_pending or not has_mandate:
            handled.append(
                {
                    "loanId": str(loan_id),
                    "status": "blocked-state",
                    "depositGuid": deposit_guid,
                    "tradeGuid": trade_guid,
                    "hasPendingDeposit": has_pending,
                    "hasMandate": has_mandate,
                }
            )
            continue

        attempts += 1
        item = {
            "loanId": str(loan_id),
            "action": "finalizeLoan",
            "depositGuid": deposit_guid,
            "tradeGuid": trade_guid,
            "tx": None,
            "status": "dry-run",
        }

        if runtime.broadcast:
            if runtime.signer is None:
                item["status"] = "error: missing signer for broadcast mode"
            else:
                try:
                    tx_hash = runtime.signer.send_contract_tx(
                        contract_name="CollarVault",
                        address=runtime.vault_addr,
                        fn_name="finalizeLoan",
                        args=[int(loan_id), deposit_guid, trade_guid],
                        label="L1 keeper finalizeLoan",
                    )
                    item["tx"] = tx_hash
                    state["finalizedLoans"][str(loan_id)] = {
                        "completedAt": int(time.time()),
                        "depositGuid": deposit_guid,
                        "tradeGuid": trade_guid,
                        "tx": tx_hash,
                    }
                    save_keeper_state(runtime.state_file, state)
                    item["status"] = "sent"
                    sent += 1
                except Exception as exc:
                    item["status"] = f"error: {exc}"

        handled.append(item)
        if attempts >= runtime.max_per_tick:
            break

    advanced = should_advance_cursor(
        broadcast=runtime.broadcast,
        attempts=attempts,
        sent=sent,
        advance_on_dry_run=True,
    )
    if advanced:
        next_block = write_keeper_cursor(state, scan_to + 1)
        save_keeper_state(runtime.state_file, state)

    return {
        "fromBlock": scan_from,
        "toBlock": scan_to,
        "logs": len(logs),
        "attempted": attempts,
        "sent": sent,
        "advancedCursor": advanced,
        "nextBlock": next_block,
    }, next_block


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    messenger: str = typer.Option("", "--messenger", help="Override L1 messenger address"),
    vault: str = typer.Option("", "--vault", help="Override L1 vault address"),
    logs_rpc_url: str = typer.Option("", "--logs-rpc-url", help="Optional RPC URL used only for log scans."),
    start_block: int = typer.Option(0, "--start-block", help="Start block when state file doesn't exist"),
    backfill_blocks: int = typer.Option(5000, "--backfill-blocks", min=1, help="When no state and --start-block=0, start at latest-backfill-blocks."),
    state_file: Path = typer.Option(ROOT_DIR / "deployments" / "keeper_l1_state.json", "--state-file"),
    poll_seconds: int = typer.Option(5, "--poll-seconds", min=1),
    once: bool = typer.Option(False, "--once", help="Run one polling tick and exit"),
    max_per_tick: int = typer.Option(10, "--max-per-tick", min=1),
    broadcast: bool = typer.Option(False, help="Send onchain transactions (default: dry-run)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, _ = resolve_l1_l2_env_paths(env_profile, l1_env_file, ROOT_DIR / ".env.l2.testnet")
    env = load_env(l1_env_file)

    rpc_url = must(env, "RPC_URL")
    logs_url = logs_rpc_url or env.get("LOGS_RPC_URL", "") or rpc_url
    account = env.get("ACCOUNT", "")
    pk = private_key or env.get("PRIVATE_KEY", "")
    sender = from_addr or env.get("FROM", "")
    use_unlocked = unlocked or (str(env.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})
    messenger_addr = messenger or resolve_addr(env, "L1_MESSENGER", "l1Messenger", "l1")
    vault_addr = vault or resolve_addr(env, "L1_VAULT", "l1Vault", "l1")

    keeper_signer: KeeperSigner | None = None
    if broadcast:
        keeper_signer = KeeperSigner.from_env(
            rpc_url=rpc_url,
            account=account,
            private_key=pk,
            from_addr=sender,
            unlocked=use_unlocked,
        )
        if keeper_signer is None:
            raise ValueError("missing auth for --broadcast: provide ACCOUNT, or --private-key, or --unlocked --from")

    start_block = resolve_start_block(
        state_file=state_file,
        start_block=start_block,
        backfill_blocks=backfill_blocks,
        latest_block=lambda: _block_number(rpc_url),
    )
    state = load_keeper_state(state_file, start_block)
    next_block = read_keeper_cursor(state, start_block)
    runtime = L1KeeperRuntime(
        rpc_url=rpc_url,
        logs_url=logs_url,
        messenger_addr=messenger_addr,
        vault_addr=vault_addr,
        state_file=state_file,
        max_per_tick=max_per_tick,
        broadcast=broadcast,
        signer=keeper_signer,
    )

    handled: list[dict[str, Any]] = []

    def tick() -> dict[str, Any]:
        nonlocal next_block
        result, next_block = run_keeper_tick(runtime, state=state, next_block=next_block, handled=handled)
        return result

    if once:
        result = tick()
        out = {
            "mode": "broadcast" if broadcast else "dry-run",
            "messenger": messenger_addr,
            "vault": vault_addr,
            "logsRpcUrl": logs_url,
            "stateFile": str(state_file),
            "tick": result,
            "handled": handled,
        }
        if json_out:
            print(json.dumps(out, indent=2))
        else:
            print(json.dumps(out, indent=2))
        return

    print(
        f"[bold]L1 keeper loop[/bold] mode={'broadcast' if broadcast else 'dry-run'} "
        f"messenger={messenger_addr} vault={vault_addr} startBlock={next_block}"
    )
    while True:
        result = tick()
        if result["attempted"] or result["logs"]:
            print(
                f"[cyan]tick[/cyan] blocks {result['fromBlock']}..{result['toBlock']} "
                f"logs={result['logs']} attempted={result['attempted']} sent={result['sent']}"
            )
            for item in handled[-result["attempted"] :]:
                if item.get("status") == "waiting-pair":
                    print(
                        f"  - loan={item['loanId']} waiting-pair "
                        f"deposit={item.get('depositGuid')} trade={item.get('tradeGuid')}"
                    )
                elif item.get("action") == "finalizeDepositReturn":
                    print(
                        f"  - loan={item['loanId']} finalizeDepositReturn "
                        f"guid={item['collateralReturnedGuid']} tx={item.get('tx') or '-'} -> {item['status']}"
                    )
                else:
                    print(
                        f"  - loan={item['loanId']} finalizeLoan "
                        f"deposit={item['depositGuid']} trade={item['tradeGuid']} "
                        f"tx={item.get('tx') or '-'} -> {item['status']}"
                    )
        time.sleep(poll_seconds)


if __name__ == "__main__":
    app()
