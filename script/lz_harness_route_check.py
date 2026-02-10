#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import typer
from rich import print

from lz_harness_common import ROOT_DIR, address_to_peer_bytes32, cast_call, load_env, load_harness_address, must

app = typer.Typer(add_completion=False)


def side_snapshot(
    label: str,
    rpc: str,
    endpoint: str,
    oapp: str,
    remote_eid: str,
    src_eid: str,
    src_sender_b32: str,
    nonce: int,
) -> dict:
    send_lib = cast_call(rpc, endpoint, "getSendLibrary(address,uint32)(address)", oapp, remote_eid, allow_fail=True)
    recv_lib = cast_call(rpc, endpoint, "getReceiveLibrary(address,uint32)(address,bool)", oapp, src_eid, allow_fail=True)
    recv_lib_addr = recv_lib.splitlines()[0] if recv_lib != "N/A" else "N/A"

    out = {
        "label": label,
        "delegate": cast_call(rpc, endpoint, "delegates(address)(address)", oapp, allow_fail=True),
        "sendLib": send_lib,
        "sendConfigType1": cast_call(
            rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, remote_eid, "1", allow_fail=True
        ),
        "sendConfigType2": cast_call(
            rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, remote_eid, "2", allow_fail=True
        ),
        "receiveLib": recv_lib,
        "receiveTimeout": cast_call(rpc, endpoint, "receiveLibraryTimeout(address,uint32)(address,uint256)", oapp, src_eid, allow_fail=True),
        "recvConfigType1": cast_call(
            rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, recv_lib_addr, src_eid, "1", allow_fail=True
        ) if recv_lib_addr != "N/A" else "N/A",
        "recvConfigType2": cast_call(
            rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, recv_lib_addr, src_eid, "2", allow_fail=True
        ) if recv_lib_addr != "N/A" else "N/A",
        "initializableNow": cast_call(
            rpc,
            endpoint,
            "initializable((uint32,bytes32,uint64),address)(bool)",
            f"({src_eid},{src_sender_b32},{nonce})",
            oapp,
            allow_fail=True,
        ),
        "verifiableNow": cast_call(
            rpc,
            endpoint,
            "verifiable((uint32,bytes32,uint64),address)(bool)",
            f"({src_eid},{src_sender_b32},{nonce})",
            oapp,
            allow_fail=True,
        ),
        "initializableNext": cast_call(
            rpc,
            endpoint,
            "initializable((uint32,bytes32,uint64),address)(bool)",
            f"({src_eid},{src_sender_b32},{nonce + 1})",
            oapp,
            allow_fail=True,
        ),
        "verifiableNext": cast_call(
            rpc,
            endpoint,
            "verifiable((uint32,bytes32,uint64),address)(bool)",
            f"({src_eid},{src_sender_b32},{nonce + 1})",
            oapp,
            allow_fail=True,
        ),
    }
    return out


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)
    for env in (l1, l2):
        for k in ("RPC_URL", "LZ_ENDPOINT", "REMOTE_EID", "OUTPUT_JSON"):
            must(env, k)

    l1_h = load_harness_address(l1["OUTPUT_JSON"])
    l2_h = load_harness_address(l2["OUTPUT_JSON"])

    l1_nonce = int(cast_call(l1["RPC_URL"], l1_h, "lastNonceBySourceEid(uint32)(uint64)", l1["REMOTE_EID"]))
    l2_nonce = int(cast_call(l2["RPC_URL"], l2_h, "lastNonceBySourceEid(uint32)(uint64)", l2["REMOTE_EID"]))

    l1_view = side_snapshot(
        "L1 (recv from L2)",
        l1["RPC_URL"],
        l1["LZ_ENDPOINT"],
        l1_h,
        l1["REMOTE_EID"],
        l1["REMOTE_EID"],
        address_to_peer_bytes32(l2_h),
        l1_nonce,
    )
    l2_view = side_snapshot(
        "L2 (recv from L1)",
        l2["RPC_URL"],
        l2["LZ_ENDPOINT"],
        l2_h,
        l2["REMOTE_EID"],
        l2["REMOTE_EID"],
        address_to_peer_bytes32(l1_h),
        l2_nonce,
    )

    if json_out:
        import json

        print(json.dumps({"L1": l1_view, "L2": l2_view}, indent=2))
        return

    for side in (l1_view, l2_view):
        print(f"[bold]{side['label']}[/bold]")
        for k, v in side.items():
            if k == "label":
                continue
            print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    app()
