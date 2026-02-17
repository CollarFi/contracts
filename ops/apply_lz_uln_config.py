#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

from lz_harness.common import ROOT_DIR, cast_call, cast_send, load_env, must
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.lz import encode_lz_receive_option, first_line, must_non_empty_hex, norm_hex, parse_uint

app = typer.Typer(add_completion=False)


def _collect_side(
    *,
    label: str,
    env: dict[str, str],
    oapp: str,
    dst_eid: str,
    src_eid: str,
    desired_remote_eid: str,
) -> dict[str, Any]:
    rpc = env["RPC_URL"]
    endpoint = env["LZ_ENDPOINT"]

    send_lib = first_line(cast_call(rpc, endpoint, "getSendLibrary(address,uint32)(address)", oapp, dst_eid))
    recv_lib = first_line(cast_call(rpc, endpoint, "getReceiveLibrary(address,uint32)(address,bool)", oapp, src_eid))

    send_cfg_exec = must_non_empty_hex(
        f"{label} send config type1",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, dst_eid, "1"),
    )
    send_cfg_uln = must_non_empty_hex(
        f"{label} send config type2",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, dst_eid, "2"),
    )
    recv_cfg_uln = must_non_empty_hex(
        f"{label} receive config type2",
        cast_call(rpc, endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, recv_lib, src_eid, "2"),
    )

    send_params = f"[({dst_eid},1,{send_cfg_exec}),({dst_eid},2,{send_cfg_uln})]"
    recv_params = f"[({src_eid},2,{recv_cfg_uln})]"

    desired_default_options = None
    if env.get("LZ_RECEIVE_GAS"):
        desired_default_options = encode_lz_receive_option(
            int(env["LZ_RECEIVE_GAS"]),
            int(env.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    return {
        "label": label,
        "rpc": rpc,
        "account": env["ACCOUNT"],
        "endpoint": endpoint,
        "oapp": oapp,
        "dstEid": dst_eid,
        "srcEid": src_eid,
        "sendLib": send_lib,
        "recvLib": recv_lib,
        "sendParams": send_params,
        "recvParams": recv_params,
        "desiredSendCfg1": send_cfg_exec,
        "desiredSendCfg2": send_cfg_uln,
        "desiredRecvCfg2": recv_cfg_uln,
        "desiredRemoteEid": str(desired_remote_eid),
        "desiredDefaultOptions": desired_default_options,
    }


def _apply_side(side: dict[str, Any], broadcast: bool) -> None:
    label = side["label"]
    endpoint = side["endpoint"]
    oapp = side["oapp"]
    send_lib = side["sendLib"]
    recv_lib = side["recvLib"]
    send_params = side["sendParams"]
    recv_params = side["recvParams"]

    print(f"[bold]{label}[/bold]")
    print(f"  endpoint: {endpoint}")
    print(f"  oapp:     {oapp}")
    print(f"  send lib: {send_lib}")
    print(f"  recv lib: {recv_lib}")

    sig = "setConfig(address,address,(uint32,uint32,bytes)[])"
    desired_remote_eid = side["desiredRemoteEid"]
    desired_default_options = side.get("desiredDefaultOptions")

    # Read current values and only send txs on mismatches.
    current_send_cfg_1 = cast_call(side["rpc"], endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, side["dstEid"], "1")
    current_send_cfg_2 = cast_call(side["rpc"], endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, send_lib, side["dstEid"], "2")
    current_recv_cfg_2 = cast_call(side["rpc"], endpoint, "getConfig(address,address,uint32,uint32)(bytes)", oapp, recv_lib, side["srcEid"], "2")

    desired_send_cfg_1 = side["desiredSendCfg1"]
    desired_send_cfg_2 = side["desiredSendCfg2"]
    desired_recv_cfg_2 = side["desiredRecvCfg2"]

    need_send_cfg = norm_hex(current_send_cfg_1) != norm_hex(desired_send_cfg_1) or norm_hex(current_send_cfg_2) != norm_hex(desired_send_cfg_2)
    need_recv_cfg = norm_hex(current_recv_cfg_2) != norm_hex(desired_recv_cfg_2)

    current_remote_eid = cast_call(side["rpc"], oapp, "remoteEid()(uint32)", allow_fail=True)
    current_default_options = cast_call(side["rpc"], oapp, "defaultOptions()(bytes)", allow_fail=True)
    need_remote_eid = parse_uint(current_remote_eid) != int(desired_remote_eid)
    need_default_options = bool(desired_default_options) and norm_hex(current_default_options) != norm_hex(desired_default_options)

    if not broadcast:
        print("  [yellow]dry-run[/yellow] would call:")
        if need_send_cfg:
            print(f"    setConfig({oapp}, {send_lib}, {send_params})")
        else:
            print("    skip send setConfig (already correct)")
        if need_recv_cfg:
            print(f"    setConfig({oapp}, {recv_lib}, {recv_params})")
        else:
            print("    skip receive setConfig (already correct)")
        if need_remote_eid:
            print(f"    setRemoteEid({desired_remote_eid}) on {oapp}")
        else:
            print("    skip setRemoteEid (already correct)")
        if need_default_options:
            print(f"    setDefaultOptions({desired_default_options}) on {oapp}")
        elif desired_default_options:
            print("    skip setDefaultOptions (already correct)")
        return

    if need_send_cfg:
        cast_send(
            side["rpc"],
            side["account"],
            endpoint,
            sig,
            oapp,
            send_lib,
            send_params,
        )
    else:
        print("  [green][skip][/green] send setConfig already correct")

    if need_recv_cfg:
        cast_send(
            side["rpc"],
            side["account"],
            endpoint,
            sig,
            oapp,
            recv_lib,
            recv_params,
        )
    else:
        print("  [green][skip][/green] receive setConfig already correct")

    if need_remote_eid:
        cast_send(
            side["rpc"],
            side["account"],
            oapp,
            "setRemoteEid(uint32)",
            desired_remote_eid,
        )
    else:
        print("  [green][skip][/green] remoteEid already correct")

    if need_default_options:
        cast_send(
            side["rpc"],
            side["account"],
            oapp,
            "setDefaultOptions(bytes)",
            desired_default_options,
        )
    elif desired_default_options:
        print("  [green][skip][/green] defaultOptions already correct")

    print("  [green]applied[/green]")


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    broadcast: bool = typer.Option(False, help="Execute onchain txs (default: dry-run)"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "ACCOUNT", "LZ_ENDPOINT"):
            must(env, k)

    l1_chain_eid = parse_uint(cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)"))
    l2_chain_eid = parse_uint(cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)"))
    if l1_chain_eid is None:
        raise ValueError("failed to resolve L1 endpoint eid()")
    if l2_chain_eid is None:
        raise ValueError("failed to resolve L2 endpoint eid()")

    # Route EIDs are opposite endpoint EIDs.
    l1_to_l2_eid = str(l2_chain_eid)
    l2_to_l1_eid = str(l1_chain_eid)

    l1_messenger = resolve_addr(l1, "L1_MESSENGER", "l1Messenger", "l1")
    l2_receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    l1_side = _collect_side(
        label="L1 messenger",
        env=l1,
        oapp=l1_messenger,
        dst_eid=l1_to_l2_eid,
        src_eid=l2_to_l1_eid,
        desired_remote_eid=l1_to_l2_eid,
    )
    l2_side = _collect_side(
        label="L2 receiver",
        env=l2,
        oapp=l2_receiver,
        dst_eid=l2_to_l1_eid,
        src_eid=l1_to_l2_eid,
        desired_remote_eid=l2_to_l1_eid,
    )

    if json_out:
        print(
            json.dumps(
                {
                    "mode": "broadcast" if broadcast else "dry-run",
                    "l1Env": str(l1_env_file),
                    "l2Env": str(l2_env_file),
                    "l1": l1_side,
                    "l2": l2_side,
                },
                indent=2,
            )
        )

    _apply_side(l1_side, broadcast)
    _apply_side(l2_side, broadcast)

    if broadcast:
        print("\n[green][ok][/green] ULN config applied to both OApps using current effective endpoint configs")
    else:
        print("\n[yellow][dry-run][/yellow] no transactions sent")


if __name__ == "__main__":
    app()
