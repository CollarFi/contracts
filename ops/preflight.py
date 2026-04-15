#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, load_env
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.preflight_checks import (
    asset_mapping_check,
    l2_message_preflight,
    peer_check,
    recipient_check,
    uln_route_check,
    vault_recipient_check,
)

app = typer.Typer(add_completion=False, help="Unified preflight router")


@app.command("assets")
def assets(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    effective_env_profile = env_profile or "testnet"
    out = asset_mapping_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
    else:
        print(out)


@app.command("messages")
def messages(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    effective_env_profile = env_profile or "testnet"
    out = l2_message_preflight(l2_env_file, env_profile=effective_env_profile, lookback_blocks=lookback_blocks)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
    else:
        print(out)


@app.command("all")
def all_checks(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    include_messages: bool = typer.Option(False, help="Include pending-message preflight scan"),
    include_uln: bool = typer.Option(True, help="Include ULN/route check"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    effective_env_profile = env_profile or "testnet"
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(effective_env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    recipient = recipient_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)
    vault_recipient = vault_recipient_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)
    peer = peer_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)

    uln = None
    if include_uln:
        try:
            uln = uln_route_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)
        except Exception as exc:
            uln = {"ok": False, "error": f"failed to parse check_lz_uln output: {exc}"}

    assets_out = asset_mapping_check(l1_env_file, l2_env_file, env_profile=effective_env_profile)

    messages_out = None
    if include_messages:
        messages_out = l2_message_preflight(l2_env_file, env_profile=effective_env_profile, lookback_blocks=lookback_blocks)

    messages_ok = True
    if messages_out:
        messages_ok = all(bool(r.get("ok")) for r in messages_out.get("results", []))

    uln_ok = True if uln is None else bool(uln.get("ok", False))
    ok = (
        bool(recipient.get("ok"))
        and bool(vault_recipient.get("ok"))
        and bool(peer.get("ok"))
        and uln_ok
        and bool(assets_out.get("ok"))
        and messages_ok
    )

    env_name = effective_env_profile
    recommendations: list[str] = []
    if not recipient.get("ok"):
        recommendations.append(
            f"cast send {recipient['vault']} 'setL2Recipient(address)' {recipient['l2Receiver']} --rpc-url {l1['RPC_URL']} --account {l1.get('ACCOUNT', '<ACCOUNT>')}"
        )
    if not vault_recipient.get("ok"):
        recommendations.append(
            f"cast send {vault_recipient['receiver']} 'setVaultRecipient(address)' {vault_recipient['expectedVaultRecipient']} "
            f"--rpc-url {l2['RPC_URL']} --account {l2.get('ACCOUNT', '<ACCOUNT>')}"
        )
    if not peer.get("ok"):
        recommendations.append(f"uv run python ops/ensure_lz_route.py --env {env_name} --broadcast")
    if not assets_out.get("ok"):
        l1_asset = assets_out.get("l1Asset", l1.get("WETH_ASSET", "<L1_ASSET>"))
        wrapped_underlying = assets_out.get("tsaWrappedUnderlyingAsset", "<WRAPPED_UNDERLYING>")
        recommendations.append(
            f"uv run python ops/management/set_l2_message_asset.py --env {env_name} --l1-asset {l1_asset} --l2-asset {wrapped_underlying} --broadcast"
        )
    if uln is not None and not uln.get("ok", False):
        recommendations.append(f"uv run python ops/ensure_lz_route.py --env {env_name} --broadcast")
    if messages_out is not None and not messages_ok:
        recommendations.append(f"uv run python ops/preflight/l2_message_preflight.py --env {env_name} --json")

    recommendations = list(dict.fromkeys(recommendations))

    out = {
        "ok": ok,
        "checks": {
            "recipient": recipient,
            "vaultRecipient": vault_recipient,
            "peer": peer,
            "uln": uln,
            "assetMapping": assets_out,
            "messages": messages_out,
        },
        "recommendations": recommendations,
    }

    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return

    print(f"[bold]Unified preflight[/bold] {'[green]OK[/green]' if ok else '[red]FAIL[/red]'}")
    print(f"  recipient: {'OK' if recipient['ok'] else 'MISMATCH'} ({recipient['l1Recipient']} vs {recipient['l2Receiver']})")
    print(
        f"  vault recipient: {'OK' if vault_recipient['ok'] else 'MISMATCH'} "
        f"({vault_recipient['actualVaultRecipient']} vs {vault_recipient['expectedVaultRecipient']})"
    )
    print(f"  peers: {'OK' if peer.get('ok') else 'FAIL'}")
    if uln is not None:
        print(f"  ULN/route: {'OK' if uln.get('ok', False) else 'FAIL'}")
    print(f"  asset mapping: {'OK' if assets_out.get('ok', False) else 'FAIL'}")
    if include_messages:
        print(f"  pending messages: {'OK' if messages_ok else 'FAIL'}")

    if recommendations:
        print("  recommendations:")
        for rec in recommendations:
            print(f"   - {rec}")


if __name__ == "__main__":
    app()
