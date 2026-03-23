#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR  # noqa: E402
from py_lib.preflight_checks import asset_mapping_check  # noqa: E402

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    l1_asset: str = typer.Option("", "--l1-asset", help="L1 collateral asset to validate"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    out = asset_mapping_check(l1_env_file, l2_env_file, env_profile=env_profile, l1_asset=l1_asset)

    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return

    vault = out["vault"]
    ok = bool(out["ok"])
    icon = "[green]OK[/green]" if ok else "[red]MISMATCH[/red]"
    print(f"[bold]L1->L2 message asset preflight[/bold] {icon}")
    print(f"  vault: {vault}")
    print(f"  receiver: {out['receiver']}")
    print(f"  L1 asset: {out['l1Asset']}")
    print(f"  mapped L2 message asset: {out['mappedL2MessageAsset']}")
    print(f"  TSA wrappedDepositAsset: {out['tsaWrappedDepositAsset']}")
    print(f"  TSA wrapped underlying asset: {out['tsaWrappedUnderlyingAsset']}")
    if not ok:
        print("  hint: setL2MessageAsset(l1Asset, wrappedDepositAsset.wrappedAsset()) on L1 vault")


if __name__ == "__main__":
    app()
