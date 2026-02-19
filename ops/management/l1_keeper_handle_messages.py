#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l1_l2_env_paths  # noqa: E402

app = typer.Typer(add_completion=False)

# CollarLZMessages.Action enum
ACTION_DEPOSIT_CONFIRMED = 3
ACTION_TRADE_CONFIRMED = 5


def _block_number(rpc_url: str) -> int:
    return int(run(["cast", "block-number", "--rpc-url", rpc_url]))


def _get_logs(rpc_url: str, messenger_addr: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    out = run(
        [
            "cast",
            "logs",
            "MessageReceived(bytes32,uint8,uint256)",
            "--address",
            messenger_addr,
            "--from-block",
            str(from_block),
            "--to-block",
            str(to_block),
            "--rpc-url",
            rpc_url,
            "--json",
        ]
    )
    parsed = json.loads(out)
    if not isinstance(parsed, list):
        raise RuntimeError(f"unexpected cast logs output: {out}")
    return parsed


def _action_name(action: int) -> str:
    return {
        3: "DepositConfirmed",
        5: "TradeConfirmed",
    }.get(action, f"Unknown({action})")


def _load_state(path: Path, start_block: int) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"nextBlock": start_block}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, _ = resolve_l1_l2_env_paths(env_profile, l1_env_file, ROOT_DIR / ".env.l2.testnet")
    env = load_env(l1_env_file)

    rpc_url = must(env, "RPC_URL")
    logs_url = logs_rpc_url or env.get("LOGS_RPC_URL", "") or rpc_url
    account = env.get("ACCOUNT", "")
    messenger_addr = messenger or resolve_addr(env, "L1_MESSENGER", "l1Messenger", "l1")
    vault_addr = vault or resolve_addr(env, "L1_VAULT", "l1Vault", "l1")
    if broadcast and not account:
        raise ValueError("missing ACCOUNT in env for --broadcast")

    if not state_file.is_file() and start_block == 0:
        latest = _block_number(rpc_url)
        start_block = max(0, latest - backfill_blocks)

    state = _load_state(state_file, start_block)
    next_block = int(state.get("nextBlock", start_block))

    handled: list[dict[str, Any]] = []

    def tick() -> dict[str, Any]:
        nonlocal next_block
        latest = _block_number(rpc_url)
        if latest < next_block:
            return {"fromBlock": next_block, "toBlock": latest, "logs": 0, "attempted": 0, "sent": 0}

        scan_from = next_block
        logs = _get_logs(logs_url, messenger_addr, scan_from, latest)

        # Build latest unconsumed guid per action for each loan from scanned logs.
        by_loan: dict[int, dict[int, str]] = {}
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            guid = topics[1]
            loan_id = int(topics[2], 16)
            data = log.get("data", "0x")
            action = int(data, 16) if data not in {"0x", ""} else -1
            if action not in {ACTION_DEPOSIT_CONFIRMED, ACTION_TRADE_CONFIRMED}:
                continue
            if _guid_consumed(rpc_url, vault_addr, guid):
                continue
            by_loan.setdefault(loan_id, {})[action] = guid

        attempts = 0
        sent = 0

        for loan_id in sorted(by_loan.keys()):
            pair = by_loan[loan_id]
            deposit_guid = pair.get(ACTION_DEPOSIT_CONFIRMED)
            trade_guid = pair.get(ACTION_TRADE_CONFIRMED)

            has_pending = _has_pending_deposit(rpc_url, vault_addr, loan_id)
            has_mandate = _has_mandate(rpc_url, vault_addr, loan_id)

            if not deposit_guid or not trade_guid:
                # Not processable yet; keep waiting for the pair.
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

            if broadcast:
                try:
                    tx = cast_send(
                        rpc_url,
                        account,
                        vault_addr,
                        "finalizeLoan(uint256,bytes32,bytes32)",
                        str(loan_id),
                        deposit_guid,
                        trade_guid,
                    )
                    item["tx"] = tx
                    item["status"] = "sent"
                    sent += 1
                except Exception as exc:
                    item["status"] = f"error: {exc}"

            handled.append(item)
            if attempts >= max_per_tick:
                break

        # Advance cursor only on safe successful broadcast or always on dry-run.
        advanced = False
        if not broadcast or attempts == sent:
            next_block = latest + 1
            state["nextBlock"] = next_block
            _save_state(state_file, state)
            advanced = True

        return {
            "fromBlock": scan_from,
            "toBlock": latest,
            "logs": len(logs),
            "attempted": attempts,
            "sent": sent,
            "advancedCursor": advanced,
            "nextBlock": next_block,
        }

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
                else:
                    print(
                        f"  - loan={item['loanId']} finalizeLoan "
                        f"deposit={item['depositGuid']} trade={item['tradeGuid']} -> {item['status']}"
                    )
        time.sleep(poll_seconds)


if __name__ == "__main__":
    app()
