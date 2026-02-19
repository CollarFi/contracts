#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, cast_call, load_env, must, run
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths

app = typer.Typer(add_completion=False)


def _strip_units(value: str) -> str:
    return value.strip().split()[0]


def _run_json(cmd: list[str]) -> dict:
    out = run(cmd)
    try:
        return json.loads(out)
    except Exception:
        start = out.find("{")
        end = out.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(out[start : end + 1])
        raise


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    include_messages: bool = typer.Option(False, help="Also run L2 pending-message preflight scan"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    must(l1, "RPC_URL")
    must(l2, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    l1_recipient = _strip_units(cast_call(l1["RPC_URL"], vault, "l2Recipient()(address)", allow_fail=True))
    recipient_ok = l1_recipient.lower() == receiver.lower()

    try:
        lz = _run_json(["uv", "run", "python", "ops/check_lz_uln.py", "--env", (env_profile or "testnet"), "--json"])
    except Exception as exc:
        lz = {"ok": False, "error": f"failed to parse check_lz_uln output: {exc}"}
    asset = _run_json(
        [
            "uv",
            "run",
            "python",
            "ops/management/l1_l2_message_asset_preflight.py",
            "--env",
            (env_profile or "testnet"),
            "--json",
        ]
    )

    messages = None
    if include_messages:
        messages = _run_json(
            [
                "uv",
                "run",
                "python",
                "ops/management/l2_message_preflight.py",
                "--env",
                (env_profile or "testnet"),
                "--lookback-blocks",
                str(lookback_blocks),
                "--json",
            ]
        )

    messages_ok = True
    if messages:
        results = messages.get("results", [])
        messages_ok = all(bool(r.get("ok")) for r in results)

    uln_ok = bool(lz.get("ok", False)) if isinstance(lz, dict) else False
    asset_ok = bool(asset.get("ok", False)) if isinstance(asset, dict) else False

    ok = recipient_ok and uln_ok and asset_ok and messages_ok

    out = {
        "ok": ok,
        "checks": {
            "recipient": {
                "ok": recipient_ok,
                "vault": vault,
                "l1Recipient": l1_recipient,
                "l2Receiver": receiver,
            },
            "uln": lz,
            "assetMapping": asset,
            "messages": messages,
        },
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[bold]Unified preflight[/bold] {'[green]OK[/green]' if ok else '[red]FAIL[/red]'}")
    print(f"  recipient: {'OK' if recipient_ok else 'MISMATCH'} ({l1_recipient} vs {receiver})")
    print(f"  ULN/route: {'OK' if uln_ok else 'FAIL'}")
    print(f"  asset mapping: {'OK' if asset_ok else 'FAIL'}")
    if include_messages:
        print(f"  pending messages: {'OK' if messages_ok else 'FAIL'}")

    if not recipient_ok:
        print("  hint: cast send <L1_VAULT> 'setL2Recipient(address)' <L2_RECEIVER> ...")
    if not asset_ok:
        print("  hint: run ops/management/set_l2_message_asset.py with wrapped underlying asset")
    if not uln_ok:
        print("  hint: run ops/ensure_lz_route.py --env <env> --broadcast")


if __name__ == "__main__":
    app()
