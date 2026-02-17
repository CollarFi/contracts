#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must, run  # noqa: E402

app = typer.Typer(add_completion=False)


def _resolve_env_paths(env_profile: str, l1_env_file: Path, l2_env_file: Path) -> tuple[Path, Path]:
    profile = env_profile.strip().lower()
    if profile and l1_env_file == (ROOT_DIR / ".env.l1.testnet"):
        l1_env_file = ROOT_DIR / f".env.l1.{profile}"
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{profile}"
    return l1_env_file, l2_env_file


def _read_addr_from_output(path_value: str, key: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"deployment output not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    val = addrs.get(key)
    if not val:
        raise ValueError(f"missing {key} in deployment output: {path}")
    return str(val)


def _default_output_json(rpc_url: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / "l1.json")


def _resolve_l2_wrapped_asset_from_tsa(l2_env_file: Path) -> str:
    l2 = load_env(l2_env_file)
    rpc_url = must(l2, "RPC_URL")
    receiver = l2.get("L2_RECEIVER", "")
    if not receiver:
        output_json = l2.get("OUTPUT_JSON")
        if not output_json:
            chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
            output_json = str(ROOT_DIR / "deployments" / chain_id / "l2.json")
        receiver = _read_addr_from_output(output_json, "l2Receiver")

    tsa = cast_call(rpc_url, receiver, "tsa()(address)")
    base = cast_call(rpc_url, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)")
    lines = [ln.strip() for ln in base.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError("failed to parse getBaseTSAAddresses() output")
    return lines[2]


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Option(ROOT_DIR / ".env.l2.testnet", "--l2-env-file", help="L2 env file for TSA lookup"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    l1_asset: str = typer.Option("", "--l1-asset", help="L1 collateral asset"),
    l2_asset: str = typer.Option("", "--l2-asset", help="L2 wrapped asset for LZ payload (optional: auto from TSA)"),
    vault: str = typer.Option("", "--vault", help="Override L1 vault address"),
    broadcast: bool = typer.Option(False, help="Send onchain tx"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l1_env_file, l2_env_file = _resolve_env_paths(env_profile, l1_env_file, l2_env_file)
    env = load_env(l1_env_file)

    rpc_url = must(env, "RPC_URL")
    account = env.get("ACCOUNT", "")

    if not vault:
        vault = env.get("L1_VAULT", "")
    if not vault:
        output_json = env.get("OUTPUT_JSON") or _default_output_json(rpc_url)
        vault = _read_addr_from_output(output_json, "l1Vault")

    l1_asset = l1_asset or env.get("WETH_ASSET", "")
    l2_asset = l2_asset or env.get("L2_WRAPPED_WETH_ASSET", "")
    if not l2_asset:
        l2_asset = _resolve_l2_wrapped_asset_from_tsa(l2_env_file)
        print(f"[cyan][info][/cyan] resolved L2 asset from TSA: {l2_asset}")
    if not l1_asset or not l2_asset:
        raise ValueError("set --l1-asset and provide --l2-asset or L2 TSA must be resolvable")

    current = cast_call(rpc_url, vault, "l2MessageAsset(address)(address)", l1_asset, allow_fail=True)
    needs_update = current.lower() != l2_asset.lower()

    tx = None
    if broadcast:
        if not account:
            raise ValueError("missing ACCOUNT in env for --broadcast")
        if needs_update:
            tx = cast_send(rpc_url, account, vault, "setL2MessageAsset(address,address)", l1_asset, l2_asset)

    out = {
        "vault": vault,
        "l1Asset": l1_asset,
        "targetL2Asset": l2_asset,
        "current": current,
        "needsUpdate": needs_update,
        "broadcast": broadcast,
        "tx": tx,
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[cyan][info][/cyan] vault={vault}")
    print(f"[cyan][info][/cyan] l1Asset={l1_asset}")
    print(f"[cyan][info][/cyan] current={current}")
    print(f"[cyan][info][/cyan] target={l2_asset}")
    if not needs_update:
        print("[green][ok][/green] mapping already correct")
    elif broadcast:
        print(f"[green][ok][/green] updated; tx={tx}")
    else:
        print("[yellow][dry-run][/yellow] no tx sent")


if __name__ == "__main__":
    app()
