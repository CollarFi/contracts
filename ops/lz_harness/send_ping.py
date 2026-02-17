#!/usr/bin/env python3
from __future__ import annotations

import time
from pathlib import Path

import typer
from rich import print

from common import ROOT_DIR, cast_call, cast_send, load_env, load_harness_address, must

app = typer.Typer(add_completion=False)


@app.command()
def main(
    from_side: str = typer.Option(..., "--from", help="l1 or l2"),
    nonce: int = typer.Option(..., "--nonce"),
    tag: str = typer.Option(
        "0x0000000000000000000000000000000000000000000000000000000000000000", "--tag"
    ),
    value_wei: str = typer.Option("1000000000000000", "--value"),
    timeout_sec: int = typer.Option(180, "--timeout"),
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    if from_side not in {"l1", "l2"}:
        raise typer.BadParameter("--from must be l1 or l2")
    if not (tag.startswith("0x") and len(tag) == 66):
        raise typer.BadParameter("--tag must be bytes32 hex")

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)
    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT", "OUTPUT_JSON", "REMOTE_EID"):
            must(env, k)

    l1_h = load_harness_address(l1["OUTPUT_JSON"])
    l2_h = load_harness_address(l2["OUTPUT_JSON"])

    if from_side == "l1":
        src, dst = l1, l2
        src_h, dst_h = l1_h, l2_h
        src_eid_on_dst = l2["REMOTE_EID"]
    else:
        src, dst = l2, l1
        src_h, dst_h = l2_h, l1_h
        src_eid_on_dst = l1["REMOTE_EID"]

    default_options = cast_call(src["RPC_URL"], src_h, "defaultOptions()(bytes)")
    if default_options in {"", "0x"}:
        raise RuntimeError("source harness defaultOptions is empty; set options first")

    print(f"[cyan][info][/cyan] sending ping from {from_side} nonce={nonce} tag={tag}")
    tx_out = cast_send(
        src["RPC_URL"],
        src["ACCOUNT"],
        src_h,
        "sendPing(uint64,bytes32)",
        str(nonce),
        tag,
        value_wei=value_wei,
    )

    tx_hash = ""
    for line in tx_out.splitlines():
        if line.lower().startswith("transaction hash"):
            tx_hash = line.split()[-1]
            break

    start = time.time()
    last_nonce = "0"
    while True:
        last_nonce = cast_call(dst["RPC_URL"], dst_h, "lastNonceBySourceEid(uint32)(uint64)", src_eid_on_dst)
        if last_nonce.isdigit() and int(last_nonce) >= nonce:
            break
        if time.time() - start > timeout_sec:
            raise TimeoutError(f"timeout waiting for relay; observed nonce={last_nonce}")
        time.sleep(4)

    last_guid = cast_call(dst["RPC_URL"], dst_h, "lastReceivedGuid()(bytes32)")

    result = {
        "from": from_side,
        "srcHarness": src_h,
        "dstHarness": dst_h,
        "nonce": nonce,
        "txHash": tx_hash,
        "dstObservedNonce": int(last_nonce),
        "dstLastReceivedGuid": last_guid,
    }

    if json_out:
        import json

        print(json.dumps(result, indent=2))
    else:
        print("[green][ok][/green] relay observed")
        for k, v in result.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    app()
