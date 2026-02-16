#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

from lz_harness.common import ROOT_DIR, address_to_peer_bytes32, cast_send, load_env, must, run

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


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_oapp_addr(*, env: dict[str, str], env_key: str, output_key: str, output_side: str) -> str:
    if env.get(env_key):
        return str(env[env_key])

    output_json = env.get("OUTPUT_JSON") or _default_output_json(must(env, "RPC_URL"), output_side)
    return _read_addr_from_output(output_json, output_key)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, l2_env_file = _resolve_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT"):
            must(env, k)

    l1_eid = l1.get("L2_EID") or l1.get("REMOTE_EID")
    l2_eid = l2.get("L1_EID") or l2.get("REMOTE_EID")
    if not l1_eid:
        raise ValueError("missing L2_EID in L1 env (or legacy REMOTE_EID)")
    if not l2_eid:
        raise ValueError("missing L1_EID in L2 env (or legacy REMOTE_EID)")

    l1_messenger = _resolve_oapp_addr(
        env=l1,
        env_key="L1_MESSENGER",
        output_key="l1Messenger",
        output_side="l1",
    )
    l2_receiver = _resolve_oapp_addr(
        env=l2,
        env_key="L2_RECEIVER",
        output_key="l2Receiver",
        output_side="l2",
    )

    l1_peer = address_to_peer_bytes32(l2_receiver)
    l2_peer = address_to_peer_bytes32(l1_messenger)

    summary = {
        "l1Messenger": l1_messenger,
        "l2Receiver": l2_receiver,
        "l1L2Eid": l1_eid,
        "l2L1Eid": l2_eid,
        "l1SetPeerArg": l1_peer,
        "l2SetPeerArg": l2_peer,
        "mode": "broadcast" if broadcast else "dry-run",
    }

    if broadcast:
        print("[cyan][info][/cyan] wiring L1 messenger -> L2 receiver")
        cast_send(
            l1["RPC_URL"],
            l1["ACCOUNT"],
            l1_messenger,
            "setPeer(uint32,bytes32)",
            l1_eid,
            l1_peer,
        )

        print("[cyan][info][/cyan] wiring L2 receiver -> L1 messenger")
        cast_send(
            l2["RPC_URL"],
            l2["ACCOUNT"],
            l2_receiver,
            "setPeer(uint32,bytes32)",
            l2_eid,
            l2_peer,
        )

        print("[green][ok][/green] peers wired")
    else:
        print("[yellow][dry-run][/yellow] no onchain txs sent")
        print(f"  L1 call: setPeer({l1_eid}, {l1_peer}) on {l1_messenger}")
        print(f"  L2 call: setPeer({l2_eid}, {l2_peer}) on {l2_receiver}")

    if json_out:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    app()
