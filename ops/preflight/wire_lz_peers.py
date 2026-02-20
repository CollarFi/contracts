#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lz_harness.common import ROOT_DIR, address_to_peer_bytes32, cast_call, cast_send, load_env, must
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths

app = typer.Typer(add_completion=False)


def _must_uint(name: str, raw: str) -> int:
    token = raw.strip().split()[0]
    try:
        return int(token)
    except Exception as exc:
        raise ValueError(f"failed to parse {name} from: {raw}") from exc


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT", "LZ_ENDPOINT"):
            must(env, k)

    l1_chain_eid = _must_uint(
        "L1 endpoint eid",
        cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)"),
    )
    l2_chain_eid = _must_uint(
        "L2 endpoint eid",
        cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)"),
    )

    # Route EIDs are opposite endpoint EIDs.
    l1_eid = str(l2_chain_eid)
    l2_eid = str(l1_chain_eid)

    l1_messenger = resolve_addr(l1, "L1_MESSENGER", "l1Messenger", "l1")
    l2_receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    l1_peer = address_to_peer_bytes32(l2_receiver)
    l2_peer = address_to_peer_bytes32(l1_messenger)

    l1_current_peer = cast_call(l1["RPC_URL"], l1_messenger, "peers(uint32)(bytes32)", l1_eid, allow_fail=True)
    l2_current_peer = cast_call(l2["RPC_URL"], l2_receiver, "peers(uint32)(bytes32)", l2_eid, allow_fail=True)

    l1_needs_update = l1_current_peer.lower() != l1_peer.lower()
    l2_needs_update = l2_current_peer.lower() != l2_peer.lower()

    summary = {
        "l1Messenger": l1_messenger,
        "l2Receiver": l2_receiver,
        "l1L2Eid": l1_eid,
        "l2L1Eid": l2_eid,
        "l1CurrentPeer": l1_current_peer,
        "l2CurrentPeer": l2_current_peer,
        "l1SetPeerArg": l1_peer,
        "l2SetPeerArg": l2_peer,
        "l1NeedsUpdate": l1_needs_update,
        "l2NeedsUpdate": l2_needs_update,
        "mode": "broadcast" if broadcast else "dry-run",
    }

    if broadcast:
        if l1_needs_update:
            print("[cyan][info][/cyan] wiring L1 messenger -> L2 receiver")
            cast_send(
                l1["RPC_URL"],
                l1["ACCOUNT"],
                l1_messenger,
                "setPeer(uint32,bytes32)",
                l1_eid,
                l1_peer,
            )
        else:
            print("[green][skip][/green] L1 peer already correct")

        if l2_needs_update:
            print("[cyan][info][/cyan] wiring L2 receiver -> L1 messenger")
            cast_send(
                l2["RPC_URL"],
                l2["ACCOUNT"],
                l2_receiver,
                "setPeer(uint32,bytes32)",
                l2_eid,
                l2_peer,
            )
        else:
            print("[green][skip][/green] L2 peer already correct")

        print("[green][ok][/green] peer wiring checked")
    else:
        print("[yellow][dry-run][/yellow] no onchain txs sent")
        if l1_needs_update:
            print(f"  L1 call: setPeer({l1_eid}, {l1_peer}) on {l1_messenger}")
        else:
            print("  L1 peer already correct")
        if l2_needs_update:
            print(f"  L2 call: setPeer({l2_eid}, {l2_peer}) on {l2_receiver}")
        else:
            print("  L2 peer already correct")

    if json_out:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    app()
