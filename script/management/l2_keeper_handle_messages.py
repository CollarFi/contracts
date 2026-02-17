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

app = typer.Typer(add_completion=False)

# CollarLZMessages.Action enum
ACTION_DEPOSIT_INTENT = 0
ACTION_RETURN_REQUEST = 1
ACTION_SETTLEMENT_REPORT = 2
ACTION_DEPOSIT_CONFIRMED = 3
ACTION_COLLATERAL_RETURNED = 4
ACTION_TRADE_CONFIRMED = 5
ACTION_MANDATE_CREATED = 6


def _resolve_env_path(env_profile: str, l2_env_file: Path) -> Path:
    profile = env_profile.strip().lower()
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        return ROOT_DIR / f".env.l2.{profile}"
    return l2_env_file


def _read_addr_from_output(path_value: str, key: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"deployment output not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    val = addrs.get(key)
    if not val:
        raise ValueError(f"missing {key} in deployment output: {path}")
    return str(val)


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_receiver_addr(env: dict[str, str]) -> str:
    if env.get("L2_RECEIVER"):
        return str(env["L2_RECEIVER"])
    output_json = env.get("OUTPUT_JSON") or _default_output_json(must(env, "RPC_URL"), "l2")
    return _read_addr_from_output(output_json, "l2Receiver")


def _block_number(rpc_url: str) -> int:
    return int(run(["cast", "block-number", "--rpc-url", rpc_url]))


def _get_logs(rpc_url: str, receiver_addr: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
    out = run(
        [
            "cast",
            "logs",
            "MessageReceived(bytes32,uint8,uint256)",
            "--address",
            receiver_addr,
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
        0: "DepositIntent",
        1: "ReturnRequest",
        2: "SettlementReport",
        3: "DepositConfirmed",
        4: "CollateralReturned",
        5: "TradeConfirmed",
        6: "MandateCreated",
    }.get(action, f"Unknown({action})")


def _load_state(path: Path, start_block: int) -> dict[str, Any]:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"nextBlock": start_block}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    start_block: int = typer.Option(0, "--start-block", help="Start block when state file doesn't exist"),
    state_file: Path = typer.Option(ROOT_DIR / "deployments" / "keeper_l2_state.json", "--state-file"),
    poll_seconds: int = typer.Option(5, "--poll-seconds", min=1),
    once: bool = typer.Option(False, "--once", help="Run one polling tick and exit"),
    max_per_tick: int = typer.Option(10, "--max-per-tick", min=1),
    include_return_requests: bool = typer.Option(False, "--include-return-requests", help="Also handle ReturnRequest messages"),
    broadcast: bool = typer.Option(False, help="Send onchain transactions (default: dry-run)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l2_env_file = _resolve_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")
    receiver_addr = receiver or _resolve_receiver_addr(env)
    if broadcast and not account:
        raise ValueError("missing ACCOUNT in env for --broadcast")

    state = _load_state(state_file, start_block)
    next_block = int(state.get("nextBlock", start_block))

    allowed_actions = {ACTION_DEPOSIT_INTENT}
    if include_return_requests:
        allowed_actions.add(ACTION_RETURN_REQUEST)

    handled: list[dict[str, Any]] = []

    def tick() -> dict[str, Any]:
        nonlocal next_block
        latest = _block_number(rpc_url)
        if latest < next_block:
            return {"fromBlock": next_block, "toBlock": latest, "logs": 0, "attempted": 0, "sent": 0}

        scan_from = next_block
        logs = _get_logs(rpc_url, receiver_addr, scan_from, latest)

        attempts = 0
        sent = 0
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            guid = topics[1]
            loan_id = int(topics[2], 16)
            data = log.get("data", "0x")
            action = int(data, 16) if data not in {"0x", ""} else -1

            if action not in allowed_actions:
                continue

            already_handled_raw = cast_call(
                rpc_url,
                receiver_addr,
                "handledMessages(bytes32)(bool)",
                guid,
                allow_fail=True,
            )
            already_handled = already_handled_raw.strip().lower() == "true"
            if already_handled:
                continue

            attempts += 1
            item = {
                "guid": guid,
                "loanId": str(loan_id),
                "action": _action_name(action),
                "tx": None,
                "status": "dry-run",
            }

            if broadcast:
                try:
                    tx = cast_send(
                        rpc_url,
                        account,
                        receiver_addr,
                        "handleMessage(bytes32)",
                        guid,
                    )
                    item["tx"] = tx
                    item["status"] = "sent"
                    sent += 1
                except Exception as exc:
                    item["status"] = f"error: {exc}"
            handled.append(item)

            if attempts >= max_per_tick:
                break

        next_block = latest + 1
        state["nextBlock"] = next_block
        _save_state(state_file, state)

        return {
            "fromBlock": scan_from,
            "toBlock": latest,
            "logs": len(logs),
            "attempted": attempts,
            "sent": sent,
        }

    if once:
        result = tick()
        out = {
            "mode": "broadcast" if broadcast else "dry-run",
            "receiver": receiver_addr,
            "stateFile": str(state_file),
            "tick": result,
            "handled": handled,
        }
        if json_out:
            print(json.dumps(out, indent=2))
        else:
            print(out)
        return

    print(
        f"[bold]L2 keeper loop[/bold] mode={'broadcast' if broadcast else 'dry-run'} "
        f"receiver={receiver_addr} poll={poll_seconds}s"
    )
    while True:
        try:
            result = tick()
            if result["attempted"]:
                print(
                    f"[cyan][tick][/cyan] blocks {result['fromBlock']}..{result['toBlock']} "
                    f"logs={result['logs']} attempted={result['attempted']} sent={result['sent']}"
                )
                for item in handled[-result["attempted"] :]:
                    print(f"  - {item['action']} loan={item['loanId']} guid={item['guid']} -> {item['status']}")
        except Exception as exc:
            print(f"[red][error][/red] {exc}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    app()
