#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must  # noqa: E402
from py_lib.deployments import resolve_addr  # noqa: E402
from py_lib.envs import resolve_l1_l2_env_paths  # noqa: E402

app = typer.Typer(add_completion=False)


def _strip_units(value: str) -> str:
    return value.strip().split()[0]


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    l1_asset: str = typer.Option("", "--l1-asset", help="L1 collateral asset to validate"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        must(env, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    asset = l1_asset or l1.get("WETH_ASSET", "")
    if not asset:
        raise ValueError("missing L1 asset: pass --l1-asset or set WETH_ASSET in L1 env")

    mapped_l2_asset_raw = cast_call(l1["RPC_URL"], vault, "l2MessageAsset(address)(address)", asset, allow_fail=True)
    mapped_l2_asset = _strip_units(mapped_l2_asset_raw) if mapped_l2_asset_raw != "N/A" else "N/A"
    tsa = cast_call(l2["RPC_URL"], receiver, "tsa()(address)", allow_fail=True)

    wrapped = "N/A"
    wrapped_underlying = "N/A"
    if tsa != "N/A":
        base = cast_call(
            l2["RPC_URL"],
            tsa,
            "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
            allow_fail=True,
        )
        if base != "N/A":
            lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
            if len(lines) >= 3:
                wrapped = lines[2]
                wrapped_underlying_raw = cast_call(
                    l2["RPC_URL"], wrapped, "wrappedAsset()(address)", allow_fail=True
                )
                if wrapped_underlying_raw != "N/A":
                    wrapped_underlying = _strip_units(wrapped_underlying_raw)

    ok = (
        mapped_l2_asset != "N/A"
        and wrapped_underlying != "N/A"
        and mapped_l2_asset.lower() == wrapped_underlying.lower()
    )
    out = {
        "vault": vault,
        "receiver": receiver,
        "tsa": tsa,
        "l1Asset": asset,
        "mappedL2MessageAsset": mapped_l2_asset,
        "tsaWrappedDepositAsset": wrapped,
        "tsaWrappedUnderlyingAsset": wrapped_underlying,
        "ok": ok,
    }

    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return

    icon = "[green]OK[/green]" if ok else "[red]MISMATCH[/red]"
    print(f"[bold]L1->L2 message asset preflight[/bold] {icon}")
    print(f"  vault: {vault}")
    print(f"  receiver: {receiver}")
    print(f"  L1 asset: {asset}")
    print(f"  mapped L2 message asset: {mapped_l2_asset}")
    print(f"  TSA wrappedDepositAsset: {wrapped}")
    print(f"  TSA wrapped underlying asset: {wrapped_underlying}")
    if not ok:
        print("  hint: setL2MessageAsset(l1Asset, wrappedDepositAsset.wrappedAsset()) on L1 vault")


if __name__ == "__main__":
    app()
