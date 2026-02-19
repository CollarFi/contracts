#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, cast_call, load_env, must, run
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths, resolve_l2_env_path

app = typer.Typer(add_completion=False, help="Unified preflight router")


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


def _recipient_check(env_profile: str, l1_env_file: Path, l2_env_file: Path) -> dict:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    must(l1, "RPC_URL")
    must(l2, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")
    l1_recipient = _strip_units(cast_call(l1["RPC_URL"], vault, "l2Recipient()(address)", allow_fail=True))

    return {
        "ok": l1_recipient.lower() == receiver.lower(),
        "vault": vault,
        "l1Recipient": l1_recipient,
        "l2Receiver": receiver,
    }


@app.command("assets")
def assets(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    cmd = [
        "uv",
        "run",
        "python",
        "ops/preflight/l1_l2_message_asset_preflight.py",
        "--env",
        (env_profile or "testnet"),
        "--json",
    ]
    out = _run_json(cmd)
    if json_out:
        print(json.dumps(out, indent=2))
    else:
        print(out)


@app.command("messages")
def messages(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    cmd = [
        "uv",
        "run",
        "python",
        "ops/preflight/l2_message_preflight.py",
        str(l2_env_file),
        "--lookback-blocks",
        str(lookback_blocks),
        "--json",
    ]
    out = _run_json(cmd)
    if json_out:
        print(json.dumps(out, indent=2))
    else:
        print(out)


@app.command("all")
def all_checks(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    include_messages: bool = typer.Option(False, help="Include pending-message preflight scan"),
    include_uln: bool = typer.Option(False, help="Include ULN/route check"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    recipient = _recipient_check(env_profile, l1_env_file, l2_env_file)

    uln = None
    if include_uln:
        try:
            uln = _run_json(["uv", "run", "python", "ops/check_lz_uln.py", "--env", (env_profile or "testnet"), "--json"])
        except Exception as exc:
            uln = {"ok": False, "error": f"failed to parse check_lz_uln output: {exc}"}

    assets_out = _run_json(
        [
            "uv",
            "run",
            "python",
            "ops/preflight/l1_l2_message_asset_preflight.py",
            "--env",
            (env_profile or "testnet"),
            "--json",
        ]
    )

    messages_out = None
    if include_messages:
        l2_resolved = resolve_l2_env_path(env_profile, l2_env_file)
        messages_out = _run_json(
            [
                "uv",
                "run",
                "python",
                "ops/preflight/l2_message_preflight.py",
                str(l2_resolved),
                "--lookback-blocks",
                str(lookback_blocks),
                "--json",
            ]
        )

    messages_ok = True
    if messages_out:
        messages_ok = all(bool(r.get("ok")) for r in messages_out.get("results", []))

    uln_ok = True if uln is None else bool(uln.get("ok", False))
    ok = bool(recipient.get("ok")) and uln_ok and bool(assets_out.get("ok")) and messages_ok

    recommendations: list[str] = []
    if not recipient.get("ok"):
        recommendations.append(
            "Set L1 vault l2Recipient to current L2 receiver: cast send <L1_VAULT> 'setL2Recipient(address)' <L2_RECEIVER> --rpc-url <L1_RPC> --account <ACCOUNT>"
        )
    if not assets_out.get("ok"):
        recommendations.append(
            "Set L1->L2 message asset to wrappedDepositAsset.wrappedAsset(): uv run python ops/management/set_l2_message_asset.py --env <env> --l1-asset <L1_ASSET> --l2-asset <WRAPPED_UNDERLYING> --broadcast"
        )
    if uln is not None and not uln.get("ok", False):
        recommendations.append(
            "Repair peers/ULN/default options: uv run python ops/ensure_lz_route.py --env <env> --broadcast"
        )
    if messages_out is not None and not messages_ok:
        recommendations.append(
            "Inspect failing GUIDs and reasons: uv run python ops/preflight/l2_message_preflight.py --env <env> --json"
        )

    out = {
        "ok": ok,
        "checks": {
            "recipient": recipient,
            "uln": uln,
            "assetMapping": assets_out,
            "messages": messages_out,
        },
        "recommendations": recommendations,
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[bold]Unified preflight[/bold] {'[green]OK[/green]' if ok else '[red]FAIL[/red]'}")
    print(
        f"  recipient: {'OK' if recipient['ok'] else 'MISMATCH'} "
        f"({recipient['l1Recipient']} vs {recipient['l2Receiver']})"
    )
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
