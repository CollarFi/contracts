#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, forge_script, load_env, must, require_cmd, resolve_output_json, run
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.l2_discovery import (
    resolve_l2_receiver_from_env_file,
    resolve_l2_subaccount_id_from_tsa,
    resolve_l2_wrapped_asset_from_tsa,
)

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file", help="L2 env file used for TSA lookup"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env> and .env.l2.<env>."),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run/simulation)"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    require_cmd("forge")
    require_cmd("cast")

    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)

    must(l1, "RPC_URL")
    must(l1, "TREASURY")

    account = l1.get("ACCOUNT", "")
    pk = private_key or l1.get("PRIVATE_KEY", "")
    sender = from_addr or l1.get("FROM", "")
    use_unlocked = unlocked or (str(l1.get("UNLOCKED", "")).lower() in {"1", "true", "yes"})

    if not account and not pk and not (use_unlocked and sender):
        raise ValueError("provide ACCOUNT, or --private-key, or --unlocked --from")

    if pk:
        deployer = run(["cast", "wallet", "address", "--private-key", pk])
    elif use_unlocked and sender:
        deployer = sender
    else:
        deployer = run(["cast", "wallet", "address", "--account", account])
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
    if l1.get("PROXY_ADMIN"):
        env_overrides["PROXY_ADMIN"] = l1["PROXY_ADMIN"]

    if l1.get("WETH_ASSET") and not l1.get("L2_WRAPPED_WETH_ASSET"):
        l1["L2_WRAPPED_WETH_ASSET"] = resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(
            "[cyan][info][/cyan] resolved L2_WRAPPED_WETH_ASSET from L2 TSA:",
            l1["L2_WRAPPED_WETH_ASSET"],
        )

    if not l1.get("L2_RECIPIENT"):
        l1["L2_RECIPIENT"] = resolve_l2_receiver_from_env_file(l2_env_file)
        print("[cyan][info][/cyan] resolved L2_RECIPIENT (receiver) from L2 deployment/env:", l1["L2_RECIPIENT"])

    if not l1.get("DERIVE_SUBACCOUNT_ID"):
        subaccount_id = resolve_l2_subaccount_id_from_tsa(l2_env_file)
        l1["DERIVE_SUBACCOUNT_ID"] = str(subaccount_id)
        print("[cyan][info][/cyan] resolved DERIVE_SUBACCOUNT_ID from L2 TSA:", l1["DERIVE_SUBACCOUNT_ID"])

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
        "L2_WRAPPED_WETH_ASSET",
        "DERIVE_SUBACCOUNT_ID",
        "RFQ_SIGNER",
    ):
        if l1.get(opt):
            env_overrides[opt] = l1[opt]

    print(f"[cyan][info][/cyan] deploying L1 protocol via script/DeployL1.s.sol (broadcast={broadcast})")
    _forge_out = forge_script(
        "script/DeployL1.s.sol:DeployL1",
        l1["RPC_URL"],
        account or None,
        broadcast,
        env_overrides,
        private_key=pk or None,
        from_addr=sender or None,
        unlocked=use_unlocked,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "outputJson": str(out_abs),
                    "broadcast": broadcast,
                    "account": account or None,
                    "privateKey": bool(pk),
                    "from": sender or None,
                    "unlocked": use_unlocked,
                    "envProfile": (env_profile.strip().lower() or None),
                    "l1Env": str(l1_env_file),
                    "l2Env": str(l2_env_file),
                    "l2WrappedWethAsset": l1.get("L2_WRAPPED_WETH_ASSET"),
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
