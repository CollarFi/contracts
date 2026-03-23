#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR  # noqa: E402
from py_lib.operation_engine import OperationRuntime, OperationStep, build_operation_summary, resolve_l1_vault_address, strip_cast_units  # noqa: E402
from py_lib.envs import resolve_l1_l2_env_paths  # noqa: E402
from py_lib.l2_discovery import resolve_l2_wrapped_asset_from_tsa  # noqa: E402

app = typer.Typer(add_completion=False)

def run_set_l2_message_asset(
    runtime: Any,
    *,
    env_profile: str,
    l2_env_file: Path,
    l1_asset: str,
    l2_asset: str,
    vault: str,
) -> dict[str, Any]:
    vault_addr = resolve_l1_vault_address(runtime.env, runtime.rpc_url, vault)

    target_l1_asset = l1_asset or runtime.env.get("WETH_ASSET", "")
    target_l2_asset = l2_asset or runtime.env.get("L2_WRAPPED_WETH_ASSET", "")
    if not target_l2_asset:
        target_l2_asset = resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(f"[cyan][info][/cyan] resolved L2 asset from TSA: {target_l2_asset}")
    if not target_l1_asset or not target_l2_asset:
        raise ValueError("set --l1-asset and provide --l2-asset or L2 TSA must be resolvable")

    current = strip_cast_units(runtime.cast_call(vault_addr, "l2MessageAsset(address)(address)", target_l1_asset, allow_fail=True))
    allowed = strip_cast_units(runtime.cast_call(vault_addr, "collateralAllowed(address)(bool)", target_l1_asset, allow_fail=True)).lower()
    scale = strip_cast_units(runtime.cast_call(vault_addr, "strikeScale(address)(uint256)", target_l1_asset, allow_fail=True))
    needs_update = current.lower() != target_l2_asset.lower()

    step = OperationStep(
        name="setCollateralConfig",
        action="set L2 message asset",
        command=runtime.render_cast_send(
            vault_addr,
            "setCollateralConfig(address,bool,uint256,address)",
            target_l1_asset,
            allowed,
            scale,
            target_l2_asset,
        ),
        needs_update=needs_update,
        current={
            "allowed": allowed,
            "strikeScale": scale,
            "l2MessageAsset": current,
        },
        target={
            "allowed": allowed,
            "strikeScale": scale,
            "l2MessageAsset": target_l2_asset,
        },
        details={
            "vault": vault_addr,
            "l1Asset": target_l1_asset,
        },
    )

    tx = None
    if runtime.broadcast and needs_update:
        tx = runtime.cast_send(
            vault_addr,
            "setCollateralConfig(address,bool,uint256,address)",
            target_l1_asset,
            allowed,
            scale,
            target_l2_asset,
        )
        step.executed = True
        step.tx = tx
    elif runtime.broadcast:
        step.skipped_reason = "already_correct"

    return build_operation_summary(
        runtime,
        resolved_addrs={
            "vault": vault_addr,
            "l1Asset": target_l1_asset,
            "l2Asset": target_l2_asset,
        },
        steps=[step],
        extra={
            "envFile": str(runtime.env_file),
            "envProfile": (env_profile.strip().lower() or None),
            "vault": vault_addr,
            "l1Asset": target_l1_asset,
            "targetL2Asset": target_l2_asset,
            "current": current,
            "currentAllowed": allowed,
            "currentStrikeScale": scale,
            "needsUpdate": needs_update,
            "tx": tx,
        },
    )


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file", help="L2 env file for TSA lookup"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    l1_asset: str = typer.Option("", "--l1-asset", help="L1 collateral asset"),
    l2_asset: str = typer.Option("", "--l2-asset", help="L2 wrapped asset for LZ payload (optional: auto from TSA)"),
    vault: str = typer.Option("", "--vault", help="Override L1 vault address"),
    broadcast: bool = typer.Option(False, help="Send onchain tx"),
    account: str = typer.Option("", "--account", help="Use Foundry keystore account instead of env ACCOUNT"),
    private_key: str = typer.Option("", "--private-key", help="Use raw private key instead of --account"),
    from_addr: str = typer.Option("", "--from", help="Use unlocked sender address (for anvil --auto-impersonate)"),
    unlocked: bool = typer.Option(False, "--unlocked", help="Use unlocked mode with --from"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    runtime = OperationRuntime.from_env_file(
        l1_env_file,
        broadcast=broadcast,
        account=account,
        private_key=private_key,
        from_addr=from_addr,
        unlocked=unlocked,
    )
    runtime.require_cast()
    out = run_set_l2_message_asset(
        runtime,
        env_profile=env_profile,
        l2_env_file=l2_env_file,
        l1_asset=l1_asset,
        l2_asset=l2_asset,
        vault=vault,
    )

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[cyan][info][/cyan] vault={out['vault']}")
    print(f"[cyan][info][/cyan] l1Asset={out['l1Asset']}")
    print(f"[cyan][info][/cyan] current={out['current']}")
    print(f"[cyan][info][/cyan] target={out['targetL2Asset']}")
    if not out["needsUpdate"]:
        print("[green][ok][/green] mapping already correct")
    elif broadcast:
        print(f"[green][ok][/green] updated; tx={out['tx']}")
    else:
        print("[yellow][dry-run][/yellow] no tx sent")
        step = out["steps"][0]
        if step["command"]:
            print(f"  {step['command']}")


if __name__ == "__main__":
    app()
