#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json, run

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env>."),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run/simulation)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    if env_profile and l1_env_file == (ROOT_DIR / ".env.l1.testnet"):
        l1_env_file = ROOT_DIR / f".env.l1.{env_profile.strip().lower()}"

    l1 = load_env(l1_env_file)

    for k in ("RPC_URL", "ACCOUNT", "TREASURY"):
        must(l1, k)

    # Keep keystore/named-account workflow only.
    deployer = run(["cast", "wallet", "address", "--account", l1["ACCOUNT"]])
    if not l1.get("ADMIN"):
        l1["ADMIN"] = deployer

    if not l1.get("OUTPUT_JSON"):
        chain_id = run(["cast", "chain-id", "--rpc-url", l1["RPC_URL"]])
        l1["OUTPUT_JSON"] = f"./deployments/{chain_id}/l1.json"

    out_abs = resolve_output_json(l1["OUTPUT_JSON"])
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    if not l1.get("L2_EID") and l1.get("REMOTE_EID"):
        # Backward-compat fallback for older env files.
        l1["L2_EID"] = l1["REMOTE_EID"]

    env_overrides = {
        "ADMIN": l1["ADMIN"],
        "TREASURY": l1["TREASURY"],
        "OUTPUT_JSON": l1["OUTPUT_JSON"],
    }

    for opt in (
        "VAULT_OWNER",
        "PERMIT2",
        "L2_RECIPIENT",
        "LIQUIDITY_VAULT",
        "USDC_ASSET",
        "EULER_ADAPTER",
        "LZ_ENDPOINT",
        "L2_EID",
        "WETH_ASSET",
        "WETH_SOCKET_VAULT",
        "WETH_SOCKET_BRIDGE",
        "WETH_SOCKET_CONNECTOR",
        "WETH_MSG_GAS_LIMIT",
        "WETH_PAYLOAD_SIZE",
        "WETH_STRIKE_SCALE",
    ):
        if l1.get(opt):
            env_overrides[opt] = l1[opt]

    print(f"[cyan][info][/cyan] deploying L1 protocol via script/DeployL1.s.sol (broadcast={broadcast})")
    _forge_out = forge_script(
        "script/DeployL1.s.sol:DeployL1",
        l1["RPC_URL"],
        l1["ACCOUNT"],
        broadcast,
        env_overrides,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(out_abs),
                    "broadcast": broadcast,
                    "account": l1["ACCOUNT"],
                    "envProfile": (env_profile.strip().lower() or None),
                },
                indent=2,
            )
        )
    else:
        print("[green][ok][/green] L1 deployment script finished")
        print(f"  output json: {out_abs}")
        print("  broadcast dir: script/../broadcast")


if __name__ == "__main__":
    app()
