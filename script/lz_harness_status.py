#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import typer
from rich import print

from lz_harness_common import ROOT_DIR, address_to_peer_bytes32, cast_call, load_env, load_harness_address, must

app = typer.Typer(add_completion=False)


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)
    for env in (l1, l2):
        for k in ("RPC_URL", "REMOTE_EID", "OUTPUT_JSON"):
            must(env, k)

    l1_h = load_harness_address(l1["OUTPUT_JSON"])
    l2_h = load_harness_address(l2["OUTPUT_JSON"])

    l1_expected_peer = address_to_peer_bytes32(l2_h)
    l2_expected_peer = address_to_peer_bytes32(l1_h)

    l1_data = {
        "harness": l1_h,
        "remoteEidConfig": cast_call(l1["RPC_URL"], l1_h, "remoteEid()(uint32)"),
        "expectedRemoteEid": l1["REMOTE_EID"],
        "peer": cast_call(l1["RPC_URL"], l1_h, "peers(uint32)(bytes32)", l1["REMOTE_EID"]),
        "expectedPeer": l1_expected_peer,
        "lastReceivedGuid": cast_call(l1["RPC_URL"], l1_h, "lastReceivedGuid()(bytes32)"),
        "lastNonce": cast_call(l1["RPC_URL"], l1_h, "lastNonceBySourceEid(uint32)(uint64)", l1["REMOTE_EID"]),
    }
    l2_data = {
        "harness": l2_h,
        "remoteEidConfig": cast_call(l2["RPC_URL"], l2_h, "remoteEid()(uint32)"),
        "expectedRemoteEid": l2["REMOTE_EID"],
        "peer": cast_call(l2["RPC_URL"], l2_h, "peers(uint32)(bytes32)", l2["REMOTE_EID"]),
        "expectedPeer": l2_expected_peer,
        "lastReceivedGuid": cast_call(l2["RPC_URL"], l2_h, "lastReceivedGuid()(bytes32)"),
        "lastNonce": cast_call(l2["RPC_URL"], l2_h, "lastNonceBySourceEid(uint32)(uint64)", l2["REMOTE_EID"]),
    }

    if json_out:
        import json

        print(json.dumps({"L1": l1_data, "L2": l2_data}, indent=2))
        return

    for label, d in (("L1", l1_data), ("L2", l2_data)):
        print(f"[bold]{label}[/bold]")
        print(f"  harness:                {d['harness']}")
        print(f"  remoteEid(config):      {d['remoteEidConfig']}")
        print(f"  expected remoteEid:     {d['expectedRemoteEid']}")
        print(f"  peer(remoteEid):        {d['peer']}")
        print(f"  expected peer(bytes32): {d['expectedPeer']}")
        print(f"  lastReceivedGuid:       {d['lastReceivedGuid']}")
        print(f"  lastNonce(srcEid):      {d['lastNonce']}")
        print()


if __name__ == "__main__":
    app()
