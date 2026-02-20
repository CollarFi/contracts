#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must, run  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l1_l2_env_paths  # noqa: E402

app = typer.Typer(add_completion=False)

ACTION_DEPOSIT_CONFIRMED = 3
ACTION_TRADE_CONFIRMED = 5


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
    lookback_blocks: int = typer.Option(5000, "--lookback-blocks", min=1),
    logs_rpc_url: str = typer.Option("", "--logs-rpc-url", help="Optional RPC URL used only for log scans."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l1_env_file, _ = resolve_l1_l2_env_paths(env_profile, l1_env_file, ROOT_DIR / ".env.l2.testnet")
    env = load_env(l1_env_file)

    rpc_url = must(env, "RPC_URL")
    logs_url = logs_rpc_url or env.get("LOGS_RPC_URL", "") or rpc_url
    messenger_addr = messenger or resolve_addr(env, "L1_MESSENGER", "l1Messenger", "l1")
    vault_addr = vault or resolve_addr(env, "L1_VAULT", "l1Vault", "l1")

    latest = int(run(["cast", "block-number", "--rpc-url", logs_url]))
    from_block = max(0, latest - lookback_blocks)
    logs = _get_logs(logs_url, messenger_addr, from_block, latest)

    by_loan: dict[int, dict[str, Any]] = {}
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        guid = topics[1].lower()
        loan_id = int(topics[2], 16)
        action = int(log.get("data", "0x"), 16)
        if action not in {ACTION_DEPOSIT_CONFIRMED, ACTION_TRADE_CONFIRMED}:
            continue

        consumed = _guid_consumed(rpc_url, vault_addr, guid)
        item = by_loan.setdefault(
            loan_id,
            {
                "loanId": str(loan_id),
                "depositGuid": None,
                "tradeGuid": None,
                "depositConsumed": None,
                "tradeConsumed": None,
            },
        )
        if action == ACTION_DEPOSIT_CONFIRMED:
            item["depositGuid"] = guid
            item["depositConsumed"] = consumed
        elif action == ACTION_TRADE_CONFIRMED:
            item["tradeGuid"] = guid
            item["tradeConsumed"] = consumed

    results: list[dict[str, Any]] = []
    for loan_id in sorted(by_loan.keys()):
        row = by_loan[loan_id]
        row["hasPendingDeposit"] = _has_pending_deposit(rpc_url, vault_addr, loan_id)
        row["hasMandate"] = _has_mandate(rpc_url, vault_addr, loan_id)
        row["readyToFinalize"] = bool(
            row["depositGuid"]
            and row["tradeGuid"]
            and not row["depositConsumed"]
            and not row["tradeConsumed"]
            and row["hasPendingDeposit"]
            and row["hasMandate"]
        )
        results.append(row)

    out = {
        "messenger": messenger_addr,
        "vault": vault_addr,
        "rpcUrl": rpc_url,
        "logsRpcUrl": logs_url,
        "latestBlock": latest,
        "fromBlock": from_block,
        "inspectedLoans": len(results),
        "results": results,
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[bold]L1 message preflight[/bold] messenger={messenger_addr} loans={len(results)}")
    for r in results:
        icon = "✅" if r["readyToFinalize"] else "⚠️"
        print(
            f"{icon} loan={r['loanId']} deposit={r['depositGuid']} trade={r['tradeGuid']} "
            f"pending={r['hasPendingDeposit']} mandate={r['hasMandate']} ready={r['readyToFinalize']}"
        )


if __name__ == "__main__":
    app()
