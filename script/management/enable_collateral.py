#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich import print

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, require_cmd, run

app = typer.Typer(add_completion=False)


def _load_l1_deployment(chain_id: str) -> dict:
    path = ROOT_DIR / "deployments" / chain_id / "l1.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing deployment file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid deployment json shape in {path}")
    return data


def _resolve_vault_address(deployment: dict) -> str:
    candidates = [
        "l1VaultProxy",
        "l1Vault",
        "collarVault",
        "vault",
    ]
    for key in candidates:
        value = deployment.get(key)
        if isinstance(value, str) and value:
            return value
    raise KeyError("could not resolve CollarVault address from deployment json")


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env>."),
    asset: str = typer.Option("", "--asset", help="Collateral ERC20 address (defaults to WETH_ASSET from env)."),
    scale: int = typer.Option(10**30, "--scale", help="strikeScale for this asset (default: 1e30 for ETH collateral and 1e18 strike)."),
    l2_asset: str = typer.Option("", "--l2-asset", help="L2 asset encoded in LZ payload (default: env L2_WRAPPED_WETH_ASSET or existing mapping)."),
    broadcast: bool = typer.Option(False, help="Send onchain tx (default: dry-run)."),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary."),
) -> None:
    require_cmd("cast")

    if env_profile and l1_env_file == (ROOT_DIR / ".env.l1.testnet"):
        l1_env_file = ROOT_DIR / f".env.l1.{env_profile.strip().lower()}"

    env = load_env(l1_env_file)
    rpc_url = must(env, "RPC_URL")

    chain_id = env.get("CHAIN_ID") or run(["cast", "chain-id", "--rpc-url", rpc_url])
    deployment = _load_l1_deployment(chain_id)
    vault = _resolve_vault_address(deployment)

    collateral_asset = asset or env.get("WETH_ASSET", "")
    if not collateral_asset:
        raise ValueError("missing collateral asset: pass --asset or set WETH_ASSET in env")

    allowed_now = cast_call(rpc_url, vault, "collateralAllowed(address)(bool)", collateral_asset)
    scale_now = cast_call(rpc_url, vault, "strikeScale(address)(uint256)", collateral_asset)
    l2_asset_now = cast_call(rpc_url, vault, "l2MessageAsset(address)(address)", collateral_asset)

    target_l2_asset = l2_asset or env.get("L2_WRAPPED_WETH_ASSET", "")
    if not target_l2_asset or target_l2_asset.lower() == "0x0000000000000000000000000000000000000000":
        if l2_asset_now and l2_asset_now.lower() != "0x0000000000000000000000000000000000000000":
            target_l2_asset = l2_asset_now
        else:
            raise ValueError("missing L2 asset: pass --l2-asset or set L2_WRAPPED_WETH_ASSET")

    tx_hash = None
    if broadcast:
        account = must(env, "ACCOUNT")
        tx_hash = cast_send(
            rpc_url,
            account,
            vault,
            "setCollateralConfig(address,bool,uint256,address)",
            collateral_asset,
            "true",
            str(scale),
            target_l2_asset,
        )

    if json_out:
        print(
            json.dumps(
                {
                    "envFile": str(l1_env_file),
                    "envProfile": (env_profile.strip().lower() or None),
                    "chainId": chain_id,
                    "vault": vault,
                    "asset": collateral_asset,
                    "current": {
                        "allowed": allowed_now,
                        "strikeScale": scale_now,
                        "l2MessageAsset": l2_asset_now,
                    },
                    "target": {
                        "allowed": True,
                        "strikeScale": str(scale),
                        "l2MessageAsset": target_l2_asset,
                    },
                    "broadcast": broadcast,
                    "tx": tx_hash,
                },
                indent=2,
            )
        )
        return

    print(f"[cyan][info][/cyan] chain_id: {chain_id}")
    print(f"[cyan][info][/cyan] CollarVault: {vault}")
    print(f"[cyan][info][/cyan] asset: {collateral_asset}")
    print(
        f"[cyan][info][/cyan] current config -> allowed={allowed_now}, strikeScale={scale_now},"
        f" l2MessageAsset={l2_asset_now}"
    )

    if broadcast:
        print(
            "[green][ok][/green] setCollateralConfig sent"
            f" (allowed=true, scale={scale}, l2Asset={target_l2_asset})"
        )
        if tx_hash:
            print(f"  tx: {tx_hash}")
    else:
        print("[yellow][dry-run][/yellow] no tx sent")
        print("  rerun with --broadcast to apply")
        print(
            "  cast send"
            f" {vault} 'setCollateralConfig(address,bool,uint256,address)'"
            f" {collateral_asset} true {scale} {target_l2_asset}"
            f" --rpc-url {rpc_url} --account {env.get('ACCOUNT', '<ACCOUNT>')}"
        )


if __name__ == "__main__":
    app()
