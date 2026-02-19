#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print

from lz_harness.common import ROOT_DIR, address_to_peer_bytes32, cast_call, load_env, must
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths
from py_lib.lz import decode_send_executor_config, encode_lz_receive_option, is_empty_hex, is_zero_address, parse_uint

app = typer.Typer(add_completion=False)


def _snapshot_side(
    *,
    label: str,
    rpc_url: str,
    endpoint: str,
    oapp: str,
    remote_eid: str,
    source_eid: str,
    expected_peer_b32: str,
    expected_remote_eid: str,
    expected_default_options: str | None,
) -> dict[str, Any]:
    peer = cast_call(rpc_url, oapp, "peers(uint32)(bytes32)", remote_eid, allow_fail=True)
    delegate = cast_call(rpc_url, endpoint, "delegates(address)(address)", oapp, allow_fail=True)
    configured_remote_eid = cast_call(rpc_url, oapp, "remoteEid()(uint32)", allow_fail=True)
    default_options = cast_call(rpc_url, oapp, "defaultOptions()(bytes)", allow_fail=True)

    send_lib = cast_call(rpc_url, endpoint, "getSendLibrary(address,uint32)(address)", oapp, remote_eid, allow_fail=True)
    recv_lib_raw = cast_call(
        rpc_url,
        endpoint,
        "getReceiveLibrary(address,uint32)(address,bool)",
        oapp,
        source_eid,
        allow_fail=True,
    )
    recv_lib = recv_lib_raw.splitlines()[0] if recv_lib_raw != "N/A" else "N/A"

    send_cfg_1 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            send_lib,
            remote_eid,
            "1",
            allow_fail=True,
        )
        if send_lib != "N/A"
        else "N/A"
    )
    send_cfg_2 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            send_lib,
            remote_eid,
            "2",
            allow_fail=True,
        )
        if send_lib != "N/A"
        else "N/A"
    )

    recv_cfg_2 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            recv_lib,
            source_eid,
            "2",
            allow_fail=True,
        )
        if recv_lib != "N/A"
        else "N/A"
    )

    send_max_message_size, send_executor = decode_send_executor_config(send_cfg_1 if send_cfg_1 != "N/A" else "")

    receive_timeout = cast_call(
        rpc_url,
        endpoint,
        "receiveLibraryTimeout(address,uint32)(address,uint256)",
        oapp,
        source_eid,
        allow_fail=True,
    )

    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "peer wired",
            "ok": peer.lower() == expected_peer_b32.lower(),
            "actual": peer,
            "expected": expected_peer_b32,
            "hint": "Run ops/preflight/wire_lz_peers.py --broadcast if mismatch.",
        }
    )
    checks.append(
        {
            "name": "remoteEid set",
            "ok": parse_uint(configured_remote_eid) == int(expected_remote_eid),
            "actual": configured_remote_eid,
            "expected": str(expected_remote_eid),
            "hint": "Set setRemoteEid(...) on the OApp contract.",
        }
    )
    checks.append(
        {
            "name": "defaultOptions set",
            "ok": default_options not in {"N/A"} and not is_empty_hex(default_options),
            "actual": default_options,
            "hint": "Set setDefaultOptions(...) on the OApp contract.",
        }
    )
    if expected_default_options:
        checks.append(
            {
                "name": "defaultOptions matches env",
                "ok": default_options.lower().strip() == expected_default_options.lower().strip(),
                "actual": default_options,
                "expected": expected_default_options,
                "hint": "Re-apply setDefaultOptions from env LZ_RECEIVE_GAS/LZ_RECEIVE_VALUE.",
            }
        )
    checks.append(
        {
            "name": "delegate set",
            "ok": delegate != "N/A" and not is_zero_address(delegate),
            "actual": delegate,
            "hint": "Set OApp delegate via endpoint.setDelegate(...) if zero.",
        }
    )
    checks.append(
        {
            "name": "send library set",
            "ok": send_lib != "N/A" and not is_zero_address(send_lib),
            "actual": send_lib,
            "hint": "Missing send lib route config on endpoint.",
        }
    )
    checks.append(
        {
            "name": "receive library set",
            "ok": recv_lib != "N/A" and not is_zero_address(recv_lib),
            "actual": recv_lib,
            "hint": "Missing receive lib route config on endpoint.",
        }
    )

    checks.append(
        {
            "name": "send Executor config (type 1) present",
            "ok": send_cfg_1 not in {"N/A"} and not is_empty_hex(send_cfg_1),
            "actual": send_cfg_1,
            "hint": "Likely missing executor config for send path.",
        }
    )
    checks.append(
        {
            "name": "send executor address set (non-zero)",
            "ok": bool(send_executor) and not is_zero_address(send_executor),
            "actual": send_executor or "N/A",
            "hint": "Send executor is empty; set endpoint send executor config for this OApp/eid route.",
        }
    )
    checks.append(
        {
            "name": "send maxMessageSize > 0",
            "ok": (send_max_message_size or 0) > 0,
            "actual": str(send_max_message_size) if send_max_message_size is not None else "N/A",
            "hint": "Set a non-zero maxMessageSize in send executor config.",
        }
    )
    checks.append(
        {
            "name": "send ULN config (type 2) present",
            "ok": send_cfg_2 not in {"N/A"} and not is_empty_hex(send_cfg_2),
            "actual": send_cfg_2,
            "hint": "Likely missing ULN config (DVN/confirmations) for send path.",
        }
    )
    checks.append(
        {
            "name": "receive ULN config (type 2) present",
            "ok": recv_cfg_2 not in {"N/A"} and not is_empty_hex(recv_cfg_2),
            "actual": recv_cfg_2,
            "hint": "Likely missing ULN config on receive path.",
        }
    )

    ok = all(c["ok"] for c in checks)

    return {
        "label": label,
        "oapp": oapp,
        "endpoint": endpoint,
        "remoteEid": remote_eid,
        "sourceEid": source_eid,
        "peer": peer,
        "expectedPeer": expected_peer_b32,
        "delegate": delegate,
        "configuredRemoteEid": configured_remote_eid,
        "defaultOptions": default_options,
        "sendLibrary": send_lib,
        "receiveLibrary": recv_lib,
        "sendConfigType1": send_cfg_1,
        "sendConfigType2": send_cfg_2,
        "sendExecutor": send_executor,
        "sendMaxMessageSize": send_max_message_size,
        "receiveConfigType2": recv_cfg_2,
        "receiveLibraryTimeout": receive_timeout,
        "checks": checks,
        "ok": ok,
    }


@app.command()
def main(
    l1_env_file: Path = typer.Argument(ROOT_DIR / ".env.l1.testnet"),
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary"),
) -> None:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for k in ("RPC_URL", "LZ_ENDPOINT"):
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

    l1_expected_peer = address_to_peer_bytes32(l2_receiver)
    l2_expected_peer = address_to_peer_bytes32(l1_messenger)

    l1_expected_options = None
    if l1.get("LZ_RECEIVE_GAS"):
        l1_expected_options = encode_lz_receive_option(
            int(l1["LZ_RECEIVE_GAS"]),
            int(l1.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    l2_expected_options = None
    if l2.get("LZ_RECEIVE_GAS"):
        l2_expected_options = encode_lz_receive_option(
            int(l2["LZ_RECEIVE_GAS"]),
            int(l2.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    l1_side = _snapshot_side(
        label="L1 messenger (send->L2, recv<-L2)",
        rpc_url=l1["RPC_URL"],
        endpoint=l1["LZ_ENDPOINT"],
        oapp=l1_messenger,
        remote_eid=l1_to_l2_eid,
        source_eid=l2_to_l1_eid,
        expected_peer_b32=l1_expected_peer,
        expected_remote_eid=l1_to_l2_eid,
        expected_default_options=l1_expected_options,
    )
    l2_side = _snapshot_side(
        label="L2 receiver (send->L1, recv<-L1)",
        rpc_url=l2["RPC_URL"],
        endpoint=l2["LZ_ENDPOINT"],
        oapp=l2_receiver,
        remote_eid=l2_to_l1_eid,
        source_eid=l1_to_l2_eid,
        expected_peer_b32=l2_expected_peer,
        expected_remote_eid=l2_to_l1_eid,
        expected_default_options=l2_expected_options,
    )

    summary = {
        "env": env_profile.strip().lower() or "custom",
        "l1Env": str(l1_env_file),
        "l2Env": str(l2_env_file),
        "l1Messenger": l1_messenger,
        "l2Receiver": l2_receiver,
        "l1EndpointEid": str(l1_chain_eid),
        "l2EndpointEid": str(l2_chain_eid),
        "l1ToL2Eid": l1_to_l2_eid,
        "l2ToL1Eid": l2_to_l1_eid,
        "sides": [l1_side, l2_side],
        "ok": l1_side["ok"] and l2_side["ok"],
    }

    if json_out:
        print(json.dumps(summary, indent=2))
        return

    icon = "[green]OK[/green]" if summary["ok"] else "[red]ISSUES FOUND[/red]"
    print(f"[bold]LayerZero ULN/route check[/bold] {icon}")
    print(f"  L1 messenger: {l1_messenger}")
    print(f"  L2 receiver:  {l2_receiver}")
    print(f"  EIDs: L1->L2={l1_to_l2_eid}, L2->L1={l2_to_l1_eid}")
    print()

    for side in summary["sides"]:
        side_icon = "[green]OK[/green]" if side["ok"] else "[red]FAIL[/red]"
        print(f"[bold]{side['label']}[/bold] {side_icon}")
        for check in side["checks"]:
            check_icon = "✅" if check["ok"] else "❌"
            print(f"  {check_icon} {check['name']}")
            if not check["ok"]:
                print(f"     actual:   {check.get('actual', 'N/A')}")
                if "expected" in check:
                    print(f"     expected: {check['expected']}")
                print(f"     hint:     {check.get('hint', '')}")
        print()

    if not summary["ok"]:
        print("[yellow]Tip:[/yellow] If LayerZeroScan says [bold]WAITING FOR ULN CONFIG[/bold], config type1/type2 is usually missing on one side.")


if __name__ == "__main__":
    app()
