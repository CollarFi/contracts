#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR  # noqa: E402
from py_lib.preflight_checks import l2_message_preflight  # noqa: E402

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    guid: list[str] = typer.Option(None, "--guid", help="Specific message guid(s) to inspect"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    out = l2_message_preflight(
        l2_env_file,
        env_profile=env_profile,
        receiver=receiver,
        guid=guid,
        lookback_blocks=lookback_blocks,
    )

    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return

    print(f"[bold]L2 message preflight[/bold] receiver={out['receiver']} inspected={out['inspected']}")
    for r in out["results"]:
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} {r['guid']}")
        msg = r.get("message")
        if msg:
            print(
                f"   action={msg['action']} loanId={msg['loanId']} asset={msg['asset']} amount={msg['amount']} "
                f"subaccount={msg['subaccountId']}"
            )
        for issue in r.get("issues", []):
            print(f"   - {issue}")


if __name__ == "__main__":
    app()
