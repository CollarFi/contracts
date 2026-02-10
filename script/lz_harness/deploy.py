#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import typer
from rich import print

from common import (
    ROOT_DIR,
    address_to_peer_bytes32,
    cast_send,
    forge_script,
    load_env,
    load_harness_address,
    must,
    require_cmd,
    resolve_output_json,
    run,
)

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT", "OUTPUT_JSON", "REMOTE_EID"):
            must(env, k)

    def deploy_side(side: str, env: dict[str, str]) -> str:
        admin = env.get("ADMIN") or run(["cast", "wallet", "address", "--account", env["ACCOUNT"]])
        out_raw = env["OUTPUT_JSON"]
        out_abs = resolve_output_json(out_raw)
        out_abs.parent.mkdir(parents=True, exist_ok=True)

        print(f"[cyan][info][/cyan] deploying {side} harness (admin={admin})")

        forge_script(
            "script/DeployLZHarness.s.sol:DeployLZHarness",
            env["RPC_URL"],
            env["ACCOUNT"],
            broadcast,
            {
                "ADMIN": admin,
                "REMOTE_EID": env["REMOTE_EID"],
                "OUTPUT_JSON": out_raw,
                "LZ_ENDPOINT": env.get("LZ_ENDPOINT", ""),
            },
        )

        harness = load_harness_address(out_raw)
        print(f"[green][ok][/green] {side} harness: {harness}")

        receive_gas = env.get("LZ_RECEIVE_GAS", "200000")
        receive_value = env.get("LZ_RECEIVE_VALUE", "0")
        if broadcast:
            forge_script(
                "script/SetLZHarnessOptions.s.sol:SetLZHarnessOptions",
                env["RPC_URL"],
                env["ACCOUNT"],
                True,
                {
                    "HARNESS": harness,
                    "RECEIVE_GAS": receive_gas,
                    "RECEIVE_VALUE": receive_value,
                },
            )
        else:
            print(
                f"[yellow][dry-run][/yellow] would set {side} options receiveGas={receive_gas} receiveValue={receive_value}"
            )
        return harness

    l1_harness = deploy_side("L1", l1)
    l2_harness = deploy_side("L2", l2)

    l1_peer = address_to_peer_bytes32(l1_harness)
    l2_peer = address_to_peer_bytes32(l2_harness)

    if broadcast:
        print("[cyan][info][/cyan] wiring peers")
        cast_send(l1["RPC_URL"], l1["ACCOUNT"], l1_harness, "setPeer(uint32,bytes32)", l1["REMOTE_EID"], l2_peer)
        cast_send(l2["RPC_URL"], l2["ACCOUNT"], l2_harness, "setPeer(uint32,bytes32)", l2["REMOTE_EID"], l1_peer)
    else:
        print(f"[yellow][dry-run][/yellow] L1 setPeer({l1['REMOTE_EID']}, {l2_peer})")
        print(f"[yellow][dry-run][/yellow] L2 setPeer({l2['REMOTE_EID']}, {l1_peer})")

    summary = {
        "mode": "broadcast" if broadcast else "dry-run",
        "l1Harness": l1_harness,
        "l2Harness": l2_harness,
        "l1OutputJson": l1["OUTPUT_JSON"],
        "l2OutputJson": l2["OUTPUT_JSON"],
    }

    if json_out:
        import json

        print_json = json.dumps(summary, indent=2)
        print(print_json)
    else:
        print("\n[bold green][done][/bold green] LZ harness flow completed")
        print(f"L1 harness: {l1_harness}")
        print(f"L2 harness: {l2_harness}")


if __name__ == "__main__":
    app()
