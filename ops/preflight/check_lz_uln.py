#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lz_harness.common import ROOT_DIR
from py_lib.preflight_checks import uln_route_check

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    summary = uln_route_check(l1_env_file, l2_env_file, env_profile=env_profile)

    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return

    icon = "[green]OK[/green]" if summary["ok"] else "[red]ISSUES FOUND[/red]"
    print(f"[bold]LayerZero ULN/route check[/bold] {icon}")
    print(f"  L1 messenger: {summary['l1Messenger']}")
    print(f"  L2 receiver:  {summary['l2Receiver']}")
    print(f"  EIDs: L1->L2={summary['l1ToL2Eid']}, L2->L1={summary['l2ToL1Eid']}")
    print()

    for side in summary["sides"]:
        side_icon = "[green]OK[/green]" if side["ok"] else "[red]FAIL[/red]"
        print(f"[bold]{side['label']}[/bold] {side_icon}")
        for check in side["checks"]:
            check_icon = "✅" if check["ok"] else "❌"
            print(f"  {check_icon} {check['name']}")
            if not check["ok"]:
                print(f"     actual:   {check.get('actual', 'N/A')}")
                if "expected" in check:
                    print(f"     expected: {check['expected']}")
                print(f"     hint:     {check.get('hint', '')}")
        print()

    if not summary["ok"]:
        print("[yellow]Tip:[/yellow] If LayerZeroScan says [bold]WAITING FOR ULN CONFIG[/bold], config type1/type2 is usually missing on one side.")


if __name__ == "__main__":
    app()
