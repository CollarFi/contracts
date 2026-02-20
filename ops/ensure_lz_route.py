#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys

import typer
from rich import print

from lz_harness.common import ROOT_DIR

app = typer.Typer(add_completion=False)


def _run_py(script_rel: str, args: list[str]) -> str:
    script = ROOT_DIR / script_rel
    cmd = [sys.executable, str(script), *args]
    proc = subprocess.run(cmd, cwd=ROOT_DIR, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout


@app.command()
def main(
    env_profile: str = typer.Option("testnet", "--env", help="Environment profile: testnet|mainnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    env_arg = ["--env", env_profile]
    mode = "broadcast" if broadcast else "dry-run"

    steps: list[dict[str, str]] = []

    print(f"[bold]Ensuring LayerZero route config[/bold] ({mode})")

    peer_args = [*env_arg]
    if broadcast:
        peer_args.append("--broadcast")
    peer_out = _run_py("ops/preflight/wire_lz_peers.py", peer_args)
    steps.append({"step": "wire_lz_peers", "mode": mode, "output": peer_out})

    uln_args = [*env_arg]
    if broadcast:
        uln_args.append("--broadcast")
    uln_out = _run_py("ops/apply_lz_uln_config.py", uln_args)
    steps.append({"step": "apply_lz_uln_config", "mode": mode, "output": uln_out})

    check_out = _run_py("ops/preflight/check_lz_uln.py", env_arg)
    steps.append({"step": "check_lz_uln", "mode": "read-only", "output": check_out})

    ok = "LayerZero ULN/route check OK" in check_out

    if json_out:
        print(json.dumps({"ok": ok, "mode": mode, "steps": steps}, indent=2))
        return

    if ok:
        print("[green][ok][/green] LayerZero peers + ULN config are now consistent.")
    else:
        print("[red][warn][/red] Route check still reports issues. See check output below.")

    print("\n[bold]Final check output:[/bold]")
    print(check_out)


if __name__ == "__main__":
    app()
