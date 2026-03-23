#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich import print

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR
from py_lib.envs import resolve_l1_env_path
from py_lib.operation_engine import OperationRuntime, OperationStep, build_operation_summary, is_zero_address, resolve_l1_vault_address

app = typer.Typer(add_completion=False)

def run_enable_collateral(
    runtime: Any,
    *,
    env_profile: str,
    asset: str,
    scale: int,
    l2_asset: str,
) -> dict[str, Any]:
    vault = resolve_l1_vault_address(runtime.env, runtime.rpc_url)
    chain_id = runtime.chain_id()

    collateral_asset = asset or runtime.env.get("WETH_ASSET", "")
    if not collateral_asset:
        raise ValueError("missing collateral asset: pass --asset or set WETH_ASSET in env")

    allowed_now = runtime.cast_call(vault, "collateralAllowed(address)(bool)", collateral_asset)
    scale_now = runtime.cast_call(vault, "strikeScale(address)(uint256)", collateral_asset)
    l2_asset_now = runtime.cast_call(vault, "l2MessageAsset(address)(address)", collateral_asset)

    target_l2_asset = l2_asset or runtime.env.get("L2_WRAPPED_WETH_ASSET", "")
    if not target_l2_asset or is_zero_address(target_l2_asset):
        if l2_asset_now and not is_zero_address(l2_asset_now):
            target_l2_asset = l2_asset_now
        else:
            raise ValueError("missing L2 asset: pass --l2-asset or set L2_WRAPPED_WETH_ASSET")

    step = OperationStep(
        name="setCollateralConfig",
        action="enable collateral",
        command=runtime.render_cast_send(
            vault,
            "setCollateralConfig(address,bool,uint256,address)",
            collateral_asset,
            "true",
            str(scale),
            target_l2_asset,
        ),
        needs_update=(
            allowed_now.strip().lower() != "true"
            or scale_now.strip() != str(scale)
            or l2_asset_now.strip().lower() != target_l2_asset.lower()
        ),
        current={
            "allowed": allowed_now,
            "strikeScale": scale_now,
            "l2MessageAsset": l2_asset_now,
        },
        target={
            "allowed": True,
            "strikeScale": str(scale),
            "l2MessageAsset": target_l2_asset,
        },
        details={
            "vault": vault,
            "asset": collateral_asset,
        },
    )

    tx_hash = None
    if runtime.broadcast:
        tx_hash = runtime.cast_send(
            vault,
            "setCollateralConfig(address,bool,uint256,address)",
            collateral_asset,
            "true",
            str(scale),
            target_l2_asset,
        )
        step.executed = True
        step.tx = tx_hash

    return build_operation_summary(
        runtime,
        resolved_addrs={
            "vault": vault,
            "asset": collateral_asset,
            "l2Asset": target_l2_asset,
        },
        steps=[step],
        extra={
            "envFile": str(runtime.env_file),
            "envProfile": (env_profile.strip().lower() or None),
            "chainId": chain_id,
            "vault": vault,
            "asset": collateral_asset,
            "current": step.current,
            "target": step.target,
            "tx": tx_hash,
        },
    )


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet. Loads .env.l1.<env>."),
    asset: str = typer.Option("", "--asset", help="Collateral ERC20 address (defaults to WETH_ASSET from env)."),
    scale: int = typer.Option(10**30, "--scale", help="strikeScale for this asset (default: 1e30 for ETH collateral and 1e18 strike)."),
    l2_asset: str = typer.Option("", "--l2-asset", help="L2 asset encoded in LZ payload (default: env L2_WRAPPED_WETH_ASSET or existing mapping)."),
    broadcast: bool = typer.Option(False, help="Send onchain tx (default: dry-run)."),
    account: str = typer.Option("", "--account", help="Use Foundry keystore account instead of env ACCOUNT."),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account."),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)."),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from."),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary."),
) -> None:
    l1_env_file = resolve_l1_env_path(env_profile, l1_env_file)
    runtime = OperationRuntime.from_env_file(
        l1_env_file,
        broadcast=broadcast,
        account=account,
        private_key=private_key,
        from_addr=from_addr,
        unlocked=unlocked,
    )
    runtime.require_cast()

    summary = run_enable_collateral(
        runtime,
        env_profile=env_profile,
        asset=asset,
        scale=scale,
        l2_asset=l2_asset,
    )

    if json_out:
        print(json.dumps(summary, indent=2))
        return

    step = summary["steps"][0]
    print(f"[cyan][info][/cyan] chain_id: {summary['chainId']}")
    print(f"[cyan][info][/cyan] vault: {summary['vault']}")
    print(f"[cyan][info][/cyan] asset: {summary['asset']}")
    print(
        f"[cyan][info][/cyan] current config -> allowed={summary['current']['allowed']},"
        f" strikeScale={summary['current']['strikeScale']},"
        f" l2MessageAsset={summary['current']['l2MessageAsset']}"
    )

    if broadcast:
        print(
            "[green][ok][/green] setCollateralConfig sent"
            f" (allowed=true, scale={scale}, l2Asset={summary['target']['l2MessageAsset']})"
        )
        if summary["tx"]:
            print(f"  tx: {summary['tx']}")
        return

    if step["needsUpdate"]:
        print("[yellow][dry-run][/yellow] config differs from target")
    else:
        print("[yellow][dry-run][/yellow] config already matches target")
    print("  rerun with --broadcast to apply")
    if step["command"]:
        print(f"  {step['command']}")


if __name__ == "__main__":
    app()
